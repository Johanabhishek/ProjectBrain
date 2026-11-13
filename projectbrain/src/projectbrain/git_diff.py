"""Git Base-Ref / PR Merge-Base Diff Engine & Governance Tamper Lock for ProjectBrain.

Provides:
  1. Automatic CI PR target-branch detection (`GITHUB_BASE_REF`, GitLab MR, Bitbucket, Azure)
     and merge-base SHA resolution (`git merge-base`), including shallow PR merge-commit fallbacks.
  2. Full branch/working-tree status diffing (`A`, `M`, `D`, `R`, `C` + untracked files) so
     `projectbrain check` audits committed PR diffs even when the working tree is clean.
  3. Base-ref AST snapshot hydration for signature blast-radius & deleted-callable checks, plus
     broken-import detection for files deleted or renamed in a PR.
  4. Governance Tamper Lock on `.projectbrain_memory.json` (catching deleted/weakened `--forbid`
     boundary rules, deleted/modified AST invariants, or removed co-change rules).
  5. Merge-base git history co-change drift warnings.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import resolve
from .hooks import SUPPORTED_CODE_EXTS, _is_auditable_code_path
from .memory import extract_note_rel_path
from .parsers import parse

if TYPE_CHECKING:
    from .brain import ProjectBrain

_CI_BASE_ENV_KEYS = (
    "GITHUB_BASE_REF",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
    "BITBUCKET_PR_DESTINATION_BRANCH",
    "SYSTEM_PULLREQUEST_TARGETBRANCH",
    "CHANGE_TARGET",
)

_LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "uv.lock",
    "pipfile.lock",
    "cargo.lock",
    "go.sum",
    "composer.lock",
    "gemfile.lock",
}


def _git_run(root: Path, args: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _strip_ref_prefix(raw_ref: str) -> str:
    ref = raw_ref.strip()
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _clean_rel_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


_TEMP_PATH_PREFIXES = (".pytest", "pytest-of-", "__pycache__")


def find_git_context(root: Path) -> tuple[Path | None, str]:
    """Return `(git_toplevel_path, sub_prefix)` supporting both repo roots and monorepo subpackages."""
    resolved = Path(root).resolve()
    if (resolved / ".git").exists():
        return resolved, ""

    for parent in resolved.parents:
        try:
            rel_parts = resolved.relative_to(parent).parts
        except ValueError:
            break
        if any(seg.lower().startswith(_TEMP_PATH_PREFIXES) for seg in rel_parts):
            return None, ""
        if (parent / ".git").exists():
            rel_sub = "/".join(rel_parts)
            ig = _git_run(parent, ["check-ignore", "-q", rel_sub], timeout=1.5)
            if ig is not None and ig.returncode == 0:
                return None, ""
            return parent, f"{rel_sub}/"
    return None, ""


def _to_local_rel(repo_rel: str, sub_prefix: str) -> str | None:
    cleaned = _clean_rel_path(repo_rel)
    if not cleaned:
        return None
    if not sub_prefix:
        return cleaned
    if cleaned.startswith(sub_prefix):
        tail = cleaned[len(sub_prefix) :]
        return tail or None
    return None


def detect_ci_base_branch(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """Return `(branch_name, env_var_name)` when running inside a CI Pull/Merge Request."""
    environ = env if env is not None else os.environ
    for key in _CI_BASE_ENV_KEYS:
        val = (environ.get(key) or "").strip()
        if val:
            cleaned = _strip_ref_prefix(val)
            if cleaned:
                return cleaned, key
    return None, None


def _verify_commit(root: Path, ref_spec: str) -> str | None:
    proc = _git_run(root, ["rev-parse", "--verify", "--quiet", f"{ref_spec}^{{commit}}"], timeout=2.0)
    if proc and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[0].strip()
    return None


def resolve_git_base(
    root: Path,
    explicit_base: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Resolve an explicit `--base <ref>` or CI PR target branch to its merge-base SHA with `HEAD`."""
    explicit_clean = (explicit_base or "").strip() or None
    ci_branch, ci_env_key = (None, None) if explicit_clean else detect_ci_base_branch(env)
    requested = explicit_clean or ci_branch
    source = "explicit" if explicit_clean else ("ci_env" if ci_branch else None)

    if not requested:
        return {
            "active": False,
            "valid": True,
            "requested_ref": None,
            "resolved_ref": None,
            "merge_base": None,
            "source": None,
            "env_key": None,
            "error": None,
        }

    git_root, _ = find_git_context(root)
    if git_root is None:
        if source == "explicit":
            return {
                "active": True,
                "valid": False,
                "requested_ref": requested,
                "resolved_ref": None,
                "merge_base": None,
                "source": source,
                "env_key": ci_env_key,
                "error": (
                    f"[GIT BASE ERROR] '{root}' is not a git repository; "
                    f"cannot diff against --base '{requested}'."
                ),
            }
        return {
            "active": False,
            "valid": True,
            "requested_ref": requested,
            "resolved_ref": None,
            "merge_base": None,
            "source": source,
            "env_key": ci_env_key,
            "error": None,
        }

    stripped = _strip_ref_prefix(requested)
    if source == "ci_env":
        candidates = list(dict.fromkeys([
            f"origin/{stripped}",
            f"refs/remotes/origin/{stripped}",
            requested,
            stripped,
            f"refs/heads/{stripped}",
        ]))
    else:
        candidates = list(dict.fromkeys([
            requested,
            stripped,
            f"origin/{stripped}",
            f"refs/heads/{stripped}",
            f"refs/remotes/origin/{stripped}",
        ]))

    for cand in candidates:
        cand_sha = _verify_commit(git_root, cand)
        if not cand_sha:
            continue
        mb_proc = _git_run(git_root, ["merge-base", cand_sha, "HEAD"], timeout=3.0)
        if mb_proc and mb_proc.returncode == 0 and mb_proc.stdout.strip():
            mb_sha = mb_proc.stdout.strip().splitlines()[0].strip()
            return {
                "active": True,
                "valid": True,
                "requested_ref": requested,
                "resolved_ref": cand,
                "merge_base": mb_sha,
                "source": source,
                "env_key": ci_env_key,
                "error": None,
            }
        # Shallow history fallback when `cand` exists locally without full common-ancestor graph
        return {
            "active": True,
            "valid": True,
            "requested_ref": requested,
            "resolved_ref": cand,
            "merge_base": cand_sha,
            "source": source,
            "env_key": ci_env_key,
            "shallow_fallback": True,
            "error": None,
        }

    # CI shallow PR merge-commit fallback: on GitHub `pull_request` events, `HEAD` is a synthetic
    # merge commit (`refs/pull/<N>/merge`) whose first parent `HEAD^1` is the exact base branch tip.
    if source == "ci_env":
        if _verify_commit(git_root, "HEAD^2") is not None:
            parent1 = _verify_commit(git_root, "HEAD^1")
            if parent1:
                return {
                    "active": True,
                    "valid": True,
                    "requested_ref": requested,
                    "resolved_ref": "HEAD^1",
                    "merge_base": parent1,
                    "source": source,
                    "env_key": ci_env_key,
                    "shallow_fallback": True,
                    "error": None,
                }
        parent_prev = _verify_commit(git_root, "HEAD~1")
        if parent_prev:
            return {
                "active": True,
                "valid": True,
                "requested_ref": requested,
                "resolved_ref": "HEAD~1",
                "merge_base": parent_prev,
                "source": source,
                "env_key": ci_env_key,
                "shallow_fallback": True,
                "warning": (
                    f"Base branch '{requested}' was not fetched locally; fell back to HEAD~1. "
                    "Use `fetch-depth: 0` in CI checkout for full multi-commit merge-base diffing."
                ),
                "error": None,
            }
        return {
            "active": False,
            "valid": True,
            "requested_ref": requested,
            "resolved_ref": None,
            "merge_base": None,
            "source": source,
            "env_key": ci_env_key,
            "error": None,
        }

    return {
        "active": True,
        "valid": False,
        "requested_ref": requested,
        "resolved_ref": None,
        "merge_base": None,
        "source": source,
        "env_key": ci_env_key,
        "error": (
            f"[GIT BASE ERROR] Unable to resolve git base reference '{requested}'. "
            f"Ensure the ref exists and is fetched (e.g. `git fetch origin {stripped}`)."
        ),
    }


def compute_git_diff(
    root: Path,
    ref_commit: str = "HEAD",
    *,
    include_untracked: bool = True,
) -> dict:
    """Diff `ref_commit` against the working tree, capturing A/M/D/R/C statuses and untracked files."""
    empty = {
        "ref": ref_commit,
        "added_files": [],
        "modified_files": [],
        "deleted_files": [],
        "renamed_files": {},
        "renamed_from_old": {},
        "deleted_or_moved_paths": [],
        "active_changed_files": [],
        "all_touched_paths": [],
    }
    git_root, sub_prefix = find_git_context(root)
    if git_root is None:
        return empty

    diff_cmd = ["diff", "--name-status", "-M", ref_commit]
    if sub_prefix:
        diff_cmd.extend(["--", sub_prefix.rstrip("/")])
    proc = _git_run(git_root, diff_cmd, timeout=5.0)
    if not proc or proc.returncode != 0:
        return empty

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: dict[str, str] = {}
    renamed_from_old: dict[str, str] = {}
    deleted_or_moved: list[str] = []
    active: list[str] = []
    all_touched: list[str] = []

    def _add_unique(lst: list[str], item: str) -> None:
        if item and item not in lst:
            lst.append(item)

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip().upper()
        code = status[0]
        if code in ("R", "C") and len(parts) >= 3:
            old_rel = _to_local_rel(parts[1], sub_prefix)
            new_rel = _to_local_rel(parts[2], sub_prefix)
            if old_rel:
                _add_unique(all_touched, old_rel)
            if new_rel:
                _add_unique(all_touched, new_rel)
            if code == "R":
                if new_rel and (root / new_rel).exists():
                    if old_rel:
                        renamed[new_rel] = old_rel
                        renamed_from_old[old_rel] = new_rel
                        if not (root / old_rel).exists():
                            _add_unique(deleted_or_moved, old_rel)
                    else:
                        _add_unique(added, new_rel)
                    _add_unique(active, new_rel)
                elif old_rel:
                    _add_unique(deleted, old_rel)
                    _add_unique(deleted_or_moved, old_rel)
            else:
                if new_rel and (root / new_rel).exists():
                    _add_unique(added, new_rel)
                    _add_unique(active, new_rel)
        else:
            rel = _to_local_rel(parts[1], sub_prefix)
            if not rel:
                continue
            _add_unique(all_touched, rel)
            if code == "D" or not (root / rel).exists():
                _add_unique(deleted, rel)
                _add_unique(deleted_or_moved, rel)
            elif code == "A":
                _add_unique(added, rel)
                _add_unique(active, rel)
            else:
                _add_unique(modified, rel)
                _add_unique(active, rel)

    if include_untracked:
        ls_cmd = ["ls-files", "--others", "--exclude-standard"]
        if sub_prefix:
            ls_cmd.extend(["--", sub_prefix.rstrip("/")])
        untracked_proc = _git_run(git_root, ls_cmd, timeout=3.0)
        if untracked_proc and untracked_proc.returncode == 0:
            for u_line in untracked_proc.stdout.splitlines():
                u_rel = _to_local_rel(u_line, sub_prefix)
                if u_rel and _is_auditable_code_path(u_rel) and (root / u_rel).exists():
                    _add_unique(added, u_rel)
                    _add_unique(active, u_rel)
                    _add_unique(all_touched, u_rel)

    return {
        "ref": ref_commit,
        "added_files": added,
        "modified_files": modified,
        "deleted_files": deleted,
        "renamed_files": renamed,
        "renamed_from_old": renamed_from_old,
        "deleted_or_moved_paths": deleted_or_moved,
        "active_changed_files": active,
        "all_touched_paths": all_touched,
    }


def read_blob_at_ref(root: Path, ref_commit: str, rel_path: str) -> str | None:
    """Read `<ref_commit>:<rel_path>` from git object database, returning None if absent."""
    git_root, sub_prefix = find_git_context(root)
    if git_root is None:
        return None
    norm = _clean_rel_path(rel_path)
    proc = _git_run(git_root, ["show", f"{ref_commit}:{sub_prefix}{norm}"], timeout=3.0)
    if proc and proc.returncode == 0:
        return proc.stdout
    return None


def load_parsed_snapshots_at_ref(
    root: Path,
    ref_commit: str,
    active_files: Iterable[str],
    renamed_files: dict[str, str],
    deleted_or_moved_paths: Iterable[str],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Parse pre-change file AST snapshots from `ref_commit` for modified, renamed, and deleted files."""
    blob_info_cache: dict[str, dict | None] = {}

    def _parse_at_ref(old_rel: str) -> dict | None:
        if old_rel in blob_info_cache:
            return blob_info_cache[old_rel]
        ext = Path(old_rel).suffix.lower()
        if ext not in SUPPORTED_CODE_EXTS:
            blob_info_cache[old_rel] = None
            return None
        raw = read_blob_at_ref(root, ref_commit, old_rel)
        if raw is None:
            blob_info_cache[old_rel] = None
            return None
        try:
            inf = parse(raw, ext)
        except Exception:
            inf = None
        blob_info_cache[old_rel] = inf
        return inf

    pre_base_infos: dict[str, dict] = {}
    for act_rel in active_files:
        src_old_rel = renamed_files.get(act_rel, act_rel)
        inf = _parse_at_ref(src_old_rel)
        if inf:
            pre_base_infos[act_rel] = inf

    deleted_base_infos: dict[str, dict] = {}
    for del_rel in deleted_or_moved_paths:
        inf = _parse_at_ref(del_rel)
        if inf:
            deleted_base_infos[del_rel] = inf

    return pre_base_infos, deleted_base_infos


def verify_deleted_or_renamed_file_consumers(
    brain: "ProjectBrain",
    deleted_or_moved_rels: set[str],
    renamed_from_old: dict[str, str] | None = None,
) -> tuple[list[str], list[dict]]:
    """Detect surviving indexed files that still import a file deleted or renamed in the diff."""
    code_deleted = {
        r for r in deleted_or_moved_rels
        if Path(r).suffix.lower() in SUPPORTED_CODE_EXTS
    }
    if not code_deleted:
        return [], []

    renamed_map = renamed_from_old or {}
    current_indexed = set(brain._all_indexed_files) if brain._all_indexed_files is not None else set(brain._index.keys())
    truly_missing = {r for r in code_deleted if r not in current_indexed and not (brain.root / r).exists()}
    if not truly_missing:
        return [], []

    augmented_files = current_indexed | truly_missing
    augmented_mod_index = resolve.build_python_module_index(augmented_files)

    diags: list[str] = []
    records: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Ensure full file index is accessible if brain was partially hydrated
    if getattr(brain, "_is_partial_index", False):
        brain._reset_partial_index()
        brain.load()

    for imp_rel in sorted(brain._index.keys()):
        if imp_rel in truly_missing:
            continue
        info = brain._index[imp_rel]
        raw_imports = info.get("imports") or []
        bindings = info.get("import_bindings") or {}
        if not raw_imports and not bindings:
            continue

        line_by_mod: dict[str, int] = {}
        sym_by_mod: dict[str, list[str]] = {}
        for _, b_val in bindings.items():
            b_mod = b_val.get("module") or ""
            b_qmod = b_val.get("qualified_module") or ""
            b_ln = b_val.get("line") or 1
            b_sym = b_val.get("symbol") or ""
            for m_key in (b_mod, b_qmod):
                if m_key:
                    line_by_mod.setdefault(m_key, b_ln)
                    if b_sym and b_sym != "*":
                        sym_by_mod.setdefault(m_key, []).append(b_sym)

        checked_mods: list[str] = list(raw_imports)
        for _, b_val in bindings.items():
            for m_cand in (b_val.get("qualified_module"), b_val.get("module")):
                if m_cand and m_cand not in checked_mods:
                    checked_mods.append(m_cand)

        for mod_str in checked_mods:
            resolved_target = None
            if imp_rel.endswith(".py") and mod_str not in raw_imports:
                resolved_target = resolve.resolve_python_import(
                    imp_rel, mod_str, augmented_files, augmented_mod_index,
                )
            if not resolved_target:
                resolved_target = resolve.resolve_import(
                    imp_rel, mod_str, augmented_files, augmented_mod_index,
                )
            if resolved_target and resolved_target in truly_missing:
                pair = (imp_rel, resolved_target)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                ln = line_by_mod.get(mod_str, 1)
                syms = sym_by_mod.get(mod_str, [])
                sym_part = f" (`{', '.join(syms[:4])}`)" if syms else ""
                new_dest = renamed_map.get(resolved_target)
                rename_hint = f" (renamed to `{new_dest}` in this diff)" if new_dest else " (deleted in this diff)"
                msg = (
                    f"- ⚠️ **[6D Deleted/Renamed File Import Violation]** `{imp_rel}:L{ln}` → "
                    f"Imports `{mod_str}`{sym_part} resolving to `{resolved_target}`{rename_hint}."
                )
                diags.append(msg)
                records.append({
                    "importer_file": imp_rel,
                    "line": ln,
                    "import_spec": mod_str,
                    "missing_file": resolved_target,
                    "renamed_to": new_dest,
                    "message": msg,
                })

    return diags, records


def check_governance_tamper(
    root: Path,
    ref_commit: str | None = None,
    *,
    from_git_index: bool = False,
) -> list[dict]:
    """Compare current `.projectbrain_memory.json` against `ref_commit` (or `HEAD`) to detect rule removal/weakening."""
    if find_git_context(root)[0] is None:
        return []
    target_ref = (ref_commit or "HEAD").strip()
    base_raw = read_blob_at_ref(root, target_ref, ".projectbrain_memory.json")
    if not base_raw:
        return []
    try:
        base_data = json.loads(base_raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(base_data, dict):
        return []

    base_rules: list[dict] = [
        r for r in (base_data.get("boundary_rules") or [])
        if isinstance(r, dict) and r.get("src_glob") and r.get("dst_glob")
    ]
    base_notes: list[dict] = [
        n for n in (base_data.get("notes") or [])
        if isinstance(n, dict) and n.get("target_id") and n.get("note")
    ]
    base_cochanges: list[dict] = [
        c for c in (base_data.get("co_change_rules") or [])
        if isinstance(c, dict) and c.get("file_a") and c.get("file_b")
    ]

    if not base_rules and not base_notes and not base_cochanges:
        return []

    curr_text: str | None = None
    if from_git_index:
        curr_text = read_blob_at_ref(root, "", ".projectbrain_memory.json")
        if curr_text is None:
            return [{
                "kind": "governance_file_deleted",
                "id": ".projectbrain_memory.json",
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] `.projectbrain_memory.json` was deleted in staged changes "
                    f"(removed {len(base_rules)} boundary rule(s), {len(base_notes)} invariant(s), "
                    f"and {len(base_cochanges)} co-change rule(s) present at `{target_ref[:12]}`; "
                    f"pass `--allow-governance-change` if intentional)."
                ),
            }]
    else:
        gov_path = root / ".projectbrain_memory.json"
        if not gov_path.exists():
            return [{
                "kind": "governance_file_deleted",
                "id": ".projectbrain_memory.json",
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] `.projectbrain_memory.json` was deleted "
                    f"(removed {len(base_rules)} boundary rule(s), {len(base_notes)} invariant(s), "
                    f"and {len(base_cochanges)} co-change rule(s) present at `{target_ref[:12]}`; "
                    f"pass `--allow-governance-change` if intentional)."
                ),
            }]
        try:
            curr_text = gov_path.read_text(encoding="utf-8")
        except OSError:
            curr_text = None

    if curr_text is None:
        return [{
            "kind": "governance_file_corrupted",
            "id": ".projectbrain_memory.json",
            "file": ".projectbrain_memory.json",
            "line": 1,
            "message": (
                "[GOVERNANCE TAMPER] `.projectbrain_memory.json` contains invalid JSON or could not be read "
                "(pass `--allow-governance-change` only after restoring valid governance format)."
            ),
        }]

    try:
        curr_data = json.loads(curr_text)
    except json.JSONDecodeError:
        return [{
            "kind": "governance_file_corrupted",
            "id": ".projectbrain_memory.json",
            "file": ".projectbrain_memory.json",
            "line": 1,
            "message": (
                "[GOVERNANCE TAMPER] `.projectbrain_memory.json` contains invalid JSON or could not be read "
                "(pass `--allow-governance-change` only after restoring valid governance format)."
            ),
        }]

    if not isinstance(curr_data, dict):
        return [{
            "kind": "governance_file_corrupted",
            "id": ".projectbrain_memory.json",
            "file": ".projectbrain_memory.json",
            "line": 1,
            "message": "[GOVERNANCE TAMPER] `.projectbrain_memory.json` root must be a JSON object.",
        }]

    curr_rules = [r for r in (curr_data.get("boundary_rules") or []) if isinstance(r, dict)]
    curr_notes = [n for n in (curr_data.get("notes") or []) if isinstance(n, dict)]
    curr_cochanges = [c for c in (curr_data.get("co_change_rules") or []) if isinstance(c, dict)]

    tampers: list[dict] = []

    # 1. Check `--forbid` boundary rules for removal or scope/edge-type weakening
    used_rule_idxs: set[int] = set()
    curr_rules_by_id: dict[str, tuple[int, dict]] = {
        str(r["id"]): (idx, r) for idx, r in enumerate(curr_rules) if r.get("id")
    }

    for br in base_rules:
        b_src = str(br.get("src_glob", "")).strip()
        b_dst = str(br.get("dst_glob", "")).strip()
        b_id = str(br.get("id") or f"{b_src} -> {b_dst}")
        b_lbl = str(br.get("rule") or f"{b_src} -> {b_dst}")
        b_edges = set(br.get("edge_types") or ("imports", "calls"))

        matched_r = None
        for idx, cr in enumerate(curr_rules):
            if idx in used_rule_idxs:
                continue
            if str(cr.get("src_glob", "")).strip() == b_src and str(cr.get("dst_glob", "")).strip() == b_dst:
                matched_r = cr
                used_rule_idxs.add(idx)
                break

        if matched_r is None:
            same_id_hit = curr_rules_by_id.get(b_id)
            if same_id_hit is not None and same_id_hit[0] not in used_rule_idxs:
                used_rule_idxs.add(same_id_hit[0])
                mutated_same_id = same_id_hit[1]
                new_lbl = f"{mutated_same_id.get('src_glob', '?')} -> {mutated_same_id.get('dst_glob', '?')}"
                tampers.append({
                    "kind": "boundary_rule_weakened",
                    "id": b_id,
                    "file": ".projectbrain_memory.json",
                    "line": 1,
                    "message": (
                        f"[GOVERNANCE TAMPER] Boundary rule `{b_lbl}` (`{b_id}`) was altered to `{new_lbl}` "
                        f"relative to `{target_ref[:12]}` (pass `--allow-governance-change` if intentional)."
                    ),
                })
            else:
                tampers.append({
                    "kind": "boundary_rule_removed",
                    "id": b_id,
                    "file": ".projectbrain_memory.json",
                    "line": 1,
                    "message": (
                        f"[GOVERNANCE TAMPER] Forbidden boundary rule `{b_lbl}` (`{b_id}`) was removed from "
                        f"`.projectbrain_memory.json` relative to `{target_ref[:12]}` "
                        f"(pass `--allow-governance-change` if intentional)."
                    ),
                })
            continue

        c_edges = set(matched_r.get("edge_types") or ()) if "edge_types" in matched_r else {"imports", "calls"}
        dropped_edges = sorted(b_edges - c_edges)
        if dropped_edges:
            tampers.append({
                "kind": "boundary_rule_weakened",
                "id": b_id,
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] Forbidden boundary rule `{b_lbl}` (`{b_id}`) weakened `edge_types` "
                    f"(dropped `{', '.join(dropped_edges)}`) relative to `{target_ref[:12]}` "
                    f"(pass `--allow-governance-change` if intentional)."
                ),
            })

    # 2. Check AST-anchored invariant notes for deletion, target/note tampering, or ast_hash erasure/forgery
    used_note_idxs: set[int] = set()
    curr_notes_by_id: dict[str, tuple[int, dict]] = {
        str(n["id"]): (idx, n) for idx, n in enumerate(curr_notes) if n.get("id")
    }
    file_ast_hashes_cache: dict[str, set[str] | None] = {}

    def _valid_symbol_hashes_for(rel_p: str) -> set[str] | None:
        if rel_p in file_ast_hashes_cache:
            return file_ast_hashes_cache[rel_p]
        ext = Path(rel_p).suffix.lower()
        if ext not in SUPPORTED_CODE_EXTS:
            file_ast_hashes_cache[rel_p] = None
            return None
        src_txt: str | None = None
        if from_git_index:
            src_txt = read_blob_at_ref(root, "", rel_p)
        if src_txt is None:
            fp = root / rel_p
            if fp.is_file():
                try:
                    src_txt = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    src_txt = None
        if src_txt is None:
            file_ast_hashes_cache[rel_p] = set()
            return set()
        try:
            inf = parse(src_txt, ext)
            hashes = (
                {str(fn["ast_hash"]) for fn in inf.get("functions", []) if fn.get("ast_hash")}
                | {str(cl["ast_hash"]) for cl in inf.get("classes", []) if cl.get("ast_hash")}
            )
        except Exception:
            hashes = None
        file_ast_hashes_cache[rel_p] = hashes
        return hashes

    for bn in base_notes:
        b_tid = str(bn.get("target_id", "")).strip()
        b_note = str(bn.get("note", "")).strip()
        b_id = str(bn.get("id") or b_tid)
        b_hash = str(bn.get("ast_hash") or "").strip()

        matched_n = None
        for idx, cn in enumerate(curr_notes):
            if idx in used_note_idxs:
                continue
            if str(cn.get("target_id", "")).strip() == b_tid and str(cn.get("note", "")).strip() == b_note:
                matched_n = cn
                used_note_idxs.add(idx)
                break

        if matched_n is None:
            same_id_hit = curr_notes_by_id.get(b_id)
            if same_id_hit is not None and same_id_hit[0] not in used_note_idxs:
                used_note_idxs.add(same_id_hit[0])
                tampers.append({
                    "kind": "invariant_modified",
                    "id": b_id,
                    "file": ".projectbrain_memory.json",
                    "line": 1,
                    "message": (
                        f"[GOVERNANCE TAMPER] Pinned AST invariant `{b_id}` on `{b_tid}` had its target or rule text "
                        f"modified relative to `{target_ref[:12]}` (pass `--allow-governance-change` if intentional)."
                    ),
                })
            else:
                tampers.append({
                    "kind": "invariant_removed",
                    "id": b_id,
                    "file": ".projectbrain_memory.json",
                    "line": 1,
                    "message": (
                        f"[GOVERNANCE TAMPER] Pinned AST invariant `{b_id}` on `{b_tid}` (\"{b_note[:80]}\") "
                        f"was deleted from `.projectbrain_memory.json` relative to `{target_ref[:12]}` "
                        f"(pass `--allow-governance-change` if intentional)."
                    ),
                })
            continue

        c_hash = str(matched_n.get("ast_hash") or "").strip()
        if b_hash and not c_hash:
            tampers.append({
                "kind": "invariant_hash_wiped",
                "id": b_id,
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] Pinned AST invariant `{b_id}` on `{b_tid}` had its `ast_hash` wiped, "
                    f"disabling staleness detection (pass `--allow-governance-change` if intentional)."
                ),
            })
        elif b_hash and c_hash != b_hash:
            note_rel = extract_note_rel_path(matched_n) or extract_note_rel_path(bn)
            if note_rel:
                valid_hashes = _valid_symbol_hashes_for(note_rel)
                if valid_hashes is not None and c_hash not in valid_hashes:
                    tampers.append({
                        "kind": "invariant_hash_forged",
                        "id": b_id,
                        "file": ".projectbrain_memory.json",
                        "line": 1,
                        "message": (
                            f"[GOVERNANCE TAMPER] Pinned AST invariant `{b_id}` on `{b_tid}` had its `ast_hash` "
                            f"altered to `{c_hash}` which does not match any symbol in `{note_rel}` "
                            f"(use `projectbrain memories --refresh` after verifying or pass `--allow-governance-change`)."
                        ),
                    })

    # 3. Check `co_change_rules` for deletion or weakening
    used_cc_idxs: set[int] = set()
    for bc in base_cochanges:
        fa = str(bc.get("file_a", "")).strip()
        fb = str(bc.get("file_b", "")).strip()
        b_bi = bool(bc.get("bidirectional", True))
        b_id = str(bc.get("id") or f"{fa} <-> {fb}")
        b_lbl = str(bc.get("rule") or f"{fa} {'<->' if b_bi else '->'} {fb}")

        matched_c = None
        for idx, cc in enumerate(curr_cochanges):
            if idx in used_cc_idxs:
                continue
            ca = str(cc.get("file_a", "")).strip()
            cb = str(cc.get("file_b", "")).strip()
            c_bi = bool(cc.get("bidirectional", True))
            if (ca == fa and cb == fb) or (b_bi and c_bi and ca == fb and cb == fa):
                matched_c = cc
                used_cc_idxs.add(idx)
                break

        if matched_c is None:
            tampers.append({
                "kind": "cochange_rule_removed",
                "id": b_id,
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] Co-change contract `{b_lbl}` (`{b_id}`) was removed from "
                    f"`.projectbrain_memory.json` relative to `{target_ref[:12]}` "
                    f"(pass `--allow-governance-change` if intentional)."
                ),
            })
        elif b_bi and not bool(matched_c.get("bidirectional", True)):
            tampers.append({
                "kind": "cochange_rule_weakened",
                "id": b_id,
                "file": ".projectbrain_memory.json",
                "line": 1,
                "message": (
                    f"[GOVERNANCE TAMPER] Bidirectional co-change contract `{b_lbl}` (`{b_id}`) was weakened "
                    f"to one-way relative to `{target_ref[:12]}` (pass `--allow-governance-change` if intentional)."
                ),
            })

    return tampers


def _is_test_or_noise_cochange_path(rel: str) -> bool:
    p = Path(rel)
    name_low = p.name.lower()
    if name_low.startswith(".projectbrain") or name_low in _LOCKFILE_NAMES:
        return True
    parts_low = {seg.lower() for seg in p.parts[:-1]}
    if parts_low & {"tests", "test", "testing", "__tests__", "spec", "specs", "fixtures", "benchmarks", "eval"}:
        return True
    stem_low = p.stem.lower()
    return (
        name_low.endswith(("_test.go", "_test.py", "_test.rs", "test.java", "tests.java"))
        or stem_low.endswith((".test", ".spec", "_test", "_spec"))
        or stem_low.startswith(("test_", "spec_"))
    )


def mine_ad_hoc_co_change_drift(
    root: Path,
    ref_commit: str,
    modified_rels: set[str],
    existing_files: set[str],
    covered_pairs: set[tuple[str, str]] | None = None,
    *,
    min_commits: int = 3,
    min_confidence: float = 0.75,
    max_per_trigger: int = 2,
    max_total_warnings: int = 5,
) -> list[dict]:
    """Mine commit history up to `ref_commit` to flag high-confidence co-change drift on `modified_rels`."""
    git_root, sub_prefix = find_git_context(root)
    if not modified_rels or git_root is None:
        return []
    log_cmd = ["log", "-n", "120", "--name-only", "--diff-filter=AMRC", "--pretty=format:__PB_COMMIT__", ref_commit]
    if sub_prefix:
        log_cmd.extend(["--", sub_prefix.rstrip("/")])
    proc = _git_run(git_root, log_cmd, timeout=3.5)
    if not proc or proc.returncode != 0 or not proc.stdout:
        return []

    commits: list[set[str]] = []
    cur: set[str] = set()
    for raw_ln in proc.stdout.splitlines():
        ln = raw_ln.strip()
        if ln == "__PB_COMMIT__":
            if cur:
                if 1 <= len(cur) <= 10:
                    commits.append(cur)
                cur = set()
            continue
        if ln:
            norm = _to_local_rel(ln, sub_prefix)
            if norm and not _is_test_or_noise_cochange_path(norm):
                cur.add(norm)
    if cur and 1 <= len(cur) <= 10:
        commits.append(cur)

    total_commits = len(commits)
    if total_commits < min_commits:
        return []

    file_freq: dict[str, int] = {}
    for c_set in commits:
        for f_rel in c_set:
            file_freq[f_rel] = file_freq.get(f_rel, 0) + 1

    already = set(covered_pairs or ())
    warnings: list[dict] = []
    target_mods = sorted(r for r in modified_rels if not _is_test_or_noise_cochange_path(r))

    for trig in target_mods:
        if len(warnings) >= max_total_warnings:
            break
        trig_commits = [c for c in commits if trig in c]
        n_trig = len(trig_commits)
        if n_trig < min_commits:
            continue
        partner_counts: dict[str, int] = {}
        for c_set in trig_commits:
            for other in c_set:
                if other != trig and other not in modified_rels:
                    partner_counts[other] = partner_counts.get(other, 0) + 1
        added_for_trig = 0
        for partner, cnt in sorted(partner_counts.items(), key=lambda x: (-x[1], x[0])):
            if cnt < min_commits:
                continue
            conf = cnt / n_trig
            if conf < min_confidence:
                continue
            if total_commits >= 6:
                partner_rate = file_freq.get(partner, cnt) / total_commits
                lift = conf / max(0.01, partner_rate)
                if lift < 1.25:
                    continue
            if (trig, partner) in already:
                continue
            if partner not in existing_files and not (root / partner).exists():
                continue
            already.add((trig, partner))
            pct = round(conf * 100, 1)
            warnings.append({
                "rule_id": "git_history_cochange",
                "rule": f"{trig} -> {partner}",
                "trigger_file": trig,
                "missing_partner": partner,
                "confidence": round(conf, 3),
                "co_change_pct": pct,
                "co_change_count": cnt,
                "mode": "advisory",
                "source": "git_history",
                "reason": f"Co-changed in {cnt}/{n_trig} ({pct}%) baseline commits prior to {ref_commit[:8]}",
                "message": (
                    f"[CO-CHANGE DRIFT] `{trig}` changed in this diff without `{partner}` "
                    f"(co-changed in {cnt}/{n_trig} [{pct}%] commits prior to `{ref_commit[:8]}`)."
                ),
            })
            added_for_trig += 1
            if added_for_trig >= max_per_trigger or len(warnings) >= max_total_warnings:
                break
    return warnings
