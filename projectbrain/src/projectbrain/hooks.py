"""Real-Time Active Guardrail Hook Engine for ProjectBrain.

Designed for `<30ms` stdlib fast-path bailout on non-code edits and `<200ms` Salsa
incremental SQLite verification on code edits across:
  - Claude Code (`PostToolUse` on `Edit|MultiEdit|Write|NotebookEdit`)
  - Cursor (`.cursor/hooks.json` `afterFileEdit`)
  - Git `pre-commit` (`.git/hooks/pre-commit`)
  - Direct CLI (`projectbrain hook post-edit --files ...`)

Silent-When-Clean Guarantee:
  Clean body-only edits emit zero bytes and exit `0`, adding zero noise to the
  agent's context window while recording audit telemetry to `.projectbrain_ledger.json`.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SUPPORTED_CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".cs", ".rb", ".php", ".swift",
    ".sql", ".graphql", ".gql", ".prisma", ".proto",
}

_EDIT_TOOL_NAMES = {
    "edit",
    "multiedit",
    "write",
    "notebookedit",
    "edit_file",
    "write_file",
    "replace_file_content",
    "multi_replace_file_content",
    "create_file",
    "str_replace_editor",
}

_GOVERNANCE_FILES = frozenset({
    ".projectbrain_memory.json",
    ".projectbrain.toml",
    "pyproject.toml",
})

_JS_TS_HOOK_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_PY_DEEP_HOOK_TOKENS = (
    "model_dump",
    ".dict(",
    "except",
    "BaseModel",
    "SQLModel",
    "TypedDict",
    "@dataclass",
    "APIRouter",
    "FastAPI",
)
_JS_DEEP_HOOK_TOKENS = (
    "fetch(",
    "axios",
    ".post(",
    ".put(",
    ".patch(",
    ".delete(",
    ".get(",
    "useState",
    "useEffect",
    "useReducer",
    "<",
    "({",
)


def _file_has_inline_invariants(inf: dict | None, src_txt: str | None = None) -> bool:
    if src_txt and ("@pb-invariant" in src_txt or "@projectbrain-invariant" in src_txt):
        return True
    if not inf:
        return False
    return any(
        fn.get("inline_invariants") for fn in (inf.get("functions") or ())
    ) or any(
        cls.get("inline_invariants") for cls in (inf.get("classes") or ())
    )


def _needs_deep_hook_audit(rel_path: str, pre_inf: dict, post_inf: dict, src_txt: str) -> bool:
    """Return True when body-only edits can still trigger PB011-PB013 or inline invariants."""
    if _file_has_inline_invariants(pre_inf) or _file_has_inline_invariants(post_inf, src_txt):
        return True
    if "pb:ignore" in src_txt or "projectbrain:ignore" in src_txt:
        return True
    ext = Path(rel_path).suffix.lower()
    if ext == ".py":
        if pre_inf.get("routes") or post_inf.get("routes"):
            return True
        if any(c.get("schema_spec") for c in (pre_inf.get("classes") or ())) or any(
            c.get("schema_spec") for c in (post_inf.get("classes") or ())
        ):
            return True
        return any(tok in src_txt for tok in _PY_DEEP_HOOK_TOKENS)
    if ext in _JS_TS_HOOK_EXTS:
        if ext in (".jsx", ".tsx"):
            return True
        if (
            pre_inf.get("http_wrapper_spec")
            or post_inf.get("http_wrapper_spec")
            or pre_inf.get("routes")
            or post_inf.get("routes")
        ):
            return True
        return any(tok in src_txt for tok in _JS_DEEP_HOOK_TOKENS)
    return False

_IGNORED_PARTS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", ".pytest_cache",
}


def _is_auditable_code_path(raw_path: str) -> bool:
    if not raw_path:
        return False
    p = Path(raw_path)
    name = p.name
    if name.startswith(".projectbrain"):
        return False
    if p.suffix.lower() not in SUPPORTED_CODE_EXTS:
        return False
    parts = {seg.lower() for seg in p.parts}
    if parts & _IGNORED_PARTS:
        return False
    return True


def parse_hook_payload(
    raw_stdin: str,
    explicit_files: list[str] | None = None,
    default_root: Path | None = None,
) -> dict:
    """Parse stdin JSON from Claude Code / Cursor or explicit CLI args using stdlib only."""
    root = (default_root or Path.cwd()).resolve()
    protocol = "cli"
    candidates: list[str] = list(explicit_files or [])

    payload_str = (raw_stdin or "").strip()
    if payload_str.startswith("{"):
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            event_name = str(data.get("hook_event_name") or data.get("event") or "")
            tool_name = str(data.get("tool_name") or "")

            if event_name == "afterFileEdit" or ("workspace_roots" in data and "file_path" in data):
                protocol = "cursor"
                ws_roots = data.get("workspace_roots")
                if isinstance(ws_roots, list) and ws_roots and default_root is None:
                    root = Path(str(ws_roots[0])).resolve()
                fp = data.get("file_path")
                if isinstance(fp, str) and fp:
                    candidates.append(fp)
            elif tool_name or event_name in ("PostToolUse", "PreToolUse"):
                protocol = "claude"
                cwd_val = data.get("cwd")
                if isinstance(cwd_val, str) and cwd_val and default_root is None:
                    root = Path(cwd_val).resolve()
                if tool_name and tool_name.lower() not in _EDIT_TOOL_NAMES:
                    return {"protocol": protocol, "root": root, "files": [], "skipped_tool": tool_name}
                t_in = data.get("tool_input")
                if isinstance(t_in, dict):
                    for key in ("file_path", "path", "notebook_path", "target_file"):
                        val = t_in.get(key)
                        if isinstance(val, str) and val:
                            candidates.append(val)
                    for list_key in ("files", "paths"):
                        lval = t_in.get(list_key)
                        if isinstance(lval, list):
                            candidates.extend(str(x) for x in lval if isinstance(x, str))
                    edits_val = t_in.get("edits")
                    if isinstance(edits_val, list):
                        for ed in edits_val:
                            if isinstance(ed, dict) and isinstance(ed.get("file_path"), str):
                                candidates.append(ed["file_path"])
            else:
                fp = data.get("file_path") or data.get("path")
                if isinstance(fp, str) and fp:
                    candidates.append(fp)

    orig_root = root
    if default_root is None and not (
        (root / ".projectbrain.db").exists()
        or (root / ".projectbrain_memory.json").exists()
        or (root / ".git").exists()
    ):
        for anc in root.parents:
            if anc.name.startswith(("pytest-of-", ".pytest")):
                break
            if (
                (anc / ".projectbrain.db").exists()
                or (anc / ".projectbrain_memory.json").exists()
                or (anc / ".git").exists()
            ):
                root = anc
                break

    filtered: list[str] = []
    seen: set[str] = set()
    governance_edited = False
    for raw in candidates:
        if orig_root != root and raw and not Path(raw).is_absolute():
            cand_abs = orig_root / raw
            if cand_abs.exists() or Path(raw).name in _GOVERNANCE_FILES:
                raw = str(cand_abs.resolve())
        if Path(raw).name in _GOVERNANCE_FILES:
            governance_edited = True
            continue
        if not _is_auditable_code_path(raw):
            continue
        norm = raw.replace("\\", "/")
        if norm not in seen:
            seen.add(norm)
            filtered.append(raw)

    return {
        "protocol": protocol,
        "root": root,
        "files": filtered,
        "governance_edited": governance_edited,
    }


def compute_interface_diff(pre_info: dict | None, post_info: dict | None) -> dict:
    """Compare pre-edit and post-edit file AST symbol tables to detect public signature mutations."""
    if not pre_info or not post_info:
        return {"changed_signatures": [], "removed_symbols": [], "added_symbols": []}

    def _fn_map(info: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for fn in info.get("functions", []):
            qn = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
            out[qn] = fn
        return out

    def _cls_map(info: dict) -> dict[str, dict]:
        return {c["name"]: c for c in info.get("classes", []) if c.get("name")}

    pre_fns = _fn_map(pre_info)
    post_fns = _fn_map(post_info)
    pre_cls = _cls_map(pre_info)
    post_cls = _cls_map(post_info)

    changed: list[dict] = []
    removed: list[dict] = []
    added: list[str] = []

    for qn, old_fn in pre_fns.items():
        if qn not in post_fns:
            removed.append({
                "symbol": qn,
                "kind": "function",
                "old_signature": old_fn.get("signature", qn),
            })
        else:
            new_fn = post_fns[qn]
            old_ih = old_fn.get("interface_hash")
            new_ih = new_fn.get("interface_hash")
            old_sig = old_fn.get("signature", "")
            new_sig = new_fn.get("signature", "")
            if (old_ih and new_ih and old_ih != new_ih) or old_sig != new_sig:
                changed.append({
                    "symbol": qn,
                    "kind": "function",
                    "old_signature": old_sig,
                    "new_signature": new_sig,
                    "line": new_fn.get("line_start", 1),
                })

    for qn in post_fns:
        if qn not in pre_fns:
            added.append(qn)

    for cname, old_c in pre_cls.items():
        if cname not in post_cls:
            removed.append({
                "symbol": cname,
                "kind": "class",
                "old_signature": old_c.get("signature", cname),
            })
        else:
            new_c = post_cls[cname]
            if old_c.get("bases") != new_c.get("bases"):
                changed.append({
                    "symbol": cname,
                    "kind": "class",
                    "old_signature": old_c.get("signature", cname),
                    "new_signature": new_c.get("signature", cname),
                    "line": new_c.get("line_start", 1),
                })

    return {
        "changed_signatures": changed,
        "removed_symbols": removed,
        "added_symbols": added,
    }


def _cst_items(source: str, ext_l: str) -> list[tuple[tuple[str, str, str], int, int, str]]:
    from .parsers import _extract_sfc_script_preserving_lines, get_parser

    orig_lines = source.splitlines()
    p_src, p_ext = _extract_sfc_script_preserving_lines(source) if ext_l in (".vue", ".svelte") else (source, ext_l)
    parser = get_parser(p_ext)
    if parser is None:
        return []
    root_nd = parser.parse(p_src.encode("utf-8", errors="replace")).root_node
    if not root_nd.has_error:
        return []
    out: list[tuple[tuple[str, str, str], int, int, str]] = []
    stack = [root_nd]
    while stack:
        nd = stack.pop()
        if not nd.has_error:
            continue
        if nd.type == "ERROR" or nd.is_missing:
            pt = nd.parent.type if nd.parent else ""
            txt = (nd.text or b"").decode("utf-8", errors="replace")
            if p_ext in (".tsx", ".jsx") and pt in ("jsx_element", "jsx_fragment") and "&" in txt and "<" not in txt and "{" not in txt:
                continue
            ln, col = nd.start_point[0] + 1, nd.start_point[1] + 1
            ltxt = orig_lines[ln - 1].strip()[:120] if 1 <= ln <= len(orig_lines) else ""
            prev = (txt.splitlines() or [nd.type])[0][:40]
            out.append(((f"M:{nd.type}" if nd.is_missing else "E", pt, ltxt), ln, col, prev))
        else:
            stack.extend(reversed(nd.children))
    return out


def _file_diag_lines(root: Path, rel_path: str, *, src_override: str | None = None) -> list[str]:
    """Return pinpoint `[SYNTAX ERROR]` lines for `rel_path`."""
    from collections import Counter

    abs_path = root / rel_path
    if src_override is not None:
        src = src_override
    else:
        try:
            src = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [f"[SYNTAX ERROR] {rel_path}: unable to read file."]
    ext_l = abs_path.suffix.lower()
    if ext_l == ".py":
        try:
            ast.parse(src, filename=rel_path)
            return []
        except SyntaxError as exc:
            return [f"[SYNTAX ERROR] {rel_path}:L{exc.lineno or 1}: {exc.msg or 'invalid syntax'}"]
    items = _cst_items(src, ext_l)
    if not items:
        return []
    if (root / ".git").exists():
        try:
            p = subprocess.run(
                ["git", "show", f"HEAD:{rel_path}"],
                cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2,
            )
            if p.returncode == 0 and p.stdout:
                if p.stdout == src:
                    return []
                hc = Counter(x[0] for x in _cst_items(p.stdout, ext_l))
                items = [x for x in items if not (hc[x[0]] > 0 and not hc.subtract([x[0]]))]
                if not items:
                    return []
        except (OSError, subprocess.TimeoutExpired):
            pass
    _, ln, col, prev = items[0]
    return [f"[SYNTAX ERROR] {rel_path}:L{ln}:{col}: syntax error near `{prev}`"]


def _norm_rel_fast(root: Path, raw_f: str) -> str:
    p = Path(raw_f)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            try:
                return str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                return ""
    norm = raw_f.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def audit_edited_files(
    root: Path,
    files: list[str],
    *,
    source: str = "post-edit",
    staged_overrides: dict[str, str] | None = None,
    deleted_files: list[str] | None = None,
) -> dict:
    """Run the 5-layer incremental guardrail audit on `files` inside `root`."""
    resolved_root = Path(root).resolve()
    rel_files: list[str] = []
    for f in files:
        r = _norm_rel_fast(resolved_root, str(f))
        if r and r not in rel_files:
            rel_files.append(r)

    del_rels: list[str] = []
    for df in (deleted_files or ()):
        rd = _norm_rel_fast(resolved_root, str(df))
        if rd and rd not in del_rels:
            del_rels.append(rd)

    overrides_norm: dict[str, str] = {}
    for k_ov, v_ov in (staged_overrides or {}).items():
        r_ov = _norm_rel_fast(resolved_root, str(k_ov))
        if r_ov:
            overrides_norm[r_ov] = v_ov

    if not rel_files and not del_rels:
        return {"clean": True, "blocking": False, "report": ""}

    db_file = resolved_root / ".projectbrain.db"
    cache_file = resolved_root / ".projectbrain.json"
    pending_pre: dict[str, dict] = {}
    pending_src: dict[str, str] = {}
    pending_parsed: dict[str, tuple] = {}

    # Fast Cold-SQLite Salsa Early-Cutoff (post-edit only — NEVER skip pre-commit!):
    # If `.projectbrain.db` exists and edited files have valid syntax, unchanged AST hashes,
    # unchanged `interface_hash`/`imports`/`calls`/`return_keys`, no boundary violations touching
    # `rel_files`, and no pinned invariants on `rel_files`, exit in <15ms.
    if source != "pre-commit" and db_file.exists() and not cache_file.exists():
        from .memory import RepoMemory
        from .store import SQLiteIndexStore

        mem = RepoMemory(resolved_root)
        if not any(
            m.get("rel_path") in rel_files or any(r in (m.get("target_id") or "") for r in rel_files)
            for m in mem._notes
        ):
            from .parsers import parse_with_tree as _parse_with_tree

            db_store = SQLiteIndexStore(db_file)
            fast_clean = True
            fast_pre_infos: dict[str, dict] = {}
            for r in rel_files:
                f_hit = db_store.get_file_fast(r)
                fpath = resolved_root / r
                if not f_hit or not f_hit.get("info") or not fpath.exists():
                    fast_clean = False
                    break
                pre_inf = f_hit["info"]
                fast_pre_infos[r] = pre_inf
                try:
                    st = fpath.stat()
                    src_txt = fpath.read_text(encoding="utf-8", errors="ignore")
                    post_inf, ts_tree = _parse_with_tree(src_txt, fpath.suffix.lower())
                    pending_pre[r] = dict(pre_inf)
                    pending_src[r] = src_txt
                    pending_parsed[r] = (post_inf, ts_tree, src_txt, st.st_mtime, st.st_size)
                except OSError:
                    fast_clean = False
                    break

                if _needs_deep_hook_audit(r, pre_inf, post_inf, src_txt):
                    fast_clean = False
                    break

                def _shape(inf: dict) -> tuple:
                    return (
                        inf.get("interface_hash"),
                        tuple(sorted(inf.get("imports") or [])),
                        tuple(sorted(
                            (
                                fn_d.get("parent_class") or "",
                                fn_d["name"],
                                fn_d.get("ast_hash") or "",
                                tuple(sorted(fn_d.get("calls") or [])),
                                tuple(sorted(fn_d.get("return_keys") or [])),
                            )
                            for fn_d in inf.get("functions", [])
                        )),
                        tuple(sorted(
                            (
                                cl_d["name"],
                                cl_d.get("ast_hash") or "",
                            )
                            for cl_d in inf.get("classes", [])
                        )),
                    )

                if _shape(post_inf) != _shape(pre_inf) or (
                    post_inf.get("parse_status") == "partial"
                    and (r.endswith(".py") or pre_inf.get("parse_status") != "partial" or _file_diag_lines(resolved_root, r))
                ):
                    fast_clean = False
                    break
            if fast_clean:
                fast_bv = db_store.check_boundary_violations_fast(
                    mem, set(rel_files), fast_pre_infos,
                )
                if fast_bv is not None and len(fast_bv) == 0:
                    from .ledger import SavingsLedger

                    SavingsLedger(resolved_root).record_guardrail_audit(
                        source=source,
                        files_audited=len(rel_files),
                        syntax_errors=0,
                        contract_violations=0,
                        boundary_violations=0,
                        signature_blast_alerts=0,
                        stale_invariants=0,
                    )
                    return {
                        "clean": True,
                        "blocking": False,
                        "files": rel_files,
                        "syntax_errors": [],
                        "contract_violations": [],
                        "boundary_violations": [],
                        "signature_blast": [],
                        "memories": [],
                        "stale_memories": [],
                        "covering_tests": [],
                        "report": "",
                    }

    from .brain import ProjectBrain

    brain = ProjectBrain(str(resolved_root))
    brain._pre_edit_index.update(pending_pre)
    brain._source_cache.update(pending_src)
    brain._pending_parsed_files.update(pending_parsed)

    # Ensure pre-edit symbol snapshots are captured from SQLite / git HEAD BEFORE re-indexing
    if overrides_norm or del_rels:
        from .graph import CodeGraph
        from .parsers import parse_with_tree as _parse_ov

        brain.index(persist=False)
        for r_ov, blob_ov in overrides_norm.items():
            ov_inf, _ = _parse_ov(blob_ov, Path(r_ov).suffix.lower())
            brain._index[r_ov] = ov_inf
            brain._source_cache[r_ov] = blob_ov
            brain._sizes[r_ov] = len(blob_ov.encode("utf-8", errors="ignore"))
        for r_del in del_rels:
            brain._index.pop(r_del, None)
            brain._source_cache.pop(r_del, None)
        brain._build_blobs(recompute_imports=True)
        brain._graph = CodeGraph.build(
            brain._index,
            import_targets=brain._import_targets,
            symbol_imports=brain._symbol_imports,
        )
        brain._sync_interprocedural_effects()
        brain._blobs_ready = True
    elif not brain._db_store.db_path.exists() and not brain._cache_path.exists():
        brain.index()
    elif any(r not in brain._pre_edit_index for r in rel_files):
        brain._capture_pre_edit_snapshots(rel_files)

    pre_snapshots: dict[str, dict] = {}
    for r in rel_files:
        pre_inf = brain._pre_edit_index.get(r) or brain._get_git_head_info(r)
        if pre_inf:
            pre_snapshots[r] = pre_inf

    if overrides_norm or del_rels:
        from .config import apply_rule_and_suppression_filters, load_repo_config, resolve_active_rules
        from .git_diff import verify_deleted_or_renamed_file_consumers

        imp_res = brain.impact(rel_files, _already_fresh=True) if rel_files else {
            "details": [], "contract_diagnostics": [], "semantic_warnings": [],
        }
        diags_ov = list(imp_res.get("contract_diagnostics", []))
        if del_rels:
            del_diags, _ = verify_deleted_or_renamed_file_consumers(brain, set(del_rels))
            for dd in del_diags:
                if dd not in diags_ov:
                    diags_ov.append(dd)
        bv_ov = (
            brain._memory.check_boundary_violations(
                brain._index, brain._import_targets, brain._graph, focus_rels=set(rel_files),
            )
            if rel_files
            else []
        )
        for bv in bv_ov:
            if bv["message"] not in diags_ov:
                diags_ov.append(bv["message"])
        repo_cfg = load_repo_config(resolved_root)
        active_rules, eff_profile, has_custom = resolve_active_rules(repo_cfg)
        check_res = apply_rule_and_suppression_filters(
            {
                "impact_analysis": imp_res,
                "contract_diagnostics": diags_ov,
                "boundary_violations": bv_ov,
                "semantic_warnings": list(imp_res.get("semantic_warnings") or []),
                "memories": [],
                "all_contracts_valid": len(diags_ov) == 0,
            },
            resolved_root,
            active_rules=active_rules,
            exclude_globs=list(repo_cfg.get("exclude") or []),
            profile_name=eff_profile,
            has_custom_filter=has_custom,
            allow_inline_boundary_ignore=bool(repo_cfg.get("allow_inline_boundary_ignore", False)),
            file_text_getter=brain._get_file_text,
        )
    else:
        check_res = brain.check(files_modified=rel_files)
    imp = check_res.get("impact_analysis", {})
    details_by_rel = {d["rel_path"]: d for d in imp.get("details", []) if d.get("rel_path")}

    # 1. Pinpoint Syntax Errors (suppressing pre-existing CST grammar gaps on committed files)
    syntax_errors: list[str] = []
    files_with_syntax_err: set[str] = set()
    for r in rel_files:
        post_inf = brain._index.get(r, {})
        if post_inf.get("parse_status") == "partial":
            s_errs = _file_diag_lines(brain.root, r, src_override=overrides_norm.get(r))
            if s_errs:
                files_with_syntax_err.add(r)
                syntax_errors.extend(s_errs)

    # 2. Architectural Boundary Violations
    boundary_violations: list[dict] = check_res.get("boundary_violations", [])
    boundary_msgs = {bv["message"] for bv in boundary_violations}

    # 3. Cross-File & 6D Contract Violations (excluding syntax & boundary duplicates)
    contract_violations: list[str] = [
        d for d in check_res.get("contract_diagnostics", [])
        if not d.startswith("[SYNTAX ERROR]") and d not in boundary_msgs
    ]

    # 4. Public Signature Mutation Blast Radius (only when signatures changed or symbols deleted)
    from .graph import fn_id
    from .reporters import _clean_uri, _norm_line_key

    def _dedup_blast_callers(items: list[dict], limit: int) -> list[dict]:
        deduped: list[dict] = []
        seen_locs: set[tuple[str, int | str]] = set()
        for c in items:
            cf = _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="")
            if not cf:
                continue
            ln_key = _norm_line_key(c.get("line"))
            loc_key = (cf, ln_key)
            if loc_key not in seen_locs:
                seen_locs.add(loc_key)
                deduped.append({**c, "caller_file": cf})
                if len(deduped) >= limit:
                    break
        return deduped

    signature_blast: list[dict] = []
    covering_tests: list[str] = []
    rel_files_set = set(rel_files)
    for r in rel_files:
        post_inf = brain._index.get(r)
        pre_inf = pre_snapshots.get(r)
        if not pre_inf or not post_inf or r in files_with_syntax_err:
            continue
        idiff = compute_interface_diff(pre_inf, post_inf)
        mutated = idiff["changed_signatures"] + idiff["removed_symbols"]
        if not mutated:
            continue
        det = details_by_rel.get(r, {})
        direct_mut_callers: list[dict] = []
        if brain._graph:
            for m in mutated:
                sym = m.get("symbol") or ""
                if not sym or m.get("kind") != "function":
                    continue
                p_cls, bare_nm = sym.split(".", 1) if "." in sym else (None, sym)
                m_nid = fn_id(r, bare_nm, p_cls)
                for ei in brain._graph._radj.get(m_nid, ()):
                    edge = brain._graph.edges[ei]
                    if edge["type"] != "calls":
                        continue
                    s_nd = brain._graph.nodes.get(edge["src"], {})
                    cf_norm = _clean_uri(s_nd.get("rel_path") or "", fallback="")
                    if cf_norm and cf_norm not in rel_files_set:
                        direct_mut_callers.append({
                            "caller_file": cf_norm,
                            "caller": edge["src"],
                            "callee": bare_nm,
                            "line": edge.get("line"),
                        })
        ext_callers = [
            {
                "caller_file": _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback=""),
                "caller": c.get("caller") or c.get("called_by") or "",
                "callee": c.get("callee") or c.get("function") or "",
                "line": c.get("line"),
            }
            for c in det.get("called_by", [])
            if _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="")
            and _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="") not in rel_files_set
        ]
        ext_importers = [
            imp_f for imp_f in det.get("direct_consumers", [])
            if imp_f not in rel_files_set
        ]
        for tf in det.get("test_consumers", []):
            if tf not in covering_tests:
                covering_tests.append(tf)

        if direct_mut_callers or ext_callers or ext_importers:
            mutated_names = {m["symbol"].split(".")[-1] for m in mutated} | {m["symbol"] for m in mutated}
            matched_callers = direct_mut_callers + [
                c for c in ext_callers
                if c.get("callee") in mutated_names or c.get("callee", "").split(".")[-1] in mutated_names
            ]
            relevant_callers = (
                _dedup_blast_callers(matched_callers, limit=8)
                if matched_callers
                else _dedup_blast_callers(ext_callers, limit=6)
            )
            signature_blast.append({
                "file": r,
                "mutations": mutated,
                "callers": relevant_callers,
                "consumers": ext_importers[:8],
                "tests": det.get("test_consumers", [])[:6],
            })

    # 5. AST-Anchored Invariants, Stale Memory Warnings & Semantic Advisories
    memories = check_res.get("memories", [])
    stale_memories = [m for m in memories if m.get("stale")]
    semantic_warnings: list[str] = list(check_res.get("semantic_warnings") or [])

    brain._ledger.record_guardrail_audit(
        source=source,
        files_audited=len(rel_files),
        syntax_errors=len(syntax_errors),
        contract_violations=len(contract_violations),
        boundary_violations=len(boundary_violations),
        signature_blast_alerts=len(signature_blast),
        stale_invariants=len(stale_memories),
    )

    blocking = bool(syntax_errors or contract_violations or boundary_violations)
    advisory = bool(signature_blast or memories or semantic_warnings)
    if not blocking and not advisory:
        return {
            "clean": True,
            "blocking": False,
            "files": rel_files,
            "syntax_errors": [],
            "contract_violations": [],
            "boundary_violations": [],
            "signature_blast": [],
            "memories": [],
            "stale_memories": [],
            "semantic_warnings": [],
            "covering_tests": [],
            "report": "",
        }

    lines: list[str] = ["[ProjectBrain Guardrail]"]
    for se in syntax_errors:
        lines.append(f"  ! {se}")
    for bv in boundary_violations:
        lines.append(f"  ! {bv['message']}")
    for cv in contract_violations[:8]:
        lines.append(f"  ! [CONTRACT] {cv}")
    for sw in semantic_warnings[:6]:
        lines.append(f"  ~ [WARNING] {sw}")

    for sb in signature_blast:
        for mut in sb["mutations"][:4]:
            if "new_signature" in mut:
                if mut.get("old_signature") == mut.get("new_signature"):
                    lines.append(
                        f"  ~ [SIGNATURE CHANGED] {sb['file']} :: return/interface contract updated for `{mut['new_signature']}`"
                    )
                else:
                    lines.append(
                        f"  ~ [SIGNATURE CHANGED] {sb['file']} :: `{mut['old_signature']}` -> `{mut['new_signature']}`"
                    )
            else:
                lines.append(
                    f"  ~ [SYMBOL REMOVED] {sb['file']} :: `{mut['old_signature']}`"
                )
        if sb["callers"]:
            dedup_sb_callers = _dedup_blast_callers(sb["callers"], limit=5)
            caller_locs = ", ".join(
                f"{c['caller_file']}:L{_norm_line_key(c.get('line'))} ({c.get('caller', '?')})"
                for c in dedup_sb_callers
            )
            lines.append(f"    Downstream callers to verify: {caller_locs}")
        elif sb["consumers"]:
            lines.append(f"    Downstream importers to verify: {', '.join(sb['consumers'][:5])}")

    for mem in memories[:5]:
        badge = "[INVARIANT MUTATED — VERIFY STILL HOLDS]" if mem.get("stale") else "[PINNED INVARIANT]"
        lines.append(f"  * {badge} ({mem.get('target_id')}): {mem.get('note')}")

    if covering_tests:
        lines.append(f"  > Covering tests: {' '.join(covering_tests[:5])}")

    return {
        "clean": False,
        "blocking": blocking,
        "files": rel_files,
        "syntax_errors": syntax_errors,
        "contract_violations": contract_violations,
        "boundary_violations": boundary_violations,
        "signature_blast": signature_blast,
        "memories": memories,
        "stale_memories": stale_memories,
        "semantic_warnings": semantic_warnings,
        "covering_tests": covering_tests,
        "report": "\n".join(lines),
    }


def run_post_edit_hook(
    raw_stdin: str = "",
    explicit_files: list[str] | None = None,
    default_root: Path | None = None,
) -> tuple[int, str]:
    """Execute post-edit hook and return `(exit_code, formatted_stdout)`."""
    parsed = parse_hook_payload(raw_stdin, explicit_files=explicit_files, default_root=default_root)
    files = parsed.get("files", [])
    gov_edited = bool(parsed.get("governance_edited"))
    if not files and not gov_edited:
        return 0, ""

    protocol = parsed["protocol"]
    root: Path = parsed["root"]
    report_parts: list[str] = []

    if gov_edited:
        from .git_diff import check_governance_tamper, find_git_context
        if find_git_context(root)[0] is not None:
            tampers = check_governance_tamper(root, ref_commit="HEAD")
            if tampers:
                gov_files = ", ".join(sorted({tp.get("file", ".projectbrain_memory.json") for tp in tampers}))
                gov_lines = [
                    f"[ProjectBrain Guardrail — ACTION REQUIRED] (hook:{protocol} on {gov_files})"
                ]
                for tp in tampers:
                    gov_lines.append(f"  X {tp['message']}")
                report_parts.append("\n".join(gov_lines))

    if files:
        audit = audit_edited_files(root, files, source=f"hook:{protocol}")
        if not audit["clean"] and audit["report"]:
            report_parts.append(audit["report"])

    if not report_parts:
        return 0, ""

    combined_report = "\n\n".join(report_parts)
    if protocol == "claude":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": combined_report,
            }
        }
        return 0, json.dumps(payload)

    return 0, combined_report


def run_pre_commit_hook(root: Path, strict: bool = False) -> tuple[int, str]:
    """Audit git staged files before commit; return `(exit_code, message)`."""
    from .git_diff import _to_local_rel, check_governance_tamper, find_git_context

    git_root, sub_prefix = find_git_context(root)
    if git_root is None:
        return 0, ""

    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-status"],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, ""

    if proc.returncode != 0 or not proc.stdout.strip():
        return 0, ""

    staged: list[str] = []
    deleted_staged: list[str] = []
    staged_repo_paths: list[str] = []
    gov_staged = False
    for raw_ln in proc.stdout.splitlines():
        parts = raw_ln.strip().split("\t")
        if len(parts) < 2:
            continue
        status_code = parts[0].strip().upper()[:1]
        cand_path = parts[-1].strip()
        if any(_to_local_rel(p.strip(), sub_prefix) in _GOVERNANCE_FILES for p in parts[1:]):
            gov_staged = True
            continue
        if status_code == "D":
            local_del = _to_local_rel(parts[1].strip(), sub_prefix)
            if local_del and _is_auditable_code_path(local_del):
                deleted_staged.append(local_del)
            continue
        if status_code == "R" and len(parts) >= 3:
            local_old = _to_local_rel(parts[1].strip(), sub_prefix)
            if local_old and _is_auditable_code_path(local_old):
                deleted_staged.append(local_old)
        local_cand = _to_local_rel(cand_path, sub_prefix)
        if local_cand and status_code in ("A", "C", "M", "R") and _is_auditable_code_path(local_cand):
            staged.append(local_cand)
            staged_repo_paths.append(cand_path)

    # Detect any staged files whose working-tree copy differs from the git index (partial staging / unstaged edits)
    # so `pre-commit` audits the EXACT blobs being committed, not stale working-tree files!
    staged_overrides: dict[str, str] = {}
    if staged_repo_paths:
        from .git_diff import read_blob_at_ref

        try:
            wt_diff = subprocess.run(
                ["git", "-c", "core.quotepath=false", "diff", "--name-only", "--", *staged_repo_paths],
                cwd=str(git_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if wt_diff.returncode == 0 and wt_diff.stdout.strip():
                for dirty_rp in wt_diff.stdout.splitlines():
                    loc_dirty = _to_local_rel(dirty_rp.strip(), sub_prefix)
                    if loc_dirty and loc_dirty in staged:
                        idx_blob = read_blob_at_ref(root, "", loc_dirty)
                        if idx_blob is not None:
                            staged_overrides[loc_dirty] = idx_blob
        except (OSError, subprocess.TimeoutExpired):
            pass

    gov_fail = False
    report_blocks: list[str] = []
    if gov_staged and os.environ.get("PROJECTBRAIN_ALLOW_GOVERNANCE_CHANGE") != "1":
        tampers = check_governance_tamper(root, ref_commit="HEAD", from_git_index=True)
        if tampers:
            gov_fail = True
            gov_files = ", ".join(sorted({tp.get("file", ".projectbrain_memory.json") for tp in tampers}))
            gov_lines = [
                f"[ProjectBrain Guardrail — ACTION REQUIRED] (pre-commit on {gov_files})"
            ]
            for tp in tampers:
                gov_lines.append(f"  X {tp['message']}")
            report_blocks.append("\n".join(gov_lines))

    should_fail = gov_fail
    if staged or deleted_staged:
        audit = audit_edited_files(
            root,
            staged,
            source="pre-commit",
            staged_overrides=staged_overrides or None,
            deleted_files=deleted_staged or None,
        )
        if not audit["clean"] and audit["report"]:
            report_blocks.append(audit["report"])
            # Blocking findings (syntax errors, explicit boundary rules, proven contract errors,
            # and governance tampering) block `git commit`; advisory warnings and stale invariants
            # remain non-blocking unless `--strict` is passed.
            deterministic_fail = bool(
                audit["syntax_errors"]
                or audit["boundary_violations"]
                or audit["contract_violations"]
            )
            if deterministic_fail or (strict and bool(audit.get("semantic_warnings") or audit["stale_memories"])):
                should_fail = True

    if not report_blocks:
        return 0, ""
    return (1 if should_fail else 0), "\n\n".join(report_blocks)


_GIT_HOOK_MARKER = "# [ProjectBrain pre-commit]"
GITHUB_WORKFLOW_MARKER = "# projectbrain-generated-workflow"


def resolve_cli_shell_prefix() -> str:
    """Resolve `projectbrain` binary to an absolute quoted command string for editor/git hooks."""
    which_cli = shutil.which("projectbrain")
    if which_cli:
        resolved = str(Path(which_cli).resolve())
        return f'"{resolved}"' if " " in resolved else resolved
    exe = sys.executable
    exe_q = f'"{exe}"' if " " in exe else exe
    return f"{exe_q} -m projectbrain.cli"


def build_hook_command(
    resolved_root: str,
    subcmd: str = "hook post-edit",
    *,
    portable: bool = False,
) -> str:
    """Build a hook shell command (either local absolute-path or OS-portable team command)."""
    if portable:
        return f"projectbrain {subcmd}"
    prefix = resolve_cli_shell_prefix()
    root_q = f'"{resolved_root}"'
    return f"{prefix} --path {root_q} {subcmd}"


def merge_claude_hooks(
    claude_hooks_path: Path,
    resolved_root: str,
    *,
    portable: bool = False,
) -> None:
    """Configure active `PostToolUse` guardrail hooks in `.claude/settings[.local].json`."""
    claude_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if claude_hooks_path.exists():
        try:
            loaded = json.loads(claude_hooks_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            pass
    hooks_dict = data.get("hooks")
    if not isinstance(hooks_dict, dict):
        hooks_dict = {}
        data["hooks"] = hooks_dict

    for event_key in ("PreToolUse", "PostToolUse"):
        existing_list = hooks_dict.get(event_key)
        if isinstance(existing_list, list):
            cleaned = [
                entry for entry in existing_list
                if "[ProjectBrain]" not in json.dumps(entry)
                and "hook post-edit" not in json.dumps(entry)
            ]
            if cleaned:
                hooks_dict[event_key] = cleaned
            else:
                hooks_dict.pop(event_key, None)

    post_cmd = build_hook_command(resolved_root, "hook post-edit", portable=portable)
    pb_post_entry = {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": post_cmd,
            }
        ],
    }
    hooks_dict.setdefault("PostToolUse", []).append(pb_post_entry)
    claude_hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def merge_cursor_hooks(
    cursor_hooks_path: Path,
    resolved_root: str,
    *,
    portable: bool = False,
) -> None:
    """Configure Cursor `afterFileEdit` guardrail hook in `.cursor/hooks.json`."""
    cursor_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"version": 1, "hooks": {}}
    if cursor_hooks_path.exists():
        try:
            loaded = json.loads(cursor_hooks_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                data = loaded
                data.setdefault("version", 1)
                if not isinstance(data.get("hooks"), dict):
                    data["hooks"] = {}
        except json.JSONDecodeError:
            pass
    after_edit = data["hooks"].get("afterFileEdit")
    if not isinstance(after_edit, list):
        after_edit = []
    after_edit = [e for e in after_edit if "hook post-edit" not in json.dumps(e)]
    after_edit.append({"command": build_hook_command(resolved_root, "hook post-edit", portable=portable)})
    data["hooks"]["afterFileEdit"] = after_edit
    cursor_hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _resolve_git_hooks_dir(root_path: Path) -> Path | None:
    """Resolve `.git/hooks` directory, supporting both standard repos and linked `git worktree`s."""
    git_entry = root_path / ".git"
    if git_entry.is_dir():
        return git_entry / "hooks"
    if git_entry.is_file():
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--git-path", "hooks"],
                cwd=str(root_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw_p = Path(proc.stdout.strip())
                return raw_p if raw_p.is_absolute() else (root_path / raw_p).resolve()
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


def install_git_pre_commit_hook(
    root_path: Path,
    resolved_root: str,
    *,
    portable: bool = False,
) -> bool:
    """Install or append ProjectBrain pre-commit guardrail into `.git/hooks/pre-commit`."""
    hooks_dir = _resolve_git_hooks_dir(root_path)
    if hooks_dir is None:
        return False
    hook_file = hooks_dir / "pre-commit"
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_line = build_hook_command(resolved_root, "hook pre-commit", portable=portable)
    snippet = f"\n{_GIT_HOOK_MARKER}\n{cmd_line}\n"
    if hook_file.exists():
        existing = hook_file.read_text(encoding="utf-8", errors="ignore")
        if _GIT_HOOK_MARKER in existing or "hook pre-commit" in existing:
            return True
        updated = existing.rstrip() + "\n" + snippet
        hook_file.write_text(updated, encoding="utf-8")
    else:
        hook_file.write_text(f"#!/usr/bin/env sh\nset -e\n{snippet}", encoding="utf-8")
    try:
        cur_mode = hook_file.stat().st_mode
        hook_file.chmod(cur_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return True


def write_github_action_workflow(root_path: Path) -> Path:
    """Write a production-ready GitHub Actions PR guardrail workflow to `.github/workflows/projectbrain.yml`."""
    wf_path = root_path / ".github" / "workflows" / "projectbrain.yml"
    wf_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_yaml = f"""{GITHUB_WORKFLOW_MARKER}
name: ProjectBrain Guardrail

on:
  pull_request:
  push:
    branches: [main, master]

permissions:
  contents: read
  pull-requests: write
  security-events: write

jobs:
  guardrail:
    name: Architectural & Contract Verification
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository (full history for merge-base & co-change diffing)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install ProjectBrain
        run: |
          python -m pip install --upgrade pip
          pip install projectbrain-mcp

      - name: Build code graph index
        run: projectbrain index

      - name: Generate SARIF v2.1.0 report
        continue-on-error: true
        run: projectbrain check --format sarif > projectbrain.sarif

      - name: Upload SARIF report Artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: projectbrain-sarif
          path: projectbrain.sarif
          if-no-files-found: ignore

      - name: Upload SARIF to GitHub Code Scanning
        if: always()
        continue-on-error: true
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: projectbrain.sarif

      - name: Render sticky PR guardrail report
        if: github.event_name == 'pull_request'
        continue-on-error: true
        run: projectbrain check --format pr-comment > projectbrain-pr-comment.md

      - name: Upsert sticky PR comment
        if: always() && github.event_name == 'pull_request'
        continue-on-error: true
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            if (!fs.existsSync('projectbrain-pr-comment.md')) return;
            const body = fs.readFileSync('projectbrain-pr-comment.md', 'utf8');
            const marker = '<!-- projectbrain-pr-guardrail -->';
            const comments = await github.paginate(github.rest.issues.listComments, {{
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              per_page: 100,
            }});
            const existing = comments.find(c => c.body && c.body.includes(marker));
            if (existing) {{
              await github.rest.issues.updateComment({{
                owner: context.repo.owner,
                repo: context.repo.repo,
                comment_id: existing.id,
                body,
              }});
            }} else {{
              await github.rest.issues.createComment({{
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body,
              }});
            }}

      - name: Enforce ProjectBrain CI gate
        run: projectbrain check --ci
"""
    wf_path.write_text(workflow_yaml, encoding="utf-8")
    return wf_path


def _clean_claude_settings_file(claude_hooks_path: Path, action_label: str, removed: list[str]) -> None:
    if not claude_hooks_path.exists():
        return
    try:
        data = json.loads(claude_hooks_path.read_text(encoding="utf-8", errors="ignore"))
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            hooks_dict = data["hooks"]
            changed = False
            for event_key in ("PreToolUse", "PostToolUse"):
                existing_list = hooks_dict.get(event_key)
                if isinstance(existing_list, list):
                    cleaned = [
                        entry for entry in existing_list
                        if "[ProjectBrain]" not in json.dumps(entry)
                        and "hook post-edit" not in json.dumps(entry)
                    ]
                    if len(cleaned) != len(existing_list):
                        changed = True
                    if cleaned:
                        hooks_dict[event_key] = cleaned
                    else:
                        hooks_dict.pop(event_key, None)
            if not hooks_dict:
                data.pop("hooks", None)
            if changed:
                if data:
                    claude_hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                else:
                    claude_hooks_path.unlink(missing_ok=True)
                removed.append(action_label)
    except (OSError, json.JSONDecodeError):
        pass


def remove_projectbrain_hooks(root_path: Path) -> list[str]:
    """Remove ProjectBrain hooks from `.claude/settings[.local].json`, `.cursor/hooks.json`, `.git/hooks/pre-commit`, and generated CI workflow."""
    removed: list[str] = []

    _clean_claude_settings_file(
        root_path / ".claude" / "settings.local.json",
        "removed_.claude/settings.local.json_hooks",
        removed,
    )
    _clean_claude_settings_file(
        root_path / ".claude" / "settings.json",
        "removed_.claude/settings.json_hooks",
        removed,
    )

    cursor_hooks_path = root_path / ".cursor" / "hooks.json"
    if cursor_hooks_path.exists():
        try:
            data = json.loads(cursor_hooks_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
                after_edit = data["hooks"].get("afterFileEdit")
                if isinstance(after_edit, list):
                    cleaned = [e for e in after_edit if "hook post-edit" not in json.dumps(e)]
                    if len(cleaned) != len(after_edit):
                        if cleaned:
                            data["hooks"]["afterFileEdit"] = cleaned
                            cursor_hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                        else:
                            data["hooks"].pop("afterFileEdit", None)
                            if not data["hooks"]:
                                cursor_hooks_path.unlink(missing_ok=True)
                            else:
                                cursor_hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                        removed.append("removed_.cursor/hooks.json")
        except (OSError, json.JSONDecodeError):
            pass

    wf_path = root_path / ".github" / "workflows" / "projectbrain.yml"
    if wf_path.exists():
        try:
            wf_txt = wf_path.read_text(encoding="utf-8", errors="ignore")
            if GITHUB_WORKFLOW_MARKER in wf_txt:
                wf_path.unlink(missing_ok=True)
                removed.append("removed_.github/workflows/projectbrain.yml")
                for parent_dir in (wf_path.parent, wf_path.parent.parent):
                    if parent_dir.exists() and not any(parent_dir.iterdir()):
                        parent_dir.rmdir()
        except OSError:
            pass

    hooks_dir = _resolve_git_hooks_dir(root_path)
    hook_file = hooks_dir / "pre-commit" if hooks_dir is not None else (root_path / ".git" / "hooks" / "pre-commit")
    if hook_file.exists():
        try:
            existing = hook_file.read_text(encoding="utf-8", errors="ignore")
            if _GIT_HOOK_MARKER in existing or "hook pre-commit" in existing:
                lines = [
                    ln for ln in existing.splitlines()
                    if _GIT_HOOK_MARKER not in ln and "hook pre-commit" not in ln
                ]
                rem_body = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#!") and ln.strip() != "set -e"]
                if not rem_body:
                    hook_file.unlink(missing_ok=True)
                else:
                    hook_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
                removed.append("removed_.git/hooks/pre-commit")
        except OSError:
            pass

    return removed
