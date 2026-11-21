"""Repository configuration (`.projectbrain.toml` / `[tool.projectbrain]`), rule profiles, and inline suppressions.

Provides CI-grade escape hatches so ProjectBrain pairs cleanly with `ruff`, `pyright`, `tsc`, and `eslint`:
  1. Named profiles:
     - `all`        : PB001–PB013 (default, full verification)
     - `fullstack`  : PB002, PB007, PB008, PB011, PB012, PB013 (non-overlapping with single-language typecheckers)
     - `governance` : PB002, PB007, PB008, PB010 (boundaries, tamper lock, invariants, co-change)
  2. Explicit rule selection/ignoring (`--select`, `--ignore`, or `.projectbrain.toml`).
  3. Inline line-level suppressions (`# pb:ignore PB012 -- reason`, `// pb:ignore PB011`),
     which can suppress diagnostics on the same line or immediately preceding comment line
     (never suppressing `PB007` Governance Tamper Lock).
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Callable

from .memory import _compile_path_glob, extract_note_rel_path
from .reporters import _classify_diag_rule_id, _clean_uri, _extract_location

ALL_RULE_IDS: frozenset[str] = frozenset(f"PB{i:03d}" for i in range(1, 14))

RULE_PROFILES: dict[str, frozenset[str]] = {
    "all": ALL_RULE_IDS,
    "fullstack": frozenset({"PB002", "PB007", "PB008", "PB011", "PB012", "PB013"}),
    "cross-stack": frozenset({"PB002", "PB007", "PB008", "PB011", "PB012", "PB013"}),
    "governance": frozenset({"PB002", "PB007", "PB008", "PB010"}),
}

_INLINE_IGNORE_RE = re.compile(
    r"(?:#|//|/\*)\s*(?:pb|projectbrain)\s*:\s*ignore\b([^\r\n*]*)",
    re.IGNORECASE,
)
_RULE_TOKEN_RE = re.compile(r"\bPB\d{3}\b", re.IGNORECASE)


def _normalize_rule_list(raw: object) -> list[str]:
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, str):
        raw_parts = re.split(r"[\s,]+", raw.strip())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        raw_parts = []
        for el in raw:
            raw_parts.extend(re.split(r"[\s,]+", str(el).strip()))
    else:
        return []
    for tok in raw_parts:
        up = tok.strip().upper()
        if not up:
            continue
        if up in ALL_RULE_IDS and up not in items:
            items.append(up)
    return items


def _strip_toml_comment(val: str) -> str:
    in_q: str | None = None
    out: list[str] = []
    for ch in val:
        if in_q:
            out.append(ch)
            if ch == in_q:
                in_q = None
        elif ch in ("'", '"'):
            in_q = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _parse_toml_fallback(raw_text: str) -> dict:
    """Minimal TOML subset parser for Python 3.10 when `tomllib` is unavailable (supports multi-line arrays)."""
    root_tbl: dict = {}
    cur_tbl: dict = root_tbl
    lines = raw_text.splitlines()
    idx = 0
    n = len(lines)
    while idx < n:
        ln = lines[idx].strip()
        idx += 1
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            sec_path = [p.strip().strip("'\"") for p in ln[1:-1].split(".") if p.strip()]
            cur_tbl = root_tbl
            for part in sec_path:
                nxt = cur_tbl.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur_tbl[part] = nxt
                cur_tbl = nxt
            continue
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        key = k.strip().strip("'\"")
        val = _strip_toml_comment(v)
        if not key:
            continue
        if val.startswith("[") and not val.endswith("]"):
            buf = [val]
            while idx < n:
                nxt_part = _strip_toml_comment(lines[idx])
                idx += 1
                if nxt_part:
                    buf.append(nxt_part)
                if nxt_part.endswith("]"):
                    break
            val = " ".join(buf).strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            arr = [
                el.strip().strip("'\"")
                for el in inner.split(",")
                if el.strip().strip("'\"")
            ]
            cur_tbl[key] = arr
        elif val.lower() in ("true", "false"):
            cur_tbl[key] = val.lower() == "true"
        elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            cur_tbl[key] = val[1:-1]
        else:
            cur_tbl[key] = val
    return root_tbl


def parse_toml_text(raw_text: str) -> dict:
    """Parse TOML text using stdlib `tomllib` (3.11+), `tomli` if installed, or fallback parser."""
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(raw_text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        try:
            import tomli  # type: ignore[import-not-found]
            data = tomli.loads(raw_text)
            return data if isinstance(data, dict) else {}
        except ImportError:
            return _parse_toml_fallback(raw_text)
    except Exception:
        return {}


def _load_toml_file(path: Path) -> dict:
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_toml_text(raw_text)


def extract_config_from_toml_dict(loaded: dict, *, from_pyproject: bool = False) -> dict:
    """Normalize ProjectBrain config keys from a parsed `.projectbrain.toml` or `pyproject.toml` dict."""
    raw_cfg: dict = {}
    if from_pyproject:
        tool_sec = loaded.get("tool")
        if isinstance(tool_sec, dict) and isinstance(tool_sec.get("projectbrain"), dict):
            pb_sec = tool_sec["projectbrain"]
            raw_cfg = {**pb_sec, **pb_sec["check"]} if isinstance(pb_sec.get("check"), dict) else dict(pb_sec)
    else:
        raw_cfg = {**loaded, **loaded["check"]} if isinstance(loaded.get("check"), dict) else dict(loaded)

    profile_raw = str(raw_cfg.get("profile") or "all").strip().lower()
    if profile_raw not in RULE_PROFILES:
        profile_raw = "all"

    exclude_raw = raw_cfg.get("exclude") or []
    if isinstance(exclude_raw, str):
        exclude_list = [exclude_raw.strip()] if exclude_raw.strip() else []
    elif isinstance(exclude_raw, list):
        exclude_list = [str(x).strip() for x in exclude_raw if str(x).strip()]
    else:
        exclude_list = []

    return {
        "has_config": bool(raw_cfg),
        "profile": profile_raw,
        "select": _normalize_rule_list(raw_cfg.get("select")),
        "ignore": _normalize_rule_list(raw_cfg.get("ignore")),
        "exclude": exclude_list,
        "allow_inline_boundary_ignore": bool(raw_cfg.get("allow_inline_boundary_ignore", False)),
    }


def load_repo_config(root: Path) -> dict:
    """Load ProjectBrain config from `.projectbrain.toml` or `[tool.projectbrain]` in `pyproject.toml`."""
    pb_toml = root / ".projectbrain.toml"
    if pb_toml.is_file():
        cfg = extract_config_from_toml_dict(_load_toml_file(pb_toml), from_pyproject=False)
        return {"source": ".projectbrain.toml", **cfg}
    pyproj = root / "pyproject.toml"
    if pyproj.is_file():
        cfg = extract_config_from_toml_dict(_load_toml_file(pyproj), from_pyproject=True)
        if cfg.get("has_config"):
            return {"source": "pyproject.toml", **cfg}
    return {
        "source": None,
        "has_config": False,
        "profile": "all",
        "select": [],
        "ignore": [],
        "exclude": [],
        "allow_inline_boundary_ignore": False,
    }


def resolve_active_rules(
    repo_cfg: dict,
    *,
    profile: str | None = None,
    select_rules: list[str] | str | None = None,
    ignore_rules: list[str] | str | None = None,
) -> tuple[set[str], str, bool]:
    """Compute `(active_rule_ids, effective_profile, has_custom_filter)`."""
    eff_profile = (profile or repo_cfg.get("profile") or "all").strip().lower()
    if eff_profile not in RULE_PROFILES:
        eff_profile = "all"

    cli_select = _normalize_rule_list(select_rules)
    cfg_select = list(repo_cfg.get("select") or [])
    eff_select = cli_select if cli_select else cfg_select

    cli_ignore = _normalize_rule_list(ignore_rules)
    cfg_ignore = list(repo_cfg.get("ignore") or [])
    eff_ignore = set(cfg_ignore) | set(cli_ignore)

    if eff_select:
        active = set(eff_select) - eff_ignore
    else:
        active = set(RULE_PROFILES[eff_profile]) - eff_ignore
    # PB007 (Governance Tamper Lock) can NEVER be disabled via repo config files;
    # only an explicit human `--allow-governance-change` CLI flag may authorize governance changes.
    if "PB007" not in cli_ignore:
        active.add("PB007")

    has_custom = (
        eff_profile != "all"
        or bool(eff_select)
        or bool(eff_ignore)
        or bool(repo_cfg.get("exclude"))
    )
    return active, eff_profile, has_custom


def path_is_excluded(rel_path: str, exclude_globs: list[str]) -> bool:
    if not exclude_globs or not rel_path:
        return False
    norm = _clean_uri(rel_path, fallback="")
    if not norm:
        return False
    for pat in exclude_globs:
        p_clean = pat.strip().replace("\\", "/")
        if not p_clean:
            continue
        if _compile_path_glob(p_clean).match(norm) or fnmatch.fnmatch(norm, p_clean):
            return True
    return False


def parse_inline_suppressions(src_text: str) -> dict[int, set[str]]:
    """Parse `# pb:ignore [PBxxx]` and `// pb:ignore [PBxxx]` comments into `{line_no: {rule_ids | '*'}}`.

    A suppression comment at the end of a code line applies to that line.
    A standalone comment line applies to itself and the next non-blank code line (within 2 lines).
    """
    if not src_text or "ignore" not in src_text.lower():
        return {}
    lines = src_text.splitlines()
    n = len(lines)
    suppressed: dict[int, set[str]] = {}

    for idx, raw_ln in enumerate(lines):
        m = _INLINE_IGNORE_RE.search(raw_ln)
        if not m:
            continue
        tail = m.group(1) or ""
        # Strip optional `-- reason` explanation before scanning rule IDs
        if "--" in tail:
            tail = tail.split("--", 1)[0]
        rids = {tok.upper() for tok in _RULE_TOKEN_RE.findall(tail)}
        rule_set = rids if rids else {"*"}

        lno = idx + 1
        suppressed.setdefault(lno, set()).update(rule_set)

        prefix = raw_ln[: m.start()].strip()
        if not prefix:
            # Pure comment line: walk down across contiguous comments & single/multi-line decorators to the target line
            target_idx: int | None = None
            dec_depth = 0
            for look in range(idx + 1, min(n, idx + 40)):
                nxt = lines[look].strip()
                if not nxt:
                    if dec_depth > 0:
                        suppressed.setdefault(look + 1, set()).update(rule_set)
                        continue
                    break
                suppressed.setdefault(look + 1, set()).update(rule_set)
                code_part = nxt.split("#", 1)[0]
                if dec_depth > 0:
                    dec_depth = max(0, dec_depth + code_part.count("(") + code_part.count("[") - code_part.count(")") - code_part.count("]"))
                    continue
                if nxt.startswith(("#", "//", "/*", "*", "///")):
                    continue
                if nxt.startswith(("@", "#[")):
                    dec_depth = max(0, code_part.count("(") + code_part.count("[") - code_part.count(")") - code_part.count("]"))
                    continue
                target_idx = look
                break
            # If the target line opens a function/method declaration, propagate across its body
            if target_idx is not None:
                tgt_raw = lines[target_idx]
                tgt_s = tgt_raw.strip()
                if re.match(r"^(?:async\s+)?def\s+\w+", tgt_s):
                    def_indent = len(tgt_raw) - len(tgt_raw.lstrip(" \t"))
                    for b_idx in range(target_idx + 1, min(n, target_idx + 200)):
                        b_raw = lines[b_idx]
                        b_s = b_raw.strip()
                        if not b_s or b_s.startswith("#"):
                            continue
                        b_indent = len(b_raw) - len(b_raw.lstrip(" \t"))
                        if b_indent <= def_indent:
                            break
                        suppressed.setdefault(b_idx + 1, set()).update(rule_set)
                elif re.match(
                    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+\w+|(?:const|let|var)\s+\w+(?:\s*:\s*[^=]+)?\s*=\s*(?:async\s*)?(?:\(|[A-Za-z_$]\w*\s*=>|function\b))",
                    tgt_s,
                ):
                    depth = 0
                    started = False
                    for b_idx in range(target_idx, min(n, target_idx + 200)):
                        b_raw = lines[b_idx]
                        suppressed.setdefault(b_idx + 1, set()).update(rule_set)
                        for ch in b_raw:
                            if ch == "{":
                                depth += 1
                                started = True
                            elif ch == "}" and started:
                                depth -= 1
                        if started and depth <= 0:
                            break

    return suppressed


def is_suppressed_at(
    suppressions: dict[int, set[str]],
    line: int,
    rule_id: str,
    *,
    allow_inline_boundary_ignore: bool = False,
) -> bool:
    """Return True if `rule_id` is suppressed by an inline comment at `line`.

    `PB007` (Governance Tamper Lock) is never suppressible via inline comments.
    `PB002` (team-pinned `--forbid` boundary rules) is also immune by default unless
    `allow_inline_boundary_ignore = true` is explicitly configured in `.projectbrain.toml`.
    """
    if not suppressions or rule_id == "PB007":
        return False
    if rule_id == "PB002" and not allow_inline_boundary_ignore:
        return False
    rules_here = suppressions.get(line)
    if rules_here and ("*" in rules_here or rule_id in rules_here):
        return True
    return False


def apply_rule_and_suppression_filters(
    res: dict,
    root: Path,
    *,
    active_rules: set[str],
    exclude_globs: list[str],
    profile_name: str = "all",
    has_custom_filter: bool = False,
    allow_inline_boundary_ignore: bool = False,
    file_text_getter: Callable[[str], str | None] | None = None,
) -> dict:
    """Filter `run_check()` output by active rule set, exclude globs, and inline `# pb:ignore` comments."""
    supp_cache: dict[str, dict[int, set[str]]] = {}

    def _get_suppressions(rel_path: str) -> dict[int, set[str]]:
        uri = _clean_uri(rel_path, fallback="")
        if not uri or uri == "REPOSITORY":
            return {}
        if uri in supp_cache:
            return supp_cache[uri]
        txt: str | None = None
        if file_text_getter is not None:
            txt = file_text_getter(uri)
        if txt is None:
            p = root / uri
            if p.is_file():
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    txt = ""
        parsed = parse_inline_suppressions(txt or "")
        supp_cache[uri] = parsed
        return parsed

    suppressed_keys: set[tuple[str, str, int]] = set()
    config_filtered = 0

    def _keep_finding(rule_id: str, uri: str, line: int) -> bool:
        nonlocal config_filtered
        # PB007 Governance Tamper Lock is strictly immune to exclude globs and inline suppressions
        if rule_id == "PB007":
            return rule_id in active_rules
        norm_u = _clean_uri(uri, fallback="")
        if exclude_globs and norm_u and path_is_excluded(norm_u, exclude_globs):
            config_filtered += 1
            return False
        if rule_id not in active_rules:
            config_filtered += 1
            return False
        if norm_u and is_suppressed_at(
            _get_suppressions(norm_u),
            max(1, int(line or 1)),
            rule_id,
            allow_inline_boundary_ignore=allow_inline_boundary_ignore,
        ):
            suppressed_keys.add((rule_id, norm_u, max(1, int(line or 1))))
            return False
        return True

    # 1. Boundary violations (PB002)
    if "boundary_violations" in res and isinstance(res["boundary_violations"], list):
        kept_bv = [
            bv for bv in res["boundary_violations"]
            if _keep_finding("PB002", bv.get("src_file", ""), int(bv.get("line") or 1))
        ]
        if kept_bv:
            res["boundary_violations"] = kept_bv
        else:
            res.pop("boundary_violations", None)

    # 2. Governance tamper (PB007 — never inline-suppressed, only configurable via rules)
    if "governance_tamper" in res and isinstance(res["governance_tamper"], list):
        if "PB007" not in active_rules:
            config_filtered += len(res["governance_tamper"])
            res.pop("governance_tamper", None)
            res.pop("governance_tamper_blocked", None)

    # 3. Primary blocking diagnostics (`contract_diagnostics`)
    if "contract_diagnostics" in res and isinstance(res["contract_diagnostics"], list):
        kept_diags: list[str] = []
        for msg in res["contract_diagnostics"]:
            rid = _classify_diag_rule_id(msg)
            uri, ln, _ = _extract_location(msg, fallback_uri="")
            if _keep_finding(rid, uri, ln):
                kept_diags.append(msg)
        res["contract_diagnostics"] = kept_diags
        imp = res.get("impact_analysis")
        if isinstance(imp, dict) and "contract_diagnostics" in imp:
            imp["contract_diagnostics"] = list(kept_diags)

    # 4. Semantic warnings & static_audit_findings
    for list_key in ("semantic_warnings", "static_audit_findings"):
        if list_key in res and isinstance(res[list_key], list):
            kept_lst: list[str] = []
            for msg in res[list_key]:
                rid = _classify_diag_rule_id(msg)
                uri, ln, _ = _extract_location(msg, fallback_uri="")
                if _keep_finding(rid, uri, ln):
                    kept_lst.append(msg)
            if kept_lst:
                res[list_key] = kept_lst
            else:
                res.pop(list_key, None)

    # 5. Ranked hotspots (preserve clean re-ranking and audit coverage accuracy)
    if "ranked_hotspots" in res and isinstance(res["ranked_hotspots"], list):
        kept_hs: list[dict] = []
        for hs in res["ranked_hotspots"]:
            rid = str(hs.get("rule_id") or "PB006").upper()
            uri = str(hs.get("rel_path") or "")
            ln = int(hs.get("line") or 1)
            if _keep_finding(rid, uri, ln):
                kept_hs.append(dict(hs))
        for idx, hs in enumerate(kept_hs, 1):
            hs["rank"] = idx
        res["ranked_hotspots"] = kept_hs
        cov = res.get("audit_coverage")
        if isinstance(cov, dict):
            flagged_set = {h.get("rel_path") for h in kept_hs if h.get("rel_path")}
            scanned = int(cov.get("files_scanned") or 0)
            cov["flagged_files_count"] = len(flagged_set)
            cov["clean_files_verified"] = max(0, scanned - len(flagged_set))

    # 6. Stale invariants (PB008)
    if "stale_memories" in res and isinstance(res["stale_memories"], list):
        if "PB008" not in active_rules:
            config_filtered += len(res["stale_memories"])
            res.pop("stale_memories", None)
        elif exclude_globs:
            kept_sm = [
                sm for sm in res["stale_memories"]
                if not path_is_excluded(extract_note_rel_path(sm), exclude_globs)
            ]
            if kept_sm:
                res["stale_memories"] = kept_sm
            else:
                res.pop("stale_memories", None)

    # 7. Signature blast (PB009)
    if "signature_blast" in res and isinstance(res["signature_blast"], list):
        if "PB009" not in active_rules:
            config_filtered += len(res["signature_blast"])
            res.pop("signature_blast", None)
        elif exclude_globs:
            kept_sb = [
                sb for sb in res["signature_blast"]
                if not path_is_excluded(str(sb.get("file") or ""), exclude_globs)
            ]
            if kept_sb:
                res["signature_blast"] = kept_sb
            else:
                res.pop("signature_blast", None)

    # 8. Co-change warnings (PB010)
    if "co_change_warnings" in res and isinstance(res["co_change_warnings"], list):
        if "PB010" not in active_rules:
            config_filtered += len(res["co_change_warnings"])
            res.pop("co_change_warnings", None)
        elif exclude_globs:
            kept_cw = [
                cw for cw in res["co_change_warnings"]
                if not path_is_excluded(str(cw.get("trigger_file") or ""), exclude_globs)
            ]
            if kept_cw:
                res["co_change_warnings"] = kept_cw
            else:
                res.pop("co_change_warnings", None)

    # 9. Recompute verdict & validity if filtering removed all blocking diagnostics
    remaining_diags = res.get("contract_diagnostics") or []
    tamper_blocked = bool(res.get("governance_tamper_blocked"))
    if not remaining_diags and not tamper_blocked and res.get("all_contracts_valid") is False:
        res["all_contracts_valid"] = True
        if res.get("verdict") in ("UNSAFE_CONTRACT_VIOLATION", "UNSAFE_GOVERNANCE_TAMPER"):
            imp = res.get("impact_analysis")
            if isinstance(imp, dict):
                imp["all_contracts_valid"] = True
                if imp.get("total_impact_zone", 0) > 0 and any(
                    d.get("risk") in ("high", "critical") for d in imp.get("details", [])
                ):
                    imp["verdict"] = "HIGH_IMPACT_REVIEW_CALLERS"
                    res["verdict"] = "HIGH_IMPACT_REVIEW_CALLERS"
                else:
                    imp["verdict"] = "SAFE_TO_PROCEED"
                    res["verdict"] = "SAFE_TO_PROCEED"
            else:
                res["verdict"] = "SAFE_TO_PROCEED"

    if suppressed_keys:
        res["suppressed_count"] = len(suppressed_keys)
    if has_custom_filter:
        res["active_profile"] = profile_name
        res["active_rules"] = sorted(active_rules)
    if config_filtered > 0 and has_custom_filter:
        res["filtered_by_config_count"] = config_filtered

    return res
