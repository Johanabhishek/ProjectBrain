import json
import math
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from . import frameworks, resolve
from .classify import classify_file_functions
from .graph import CodeGraph, fn_id, cls_id, file_id, route_id, compact_explore_result, normalize_ident_key
from .intent import classify_intent, LOCATE_SYMBOL, LOCATE_ROUTE, UNDERSTAND_FLOW, IMPACT_ANALYSIS, ADD_FEATURE
from .ledger import SavingsLedger
from .memory import RepoMemory
from .parsers import (
    parse, parse_with_tree, parse_files_parallel, apply_sql_alters_to_schema,
    compute_file_interface_hash, _is_minified_src, _is_unexpanded_symlink_src as _is_unexpanded_symlink,
)
from .session import SessionManager
from .store import (
    SQLiteIndexStore, LazyFileIndex, pack_symbol_imports, unpack_symbol_imports, compute_stats_summary,
)
from .slicer import (
    slice_lines,
    build_file_skeleton,
    build_monolith_component_map,
    extract_file_globals_slice,
    snap_to_block_boundaries,
    format_boundary_header,
    extract_emitted_intervals,
    extract_4d_coordinate_matrix,
    query_4d_coordinate_matrix,
)
from .versions import SCHEMA_VERSION, PARSER_VERSION, GRAPH_VERSION

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", ".turbo", "coverage",
    ".cache", ".parcel-cache", ".webpack", "out", ".output",
    "dist-deploy", ".vite-cache", "public", ".wrangler",
    ".firebase", ".pytest_cache",
}

SKIP_FILES = {
    "out.js", "out.cjs", "out.mjs", "out.min.js",
    "bundle.js", "bundle.cjs", "bundle.min.js",
    "index.min.js", "main.min.js",
    "vendor.js", "vendors.js", "chunk.js",
}

SKIP_SUFFIXES = {".min.js", ".min.cjs", ".min.mjs", ".min.ts"}

MINIFIED_LINE_THRESHOLD = 500

# Indexing and dossier/slice compilation index 100% of functions per file with no cap;
# MAX_FUNCTIONS_PER_FILE only bounds the legacy JSON summary card in `query()`.
MAX_FUNCTIONS_PER_FILE = int(os.environ.get("PROJECTBRAIN_MAX_FUNCTIONS", "200"))
MAX_SUBGRAPH_NODES = int(os.environ.get("PROJECTBRAIN_MAX_NODES", "120"))
MAX_SUBGRAPH_EDGES = int(os.environ.get("PROJECTBRAIN_MAX_EDGES", "200"))

_NODE_PRIORITY = {"file": 0, "route": 1, "cls": 2, "mod": 3, "fn": 4}
_STRUCTURAL_EDGES = {"handles", "inherits", "has_method", "exports"}

from .contracts import (
    _extract_balanced_call_args,
    contract_in_query_scope as _contract_in_query_scope_fn,
    compile_6d_contract_diagnostics as _compile_6d_contract_diagnostics_fn,
    verify_cross_file_contracts,
)
from .hooks import _file_diag_lines
from .verify import (
    verify_claim as _verify_claim_fn,
    support_evidence as _verify_support_evidence,
    verify_batch as _verify_batch_fn,
    verify_plan as _verify_plan_fn,
    impact as _verify_impact_fn,
    load_neighborhood_for_check as _load_neighborhood_for_check_fn,
    run_check as _verify_run_check,
    trace_flow as _verify_trace_flow,
    url_matches as _verify_url_matches,
    co_change_analysis as _verify_co_change_analysis,
)

def _compact_edge(edge: dict) -> dict:
    out = {k: edge[k] for k in ("src", "dst", "type")}
    if edge.get("confidence") != "exact":
        out["confidence"] = edge.get("confidence")
        if edge.get("reason"):
            out["reason"] = edge["reason"]
    return out


def _trim_subgraph(subgraph: dict) -> dict:
    rel_path_of = {n["id"]: n.get("rel_path") for n in subgraph["nodes"]}
    edges = [
        e for e in subgraph["edges"]
        if e["type"] in _STRUCTURAL_EDGES
        or rel_path_of.get(e["src"]) != rel_path_of.get(e["dst"])
    ]

    referenced = {e["src"] for e in edges} | {e["dst"] for e in edges}
    nodes = [
        n for n in subgraph["nodes"]
        if n.get("type") != "fn" or n["id"] in referenced
    ]

    kept_nodes = sorted(
        nodes, key=lambda n: (_NODE_PRIORITY.get(n.get("type"), 5), n["id"])
    )[:MAX_SUBGRAPH_NODES]
    kept_ids = {n["id"] for n in kept_nodes}
    kept_edges = [
        e for e in edges if e["src"] in kept_ids and e["dst"] in kept_ids
    ][:MAX_SUBGRAPH_EDGES]

    result = {
        "nodes": kept_nodes,
        "edges": [_compact_edge(e) for e in kept_edges],
    }
    omitted_nodes = len(subgraph["nodes"]) - len(kept_nodes)
    omitted_edges = len(edges) - len(kept_edges)
    if omitted_nodes or omitted_edges:
        result["truncated"] = {
            "nodes_omitted": omitted_nodes,
            "edges_omitted": omitted_edges,
            "hint": (
                "'defines' edges are omitted by design (see results[].functions). "
                "Use brain_explore or projectbrain_get_neighbors on a node ID for the full neighborhood."
            ),
        }
    return result


PARSED_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".cs", ".rb", ".php", ".swift",
    ".sql", ".graphql", ".gql", ".prisma", ".proto",
}
from .runner import (
    _extract_stack_trace_frames,
    compress_command_output as _runner_compress_command_output,
    run_command_compressed as _runner_run_command_compressed,
)

DEPRIORITY_DIRS = {
    "examples", "example", "demo", "demos",
    "scripts", "script",
    "clients", "client",
    "tests", "test", "testing",
    "docs", "doc", "documentation",
    "samples", "sample",
    "fixtures", "fixture",
    "eval", "evals",
    "benchmarks", "benchmark",
}

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "i", "my", "me", "we", "our",
    "in", "on", "at", "to", "for", "of", "and", "or", "but", "not",
    "why", "am", "getting", "get", "how", "what", "when", "where", "which",
    "this", "that", "it", "its", "with", "from", "by", "about", "into",
    "used", "uses", "using",
}


class ProjectBrain:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self._index: dict = {}
        self._blobs: dict[str, str] = {}
        self._token_sets: dict[str, set[str]] = {}
        self._body_token_sets: dict[str, set[str]] = {}
        self._idf: dict[str, float] = {}
        self._cache_path = self.root / ".projectbrain.json"
        self._db_store = SQLiteIndexStore(self.root / ".projectbrain.db")
        self._store = self._db_store
        self._graph: CodeGraph | None = None
        self._graph_path = self.root / ".projectbrain_graph.json"
        self._defn_sets: dict[str, set[str]] = {}
        self._importer_counts: dict[str, int] = {}
        self._sizes: dict[str, int] = {}
        self._import_targets: dict[str, list[str]] = {}
        self._symbol_imports: dict[str, dict[str, str]] = {}
        self._imported_by: dict[str, list[str]] = {}
        self._skipped: dict[str, str] = {}
        self._behaviors: dict[str, dict[str, str]] = {}
        self._ast_hashes: dict[str, str] = {}
        self._z_write_sets: dict[str, set[str]] = {}
        self._trans_effect_sets: dict[str, set[str]] = {}
        self._cell_4d_tokens: dict[str, set[str]] = {}
        self._session_manager = SessionManager()
        self._memory = RepoMemory(self.root)
        self._ledger = SavingsLedger(self.root)
        # Auto-session tracking: remembers which symbol slices & file skeletons
        # have already been served to the agent in this process lifetime so we
        # never waste tokens re-sending identical code slices.
        self._served_slices: set[str] = set()
        self._served_skeletons: set[str] = set()
        self._source_cache: dict[str, str] = {}
        self._pre_edit_index: dict[str, dict] = {}
        self._git_head_cache: dict[str, dict | None] = {}
        self._last_line_annotations: dict[str, dict[int, list[str]]] = {}
        self._last_pin_lines: dict[str, set[int]] = {}
        self._is_partial_index: bool = False
        self._all_indexed_files: set[str] | None = None
        self._blobs_ready: bool = False
        self._pending_parsed_files: dict[str, tuple] = {}
        self._op_lock = threading.RLock()

    def _reset_partial_index(self) -> None:
        if getattr(self, "_is_partial_index", False):
            self._index = {}
            self._graph = None
            self._is_partial_index = False
            self._all_indexed_files = None
            self._blobs_ready = False

    def _get_file_info(self, rel_path: str) -> dict | None:
        if rel_path in self._index:
            return self._index[rel_path]
        if getattr(self, "_is_partial_index", False) and self._db_store.db_path.exists():
            f_hit = self._db_store.get_file_fast(rel_path)
            if f_hit and f_hit.get("info") is not None:
                inf = {**f_hit["info"], "mtime": f_hit.get("mtime")}
                self._index[rel_path] = inf
                if "_resolved_sym_map" in inf:
                    self._symbol_imports.setdefault(rel_path, inf["_resolved_sym_map"])
                if "_resolved_targets" in inf:
                    self._import_targets.setdefault(rel_path, inf["_resolved_targets"])
                return inf
        return None

    def _get_git_head_info(self, rel_path: str) -> dict | None:
        """Retrieve and parse `HEAD:<rel_path>` from Git when available and different from disk."""
        if rel_path in self._git_head_cache:
            return self._git_head_cache[rel_path]
        if not (self.root / ".git").exists():
            self._git_head_cache[rel_path] = None
            return None
        try:
            proc = subprocess.run(
                ["git", "show", f"HEAD:{rel_path}"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
            )
            if proc.returncode != 0 or not proc.stdout:
                self._git_head_cache[rel_path] = None
                return None
            cur_txt = self._get_file_text(rel_path)
            if cur_txt is not None and proc.stdout == cur_txt:
                self._git_head_cache[rel_path] = None
                return None
            inf = parse(proc.stdout, Path(rel_path).suffix.lower())
            self._git_head_cache[rel_path] = inf
            return inf
        except Exception:
            self._git_head_cache[rel_path] = None
            return None

    def _capture_pre_edit_snapshots(self, candidate_files: list[str] | None = None) -> None:
        """Before `_ensure_fresh()` overwrites `self._index`, preserve pre-edit file AST info for modified files."""
        if not self._index:
            if candidate_files and self._db_store.db_path.exists():
                for f in candidate_files:
                    r = self._normalize_rel(str(f))
                    if not r or r in self._pre_edit_index:
                        continue
                    f_hit = self._db_store.get_file_fast(r)
                    if not f_hit or not f_hit.get("info"):
                        continue
                    fpath = self.root / r
                    try:
                        st = fpath.stat()
                        if f_hit.get("mtime") is None or st.st_mtime != f_hit.get("mtime"):
                            self._pre_edit_index[r] = dict(f_hit["info"])
                    except OSError:
                        self._pre_edit_index[r] = dict(f_hit["info"])
            return
        rels_to_check: list[str] = []
        if candidate_files:
            for f in candidate_files:
                r = self._normalize_rel(str(f))
                if r in self._index:
                    rels_to_check.append(r)
        else:
            rels_to_check = list(self._index.keys())
        for r in rels_to_check:
            if r in self._pre_edit_index:
                continue
            old_inf = self._index.get(r)
            if not old_inf:
                continue
            fpath = self.root / r
            try:
                st = fpath.stat()
                if st.st_mtime != old_inf.get("mtime") or st.st_size != self._sizes.get(r):
                    self._pre_edit_index[r] = dict(old_inf)
            except OSError:
                self._pre_edit_index[r] = dict(old_inf)

    def _get_file_text(self, rel_path: str) -> str | None:
        if rel_path in self._source_cache:
            return self._source_cache[rel_path]
        fpath = self.root / rel_path
        if fpath.exists():
            try:
                txt = fpath.read_text(encoding="utf-8", errors="ignore")
                self._source_cache[rel_path] = txt
                return txt
            except OSError:
                pass
        blob_txt = self._db_store.get_source_blob(rel_path)
        if blob_txt is not None:
            self._source_cache[rel_path] = blob_txt
            return blob_txt
        return None
    # ── v2 Power Tools: Context Compiler, Surgical Slicer, Explorer, Check, Memory ──

    def compile_context(
        self,
        task: str,
        budget_tokens: int = 3000,
        top_n: int = 4,
        deduplicate_session: bool = True,
    ) -> dict:
        """Compile a surgical, token-budgeted Evidence Packet for an agent task."""
        from .dossier import compile_evidence_packet
        return compile_evidence_packet(
            self,
            task,
            budget_tokens=budget_tokens,
            top_n=top_n,
            deduplicate_session=deduplicate_session,
        )

    def compile_dossier(
        self,
        tasks: list[str] | str,
        impact_files: list[str] | None = None,
        out_path: str | None = None,
        zoom: bool = False,
        write_file: bool = True,
    ) -> dict:
        """Compile a self-contained, zero-follow-up Engineering Dossier across one or more tasks."""
        from .dossier import compile_engineering_dossier
        return compile_engineering_dossier(
            self,
            tasks,
            impact_files=impact_files,
            out_path=out_path,
            zoom=zoom,
            write_file=write_file,
        )

    def patch(
        self,
        spec_text: str,
        apply: bool = False,
        diff_out: str | None = None,
        fuzzy_window: int = 8,
    ) -> dict:
        """Apply or dry-run a Surgical Anchor Patch spec and return unified diff + syntax verification."""
        from .patcher import apply_patch_spec
        diff_path = Path(diff_out) if diff_out else (self.root / ".projectbrain_patch.diff")
        res = apply_patch_spec(
            self.root,
            spec_text,
            apply=apply,
            diff_out_path=diff_path,
            fuzzy_window=fuzzy_window,
        )
        if res.get("total_ops_applied", 0) > 0:
            self._ledger.record_patch_op(
                files_patched=res.get("files_patched", 0),
                ops_applied=res.get("total_ops_applied", 0),
                mode="applied" if apply else "dry_run",
                all_syntax_valid=bool(res.get("all_syntax_valid", False)),
            )
        if res.get("all_syntax_valid"):
            self._ensure_fresh()
            patched_rels: set[str] = set()
            saved_cells: dict[str, list[dict]] = {}
            patched_texts: dict[str, str] = {}
            pre_patch_infos: dict[str, dict] = {}
            for f_item in res.get("files", []):
                f_rel = self._normalize_rel(f_item.get("file", ""))
                p_txt = f_item.pop("_patched_text", None)
                if f_rel:
                    patched_rels.add(f_rel)
                    if f_rel in self._index:
                        pre_patch_infos[f_rel] = dict(self._index[f_rel])
                    if p_txt is not None:
                        patched_texts[f_rel] = p_txt
                    if apply:
                        self._source_cache.pop(f_rel, None)
                        self._ledger.invalidate_file(f_rel)
                        self._served_skeletons.discard(f_rel)
                    elif p_txt is not None and f_rel in self._index:
                        saved_cells[f_rel] = self._index[f_rel].get("cells_4d", [])
                        self._index[f_rel]["cells_4d"] = extract_4d_coordinate_matrix(
                            p_txt.splitlines(), ext=Path(f_rel).suffix.lower()
                        )
            try:
                if apply:
                    self.incremental_index()
                diag_lines, _ = self._compile_6d_contract_diagnostics(
                    "patch verification", patched_rels, strict_focus=True
                )
                cross_diags = verify_cross_file_contracts(
                    self,
                    patched_rels,
                    patched_texts=patched_texts,
                    pre_edit_infos=pre_patch_infos,
                )
                for cd in cross_diags:
                    if cd not in diag_lines:
                        diag_lines.append(cd)
            finally:
                for f_rel, orig_c in saved_cells.items():
                    if f_rel in self._index:
                        self._index[f_rel]["cells_4d"] = orig_c
            res["contract_diagnostics"] = diag_lines
            res["all_contracts_valid"] = len(diag_lines) == 0
        else:
            for f_item in res.get("files", []):
                f_item.pop("_patched_text", None)
            res["contract_diagnostics"] = []
            res["all_contracts_valid"] = False
        return res

    def _is_db_fresh_fast(self, allow_zero_disk: bool = False) -> bool:
        """Verify in ~25ms that `.projectbrain.db` is version-compatible and matches disk mtimes without hydrating."""
        if (
            not self._db_store.db_path.exists()
            or self._cache_path.exists()
            or not self._db_store.is_compatible_meta(SCHEMA_VERSION, PARSER_VERSION, GRAPH_VERSION)
        ):
            return False
        snap = self._db_store.get_indexed_mtimes_fast()
        if snap is None:
            return False
        db_mtimes, db_skipped = snap
        current = {rel: mtime for rel, _, mtime in self._walk_entries()}
        if not current and db_mtimes:
            return allow_zero_disk
        if current.keys() != (set(db_mtimes.keys()) | db_skipped):
            return False
        return not any(current[rel] != db_mtimes[rel] for rel in db_mtimes if rel in current)

    def slice_symbols(
        self,
        targets: list[str] | str,
        max_lines_per_symbol: int = 180,
        summary_mode: bool = False,
        focus: list[str] | str | None = None,
    ) -> dict:
        """Surgically slice specific symbols or files without reading full files.

        Each target in `targets` can be:
          - A graph node ID: `"fn:src/brain.py:ProjectBrain.query"` or `"cls:src/graph.py:CodeGraph"`
          - A `file:symbol` shorthand: `"brain.py:verify_claim"`
          - A bare symbol name: `"verify_claim"` or `"CodeGraph.build"`
          - A file range: `"src/brain.py:100-150"`
          - A file path (returns its signature skeleton): `"src/brain.py"`

        Optional `focus`: variable or call identifier(s) for intra-function PDG slicing
        (traces backward definitions, forward uses, and enclosing control guards).
        """
        if isinstance(targets, (str, Path)):
            targets = [str(targets)]
        if isinstance(focus, str):
            focus_terms: list[str] | None = [s.strip() for s in re.split(r"[,;\s]+", focus) if s.strip()] or None
        elif isinstance(focus, list):
            focus_terms = [str(s).strip() for s in focus if str(s).strip()] or None
        else:
            focus_terms = None
        pdg_max_lines = min(max_lines_per_symbol, 45) if (focus_terms and max_lines_per_symbol == 180) else max_lines_per_symbol

        def _maybe_prepend_monolith_map(rel_p: str, ls: int, le: int, raw_code: str) -> str:
            if focus_terms or summary_mode or (le - ls) < 220:
                return raw_code
            try:
                f_lines = (self.root / rel_p).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return raw_code
            cmap = build_monolith_component_map(f_lines, ls, le, rel_path=rel_p)
            return f"{cmap}\n{raw_code}" if cmap else raw_code

        # Fast cold-start SQLite path: slice directly from `.projectbrain.db` in <5ms
        # without loading the entire repository index/graph into memory.
        if (
            not self._index
            and self._db_store.db_path.exists()
            and not self._cache_path.exists()
            and self._db_store.is_compatible_meta(SCHEMA_VERSION, PARSER_VERSION, GRAPH_VERSION)
        ):
            fast_slices: list[dict] = []
            fast_ok = True
            db_fresh_checked: bool | None = None

            def _emit_fast_missing(raw_t: str, sym_q: str, f_filt: str | None = None) -> bool:
                nonlocal db_fresh_checked
                if db_fresh_checked is None:
                    db_fresh_checked = self._is_db_fresh_fast(allow_zero_disk=False)
                if not db_fresh_checked:
                    return False
                did_you_mean, pkg_matches = self._db_store.suggest_missing_target_fast(raw_t, sym_q, file_filter=f_filt)
                hint = f" Did you mean: {', '.join(did_you_mean)}?" if did_you_mean else (
                    f" Closest indexed files: {', '.join(pkg_matches)}" if pkg_matches else ""
                )
                fast_slices.append({
                    "target": raw_t,
                    "error": f"Symbol or file '{raw_t}' not found in index.{hint}",
                    "did_you_mean": did_you_mean,
                })
                return True

            for raw_target in targets:
                t = raw_target.strip()
                if not t:
                    continue
                range_m = re.match(r"^(.+?):(\d+)-(\d+)$", t)
                if range_m:
                    fpath, s_line, e_line = range_m.group(1), int(range_m.group(2)), int(range_m.group(3))
                    f_hit = self._db_store.get_file_fast(self._normalize_rel(fpath) or fpath, include_info=False)
                    if not f_hit:
                        if not _emit_fast_missing(t, t):
                            fast_ok = False
                            break
                        continue
                    rel = f_hit["rel_path"]
                    disk_p = self.root / rel
                    if not disk_p.exists() or disk_p.stat().st_mtime != f_hit["mtime"]:
                        fast_ok = False
                        break
                    f_lines = disk_p.read_text(encoding="utf-8", errors="replace").splitlines()
                    s_line, e_line = snap_to_block_boundaries(f_lines, s_line, e_line)
                    span_len = pdg_max_lines if focus_terms else (
                        max_lines_per_symbol if summary_mode else max(max_lines_per_symbol, min(400, e_line - s_line + 1))
                    )
                    code = slice_lines(self.root, rel, s_line, e_line, max_lines=span_len, focus_terms=focus_terms)
                    self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
                    fast_slices.append({
                        "target": t,
                        "rel_path": rel,
                        "symbol": f"L{s_line}-{e_line}",
                        "name": f"L{s_line}-{e_line}",
                        "lines": f"L{s_line}-{e_line}",
                        "line_start": s_line,
                        "line_end": e_line,
                        "code": code,
                    })
                    continue

                if t.startswith("file:"):
                    f_hit = self._db_store.get_file_fast(t[5:])
                    if not f_hit:
                        if not _emit_fast_missing(t, t[5:]):
                            fast_ok = False
                            break
                        continue
                    rel = f_hit["rel_path"]
                    skel = build_file_skeleton(rel, f_hit["info"], root=self.root)
                    fast_slices.append({"node_id": t, "rel_path": rel, "symbol": rel, "name": rel, "skeleton": skel, "code": skel})
                    continue

                rel_try = self._normalize_rel(t)
                f_direct = self._db_store.get_file_fast(rel_try) if ("/" in t or "." in t) and ":" not in t else None
                if f_direct and f_direct["rel_path"] == rel_try:
                    disk_p = self.root / rel_try
                    if not disk_p.exists() or disk_p.stat().st_mtime != f_direct["mtime"]:
                        fast_ok = False
                        break
                    skel = build_file_skeleton(rel_try, f_direct["info"], root=self.root)
                    self._served_skeletons.add(rel_try)
                    fast_slices.append({"target": t, "rel_path": rel_try, "symbol": rel_try, "name": rel_try, "skeleton": skel, "code": skel})
                    continue

                file_filter = None
                sym_query = t
                if ":" in t and not t.startswith(("fn:", "cls:", "file:", "route:", "mod:")):
                    parts = t.rsplit(":", 1)
                    file_filter = parts[0].lower().replace("\\", "/")
                    sym_query = parts[1]

                sym_hits = self._db_store.find_symbols_fast(sym_query, file_filter=file_filter)
                if not sym_hits:
                    if not _emit_fast_missing(t, sym_query, file_filter):
                        fast_ok = False
                        break
                    continue
                for sh in sym_hits:
                    rel = sh["rel_path"]
                    disk_p = self.root / rel
                    if not disk_p.exists() or (sh["mtime"] is not None and disk_p.stat().st_mtime != sh["mtime"]):
                        fast_ok = False
                        break
                    l_start, l_end = sh["line_start"], sh["line_end"]
                    code = slice_lines(self.root, rel, l_start, l_end, max_lines=pdg_max_lines, focus_terms=focus_terms)
                    self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
                    code = _maybe_prepend_monolith_map(rel, l_start, l_end, code)
                    self._served_slices.add(sh["node_id"])
                    fast_slices.append({
                        "node_id": sh["node_id"],
                        "rel_path": rel,
                        "symbol": sh["qname"],
                        "name": sh["qname"],
                        "signature": sh["signature"],
                        "lines": f"L{l_start}-{l_end}",
                        "line_start": l_start,
                        "line_end": l_end,
                        "code": code,
                    })
                if not fast_ok:
                    break

            if fast_ok and fast_slices:
                total_slice_chars = sum(len(s.get("code") or s.get("skeleton") or "") for s in fast_slices)
                if total_slice_chars > 0:
                    s_files = {s.get("rel_path") for s in fast_slices if s.get("rel_path")}
                    self._ledger.record_input_op(
                        op="slice",
                        query=", ".join(targets)[:120],
                        files_covered=len(s_files),
                        touched_files=s_files,
                        slices_included=len(fast_slices),
                    )
                return {"slices": fast_slices, "count": len(fast_slices)}

        self._ensure_fresh()
        slices: list[dict] = []

        for raw_target in targets:
            t = raw_target.strip()
            if not t:
                continue

            # 1. File line range `path/to/file.py:10-40`
            range_match = re.match(r"^(.+?):(\d+)-(\d+)$", t)
            if range_match:
                fpath, s_line, e_line = range_match.group(1), int(range_match.group(2)), int(range_match.group(3))
                rel = self._normalize_rel(fpath)
                if rel not in self._index:
                    fp_low = fpath.replace("\\", "/").strip("/").lower()
                    suf_cands = [k for k in self._index if k.lower() == fp_low or k.lower().endswith("/" + fp_low)]
                    if suf_cands:
                        rel = min(suf_cands, key=lambda k: (len(k.split("/")), len(k)))
                if rel in self._index:
                    try:
                        f_lines = (self.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        f_lines = []
                    if f_lines and not focus_terms:
                        s_line, e_line = snap_to_block_boundaries(f_lines, s_line, e_line)
                        plan = self._ledger.plan_slice_intervals(rel, s_line, e_line, len(f_lines), commit=False)
                        unseen = plan["unseen_intervals"]
                        skipped = plan["skipped_intervals"]
                        b_hdr = format_boundary_header(f_lines, s_line, e_line)
                        if not unseen:
                            code = (
                                f"{b_hdr}\n" if b_hdr else ""
                            ) + f"# [Zero-Waste Guard] L{s_line}-L{e_line} of {rel} was already delivered in this session (file unchanged — 0 duplicate lines emitted)."
                        else:
                            parts_out: list[str] = []
                            if b_hdr:
                                parts_out.append(b_hdr)
                            if skipped and not plan["auto_completed_file"]:
                                sk_str = ", ".join(f"L{ks}-L{ke}" for ks, ke in skipped)
                                un_str = ", ".join(f"L{us}-L{ue}" for us, ue in unseen)
                                parts_out.append(
                                    f"# [Zero-Waste Guard] Trimmed already-read ({sk_str}); emitting only unseen ({un_str}):"
                                )
                            elif plan["auto_completed_file"] and not summary_mode:
                                un_str = ", ".join(f"L{us}-L{ue}" for us, ue in unseen)
                                parts_out.append(
                                    f"# [Small-File Remainder Guard] Emitting all remaining unread lines ({un_str}) of {rel} ({len(f_lines)} lines total) in one pass:"
                                )
                            span_req = e_line - s_line + 1
                            target_cap = max_lines_per_symbol if (summary_mode and max_lines_per_symbol < 110) else max(max_lines_per_symbol, min(400, span_req))
                            emitted_ivs: list[tuple[int, int]] = []
                            for u_s, u_e in unseen:
                                u_len = min(target_cap, u_e - u_s + 1)
                                chunk_code = slice_lines(self.root, rel, u_s, u_e, max_lines=u_len)
                                parts_out.append(chunk_code)
                                emitted_ivs.extend(extract_emitted_intervals(chunk_code))
                            self._ledger.commit_slice_intervals(rel, emitted_ivs)
                            code = "\n".join(parts_out)
                    else:
                        span_len = pdg_max_lines if focus_terms else (
                            max_lines_per_symbol if summary_mode else max(max_lines_per_symbol, min(400, e_line - s_line + 1))
                        )
                        code = slice_lines(self.root, rel, s_line, e_line, max_lines=span_len, focus_terms=focus_terms)
                    slices.append({
                        "target": t,
                        "rel_path": rel,
                        "symbol": f"L{s_line}-{e_line}",
                        "name": f"L{s_line}-{e_line}",
                        "lines": f"L{s_line}-{e_line}",
                        "line_start": s_line,
                        "line_end": e_line,
                        "code": code,
                    })
                    continue
            # 2. Exact node_id in graph
            if self._graph and t in self._graph.nodes:
                nd = self._graph.nodes[t]
                rel = nd.get("rel_path", "")
                if nd.get("type") in ("fn", "cls") and rel:
                    l_start = nd.get("line_start", 1)
                    l_end = nd.get("line_end", l_start)
                    code = slice_lines(self.root, rel, l_start, l_end, max_lines=pdg_max_lines, focus_terms=focus_terms)
                    self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
                    code = _maybe_prepend_monolith_map(rel, l_start, l_end, code)
                    self._served_slices.add(t)
                    slices.append({
                        "node_id": t,
                        "rel_path": rel,
                        "symbol": nd.get("name"),
                        "name": nd.get("name"),
                        "signature": nd.get("signature", ""),
                        "lines": f"L{l_start}-{l_end}",
                        "line_start": l_start,
                        "line_end": l_end,
                        "code": code,
                    })
                    continue
                elif nd.get("type") == "file" and rel in self._index:
                    skel = build_file_skeleton(rel, self._index[rel], behaviors=self._behaviors.get(rel, {}), root=self.root)
                    slices.append({"node_id": t, "rel_path": rel, "symbol": rel, "name": rel, "skeleton": skel, "code": skel})
                    continue

            # 3. Direct file path -> return file skeleton
            rel_try = self._normalize_rel(t)
            if rel_try in self._index:
                skel = build_file_skeleton(rel_try, self._index[rel_try], behaviors=self._behaviors.get(rel_try, {}), root=self.root)
                self._served_skeletons.add(rel_try)
                slices.append({"target": t, "rel_path": rel_try, "symbol": rel_try, "name": rel_try, "skeleton": skel, "code": skel})
                continue

            # 4. `file_fragment:symbol_name` or bare `symbol_name`
            file_filter = None
            sym_query = t
            if ":" in t and not t.startswith(("fn:", "cls:", "file:", "route:", "mod:")):
                parts = t.rsplit(":", 1)
                file_filter = parts[0].lower().replace("\\", "/")
                sym_query = parts[1]

            sym_lower = sym_query.lower()
            sym_norm = normalize_ident_key(sym_query)
            found_any = False
            for rel, info in self._index.items():
                if file_filter and file_filter not in rel.lower():
                    continue
                for fn in info.get("functions", []):
                    qname = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                    if (
                        sym_lower in (fn["name"].lower(), qname.lower())
                        or (len(sym_norm) >= 4 and sym_norm in (normalize_ident_key(fn["name"]), normalize_ident_key(qname)))
                    ):
                        nid = fn_id(rel, fn["name"], fn.get("parent_class"))
                        l_start, l_end = fn.get("line_start", 1), fn.get("line_end", 1)
                        code = slice_lines(self.root, rel, l_start, l_end, max_lines=pdg_max_lines, focus_terms=focus_terms)
                        self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
                        code = _maybe_prepend_monolith_map(rel, l_start, l_end, code)
                        self._served_slices.add(nid)
                        slices.append({
                            "node_id": nid,
                            "rel_path": rel,
                            "symbol": qname,
                            "name": qname,
                            "signature": fn.get("signature", ""),
                            "lines": f"L{l_start}-{l_end}",
                            "line_start": l_start,
                            "line_end": l_end,
                            "code": code,
                        })
                        found_any = True
                for cls in info.get("classes", []):
                    if sym_lower == cls["name"].lower():
                        nid = cls_id(rel, cls["name"])
                        l_start, l_end = cls.get("line_start", 1), cls.get("line_end", 1)
                        code = slice_lines(self.root, rel, l_start, l_end, max_lines=pdg_max_lines, focus_terms=focus_terms)
                        self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
                        self._served_slices.add(nid)
                        slices.append({
                            "node_id": nid,
                            "rel_path": rel,
                            "symbol": cls["name"],
                            "name": cls["name"],
                            "signature": cls["name"],
                            "lines": f"L{l_start}-{l_end}",
                            "line_start": l_start,
                            "line_end": l_end,
                            "code": code,
                        })
                        found_any = True
            if not found_any:
                import difflib
                q_clean = re.sub(r":\d+-\d+$", "", t).replace("\\", "/").strip("/")
                q_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", q_clean.split(":")[0]).lower()
                pkg_matches = sorted(
                    k for k in self._index
                    if q_stem and (q_stem in k.lower() or any(tok in k.lower() for tok in q_stem.split("/")[-2:] if len(tok) >= 4))
                )[:6]
                sym_candidates: list[str] = []
                sym_map_display: dict[str, str] = {}
                for cand_rel, cand_info in self._index.items():
                    if file_filter and file_filter not in cand_rel.lower():
                        continue
                    for fn in cand_info.get("functions", []):
                        qn = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                        sym_candidates.extend([fn["name"], qn])
                        sym_map_display.setdefault(fn["name"].lower(), f"{cand_rel}:{qn}")
                        sym_map_display.setdefault(qn.lower(), f"{cand_rel}:{qn}")
                    for cls in cand_info.get("classes", []):
                        sym_candidates.append(cls["name"])
                        sym_map_display.setdefault(cls["name"].lower(), f"{cand_rel}:{cls['name']}")
                close_raw = difflib.get_close_matches(sym_query, list(dict.fromkeys(sym_candidates)), n=5, cutoff=0.5)
                if not close_raw and "." in sym_query:
                    bare_m = sym_query.rsplit(".", 1)[-1]
                    close_raw = difflib.get_close_matches(bare_m, list(dict.fromkeys(sym_candidates)), n=5, cutoff=0.5)
                did_you_mean = list(dict.fromkeys(sym_map_display.get(c.lower(), c) for c in close_raw))[:5]
                hint = f" Did you mean: {', '.join(did_you_mean)}?" if did_you_mean else (
                    f" Closest indexed files: {', '.join(pkg_matches)}" if pkg_matches else ""
                )
                slices.append({
                    "target": t,
                    "error": f"Symbol or file '{t}' not found in index.{hint}",
                    "did_you_mean": did_you_mean,
                })
        total_slice_chars = sum(len(s.get("code") or s.get("skeleton") or "") for s in slices)
        if total_slice_chars > 0:
            slice_files = {s.get("rel_path") for s in slices if s.get("rel_path")}
            self._ledger.record_input_op(
                op="slice",
                query=", ".join(targets)[:120],
                files_covered=len(slice_files),
                touched_files=slice_files,
                slices_included=len(slices),
            )
        return {"slices": slices, "count": len(slices)}

    def explore(
        self,
        target: str,
        target_b: str | None = None,
        edge_types: list[str] | None = None,
        depth: int = 2,
        include_co_changes: bool = False,
    ) -> dict:
        """Unified graph explorer: neighbors, shortest relationship path, or node search.

        - If both `target` and `target_b` are provided: finds the shortest typed
          path connecting `target` and `target_b` (`find_relationship`), or traces
          the request flow if `target` is a URL path.
        - If `target` is a URL path (starts with `/`): runs `trace_flow`.
        - Otherwise: resolves `target` to its graph node(s) and returns its
          multi-hop neighborhood (`neighbors`) plus optional git co-changes.
        """
        target = target.strip()
        is_route_target = target.startswith("/") or bool(
            re.match(r"^(?:GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD|ANY)\s+/", target, re.IGNORECASE)
        )
        if not self._index and not is_route_target and self._is_db_fresh_fast(allow_zero_disk=True):
            fast_res = self._db_store.explore_fast(
                target, target_b=target_b, edge_types=edge_types, depth=depth,
            )
            if fast_res is not None:
                if include_co_changes and not target_b and "center" in fast_res:
                    rel_path = fast_res["center"].get("rel_path")
                    if rel_path:
                        fast_res["co_changes"] = self.co_change_analysis(rel_path).get("co_changes", [])[:10]
                return fast_res

        self._ensure_fresh()

        if target_b:
            return compact_explore_result(self.find_relationship(target, target_b.strip()))

        if is_route_target:
            return self.trace_flow(target)

        if not self._graph:
            return {"error": "Graph not available"}

        node_id = target if target in self._graph.nodes else None
        if not node_id:
            matches = self._graph.find_nodes_by_name(target)
            if not matches:
                import difflib
                t_norm = normalize_ident_key(target)
                norm_map: dict[str, str] = {}
                for nid, nd in self._graph.nodes.items():
                    nm = nd.get("name") or ""
                    if nm and nd.get("type") in ("fn", "cls"):
                        nk = normalize_ident_key(nm)
                        if nk:
                            norm_map.setdefault(nk, f"{nd.get('rel_path', '')}:{nm}".lstrip(":"))
                close_keys = difflib.get_close_matches(t_norm, list(norm_map.keys()), n=5, cutoff=0.5) if t_norm else []
                did_you_mean = [norm_map[ck] for ck in close_keys]
                q_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", target.replace("\\", "/").split(":")[0]).lower()
                closest_files = sorted(k for k in self._index if q_stem and q_stem in k.lower())[:5]
                miss_res: dict = {"error": f"No graph node matching '{target}'", "nodes": [], "edges": []}
                if did_you_mean:
                    miss_res["did_you_mean"] = did_you_mean
                if closest_files:
                    miss_res["closest_files"] = closest_files
                return miss_res
            node_id = matches[0]

        raw_res = self.get_neighbors(node_id, edge_types=edge_types, depth=depth)
        res = compact_explore_result(raw_res)
        rel_path = self._graph.nodes.get(node_id, {}).get("rel_path")
        if rel_path:
            f_sz = self._sizes.get(rel_path)
            if f_sz is not None and f_sz <= 12_000:
                est_lines = max(1, int(f_sz) // 34)
                res["direct_read_hint"] = (
                    f"'{rel_path}' is small (~{est_lines} lines, {int(f_sz)} bytes) — "
                    f"read_file('{rel_path}') reads the full source directly in 1 turn."
                )
            if include_co_changes:
                res["co_changes"] = self.co_change_analysis(rel_path).get("co_changes", [])[:10]
        return res

    def check(
        self,
        files_modified: list[str] | None = None,
        claims: list[list[str]] | list[tuple[str, str, str]] | None = None,
        plan_files: list[str] | None = None,
        task: str = "",
        *,
        base_ref: str | None = None,
        allow_governance_change: bool = False,
    ) -> dict:
        """Unified verification gate: checks claims, validates plans, and audits working-tree or PR merge-base diffs."""
        return _verify_run_check(
            self,
            files_modified=files_modified,
            claims=claims,
            plan_files=plan_files,
            task=task,
            base_ref=base_ref,
            allow_governance_change=allow_governance_change,
        )

    def learn(
        self,
        *,
        apply: bool = False,
        pin_hubs: bool = False,
        max_rules: int = 12,
        days: int = 180,
        min_cochanges: int = 3,
        min_confidence: float = 0.70,
    ) -> dict:
        """Discover zero-violation architectural boundary rules, co-change contracts, and hub symbols."""
        from .learn import learn_repository_governance

        return learn_repository_governance(
            self,
            apply=apply,
            pin_hubs=pin_hubs,
            max_rules=max_rules,
            days=days,
            min_cochanges=min_cochanges,
            min_confidence=min_confidence,
        )

    def remember(
        self,
        target: str = "",
        note: str = "",
        tags: list[str] | None = None,
        *,
        forbid: str | None = None,
        co_change: str | None = None,
        action: str = "save",
    ) -> dict:
        """Manage AST-anchored architectural invariants, boundary rules, & co-change contracts in `.projectbrain_memory.json`."""
        act = (action or "save").strip().lower()
        if act == "list":
            return self.list_memories()
        if act == "stale":
            res = self.list_memories()
            stale = [m for m in res["memories"] if m.get("stale")]
            return {
                "memories": stale,
                "count": len(stale),
                "boundary_rules": res["boundary_rules"],
                "co_change_rules": res["co_change_rules"],
            }
        if act == "forget":
            key = (target or forbid or co_change or note).strip()
            if not key:
                return {"error": "Provide `target` (id, symbol, file, or rule) to forget."}
            return {"status": "removed", **self._memory.forget(key)}

        self._ensure_fresh()
        if act == "refresh":
            updated = self._memory.refresh_hashes(self._ast_hashes, target.strip() or None)
            return {"status": "refreshed", "updated_count": updated}

        if forbid:
            rule = self._memory.add_boundary_rule(forbid, reason=note)
            current_violations = self._memory.check_boundary_violations(
                self._index, self._import_targets, self._graph, focus_rels=None,
            )
            rule_violations = [v for v in current_violations if v.get("rule_id") == rule.get("id")]
            return {
                "status": "saved_boundary_rule",
                "boundary_rule": rule,
                "existing_violations": rule_violations,
            }

        if co_change:
            c_rule = self._memory.add_co_change_rule(co_change, reason=note)
            return {
                "status": "saved_co_change_rule",
                "co_change_rule": c_rule,
            }

        target = target.strip()
        if not target or not note.strip():
            return {"error": "Provide both `target` and `note` (or `forbid` / `co_change`)."}

        node_id = target
        rel_path = ""
        ast_hash = ""

        if self._graph:
            if target not in self._graph.nodes:
                matches = self._graph.find_nodes_by_name(target)
                if matches:
                    node_id = matches[0]
            nd = self._graph.nodes.get(node_id, {})
            rel_path = nd.get("rel_path", "")
            ast_hash = nd.get("ast_hash", "")

        if not rel_path:
            rel_try = self._normalize_rel(target)
            if rel_try in self._index:
                rel_path = rel_try
                node_id = file_id(rel_try)

        entry = self._memory.remember(
            node_id, note, rel_path=rel_path, ast_hash=ast_hash, tags=tags,
        )
        return {"status": "saved", "memory": entry}

    def list_memories(self) -> dict:
        self._memory._ensure_fresh()
        if not self._index and self._is_db_fresh_fast(allow_zero_disk=True):
            raw_notes = getattr(self._memory, "_notes", [])
            nids = [m.get("target_id", "") for m in raw_notes if m.get("target_id")]
            fast_hashes = self._db_store.get_ast_hashes_fast(nids) if nids else {}
            notes = self._memory.list_all(fast_hashes)
            rules = self._memory.list_boundary_rules()
            co_rules = self._memory.list_co_change_rules()
            stale_count = sum(1 for m in notes if m.get("stale"))
            return {
                "memories": notes,
                "count": len(notes),
                "stale_count": stale_count,
                "boundary_rules": rules,
                "boundary_rules_count": len(rules),
                "co_change_rules": co_rules,
                "co_change_rules_count": len(co_rules),
            }
        self._ensure_fresh()
        notes = self._memory.list_all(self._ast_hashes, indexed_files=set(self._index.keys()))
        rules = self._memory.list_boundary_rules()
        co_rules = self._memory.list_co_change_rules()
        stale_count = sum(1 for m in notes if m.get("stale"))
        return {
            "memories": notes,
            "count": len(notes),
            "stale_count": stale_count,
            "boundary_rules": rules,
            "boundary_rules_count": len(rules),
            "co_change_rules": co_rules,
            "co_change_rules_count": len(co_rules),
        }

    def _normalize_rel(self, file_path: str) -> str:
        try:
            return str(Path(file_path).resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            norm = file_path.replace("\\", "/")
            while norm.startswith("./"):
                norm = norm[2:]
            return norm.lstrip("/")

    def _score_symbol_for_query(
        self,
        fn: dict,
        keywords: list[str],
        behaviors: dict[str, str],
        routes: list[dict],
        cells_4d: list[dict] | None = None,
        local_idf: dict[str, float] | None = None,
    ) -> float:
        from .dossier import score_symbol_for_query
        self._ensure_blobs()
        return score_symbol_for_query(self, fn, keywords, behaviors, routes, cells_4d=cells_4d, local_idf=local_idf)

    def _contract_in_query_scope(self, rel: str, scope_text: str, summary: str, terms: list[str]) -> bool:
        return _contract_in_query_scope_fn(self, rel, scope_text, summary, terms)

    def _compile_6d_contract_diagnostics(
        self,
        task: str,
        focus_files: set[str],
        strict_focus: bool = False,
    ) -> tuple[list[str], list[str]]:
        return _compile_6d_contract_diagnostics_fn(self, task, focus_files, strict_focus=strict_focus)

    def _score_class_for_query(self, cls: dict, keywords: list[str]) -> float:
        from .dossier import score_class_for_query
        self._ensure_blobs()
        return score_class_for_query(self, cls, keywords)

    # ── Session management ──────────────────────────────────────────────────

    def start_session(self, task: str) -> dict:
        """Start an iterative routing session for a coding task."""
        session = self._session_manager.start_session(self, task)
        return {
            "session_id": session.session_id,
            "task": task,
            "initial_files": session._initial_files,
            "next_suggestions": session.peek_frontier(5),
            "message": (
                f"Session started. {len(session._initial_files)} entry point(s) found. "
                "Read the first suggestion, then call brain_mark_read to get the next."
            ),
        }

    def session_mark_read(self, session_id: str, file_path: str) -> dict:
        """Mark a file as read in a session and get updated suggestions."""
        session = self._session_manager.get_session(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found"}
        try:
            rel_path = str(Path(file_path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel_path = file_path.replace("\\", "/")
        result = session.mark_read(rel_path)
        result["next_suggestions"] = session.peek_frontier(5)
        return result

    def session_mark_modified(self, session_id: str, file_path: str, summary: str = "") -> dict:
        """Mark a file as modified and see downstream impact."""
        session = self._session_manager.get_session(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found"}
        try:
            rel_path = str(Path(file_path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel_path = file_path.replace("\\", "/")
        return session.mark_modified(rel_path, summary)

    def session_next(self, session_id: str, count: int = 1) -> dict:
        """Get the next file(s) to read from the session frontier."""
        session = self._session_manager.get_session(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found"}
        items = session.next_file(count)
        return {
            "files": items,
            "frontier_remaining": sum(
                1 for item in session._frontier_heap
                if item.rel_path not in session.read_files
            ),
            "completeness": session._estimate_completeness(),
        }

    def session_status(self, session_id: str) -> dict:
        """Get current session status."""
        session = self._session_manager.get_session(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found"}
        return session.status()

    def index(self) -> dict:
        """Parse all Python and JS/TS files under root and cache the result."""
        import time as _time

        self._reset_partial_index()
        if not self._index and self._db_store.db_path.exists() and not self._cache_path.exists():
            self.load()

        prev_index = self._index
        prev_sizes = dict(self._sizes)
        prev_skipped = dict(self._skipped)
        prev_graph = self._graph
        prev_stats = getattr(self, "_cached_stats_summary", None)

        self._index = {}
        self._sizes = {}
        self._skipped = {}
        self._cached_stats_summary = None
        self._source_cache.clear()

        now_ts = _time.time()
        any_changed = False
        dirty_items: list[tuple[str, Path, str, float, int]] = []
        walk_order: list[tuple[str, str]] = []
        is_lazy_prev = isinstance(prev_index, LazyFileIndex)

        for path in self._walk():
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            ext = path.suffix.lower()

            try:
                st = path.stat()
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                if prev_skipped.get(rel) != self._skipped[rel]:
                    any_changed = True
                continue

            is_settled = (now_ts - st.st_mtime) > 2.0
            prev_mt = prev_index.get_mtime(rel) if is_lazy_prev else (prev_index[rel].get("mtime") if rel in prev_index else None)
            if is_settled and prev_mt is not None and st.st_mtime == prev_mt and st.st_size == prev_sizes.get(rel):
                walk_order.append(("clean_idx", rel))
                self._sizes[rel] = st.st_size
                continue
            if is_settled and rel in prev_skipped:
                self._skipped[rel] = prev_skipped[rel]
                continue

            any_changed = True
            walk_order.append(("dirty", rel))
            dirty_items.append((rel, path, ext, st.st_mtime, st.st_size))

        parsed_idx, parsed_sz, parsed_skip = parse_files_parallel(dirty_items, MINIFIED_LINE_THRESHOLD)
        self._skipped.update(parsed_skip)
        if (
            not any_changed
            and prev_index
            and {r for _, r in walk_order} == set(prev_index.keys())
            and set(self._skipped.keys()) == set(prev_skipped.keys())
            and prev_graph is not None
            and self._db_store.db_path.exists()
            and not self._cache_path.exists()
        ):
            self._index = prev_index
            self._sizes = prev_sizes
            self._graph = prev_graph
            self._cached_stats_summary = prev_stats
            return self.stats()

        for kind, rel in walk_order:
            if kind == "clean_idx":
                self._index[rel] = prev_index[rel]
            elif rel in parsed_idx:
                self._index[rel] = parsed_idx[rel]
                self._sizes[rel] = parsed_sz[rel]

        self._build_blobs(recompute_imports=True)
        self._graph = CodeGraph.build(
            self._index,
            import_targets=self._import_targets,
            symbol_imports=self._symbol_imports,
        )
        self._sync_interprocedural_effects()
        self._blobs_ready = True
        self._cached_stats_summary = compute_stats_summary(self._index)
        self._db_store.write_full(
            {
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "graph_version": GRAPH_VERSION,
                "root": str(self.root),
                "stats_summary": self._cached_stats_summary,
                "symbol_imports_b85": pack_symbol_imports(self._symbol_imports),
            },
            self._index,
            self._sizes,
            self._skipped,
            graph_json=self._graph.to_dict(),
        )
        for legacy in (self._cache_path, self._graph_path):
            if legacy.exists():
                legacy.unlink(missing_ok=True)
        return self.stats()

    def _load_neighborhood_for_check(self, candidate_files: list[str]) -> bool:
        return _load_neighborhood_for_check_fn(self, candidate_files)

    def load(self) -> bool:
        """Load a previously saved index from disk (SQLite .projectbrain.db primary)."""
        self._is_partial_index = False
        self._all_indexed_files = None
        data = None
        db_exists = self._db_store.db_path.exists()
        json_exists = self._cache_path.exists()
        if not db_exists and not json_exists:
            return False

        # Prefer SQLite .projectbrain.db unless a legacy/tampered .projectbrain.json is present
        json_header_ok = True
        if json_exists:
            try:
                with self._cache_path.open("r", encoding="utf-8") as fh:
                    head = fh.read(200)
                json_header_ok = (
                    f'"schema_version": {SCHEMA_VERSION}' in head
                    and f'"parser_version": "{PARSER_VERSION}"' in head
                )
            except OSError:
                json_header_ok = False
        use_sqlite = db_exists and (not json_exists or json_header_ok)
        if use_sqlite:
            data = self._db_store.load_all(lazy_files=True)

        if data is None:
            if not json_exists:
                return False
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False

        if not self._is_compatible_cache(data):
            return False
        if "lazy_index" in data:
            self._index, self._sizes = data["lazy_index"], data["sizes"]
        else:
            self._index = {rel: {**entry.get("info", {}), "mtime": entry.get("mtime")} for rel, entry in data.get("files", {}).items()}
            self._sizes = {rel: entry.get("size") for rel, entry in data.get("files", {}).items()}
        self._skipped = data.get("skipped", {})
        self._cached_stats_summary = data.get("stats_summary")
        if data.get("graph_json"):
            self._graph = CodeGraph.from_json(data["graph_json"])
        elif self._graph_path.exists():
            self._graph = CodeGraph.from_json(self._graph_path.read_text(encoding="utf-8"))
        else:
            self._graph = None
        self._wire_loaded_imports(data)
        if self._graph is None:
            self._graph = CodeGraph.build(self._index, import_targets=self._import_targets, symbol_imports=self._symbol_imports)
        return True

    def _wire_loaded_imports(self, data: dict | None = None) -> None:
        """Fast <8ms hydration of AST hashes and pre-resolved import maps without building TF-IDF blobs."""
        self._blobs_ready, self._doc_freq = False, None
        for attr in (
            "_blobs", "_token_sets", "_import_token_sets", "_body_token_sets", "_defn_sets",
            "_z_write_sets", "_z_read_sets", "_trans_effect_sets", "_cell_4d_tokens",
            "_const_aliases", "_idf", "_behaviors", "_ast_hashes", "_import_targets",
            "_symbol_imports", "_imported_by",
        ):
            setattr(self, attr, {})
        if data is not None and "lazy_index" in data and "symbol_imports_b85" in data and self._graph is not None:
            self._python_module_index = {}
            for nid, nd in self._graph.nodes.items():
                if nd.get("ast_hash"):
                    self._ast_hashes[nid] = nd["ast_hash"]
            for rp in self._index:
                self._import_targets[rp], self._symbol_imports[rp] = [], {}
            for dst_r, src_r in data.get("file_imports") or ():
                if src_r in self._import_targets:
                    self._import_targets[src_r].append(dst_r)
                self._imported_by.setdefault(dst_r, []).append(src_r)
            for rp, sm in unpack_symbol_imports(data.get("symbol_imports_b85")).items():
                self._symbol_imports[rp] = sm
            self._importer_counts = {rp: len(self._imported_by.get(rp, ())) for rp in self._index}
            return
        need_resolve = any("_resolved_targets" not in inf or "_resolved_sym_map" not in inf for inf in self._index.values())
        file_set = set(self._index) if need_resolve else set()
        module_index = resolve.build_python_module_index(self._index) if need_resolve else {}
        self._python_module_index = module_index
        for rel_path, info in self._index.items():
            for f in info.get("functions", []):
                if f.get("ast_hash"):
                    self._ast_hashes[fn_id(rel_path, f["name"], f.get("parent_class"))] = f["ast_hash"]
            for c in info.get("classes", []):
                if c.get("ast_hash"):
                    self._ast_hashes[cls_id(rel_path, c["name"])] = c["ast_hash"]
            if need_resolve:
                targets, sym_map = resolve.resolve_file_imports_and_symbols(
                    rel_path, info, file_set, module_index, index=self._index,
                )
                info["_resolved_targets"] = targets
                info["_resolved_sym_map"] = sym_map
            else:
                targets = info.get("_resolved_targets", [])
                sym_map = info.get("_resolved_sym_map", {})
            self._import_targets[rel_path] = targets
            self._symbol_imports[rel_path] = sym_map
            for target in targets:
                self._imported_by.setdefault(target, []).append(rel_path)
        self._importer_counts = {
            rel_path: len(self._imported_by.get(rel_path, []))
            for rel_path in self._index
        }

    def _ensure_blobs(self) -> None:
        """Lazily build TF-IDF lexical blobs and transitive 6D effect tokens on first lexical query."""
        if self._index and not self._blobs_ready and not getattr(self, "_is_partial_index", False):
            self._build_blobs(recompute_imports=False)
            self._sync_interprocedural_effects()
            self._blobs_ready = True

    def _sync_interprocedural_effects(self, target_rels: set[str] | None = None) -> None:
        """Fold local exit_contracts into file tokens and transitive callee effects into `_trans_effect_sets`."""
        if not self._graph:
            return
        new_toks_by_file: dict[str, set[str]] = {}
        if not hasattr(self, "_trans_effect_sets"):
            self._trans_effect_sets = {}
        if target_rels is not None:
            for r in target_rels:
                self._trans_effect_sets.pop(r, None)
            self._graph._ensure_registries()
            candidate_nids = [
                nid
                for r in target_rels
                for nid in self._graph._nodes_by_file.get(r, set())
                if nid in self._graph.nodes
            ]
        else:
            self._trans_effect_sets = {}
            candidate_nids = list(self._graph.nodes.keys())
        for nid in candidate_nids:
            nd = self._graph.nodes[nid]
            if nd.get("type") != "fn":
                continue
            rel = nd.get("rel_path", "")
            if not rel or rel not in self._index:
                continue
            tok_set = self._token_sets.setdefault(rel, set())
            trans_set = self._trans_effect_sets.setdefault(rel, set())
            added = new_toks_by_file.setdefault(rel, set())
            for zw in nd.get("transitive_z_write", []):
                zw_l = zw.lower()
                if zw_l not in tok_set and zw_l not in trans_set:
                    added.add(zw_l)
                trans_set.add(zw_l)
                for sub_t in _tokenize_symbol(zw):
                    if len(sub_t) >= 3:
                        if sub_t not in tok_set and sub_t not in trans_set:
                            added.add(sub_t)
                        trans_set.add(sub_t)
            for ex in nd.get("exit_contracts", []):
                ex_clean = ex.split(":", 1)[-1].lower()
                if len(ex_clean) >= 3:
                    if ex_clean not in tok_set:
                        added.add(ex_clean)
                    tok_set.add(ex_clean)
                    for sub_t in _tokenize_symbol(ex.split(":", 1)[-1]):
                        if len(sub_t) >= 3:
                            if sub_t not in tok_set:
                                added.add(sub_t)
                            tok_set.add(sub_t)
            for ex in nd.get("transitive_exits", []):
                ex_clean = ex.split(":", 1)[-1].lower()
                if len(ex_clean) >= 3:
                    if ex_clean not in tok_set and ex_clean not in trans_set:
                        added.add(ex_clean)
                    trans_set.add(ex_clean)
                    for sub_t in _tokenize_symbol(ex.split(":", 1)[-1]):
                        if len(sub_t) >= 3:
                            if sub_t not in tok_set and sub_t not in trans_set:
                                added.add(sub_t)
                            trans_set.add(sub_t)
        n_files = max(len(self._index), 1)
        for rel, toks in new_toks_by_file.items():
            if toks:
                self._blobs[rel] = self._blobs.get(rel, "") + " " + " ".join(sorted(toks))
                for t in toks:
                    if t not in self._idf:
                        self._idf[t] = math.log((n_files + 1) / 2.0) + 1.0

    @staticmethod
    def _is_compatible_cache(data) -> bool:
        return (
            isinstance(data, dict)
            and data.get("schema_version") == SCHEMA_VERSION
            and data.get("parser_version") == PARSER_VERSION
            and data.get("graph_version") == GRAPH_VERSION
            and "files" in data
        )

    def _compute_file_blob(self, rel_path: str, info: dict) -> set[str]:
        """Compute per-file searchable tokens, 4D cell sets, and AST hashes for a single file."""
        for ak, a_vals in (info.get("const_aliases") or {}).items():
            dst_list = self._const_aliases.setdefault(ak, [])
            for av in a_vals:
                if av not in dst_list:
                    dst_list.append(av)
        cls_field_names = [
            f_str.split("=")[0].split(":")[0].strip()
            for c in info.get("classes", [])
            for f_str in c.get("fields", [])
            if f_str.split("=")[0].split(":")[0].strip()
        ]
        symbol_names = (
            [f["name"] for f in info.get("functions", [])]
            + [c["name"] for c in info.get("classes", [])]
            + cls_field_names
            + info.get("exports", [])
        )
        tokenized: list[str] = []
        for name in set(symbol_names):
            tokenized.extend(_tokenize_symbol(name))

        z_write_toks: set[str] = set()
        z_read_toks: set[str] = set()
        cell_4d_toks: set[str] = set()
        raw_zw: set[str] = set()
        raw_zr: set[str] = set()
        for cell in info.get("cells_4d", []):
            raw_zw.update(cell.get("Z_write", ()))
            raw_zr.update(cell.get("Z_read", ()))
            x_sc = cell.get("X_scope", "")
            if x_sc.startswith(("useEffect", "useLayoutEffect", "useMemo", "useCallback", "case:")):
                for x_id in _IDENT_RE.findall(x_sc):
                    if x_id not in ("useEffect", "useLayoutEffect", "useMemo", "useCallback", "case"):
                        cell_4d_toks.add(x_id.lower())
                        for sub_t in _tokenize_symbol(x_id):
                            if len(sub_t) >= 4:
                                cell_4d_toks.add(sub_t)
        for zw in raw_zw:
            zw_l = zw.lower()
            z_write_toks.add(zw_l)
            cell_4d_toks.add(zw_l)
            for sub_t in _tokenize_symbol(zw):
                if len(sub_t) >= 4:
                    cell_4d_toks.add(sub_t)
        for zr in raw_zr:
            zr_l = zr.lower()
            z_read_toks.add(zr_l)
            cell_4d_toks.add(zr_l)
            for sub_t in _tokenize_symbol(zr):
                if len(sub_t) >= 4:
                    cell_4d_toks.add(sub_t)
        self._z_write_sets[rel_path] = z_write_toks
        self._z_read_sets[rel_path] = z_read_toks
        self._cell_4d_tokens[rel_path] = cell_4d_toks

        for f in info.get("functions", []):
            nid = fn_id(rel_path, f["name"], f.get("parent_class"))
            if f.get("ast_hash"):
                self._ast_hashes[nid] = f["ast_hash"]
        for c in info.get("classes", []):
            nid = cls_id(rel_path, c["name"])
            if c.get("ast_hash"):
                self._ast_hashes[nid] = c["ast_hash"]

        own_parts = [
            rel_path,
            *[f["name"] for f in info.get("functions", [])],
            *[c["name"] for c in info.get("classes", [])],
            *[f_str for c in info.get("classes", []) for f_str in c.get("fields", [])],
            *[r["path"] for r in info.get("routes", [])],
            *info.get("exports", []),
            *tokenized,
            *cell_4d_toks,
            *[f["docstring"] for f in info.get("functions", []) if f.get("docstring")],
            *[c["docstring"] for c in info.get("classes", []) if c.get("docstring")],
        ]
        own_blob_str = " ".join(own_parts).lower()
        imp_str = " ".join(info.get("imports", [])).lower()
        self._blobs[rel_path] = (own_blob_str + " " + imp_str).strip()

        file_tokens = set(_WORD_TOK_RE.findall(own_blob_str)) | set(tokenized) | cell_4d_toks
        for p_seg in _SEG_SPLIT_RE.split(rel_path.lower()):
            if p_seg:
                file_tokens.add(p_seg)
                file_tokens.update(_tokenize_symbol(p_seg))
        self._token_sets[rel_path] = file_tokens
        imp_toks: set[str] = set(_WORD_TOK_RE.findall(imp_str))
        for raw_imp in info.get("imports", []):
            for seg in _SEG_SPLIT_RE.split(raw_imp):
                if seg:
                    imp_toks.update(_tokenize_symbol(seg))
        self._import_token_sets[rel_path] = imp_toks - file_tokens
        body_toks: set[str] = set()
        raw_calls: set[str] = set()
        for f in info.get("functions", []):
            for bt in f.get("body_tokens", []):
                body_toks.add(bt.lower())
            raw_calls.update(f.get("calls", ()))
        for cl in raw_calls:
            body_toks.add(cl.lower())
            body_toks.update(_tokenize_symbol(cl))
        self._body_token_sets[rel_path] = body_toks

        self._defn_sets[rel_path] = (
            {name.lower() for name in symbol_names}
            | set(tokenized)
            | {r["path"].lower() for r in info.get("routes", [])}
        )
        return file_tokens

    def _build_blobs(self, recompute_imports: bool = False) -> None:
        """Precompute searchable tokens, IDF weights, import maps, and AST hashes per file."""
        self._blobs = {}
        self._token_sets = {}
        self._import_token_sets: dict[str, set[str]] = {}
        self._defn_sets = {}
        self._ast_hashes = {}
        self._z_write_sets = {}
        self._z_read_sets: dict[str, set[str]] = {}
        self._trans_effect_sets = {}
        self._cell_4d_tokens = {}
        self._const_aliases: dict[str, list[str]] = {}
        doc_freq: dict[str, int] = {}
        for rel_path, info in self._index.items():
            file_tokens = self._compute_file_blob(rel_path, info)
            for tok in file_tokens:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1
        self._doc_freq = doc_freq
        # Precompute BM25-style IDF weights so specific domain symbols outrank common words
        n_docs = max(len(self._index), 1)
        self._idf = {
            tok: math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)) + 1.0
            for tok, df in doc_freq.items()
        }

        self._import_targets = {}
        self._symbol_imports = {}
        self._imported_by = {}
        need_resolve = recompute_imports or any(
            "_resolved_targets" not in info or "_resolved_sym_map" not in info
            for info in self._index.values()
        )
        if need_resolve:
            file_set = set(self._index)
            module_index = resolve.build_python_module_index(self._index)
            self._python_module_index = module_index
            for importer_path, info in self._index.items():
                targets, sym_map = resolve.resolve_file_imports_and_symbols(
                    importer_path, info, file_set, module_index, index=self._index,
                )
                info["_resolved_targets"] = targets
                info["_resolved_sym_map"] = sym_map
                self._import_targets[importer_path] = targets
                self._symbol_imports[importer_path] = sym_map
                for target in targets:
                    self._imported_by.setdefault(target, []).append(importer_path)
        else:
            self._python_module_index = {}
            for importer_path, info in self._index.items():
                targets = info.get("_resolved_targets", [])
                sym_map = info.get("_resolved_sym_map", {})
                self._import_targets[importer_path] = targets
                self._symbol_imports[importer_path] = sym_map
                for target in targets:
                    self._imported_by.setdefault(target, []).append(importer_path)

        self._importer_counts = {
            rel_path: len(self._imported_by.get(rel_path, []))
            for rel_path in self._index
        }
        self._behaviors = {}
        self._blobs_ready = True

    def _get_file_behaviors(self, rel_path: str) -> dict[str, str]:
        """Lazily compute and cache behavioral roles per file on demand."""
        beh = self._behaviors.get(rel_path)
        if beh is None:
            info = self._index.get(rel_path)
            if not info:
                return {}
            ext = os.path.splitext(rel_path)[1].lower()
            beh = classify_file_functions(info, is_jsx=(ext in (".tsx", ".jsx")))
            self._behaviors[rel_path] = beh
        return beh

    def _update_blobs_incremental(self, changed_rels: set[str], deleted_rels: set[str], has_added_or_deleted: bool = False) -> None:
        """Incrementally update per-file blobs, IDF, imports, and behaviors in O(delta) time."""
        doc_freq = getattr(self, "_doc_freq", None)
        if not self._blobs_ready and not getattr(self, "_is_partial_index", False):
            # Fast O(delta) import & AST-hash update without triggering full-repo TF-IDF blob synthesis
            affected_t: set[str] = set()
            for rel in deleted_rels | changed_rels:
                self._behaviors.pop(rel, None)
                self._importer_counts.pop(rel, None)
                for old_t in self._import_targets.pop(rel, []):
                    affected_t.add(old_t)
                    imp_list = self._imported_by.get(old_t)
                    if imp_list and rel in imp_list:
                        imp_list.remove(rel)
                self._symbol_imports.pop(rel, None)
                if rel in deleted_rels:
                    self._imported_by.pop(rel, None)
            file_set_f = set(self._index)
            if has_added_or_deleted or not getattr(self, "_python_module_index", None):
                self._python_module_index = resolve.build_python_module_index(file_set_f)
            mod_idx = self._python_module_index
            for rel in changed_rels:
                info = self._index.get(rel)
                if not info:
                    continue
                for f in info.get("functions", []):
                    if f.get("ast_hash"):
                        self._ast_hashes[fn_id(rel, f["name"], f.get("parent_class"))] = f["ast_hash"]
                for c in info.get("classes", []):
                    if c.get("ast_hash"):
                        self._ast_hashes[cls_id(rel, c["name"])] = c["ast_hash"]
                targets, sym_map = resolve.resolve_file_imports_and_symbols(
                    rel, info, file_set_f, mod_idx, index=self._index,
                )
                info["_resolved_targets"] = targets
                info["_resolved_sym_map"] = sym_map
                self._import_targets[rel] = targets
                self._symbol_imports[rel] = sym_map
                for target in targets:
                    affected_t.add(target)
                    imp_list = self._imported_by.setdefault(target, [])
                    if rel not in imp_list:
                        imp_list.append(rel)
                self._importer_counts[rel] = len(self._imported_by.get(rel, []))
            for t in affected_t:
                if t in self._index:
                    self._importer_counts[t] = len(self._imported_by.get(t, []))
            return
        if doc_freq is None:
            self._build_blobs()
            return
        touched_tokens: set[str] = set()
        affected_targets: set[str] = set()
        for rel in deleted_rels | changed_rels:
            old_toks = self._token_sets.pop(rel, set())
            for tok in old_toks:
                new_df = doc_freq.get(tok, 1) - 1
                if new_df <= 0:
                    doc_freq.pop(tok, None)
                else:
                    doc_freq[tok] = new_df
                touched_tokens.add(tok)
            self._blobs.pop(rel, None)
            self._import_token_sets.pop(rel, None)
            self._body_token_sets.pop(rel, None)
            self._defn_sets.pop(rel, None)
            self._z_write_sets.pop(rel, None)
            self._z_read_sets.pop(rel, None)
            if hasattr(self, "_trans_effect_sets"):
                self._trans_effect_sets.pop(rel, None)
            self._cell_4d_tokens.pop(rel, None)
            self._behaviors.pop(rel, None)
            self._importer_counts.pop(rel, None)
            old_targets = self._import_targets.pop(rel, [])
            for old_t in old_targets:
                affected_targets.add(old_t)
                imp_list = self._imported_by.get(old_t)
                if imp_list and rel in imp_list:
                    imp_list.remove(rel)
            self._symbol_imports.pop(rel, None)
            if rel in deleted_rels:
                self._imported_by.pop(rel, None)

        _JSX_EXTS = {".tsx", ".jsx"}
        if self._all_indexed_files is not None:
            self._all_indexed_files.difference_update(deleted_rels)
            self._all_indexed_files.update(changed_rels)
            file_set = set(self._all_indexed_files)
        else:
            file_set = set(self._index)
        if has_added_or_deleted or not getattr(self, "_python_module_index", None):
            self._python_module_index = resolve.build_python_module_index(file_set)
        module_index = self._python_module_index

        if getattr(self, "_is_partial_index", False):
            _owner = self

            class _LazySelfIndex:
                def get(self, k: str, default: object = None) -> object:
                    inf = _owner._get_file_info(k)
                    return inf if inf is not None else default

            resolve_index_view: object = _LazySelfIndex()
        else:
            resolve_index_view = self._index

        for rel in changed_rels:
            info = self._index.get(rel)
            if not info:
                continue
            new_toks = self._compute_file_blob(rel, info)
            for tok in new_toks:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1
                touched_tokens.add(tok)
            targets, sym_map = resolve.resolve_file_imports_and_symbols(
                rel, info, file_set, module_index, index=resolve_index_view,
            )
            info["_resolved_targets"] = targets
            info["_resolved_sym_map"] = sym_map
            self._import_targets[rel] = targets
            self._symbol_imports[rel] = sym_map
            for target in targets:
                affected_targets.add(target)
                imp_list = self._imported_by.setdefault(target, [])
                if rel not in imp_list:
                    imp_list.append(rel)
            ext = Path(rel).suffix.lower()
            self._behaviors[rel] = classify_file_functions(
                info, is_jsx=(ext in _JSX_EXTS),
            )
            self._importer_counts[rel] = len(self._imported_by.get(rel, []))

        for t in affected_targets:
            if t in self._index:
                self._importer_counts[t] = len(self._imported_by.get(t, []))

        n_docs = max(len(self._index), 1)
        for tok in touched_tokens:
            df = doc_freq.get(tok, 0)
            if df <= 0:
                self._idf.pop(tok, None)
            else:
                self._idf[tok] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)) + 1.0

    def _graph_boost(self, base_scores: dict[str, float], top_n: int) -> dict[str, float]:
        """Expand keyword hits through graph edges for graph-boosted ranking."""
        if not self._graph:
            return base_scores

        boosted = dict(base_scores)
        top_files = sorted(base_scores, key=lambda p: base_scores[p], reverse=True)[:top_n * 2]

        def _deg(p: str) -> int:
            return max(1, len(self._import_targets.get(p, [])) + len(self._imported_by.get(p, [])))

        # Symmetric degree normalisation (D^-1/2 A D^-1/2): mass propagated along an
        # edge is divided by sqrt(deg(src) * deg(dst)), so universally-imported hubs
        # cannot accumulate rank simply by being adjacent to everything.
        reach_count: dict[str, float] = {}
        for rel_path in top_files:
            score = base_scores[rel_path]
            d_src = _deg(rel_path)
            for connected in self._import_targets.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.3 / math.sqrt(d_src * _deg(connected))
            for connected in self._imported_by.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.2 / math.sqrt(d_src * _deg(connected))

        for rel_path, reach_score in reach_count.items():
            if rel_path in boosted:
                boosted[rel_path] += reach_score * 0.5
            elif reach_score > 0:
                boosted[rel_path] = reach_score * 0.3

        return boosted

    def is_stale(self) -> bool:
        """Cheap staleness check: stat() every relevant file, no reads/parsing."""
        current = {
            rel: mtime
            for rel, _, mtime in self._walk_entries()
        }
        if not current and self._index and self._db_store.db_path.exists():
            return False
        known_keys = set(self._index.keys()) | set(self._skipped.keys())
        if current.keys() != known_keys:
            return True
        is_lazy = isinstance(self._index, LazyFileIndex)
        return any(
            current[rel] != (self._index.get_mtime(rel) if is_lazy else self._index[rel].get("mtime"))
            for rel in self._index if rel in current
        )

    def incremental_index(self, candidate_files: list[str] | None = None) -> dict:
        """Re-parse only changed files instead of a full re-index.

        When `candidate_files` is provided (e.g. from a post-edit hook), only those files
        and their 1-hop import neighbors are stat()'d on disk (`O(k)` instead of a full repo walk).
        """
        if candidate_files is None and getattr(self, "_is_partial_index", False):
            self._reset_partial_index()
            self.load()
        if not self._index:
            return self.index()

        if candidate_files is not None:
            rels_to_stat: set[str] = set()
            cand_rels: set[str] = set()
            for raw_f in candidate_files:
                r = self._normalize_rel(str(raw_f))
                if r:
                    cand_rels.add(r)
                    rels_to_stat.add(r)
                    for imp_r in self._imported_by.get(r, ()):
                        if imp_r in self._index or not self._all_indexed_files or imp_r not in self._all_indexed_files:
                            rels_to_stat.add(imp_r)
                    for tgt_r in self._import_targets.get(r, ()):
                        if tgt_r in self._index or not self._all_indexed_files or tgt_r not in self._all_indexed_files:
                            rels_to_stat.add(tgt_r)
            added: set[str] = set()
            deleted: set[str] = set()
            modified: set[str] = set()
            current_files: dict[str, tuple[str, float]] = {}
            for r in rels_to_stat:
                p = self.root / r
                try:
                    st = p.stat()
                    current_files[r] = (str(p), st.st_mtime)
                    if (
                        r not in self._index
                        and r not in self._skipped
                        and not (self._all_indexed_files and r in self._all_indexed_files)
                    ):
                        if p.suffix.lower() in PARSED_EXTS:
                            added.add(r)
                    elif r in self._index and st.st_mtime != (
                        self._index.get_mtime(r) if isinstance(self._index, LazyFileIndex) else self._index[r].get("mtime")
                    ):
                        modified.add(r)
                except OSError:
                    if r in self._index or r in self._skipped:
                        deleted.add(r)
        else:
            current_files = {
                rel: (abs_path, mtime)
                for rel, abs_path, mtime in self._walk_entries()
            }

            old_keys = set(self._index.keys()) | set(self._skipped.keys())
            new_keys = set(current_files.keys())

            added = new_keys - old_keys
            deleted = old_keys - new_keys
            potentially_modified = set(self._index.keys()) & new_keys

            is_lazy_inc = isinstance(self._index, LazyFileIndex)
            modified = {
                rel for rel in potentially_modified
                if current_files[rel][1] != (self._index.get_mtime(rel) if is_lazy_inc else self._index[rel].get("mtime"))
            }

        if not added and not deleted and not modified:
            return {
                "status": "up_to_date",
                "added": 0, "modified": 0, "deleted": 0,
                "total_files": len(self._index),
            }

        deleted_indexed: set[str] = set()
        skipped_changed = False
        for rel in deleted:
            if rel in self._index:
                deleted_indexed.add(rel)
            if rel in self._skipped:
                skipped_changed = True
            self._index.pop(rel, None)
            self._sizes.pop(rel, None)
            self._skipped.pop(rel, None)
            self._source_cache.pop(rel, None)
            self._ledger.invalidate_file(rel)

        all_interfaces_unchanged = bool(modified and not added and not deleted_indexed)
        indexed_added = 0
        indexed_modified = 0
        for rel in added | modified:
            path = Path(current_files[rel][0])
            ext = path.suffix.lower()
            self._source_cache.pop(rel, None)
            self._git_head_cache.pop(rel, None)
            self._ledger.invalidate_file(rel)
            was_in_index = rel in self._index
            old_info = self._index.get(rel) or {}
            if was_in_index and old_info:
                self._pre_edit_index.setdefault(rel, dict(old_info))
            old_iface_hash = compute_file_interface_hash(old_info) if old_info else None

            pending = self._pending_parsed_files.pop(rel, None)
            try:
                st = path.stat()
                if pending is not None and pending[3] == st.st_mtime and pending[4] == st.st_size:
                    info_raw, ts_tree, source, _, _ = pending
                else:
                    source = path.read_text(encoding="utf-8", errors="ignore")
                    info_raw = None
                    ts_tree = None
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                skipped_changed = True
                if was_in_index:
                    self._index.pop(rel, None)
                    deleted_indexed.add(rel)
                all_interfaces_unchanged = False
                continue

            if _is_minified(source) or _is_unexpanded_symlink(path, source):
                self._skipped[rel] = "minified_or_generated" if _is_minified(source) else "symlink_stub"
                skipped_changed = True
                if was_in_index:
                    self._index.pop(rel, None)
                    deleted_indexed.add(rel)
                all_interfaces_unchanged = False
                continue

            if rel in self._skipped:
                self._skipped.pop(rel, None)
                skipped_changed = True
            self._source_cache[rel] = source
            if info_raw is not None:
                info = dict(info_raw)
                info["routes"] = list(info.get("routes") or [])
            else:
                info, ts_tree = parse_with_tree(source, ext)
            info.setdefault("routes", []).extend(frameworks.infer_file_based_routes(rel, info))
            info["cells_4d"] = extract_4d_coordinate_matrix(source.splitlines(), ext=ext, tree=ts_tree)
            info["mtime"] = st.st_mtime
            if not old_iface_hash or info.get("interface_hash") != old_iface_hash:
                all_interfaces_unchanged = False
            self._index[rel] = info
            self._sizes[rel] = st.st_size
            if was_in_index:
                indexed_modified += 1
            else:
                indexed_added += 1
            # Invalidate served session cache for modified files
            self._served_skeletons.discard(rel)
            self._served_slices = {
                s for s in self._served_slices if f":{rel}:" not in s
            }

        # If newly added files were all skipped (minified/vendored) and no indexed file changed,
        # persist the updated skipped table without rebuilding blobs or the graph.
        if indexed_added == 0 and indexed_modified == 0 and not deleted_indexed:
            if skipped_changed:
                self._db_store.update_skipped_only(self._skipped)
            return {
                "status": "up_to_date",
                "added": 0, "modified": 0, "deleted": 0,
                "total_files": len(self._index),
            }

        changed_indexed = {r for r in (added | modified) if r in self._index}
        self._cached_stats_summary = None
        inc_meta = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "graph_version": GRAPH_VERSION,
            "root": str(self.root),
        }
        if self._graph is not None and len(changed_indexed | deleted_indexed) <= 30:
            self._update_blobs_incremental(
                changed_indexed,
                deleted_indexed,
                has_added_or_deleted=bool(indexed_added or deleted_indexed),
            )
            graph_delta = self._graph.update_files(
                changed_indexed,
                deleted_indexed,
                self._index,
                import_targets=self._import_targets,
                symbol_imports=self._symbol_imports,
                imported_by=self._imported_by,
                interfaces_unchanged=all_interfaces_unchanged,
            )
            if self._blobs_ready:
                self._sync_interprocedural_effects(target_rels=set(graph_delta["affected_rels"]))
            if not getattr(self, "_is_partial_index", False):
                inc_meta["symbol_imports_b85"] = pack_symbol_imports(self._symbol_imports)
            self._db_store.apply_incremental(
                inc_meta,
                {r: self._index[r] for r in changed_indexed},
                self._sizes,
                deleted_indexed,
                self._skipped,
                graph_json=None,
                graph_delta=graph_delta,
                skipped_changed=skipped_changed,
            )
        else:
            if getattr(self, "_is_partial_index", False):
                self._reset_partial_index()
                self.load()
                return self.incremental_index(candidate_files=candidate_files)
            self._build_blobs()
            self._graph = CodeGraph.build(
                self._index,
                import_targets=self._import_targets,
                symbol_imports=self._symbol_imports,
            )
            self._sync_interprocedural_effects()
            self._cached_stats_summary = compute_stats_summary(self._index)
            inc_meta["stats_summary"] = self._cached_stats_summary
            inc_meta["symbol_imports_b85"] = pack_symbol_imports(self._symbol_imports)
            self._db_store.apply_incremental(
                inc_meta,
                {r: self._index[r] for r in changed_indexed},
                self._sizes,
                deleted_indexed,
                self._skipped,
                graph_json=None if all_interfaces_unchanged else self._graph.to_dict(),
                skipped_changed=skipped_changed,
            )
        for legacy in (self._cache_path, self._graph_path):
            if legacy.exists():
                legacy.unlink(missing_ok=True)

        return {
            "status": "incremental_update",
            "added": indexed_added,
            "modified": indexed_modified,
            "deleted": len(deleted_indexed),
            "total_files": len(self._index),
            "salsa_early_cutoff": all_interfaces_unchanged,
        }

    def _ensure_fresh(self) -> None:
        self._reset_partial_index()
        if not self._index:
            self.load()
        if not self._index:
            self.index()
        elif self.is_stale():
            self.incremental_index()

    def _expand_keywords_with_aliases(self, keywords: list[str]) -> list[str]:
        """Expand query keywords using bijective symbolic constant-map aliases discovered across the repo."""
        aliases_map = getattr(self, "_const_aliases", None) or {}
        if not aliases_map or not keywords:
            return keywords
        expanded = list(keywords)
        for kw in keywords:
            for al in aliases_map.get(kw, [])[:2]:
                if al not in expanded:
                    expanded.append(al)
        return expanded

    def query(self, prompt: str, top_n: int = 10, min_score_ratio: float | None = None) -> list[dict]:
        """Return the top_n most relevant files for a natural language prompt."""
        self._ensure_fresh()
        self._ensure_blobs()

        base_keywords = _extract_keywords(prompt)
        keywords = self._expand_keywords_with_aliases(base_keywords)
        if not keywords:
            return []
        intent = classify_intent(prompt)

        # Concept groups: a query term and the identifiers the codebase itself declares
        # equivalent to it (bijective constant-map aliases) denote ONE concept. Scoring
        # takes the best-matching member per concept, so a file is not double-counted for
        # containing both spellings (e.g. the mapping table itself), and a file that only
        # uses the programmatic identifier fully covers the concept.
        concept_groups: list[list[str]] = []
        grouped: set[str] = set()
        for kw in base_keywords:
            if kw in grouped:
                continue
            members = [kw] + [a for a in self._expand_keywords_with_aliases([kw])[1:] if a not in grouped]
            grouped.update(members)
            concept_groups.append(members)
        multi_kw = len(keywords) > 1
        query_wants_tests = bool(set(keywords) & _TEST_QUERY_TERMS)

        scores: dict[str, float] = {}
        own_concept_hits: dict[str, int] = {}
        n_concepts = max(len(concept_groups), 1)
        imp_token_map = getattr(self, "_import_token_sets", {})
        for rel_path, blob in self._blobs.items():
            p_obj = Path(rel_path)
            is_test_f = _is_test_file(rel_path)
            stem_low = p_obj.stem.lower()
            stem_tokens = set(_tokenize_symbol(p_obj.stem)) | {stem_low}
            dir_lower = "/".join(p_obj.parts[:-1]).lower()
            defns = self._defn_sets.get(rel_path, set())
            z_writes = self._z_write_sets.get(rel_path, set())
            z_reads = getattr(self, "_z_read_sets", {}).get(rel_path, set())
            tokens = self._token_sets.get(rel_path, set())
            imp_tokens = imp_token_map.get(rel_path, set())
            trans_effects = getattr(self, "_trans_effect_sets", {}).get(rel_path, set())
            body_tokens = self._body_token_sets.get(rel_path, set())
            score = 0.0
            c_hits = 0
            has_stem_hit = False
            has_dir_hit = False
            dir_segs = {s.lower() for s in p_obj.parts[:-1]} | {s.lower().rstrip("s") for s in p_obj.parts[:-1] if len(s) >= 4}
            for members in concept_groups:
                best = 0.0
                hit_own = False
                for kw in members:
                    matched_token = kw in tokens
                    matched_trans = kw in trans_effects
                    matched_imp = kw in imp_tokens
                    matched_body = kw in body_tokens
                    matched_sub = len(kw) >= 4 and kw in blob
                    in_stem = (not is_test_f or query_wants_tests) and (
                        kw in stem_tokens or (len(kw) >= 4 and kw in stem_low)
                    )
                    in_dir = (not is_test_f or query_wants_tests) and (
                        kw in dir_segs or kw.rstrip("s") in dir_segs or kw in dir_lower
                    )
                    if not (matched_token or matched_trans or matched_imp or matched_body or matched_sub or in_stem or in_dir):
                        continue
                    idf_weight = (0.7 + 0.3 * self._idf.get(kw, 1.0)) if multi_kw else 1.0
                    if kw in defns:
                        base_hit = 2.0
                        hit_own = True
                    elif kw in z_writes or kw in z_reads:
                        base_hit = 1.5
                        hit_own = True
                    elif matched_token:
                        base_hit = 1.0
                        hit_own = True
                    elif matched_trans:
                        base_hit = 0.55
                        hit_own = True
                    elif matched_body:
                        base_hit = 0.35
                        hit_own = True
                    elif matched_imp:
                        base_hit = 0.18
                    elif matched_sub:
                        base_hit = 0.45
                    else:
                        base_hit = 0.0
                    if in_stem:
                        field_bonus = 1.5
                        has_stem_hit = True
                        hit_own = True
                    elif in_dir:
                        field_bonus = 0.85
                        has_dir_hit = True
                        hit_own = True
                    else:
                        field_bonus = 0.0
                    best = max(best, (base_hit + field_bonus) * idf_weight)
                score += best
                if hit_own:
                    c_hits += 1

            if score > 0:
                own_concept_hits[rel_path] = c_hits
                if has_stem_hit and has_dir_hit:
                    score *= 1.45
                if n_concepts >= 2:
                    cov_ratio = c_hits / n_concepts
                    score *= 0.35 + 0.65 * (cov_ratio ** 1.15)
                score *= _path_multiplier(rel_path, query_wants_tests=query_wants_tests)

                importer_count = self._importer_counts.get(rel_path, 0)
                if importer_count:
                    score *= 1.0 + 0.06 * math.log2(1 + importer_count)

                score = self._apply_intent_boost(
                    score, rel_path, intent, keywords,
                )

                scores[rel_path] = score

        if self._graph and scores:
            scores = self._graph_boost(scores, top_n)

        ranked_all = sorted(scores, key=lambda p: scores[p], reverse=True)
        if ranked_all and n_concepts >= 2:
            top_p = ranked_all[0]
            top_sc = scores[top_p]
            top_neighbors = set(self._imported_by.get(top_p, [])) | set(self._import_targets.get(top_p, []))
            max_c_hits = max((own_concept_hits.get(p, 0) for p in ranked_all[:3]), default=1)
            is_flow_query = intent.get("intent") in (UNDERSTAND_FLOW, IMPACT_ANALYSIS, ADD_FEATURE) or any(
                w in prompt.lower() for w in ("how ", "flow", "add ", "failing", "trace")
            )
            min_ratio = min_score_ratio if min_score_ratio is not None else (0.22 if is_flow_query else 0.55)
            filtered_ranked = [top_p]
            for p in ranked_all[1:]:
                if len(filtered_ranked) >= top_n:
                    break
                eff_min = (min_ratio * 0.55) if (is_flow_query and p in top_neighbors) else min_ratio
                if scores[p] < top_sc * eff_min:
                    continue
                if min_score_ratio is None and not is_flow_query and max_c_hits >= 3 and own_concept_hits.get(p, 0) < max_c_hits - 1 and scores[p] < top_sc * 0.72:
                    continue
                filtered_ranked.append(p)
            ranked = filtered_ranked
        else:
            ranked = ranked_all[:top_n]
        return [self._build_result(rel_path, scores[rel_path], keywords) for rel_path in ranked]
    def _apply_intent_boost(
        self, score: float, rel_path: str, intent: dict, keywords: list[str],
    ) -> float:
        """Apply intent-specific scoring adjustments."""
        intent_type = intent["intent"]
        info = self._index.get(rel_path, {})

        if intent_type == LOCATE_ROUTE:
            for r in info.get("routes", []):
                for kw in keywords:
                    if kw in r["path"].lower() or kw == r["method"].lower():
                        score *= 1.5
                        break

        elif intent_type == LOCATE_SYMBOL:
            defns = self._defn_sets.get(rel_path, set())
            matched_ents = [e.lower() for e in intent.get("entities", []) if e.lower() in defns]
            if matched_ents:
                score *= 1.25
        elif intent_type == UNDERSTAND_FLOW:
            behaviors = self._get_file_behaviors(rel_path)
            flow_roles = {"orchestrator", "handler"}
            if any(b in flow_roles for b in behaviors.values()):
                score *= 1.3

        elif intent_type == IMPACT_ANALYSIS:
            importer_count = self._importer_counts.get(rel_path, 0)
            if importer_count >= 3:
                score *= 1.2

        elif intent_type == ADD_FEATURE:
            if info.get("routes"):
                score *= 1.2
            behaviors = self._get_file_behaviors(rel_path)
            if any(b == "orchestrator" for b in behaviors.values()):
                score *= 1.1

        return score

    def _build_result(self, rel_path: str, score: float, keywords: list[str]) -> dict:
        info = self._index[rel_path]
        path_lower = rel_path.lower()
        reasons: list[str] = []

        for kw in keywords:
            if kw in path_lower:
                reasons.append(f"filename matches '{kw}'")
            for fn in info.get("functions", []):
                if kw in fn["name"].lower():
                    reasons.append(f"defines function '{fn['name']}'")
                elif fn.get("docstring") and kw in fn["docstring"].lower():
                    reasons.append(f"function '{fn['name']}' docs mention '{kw}'")
            for c in info.get("classes", []):
                if kw in c["name"].lower():
                    reasons.append(f"defines class '{c['name']}'")
                elif c.get("docstring") and kw in c["docstring"].lower():
                    reasons.append(f"class '{c['name']}' docs mention '{kw}'")
            for r in info.get("routes", []):
                if kw in r["path"].lower():
                    reasons.append(f"defines route {r['method']} {r['path']}")
            for imp in info.get("imports", []):
                if kw in imp.lower():
                    reasons.append(f"imports '{imp}'")
            for exp in info.get("exports", []):
                if kw in exp.lower():
                    reasons.append(f"exports '{exp}'")
            for cell in info.get("cells_4d", []):
                if kw in (cell.get("X_scope") or "").lower() or any(kw == zw.lower() for zw in cell.get("Z_write", [])):
                    reasons.append(f"4D cell {cell.get('Y_role')} {cell.get('X_scope')} (L{cell.get('line_start')}-L{cell.get('line_end')})")
                    break

        seen: set[str] = set()
        deduped = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)

        all_functions = info.get("functions", [])
        behaviors = self._get_file_behaviors(rel_path)
        kw_set_low = {k.lower() for k in keywords}
        sorted_functions = sorted(
            all_functions,
            key=lambda f0: (
                0 if any(k in f0["name"].lower() for k in kw_set_low) else 1,
                f0.get("line_start", 0),
            ),
        )
        functions = []
        for fn in sorted_functions[:MAX_FUNCTIONS_PER_FILE]:
            fn_entry = {k: fn[k] for k in ("name", "signature", "line_start", "line_end") if k in fn}
            qualified_name = fn["name"]
            if fn.get("parent_class"):
                qualified_name = f"{fn['parent_class']}.{fn['name']}"
            behavior = behaviors.get(qualified_name, "unknown")
            if behavior != "unknown":
                fn_entry["behavior"] = behavior
            functions.append(fn_entry)
        entry = {
            "file_path": str(self.root / rel_path),
            "rel_path": rel_path,
            "score": score,
            "reason": "; ".join(deduped[:3]) if deduped else "graph connection / keyword match",
            "functions": functions,
            "classes": info.get("classes", []),
            "routes": info.get("routes", []),
            "imports": info.get("imports", []),
            "exports": info.get("exports", []),
            "connections": self._connections(rel_path),
        }
        omitted = len(all_functions) - len(functions)
        if omitted > 0:
            entry["functions_omitted"] = omitted
        return entry

    def query_with_graph(self, prompt: str, top_n: int = 5) -> dict:
        """Like query() but wraps results with a typed symbol subgraph."""
        intent = classify_intent(prompt)
        results = self.query(prompt, top_n=top_n)
        if not self._graph or not results:
            return {
                "intent": intent,
                "results": results,
                "subgraph": {"nodes": [], "edges": []},
                "entrypoints": [],
            }

        rel_paths = [r["rel_path"] for r in results]
        subgraph = self._graph.subgraph_for_files(rel_paths)

        result_paths_set = set(rel_paths)
        entrypoints = sorted([
            f"file:{rp}" for rp in rel_paths
            if not any(importer in result_paths_set for importer in self._imported_by.get(rp, []))
        ])

        return {
            "intent": intent,
            "results": results,
            "subgraph": _trim_subgraph(subgraph),
            "entrypoints": entrypoints,
        }

    def get_neighbors(
        self,
        node_id: str,
        edge_types: list[str] | None = None,
        depth: int = 1,
    ) -> dict:
        self._ensure_fresh()
        if not self._graph:
            return {"nodes": [], "edges": [], "error": "Graph not available"}
        return self._graph.neighbors(node_id, edge_types=edge_types, depth=depth)

    def find_relationship(self, name_a: str, name_b: str | None = None) -> dict:
        self._ensure_fresh()
        if name_b is None:
            return self.explore(name_a)
        if not self._graph:
            return {"path": [], "error": "Graph not available"}

        candidates_a = self._graph.find_nodes_by_name(name_a)
        candidates_b = self._graph.find_nodes_by_name(name_b)

        if not candidates_a or not candidates_b:
            return {
                "path": [],
                "candidates_a": candidates_a[:5],
                "candidates_b": candidates_b[:5],
                "message": "No nodes matched one or both names — try a more specific name",
            }

        best_path: list[dict] | None = None
        best_a = best_b = ""
        for a_id in candidates_a[:5]:
            for b_id in candidates_b[:5]:
                p = self._graph.find_path(a_id, b_id)
                if p and (best_path is None or len(p) < len(best_path)):
                    best_path, best_a, best_b = p, a_id, b_id

        if not best_path:
            return {
                "path": [],
                "candidates_a": candidates_a[:5],
                "candidates_b": candidates_b[:5],
                "message": "Nodes found but no path connects them in the graph",
            }

        return {
            "path": best_path,
            "from_node": {"id": best_a, **self._graph.nodes[best_a]},
            "to_node": {"id": best_b, **self._graph.nodes[best_b]},
        }

    def list_nodes(self, node_type: str | None = None, query: str | None = None) -> dict:
        self._ensure_fresh()
        if not self._graph:
            return {"nodes": [], "total": 0}

        q_lower = query.lower() if query else None
        results = []
        for node_id, nd in self._graph.nodes.items():
            if node_type and nd.get("type") != node_type:
                continue
            if q_lower:
                searchable = " ".join([node_id, nd.get("name", ""), nd.get("rel_path", "")]).lower()
                if q_lower not in searchable:
                    continue
            results.append({"id": node_id, **nd})

        total = len(results)
        return {"nodes": results[:100], "total": total}

    def verify_claim(self, subject: str, relation: str, object: str) -> dict:
        """Ground a structural claim against the index (SUPPORTED / REFUTED / UNKNOWN)."""
        return _verify_claim_fn(self, subject, relation, object)

    def _support_evidence(self, m: dict, relation: str, obj: str, info: dict) -> dict:
        return _verify_support_evidence(self, m, relation, obj, info)

    def _find_importers(self, rel_path: str, limit: int = 5) -> list[str]:
        return self._imported_by.get(rel_path, [])[:limit]

    def _resolve_imports(self, rel_path: str, limit: int = 5) -> list[str]:
        return self._import_targets.get(rel_path, [])[:limit]

    # ── Verification & Safety ───────────────────────────────────────────────

    def verify_batch(self, claims: list[tuple[str, str, str]]) -> dict:
        return _verify_batch_fn(self, claims)

    def verify_plan(self, task: str, files_to_edit: list[str], files_to_create: list[str] | None = None) -> dict:
        return _verify_plan_fn(self, task, files_to_edit, files_to_create=files_to_create)

    def impact(self, files_changed: list[str] | str, *, _already_fresh: bool = False) -> dict:
        return _verify_impact_fn(self, files_changed, _already_fresh=_already_fresh)

    def _connections(self, rel_path: str) -> list[dict]:
        connections = [
            {"rel_path": other, "relation": "imports", "confidence": "import_resolved"}
            for other in self._resolve_imports(rel_path)
        ]
        connections += [
            {"rel_path": other, "relation": "imported_by", "confidence": "import_resolved"}
            for other in self._find_importers(rel_path)
        ]
        return connections

    # ── Cross-Boundary Intelligence ─────────────────────────────────────────

    def trace_flow(self, url_path: str, method: str | None = None) -> dict:
        return _verify_trace_flow(self, url_path, method=method)

    @staticmethod
    def _url_matches(pattern: str, url: str) -> bool:
        return _verify_url_matches(pattern, url)

    def co_change_analysis(self, file_path: str, days: int = 90) -> dict:
        return _verify_co_change_analysis(self, file_path, days=days)

    def get_context_for_file(self, file_path: str) -> dict:
        """Return parsed info for a file plus its 1-hop import/importer chain and skeleton."""
        self._ensure_fresh()

        target = Path(file_path)
        try:
            rel = str(target.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel = file_path.replace("\\", "/")

        if rel not in self._index:
            return {}

        info = self._index[rel]

        return {
            "file_path": file_path,
            "rel_path": rel,
            **info,
            "skeleton": build_file_skeleton(rel, info, behaviors=self._get_file_behaviors(rel)),
            "imported_by": self._find_importers(rel, limit=999),
            "imports_resolved": self._resolve_imports(rel, limit=999),
        }

    def stats(self, reset: bool = False) -> dict:
        """Summary counts for the current index plus cumulative ROI savings telemetry."""
        if reset:
            return self._ledger.reset()
        self._reset_partial_index()
        self._memory._ensure_fresh()
        if not self._index and self._db_store.db_path.exists():
            self.load()
        cached_s = getattr(self, "_cached_stats_summary", None) or compute_stats_summary(self._index)
        self._cached_stats_summary = cached_s
        result = {
            "files": len(self._index),
            "functions": cached_s["functions"],
            "classes": cached_s["classes"],
            "routes": cached_s["routes"],
            "cells_6d": cached_s["cells_4d"],
            "cells_4d": cached_s["cells_4d"],
            "memories": len(self._memory._notes),
            "boundary_rules": len(self._memory._boundary_rules),
            "co_change_rules": len(self._memory._co_change_rules),
            "storage_backend": "sqlite" if self._db_store.db_path.exists() else "json",
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "graph_version": GRAPH_VERSION,
            "root": str(self.root),
            "parse_status": dict(cached_s["parse_status"]),
            "skipped": len(self._skipped),
        }
        if self._graph:
            result["graph_nodes"] = len(self._graph.nodes)
            result["graph_edges"] = len(self._graph.edges)
        result.update(self._ledger.get_stats())
        return result

    def _parse_status_counts(self) -> dict:
        counts = {"ok": 0, "partial": 0, "unsupported": 0}
        for info in self._index.values():
            status = info.get("parse_status", "ok")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def health(self) -> dict:
        self._ensure_fresh()
        self._memory._ensure_fresh()
        per_language: dict[str, int] = {}
        partial_files: list[str] = []
        unsupported_files: list[str] = []
        for rel, info in self._index.items():
            ext = Path(rel).suffix.lower()
            per_language[ext] = per_language.get(ext, 0) + 1
            status = info.get("parse_status", "ok")
            if status == "partial":
                partial_files.append(rel)
            elif status == "unsupported":
                unsupported_files.append(rel)
        return {
            "files_indexed": len(self._index),
            "parse_status": self._parse_status_counts(),
            "per_language": dict(sorted(per_language.items())),
            "partial_files": sorted(partial_files)[:50],
            "unsupported_files": sorted(unsupported_files)[:50],
            "skipped": dict(sorted(self._skipped.items())[:50]),
            "graph_nodes": len(self._graph.nodes) if self._graph else 0,
            "graph_edges": len(self._graph.edges) if self._graph else 0,
            "memories_count": len(self._memory._notes),
        }

    def compress_command_output(self, raw_output: str, max_lines: int = 80) -> dict:
        """Graph-Aware Command, Test and Log Output Compressor (6D Traceback Resolution)."""
        return _runner_compress_command_output(self, raw_output, max_lines=max_lines)

    def run_command_compressed(self, command: str | list[str], timeout: int = 120, max_lines: int = 80) -> dict:
        """Execute a command in `self.root` and compress stdout/stderr with 6D traceback resolution."""
        return _runner_run_command_compressed(self, command, timeout=timeout, max_lines=max_lines)

    def _load_gitignore_rules(self, gitignore_file: Path, base_rel: str) -> list[tuple[bool, bool, str, re.Pattern]]:
        from .runner import load_gitignore_rules
        cache = getattr(self, "_gitignore_cache", None)
        if cache is None:
            cache = {}
            self._gitignore_cache = cache
        return load_gitignore_rules(cache, gitignore_file, base_rel)

    @staticmethod
    def _matches_gitignore(rel_posix: str, is_dir: bool, rules: list[tuple[bool, bool, str, re.Pattern]]) -> bool:
        from .runner import matches_gitignore
        return matches_gitignore(rel_posix, is_dir, rules)

    def _walk_entries(self) -> list[tuple[str, str, float]]:
        """Fast os.scandir traversal returning (rel_posix, abs_path_str, mtime)."""
        gi_cache: dict[tuple[str, float, str], list[tuple[bool, bool, str, re.Pattern]]] = getattr(
            self, "_gi_rules_cache", {}
        )
        self._gi_rules_cache = gi_cache
        union_cache: dict[tuple, tuple[re.Pattern | None, re.Pattern | None] | None] = getattr(
            self, "_gi_union_cache", {}
        )
        self._gi_union_cache = union_cache
        eval_cache: dict[tuple[tuple, str, bool], bool] = getattr(self, "_gi_eval_cache", {})
        if len(eval_cache) > 100_000:
            eval_cache.clear()
        self._gi_eval_cache = eval_cache

        def _get_cached_gi(gi_path: Path, mtime: float, base_rel: str):
            key = (str(gi_path), mtime, base_rel)
            if key not in gi_cache:
                gi_cache[key] = self._load_gitignore_rules(gi_path, base_rel)
            return gi_cache[key]

        def _get_union_rxs(
            rules_key: tuple,
            active_rules: list[tuple[bool, bool, str, re.Pattern]],
        ) -> tuple[re.Pattern | None, re.Pattern | None] | None:
            if not active_rules:
                return (None, None)
            if rules_key in union_cache:
                return union_cache[rules_key]
            if any(r[0] or r[2] for r in active_rules):
                union_cache[rules_key] = None
                return None
            dir_pats = [r[3].pattern for r in active_rules]
            file_pats = [r[3].pattern for r in active_rules if not r[1]]
            try:
                d_rx = re.compile("|".join(f"(?:{p})" for p in dir_pats)) if dir_pats else None
                f_rx = re.compile("|".join(f"(?:{p})" for p in file_pats)) if file_pats else None
                res_u = (d_rx, f_rx)
            except re.error:
                res_u = None
            union_cache[rules_key] = res_u
            return res_u

        root_rules: list[tuple[bool, bool, str, re.Pattern]] = []
        root_key_parts: list[tuple[str, float]] = []
        root_gi = self.root / ".gitignore"
        try:
            st = root_gi.stat()
            root_rules.extend(_get_cached_gi(root_gi, st.st_mtime, ""))
            root_key_parts.append((str(root_gi), st.st_mtime))
        except OSError:
            pass
        git_exclude = self.root / ".git" / "info" / "exclude"
        try:
            st = git_exclude.stat()
            root_rules.extend(_get_cached_gi(git_exclude, st.st_mtime, ""))
            root_key_parts.append((str(git_exclude), st.st_mtime))
        except OSError:
            pass

        skip_suffixes_tuple = tuple(SKIP_SUFFIXES)
        results: list[tuple[str, str, float]] = []

        def _scan_dir(
            dir_abs: str,
            rel_dir: str,
            active_rules: list[tuple[bool, bool, str, re.Pattern]],
            rules_key: tuple[tuple[str, float], ...],
        ) -> None:
            try:
                with os.scandir(dir_abs) as it:
                    entries = list(it)
            except OSError:
                return

            dir_entries: list[os.DirEntry] = []
            file_entries: list[os.DirEntry] = []
            gi_entry: os.DirEntry | None = None

            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dir_entries.append(entry)
                    elif entry.is_file(follow_symlinks=False):
                        file_entries.append(entry)
                        if rel_dir and entry.name == ".gitignore":
                            gi_entry = entry
                except OSError:
                    continue

            if gi_entry is not None:
                try:
                    gi_mtime = gi_entry.stat(follow_symlinks=False).st_mtime
                    extra = _get_cached_gi(Path(gi_entry.path), gi_mtime, rel_dir)
                    if extra:
                        active_rules = list(active_rules) + extra
                        rules_key = rules_key + ((gi_entry.path, gi_mtime),)
                except OSError:
                    pass

            has_negations = any(r[0] for r in active_rules)
            union_pair = _get_union_rxs(rules_key, active_rules)
            dir_entries.sort(key=lambda e: e.name)
            file_entries.sort(key=lambda e: e.name)

            kept_dirs: list[tuple[str, str]] = []
            for d_entry in dir_entries:
                d = d_entry.name
                if d in SKIP_DIRS or d.startswith((".pytest", "pytest-of-", ".mypy", ".ruff", ".tox", ".nox")) or d == "target":
                    continue
                if d == "projectbrain" and os.path.exists(
                    os.path.join(d_entry.path, "src", "projectbrain", "brain.py")
                ):
                    continue
                d_rel = f"{rel_dir}/{d}" if rel_dir else d
                if active_rules and not has_negations:
                    ckey = (rules_key, d_rel, True)
                    ignored = eval_cache.get(ckey)
                    if ignored is None:
                        if union_pair is not None:
                            d_rx = union_pair[0]
                            ignored = bool(d_rx and d_rx.search(d_rel))
                        else:
                            ignored = self._matches_gitignore(d_rel, True, active_rules)
                        eval_cache[ckey] = ignored
                    if ignored:
                        continue
                kept_dirs.append((d_entry.path, d_rel))

            for f_entry in file_entries:
                fname = f_entry.name
                if fname in SKIP_FILES or fname.endswith(skip_suffixes_tuple):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in PARSED_EXTS:
                    continue
                f_rel = f"{rel_dir}/{fname}" if rel_dir else fname
                if active_rules:
                    ckey = (rules_key, f_rel, False)
                    ignored = eval_cache.get(ckey)
                    if ignored is None:
                        if union_pair is not None:
                            f_rx = union_pair[1]
                            ignored = bool(f_rx and f_rx.search(f_rel))
                        else:
                            ignored = self._matches_gitignore(f_rel, False, active_rules)
                        eval_cache[ckey] = ignored
                    if ignored:
                        continue
                try:
                    mtime = f_entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                results.append((f_rel, f_entry.path, mtime))

            for sub_abs, sub_rel in kept_dirs:
                _scan_dir(sub_abs, sub_rel, active_rules, rules_key)

        _scan_dir(str(self.root), "", root_rules, tuple(root_key_parts))
        return results

    def _walk(self):
        for _, abs_path, _ in self._walk_entries():
            yield Path(abs_path)


def _is_minified(source: str) -> bool:
    return _is_minified_src(source, MINIFIED_LINE_THRESHOLD)


_TEST_QUERY_TERMS = {
    "test", "tests", "testing", "spec", "specs", "pytest",
    "unittest", "jest", "vitest", "mock", "fixture", "fixtures",
    "benchmark", "benchmarks",
}


def _is_test_file(rel_path: str) -> bool:
    p = Path(rel_path)
    parts_low = {seg.lower() for seg in p.parts[:-1]}
    if parts_low & {"tests", "test", "testing", "__tests__", "spec", "specs", "e2e", "benchmarks", "benchmark", "eval", "evals"}:
        return True
    name_low = p.name.lower()
    stem_low = p.stem.lower()
    return (
        name_low.endswith(("_test.go", "_test.py", "_test.rs", "test.java", "tests.java"))
        or stem_low.endswith((".test", ".spec", "_test", "_spec"))
        or stem_low.startswith(("test_", "spec_"))
    )


def _path_multiplier(rel_path: str, query_wants_tests: bool = False) -> float:
    if not query_wants_tests and _is_test_file(rel_path):
        return 0.3
    parts = {p.lower() for p in Path(rel_path).parts[:-1]}
    if parts & DEPRIORITY_DIRS:
        return 0.3
    return 1.0


_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SNAKE_SPLIT_RE = re.compile(r"[_\-.]")
_SEG_SPLIT_RE = re.compile(r"[/._\-]")
_WORD_TOK_RE = re.compile(r"[a-z0-9_]+")
_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_TOKENIZE_CACHE: dict[str, list[str]] = {}


def _tokenize_symbol(name: str) -> list[str]:
    cached = _TOKENIZE_CACHE.get(name)
    if cached is not None:
        return cached
    if len(_TOKENIZE_CACHE) > 100_000:
        _TOKENIZE_CACHE.clear()
    parts = _SNAKE_SPLIT_RE.split(name)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.islower() or part.isupper():
            p_low = part.lower()
            if len(p_low) > 1:
                tokens.append(p_low)
                if len(p_low) >= 7:
                    for sfx in _COMPOUND_SUFFIXES:
                        if p_low.endswith(sfx) and len(p_low) - len(sfx) >= 3:
                            pref = p_low[: -len(sfx)]
                            sfx_s = sfx[:-1] if sfx.endswith("s") else sfx
                            tokens.append(pref)
                            tokens.append(sfx_s)
                            break
        else:
            for s in _CAMEL_SPLIT_RE.sub(" ", part).split():
                if len(s) > 1:
                    tokens.append(s.lower())
    _TOKENIZE_CACHE[name] = tokens
    return tokens


_COMPOUND_SUFFIXES = (
    "handlers", "handler", "contexts", "context", "requests", "request",
    "responses", "response", "dispatchers", "dispatcher", "routers", "router",
    "loaders", "loader", "parsers", "parser", "builders", "builder",
    "wrappers", "wrapper", "helpers", "helper", "managers", "manager",
    "callbacks", "callback", "listeners", "listener", "middlewares", "middleware",
    "validators", "validator", "decorators", "decorator", "exceptions", "exception",
    "blueprints", "blueprint", "sessions", "session", "globals", "global",
    "signals", "signal", "views", "view", "clients", "client", "runners", "runner",
    "commands", "command", "files", "file", "pools", "pool", "headers", "header",
)


def _extract_keywords(prompt: str) -> list[str]:
    norm_prompt = re.sub(r"\bcommand[\s\-]+line\b", "cli", prompt.lower())
    tokens = re.findall(r"[a-zA-Z0-9_/.-]+", norm_prompt)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if "-" in token:
            expanded.extend(token.split("-"))
        if "." in token and not token.startswith("."):
            expanded.extend(p for p in token.split(".") if len(p) >= 2)
        if len(token) >= 7 and "_" not in token and "/" not in token and "." not in token:
            for sfx in _COMPOUND_SUFFIXES:
                if token.endswith(sfx) and len(token) - len(sfx) >= 3:
                    prefix = token[: -len(sfx)]
                    sfx_sing = sfx[:-1] if sfx.endswith("s") else sfx
                    expanded.append(f"{prefix}_{sfx_sing}")
                    expanded.append(prefix)
                    expanded.append(sfx)
                    if sfx_sing != sfx:
                        expanded.append(sfx_sing)
                    break
    seen: set[str] = set()
    result: list[str] = []
    for w in expanded:
        if w not in seen and w not in STOPWORDS and len(w) > 1:
            seen.add(w)
            result.append(w)
    return result
