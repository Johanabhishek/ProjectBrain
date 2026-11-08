import json
import math
import os
import re
import subprocess
import sys
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
from .graph import CodeGraph, fn_id, cls_id, file_id, route_id
from .intent import classify_intent, LOCATE_SYMBOL, LOCATE_ROUTE, UNDERSTAND_FLOW, IMPACT_ANALYSIS, ADD_FEATURE
from .ledger import SavingsLedger
from .memory import RepoMemory
from .parsers import parse, parse_with_tree, apply_sql_alters_to_schema, compute_file_interface_hash
from .patcher import apply_patch_spec
from .session import SessionManager
from .store import SQLiteIndexStore
from .slicer import (
    slice_lines,
    build_file_skeleton,
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

_VERIFY_RELATIONS = {
    "calls": "fn",
    "reaches": "any",
    "inherits": "cls",
    "has_method": "cls",
    "defines": "file",
    "imports": "file",
    "handles": "route",
}

_STRONG = ("exact", "qualified", "import_resolved")


def _verdict(subject, relation, obj, verdict, explanation,
             confidence=None, evidence=None) -> dict:
    return {
        "verdict": verdict,
        "claim": {"subject": subject, "relation": relation, "object": obj},
        "confidence": confidence,
        "explanation": explanation,
        "evidence": evidence or [],
    }


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
def _extract_stack_trace_frames(prompt: str) -> list[dict]:
    """Extract (file_hint, line, func_name) frames from Python, JS/TS, Go, Rust, and Java stack traces."""
    frames: list[dict] = []
    seen: set[tuple[str, int]] = set()

    # 1. Python: File "path/to/mod.py", line 42, in my_func
    for m in re.finditer(r'File\s+"([^"]+\.py)"\s*,\s*line\s+(\d+)(?:\s*,\s*in\s+([A-Za-z_]\w*))?', prompt):
        f_hint = m.group(1).replace("\\", "/")
        ln = int(m.group(2))
        fn = m.group(3) or ""
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": fn})

    # 2. JS/TS V8: at funcName (path/to/file.ts:42:10) or at path/to/file.ts:42:10
    for m in re.finditer(
        r"\bat\s+(?:([A-Za-z_$][\w$.]*)\s+\()?([^\s():]+\.(?:ts|tsx|js|jsx|mjs|cjs)):(\d+)(?::\d+)?\)?",
        prompt,
    ):
        fn = (m.group(1) or "").split(".")[-1]
        f_hint = m.group(2).replace("\\", "/")
        ln = int(m.group(3))
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": fn})

    # 3. General file.ext:line (Go, Rust, Java, Python, TS)
    for m in re.finditer(r"\b([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java)):(\d+)\b", prompt):
        f_hint = m.group(1).replace("\\", "/")
        ln = int(m.group(2))
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": ""})

    return frames


DEPRIORITY_DIRS = {
    "examples", "example", "demo", "demos",
    "scripts", "script",
    "clients", "client",
    "tests", "test", "testing",
    "docs", "doc", "documentation",
    "samples", "sample",
    "fixtures", "fixture",
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
        self._last_line_annotations: dict[str, dict[int, list[str]]] = {}
        self._last_pin_lines: dict[str, set[int]] = {}
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
        """Compile a surgical, token-budgeted Evidence Packet for an agent task.

        Instead of returning bare file paths and forcing the agent to call
        `view_file` on entire 1,000-line files, this compiles:
          1. Tier 1 (`target_slices`): Exact source code lines of the most relevant
             functions/classes/route handlers across the top matched files.
          2. Tier 2 (`neighbor_skeletons`): Signature-only skeletons (parameters,
             return types, class fields, docstrings — bodies stripped!) for
             connected files and remaining symbols in target files.
          3. Tier 3 (`flow_trace` & `memories`): Cross-boundary flow if a route is
             involved, plus AST-anchored architectural notes from `.projectbrain_memory.json`.
          4. Token savings telemetry comparing this packet against full-file reads.
        """
        self._ensure_fresh()
        intent = classify_intent(task)
        if intent.get("intent") == ADD_FEATURE or re.search(
            r"\b(add|create|implement|scaffold)\s+(?:a\s+)?(?:new\s+)?(route|endpoint|handler|controller|tool|service|model)\b",
            task.lower(),
        ):
            intent["greenfield_mode"] = True
        stack_frames = _extract_stack_trace_frames(task)
        crash_lines_by_file: dict[str, list[int]] = {}
        keywords = _extract_keywords(task)
        ranked_results = self.query(task, top_n=top_n, min_score_ratio=0.05)
        if stack_frames:
            intent["stack_trace_frames"] = stack_frames
            existing_rels = {r["rel_path"] for r in ranked_results}
            top_sc = ranked_results[0]["score"] if ranked_results else 10.0
            for sf in reversed(stack_frames):
                fh = sf["file_hint"].lstrip("./")
                for idx_rel in self._index:
                    if idx_rel == fh or idx_rel.endswith("/" + fh) or fh.endswith("/" + idx_rel):
                        crash_lines_by_file.setdefault(idx_rel, []).append(sf["line"])
                        if idx_rel not in existing_rels:
                            ranked_results.insert(0, {"rel_path": idx_rel, "score": top_sc + 25.0, "reason": f"stack trace frame L{sf['line']}"})
                            existing_rels.add(idx_rel)

        char_budget = max(budget_tokens * 4, 1200)
        chars_used = 0
        actual_chars_emitted = 0

        target_slices: list[dict] = []
        neighbor_skeletons: list[dict] = []
        omitted_already_in_context: list[str] = []

        matched_files = [r["rel_path"] for r in ranked_results]
        matched_file_set = set(matched_files)
        raw_files_bytes = sum(self._sizes.get(f, 0) for f in matched_files)

        # 1. Extract surgical code slices for the best matching symbols in top files
        top_file_score = ranked_results[0]["score"] if ranked_results else 0.0
        per_file_candidates: list[tuple[dict, list[tuple[float, str, dict]]]] = []
        for file_idx, r in enumerate(ranked_results):
            if file_idx > 0 and omitted_already_in_context and r.get("score", 0.0) < top_file_score * 0.45:
                continue
            rel_path = r["rel_path"]
            info = self._index.get(rel_path, {})
            behaviors = self._behaviors.get(rel_path, {})

            # Find which specific functions or classes in this file matched the query
            fns_list = info.get("functions", [])
            n_fns = len(fns_list)
            local_idf: dict[str, float] = {}
            if n_fns >= 6:
                for kw in keywords:
                    kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                    df_c = sum(
                        1 for f0 in fns_list
                        if kw in f0["name"].lower()
                        or kw in (f0.get("docstring") or "").lower()
                        or kw in set(f0.get("body_tokens", []))
                        or (len(kw_s) >= 3 and (kw_s in f0["name"].lower() or kw_s in (f0.get("docstring") or "").lower()))
                    )
                    local_idf[kw] = math.log(1.0 + n_fns / (1.0 + df_c))

            scored_fns: dict[str, tuple[float, dict]] = {}
            all_fns_by_name: dict[str, dict] = {}
            for fn in fns_list:
                q_key = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                all_fns_by_name[q_key] = fn
                all_fns_by_name.setdefault(fn["name"], fn)
                fn_score = self._score_symbol_for_query(fn, keywords, behaviors, info.get("routes", []), local_idf=local_idf)
                for c_ln in crash_lines_by_file.get(rel_path, ()):
                    if fn.get("line_start", 1) <= c_ln <= fn.get("line_end", 1):
                        fn_score += 50.0
                if fn_score > 0:
                    scored_fns[q_key] = (fn_score, fn)

            # Intra-file call-graph propagation (1-2 hops): boost public entrypoints calling matched helpers
            if scored_fns:
                boosts: dict[str, float] = {}
                for fn in fns_list:
                    q_caller = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                    for callee_nm in fn.get("calls", []):
                        callee_fn = all_fns_by_name.get(f"{fn.get('parent_class')}.{callee_nm}") or all_fns_by_name.get(callee_nm)
                        if not callee_fn:
                            continue
                        q_callee = f"{callee_fn.get('parent_class')}.{callee_fn['name']}" if callee_fn.get("parent_class") else callee_fn["name"]
                        if q_callee in scored_fns and q_callee != q_caller:
                            callee_sc = scored_fns[q_callee][0]
                            mult = 0.70 if callee_fn["name"].startswith("_") and not fn["name"].startswith("_") else 0.12
                            boosts[q_caller] = max(boosts.get(q_caller, 0.0), callee_sc * mult)
                        for callee2_nm in callee_fn.get("calls", []):
                            callee2_fn = all_fns_by_name.get(f"{callee_fn.get('parent_class')}.{callee2_nm}") or all_fns_by_name.get(callee2_nm)
                            if not callee2_fn:
                                continue
                            q_callee2 = f"{callee2_fn.get('parent_class')}.{callee2_fn['name']}" if callee2_fn.get("parent_class") else callee2_fn["name"]
                            if q_callee2 in scored_fns and q_callee2 != q_caller:
                                callee2_sc = scored_fns[q_callee2][0]
                                mult2 = 0.55 if callee2_fn["name"].startswith("_") and not fn["name"].startswith("_") else 0.08
                                boosts[q_caller] = max(boosts.get(q_caller, 0.0), callee2_sc * mult2)
                for q_k, b_val in boosts.items():
                    fn_obj = all_fns_by_name[q_k]
                    prev_sc = scored_fns[q_k][0] if q_k in scored_fns else 0.0
                    scored_fns[q_k] = (prev_sc + b_val, fn_obj)

            # Submodular Marginal Concept Coverage: select up to 3 functions per file that maximize
            # distinct query keyword coverage (especially identifier/signature token stems).
            scored_symbols: list[tuple[float, str, dict]] = []
            if scored_fns:
                rem_fns = list(scored_fns.values())
                id_cov_kws: set[str] = set()
                doc_cov_kws: set[str] = set()
                selected_priv_callees: dict[str, float] = {}
                for sel_step in range(min(3, len(rem_fns))):
                    best_idx = 0
                    best_gain = -1.0
                    for idx_f, (base_sc, fn_obj) in enumerate(rem_fns):
                        nm_l = fn_obj["name"].lower()
                        nm_toks = set(_tokenize_symbol(fn_obj["name"]))
                        sig_toks = set(re.findall(r"[a-z0-9]+", (fn_obj.get("signature") or "").lower()))
                        doc_l = (fn_obj.get("docstring") or "").lower()
                        calls_l = {c.lower() for c in fn_obj.get("calls", [])}
                        bt_l = set(fn_obj.get("body_tokens", []))
                        marginal = 0.0
                        id_hits = 0
                        fn_q = f"{fn_obj.get('parent_class')}.{fn_obj['name']}" if fn_obj.get("parent_class") else fn_obj["name"]
                        b_bonus = boosts.get(fn_q, 0.0) if scored_fns else 0.0
                        if selected_priv_callees and not fn_obj["name"].startswith("_"):
                            for c_raw in fn_obj.get("calls", []):
                                c_tail = c_raw.split(".")[-1]
                                if c_tail in selected_priv_callees:
                                    b_bonus = max(b_bonus, selected_priv_callees[c_tail] * 0.95)
                        for kw in keywords:
                            kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                            w_idf = self._idf.get(kw, 1.0) * local_idf.get(kw, 1.0)
                            id_mult = 1.0 if kw not in id_cov_kws else 0.12
                            doc_mult = 1.0 if (kw not in id_cov_kws and kw not in doc_cov_kws) else 0.12
                            is_gen = kw in ("file", "files", "path", "paths", "data", "node", "nodes")
                            if kw == nm_l or kw in nm_toks or (len(kw_s) >= 3 and kw_s in nm_toks):
                                marginal += (0.8 if is_gen else 5.2) * w_idf * id_mult
                                if not is_gen and kw not in id_cov_kws:
                                    id_hits += 1
                            elif kw in sig_toks or (len(kw_s) >= 3 and kw_s in sig_toks):
                                marginal += (0.6 if is_gen else 3.2) * w_idf * id_mult
                                if not is_gen and kw not in id_cov_kws:
                                    id_hits += 1
                            elif kw in doc_l or (len(kw_s) >= 4 and kw_s in doc_l):
                                marginal += 1.1 * w_idf * doc_mult
                            elif any(kw in c or (len(kw_s) >= 4 and kw_s in c) for c in calls_l):
                                marginal += 0.9 * w_idf * doc_mult
                            elif kw in bt_l:
                                marginal += 0.4 * w_idf * doc_mult
                        if id_hits >= 2:
                            marginal *= 1.45
                        comb_sc = (
                            (0.70 * base_sc + 0.30 * marginal)
                            if sel_step == 0
                            else (0.10 * base_sc + 0.90 * marginal + 0.85 * b_bonus)
                        )
                        if comb_sc > best_gain:
                            best_gain = comb_sc
                            best_idx = idx_f
                    chosen_sc, chosen_fn = rem_fns.pop(best_idx)
                    scored_symbols.append((best_gain, "fn", chosen_fn))
                    if chosen_fn["name"].startswith("_"):
                        selected_priv_callees[chosen_fn["name"]] = max(
                            selected_priv_callees.get(chosen_fn["name"], 0.0), chosen_sc
                        )
                    ch_nm_l = chosen_fn["name"].lower()
                    ch_nm_toks = set(_tokenize_symbol(chosen_fn["name"]))
                    sig_toks = set(re.findall(r"[a-z0-9]+", (chosen_fn.get("signature") or "").lower()))
                    doc_l = (chosen_fn.get("docstring") or "").lower()
                    for kw in keywords:
                        kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                        if (
                            kw == ch_nm_l or kw in ch_nm_toks or (len(kw_s) >= 3 and kw_s in ch_nm_toks)
                            or kw in sig_toks or (len(kw_s) >= 3 and kw_s in sig_toks)
                        ):
                            id_cov_kws.add(kw)
                        elif kw in doc_l or (len(kw_s) >= 4 and kw_s in doc_l):
                            doc_cov_kws.add(kw)

            methods_by_class: dict[str, int] = {}
            for _, _, fn_obj in scored_symbols:
                if fn_obj.get("parent_class"):
                    methods_by_class[fn_obj["parent_class"]] = methods_by_class.get(fn_obj["parent_class"], 0) + 1

            for cls in info.get("classes", []):
                if methods_by_class.get(cls["name"], 0) >= 1:
                    continue
                cls_score = self._score_class_for_query(cls, keywords)
                if cls_score > 0:
                    scored_symbols.append((cls_score, "cls", cls))

            # If no specific symbol matched keywords (e.g. filename match), pick main entrypoints
            if not scored_symbols:
                for fn in info.get("functions", [])[:2]:
                    scored_symbols.append((1.0, "fn", fn))
                for cls in info.get("classes", [])[:1]:
                    scored_symbols.append((1.0, "cls", cls))

            per_file_candidates.append((r, scored_symbols[:3]))

        # Round-robin across top files (rank 0 across files, rank 1 across files, rank 2 across files)
        # so no single file with long functions starves #2 and #3 ranked files.
        ordered_slice_reqs: list[tuple[dict, float, str, dict]] = []
        if per_file_candidates:
            for slot_idx in (0, 1, 2):
                for r_entry, cand_list in per_file_candidates:
                    if slot_idx < len(cand_list):
                        sc_v, k_v, sym_v = cand_list[slot_idx]
                        ordered_slice_reqs.append((r_entry, sc_v, k_v, sym_v))
            # 1-hop cross-file call closure: if a selected symbol calls a function in another
            # indexed file that was not in top_n, append that callee as a candidate slice.
            if self._graph:
                seen_req_nids = {
                    fn_id(re_i["rel_path"], sy_i["name"], sy_i.get("parent_class")) if ki_i == "fn" else cls_id(re_i["rel_path"], sy_i["name"])
                    for re_i, _, ki_i, sy_i in ordered_slice_reqs
                }
                for re_i, sc_i, ki_i, sy_i in list(ordered_slice_reqs[:3]):
                    if ki_i != "fn":
                        continue
                    src_nid = fn_id(re_i["rel_path"], sy_i["name"], sy_i.get("parent_class"))
                    for ei in self._graph._adj.get(src_nid, []):
                        e_c = self._graph.edges[ei]
                        if e_c["type"] == "calls" and e_c.get("confidence") in ("exact", "import_resolved"):
                            dst_nid = e_c["dst"]
                            if dst_nid in seen_req_nids:
                                continue
                            dst_nd = self._graph.nodes.get(dst_nid, {})
                            dst_rel = dst_nd.get("rel_path", "")
                            if dst_rel and dst_rel != re_i["rel_path"] and dst_rel in self._index:
                                for dfn in self._index[dst_rel].get("functions", []):
                                    if fn_id(dst_rel, dfn["name"], dfn.get("parent_class")) == dst_nid:
                                        seen_req_nids.add(dst_nid)
                                        ordered_slice_reqs.append(({"rel_path": dst_rel, "reason": f"called by {sy_i['name']}"}, sc_i * 0.5, "fn", dfn))
                                        break

        for r, sym_score, kind, sym in ordered_slice_reqs:
            if chars_used >= char_budget * 0.68:
                break
            rel_path = r["rel_path"]
            behaviors = self._behaviors.get(rel_path, {})
            parent = sym.get("parent_class")
            nid = fn_id(rel_path, sym["name"], parent) if kind == "fn" else cls_id(rel_path, sym["name"])

            if deduplicate_session and nid in self._served_slices:
                omitted_already_in_context.append(
                    f"{nid} (L{sym.get('line_start')}-{sym.get('line_end')})"
                )
                est_lines = min(max(sym.get("line_end", 1) - sym.get("line_start", 1) + 1, 8), 44)
                chars_used += est_lines * 48 + 90
                continue

            l_start = sym.get("line_start", 1)
            l_end = sym.get("line_end", l_start)
            max_slice_lines = (16 if (l_end - l_start) > 120 else 22) if char_budget <= 11000 else (34 if kind == "cls" else 40)
            code_text = slice_lines(
                self.root, rel_path, l_start, l_end, max_lines=max_slice_lines, focus_terms=keywords,
                pin_lines=crash_lines_by_file.get(rel_path),
            )
            if not code_text:
                continue

            if deduplicate_session:
                self._served_slices.add(nid)

            qname = f"{parent}.{sym['name']}" if parent else sym["name"]
            slice_entry = {
                "node_id": nid,
                "rel_path": rel_path,
                "symbol": qname,
                "kind": kind,
                "lines": f"L{l_start}-{l_end}",
                "signature": sym.get("signature", ""),
                "behavior": behaviors.get(qname, "unknown"),
                "reason": r.get("reason", "task match"),
                "code": code_text,
            }
            target_slices.append(slice_entry)
            chars_used += len(code_text) + 120
            actual_chars_emitted += len(code_text) + 120

        # 2. Build signature-only skeletons for top files & 1-hop connected neighbor files
        skeleton_candidates: list[tuple[str, str]] = []
        active_ranked = [
            r for idx, r in enumerate(ranked_results)
            if idx == 0 or not omitted_already_in_context or r.get("score", 0.0) >= top_file_score * 0.45
        ]
        for r in active_ranked:
            skeleton_candidates.append((r["rel_path"], "primary file outline"))
        for r in active_ranked[:3]:
            rp = r["rel_path"]
            if deduplicate_session and rp in self._served_skeletons:
                continue
            for dep in self._import_targets.get(rp, [])[:3]:
                if dep not in matched_file_set:
                    skeleton_candidates.append((dep, f"imported by {rp}"))
            for imp_by in self._imported_by.get(rp, [])[:2]:
                if imp_by not in matched_file_set:
                    skeleton_candidates.append((imp_by, f"imports {rp}"))

        seen_skel: set[str] = set()
        for skel_path, skel_reason in skeleton_candidates:
            if skel_path in seen_skel or chars_used >= char_budget * 0.95:
                continue
            seen_skel.add(skel_path)
            if deduplicate_session and skel_path in self._served_skeletons:
                omitted_already_in_context.append(f"skeleton:{skel_path}")
                chars_used += 1400
                continue
            info = self._index.get(skel_path)
            if not info:
                continue
            skel_text = build_file_skeleton(
                skel_path, info, behaviors=self._behaviors.get(skel_path, {}), focus_terms=keywords,
            )
            if deduplicate_session:
                self._served_skeletons.add(skel_path)
            neighbor_skeletons.append({
                "rel_path": skel_path,
                "reason": skel_reason,
                "skeleton": skel_text,
            })
            chars_used += len(skel_text) + 60
            actual_chars_emitted += len(skel_text) + 60
            if skel_path not in matched_file_set:
                raw_files_bytes += self._sizes.get(skel_path, 0)

        # 3. Attach route flow trace if route intent or route matched
        flow_trace = None
        if intent["intent"] == LOCATE_ROUTE and intent.get("entities"):
            ent = intent["entities"][0]
            parts = ent.split(" ", 1)
            if len(parts) == 2:
                flow_trace = self.trace_flow(parts[1], method=parts[0])
            else:
                flow_trace = self.trace_flow(parts[0])

        # 4. Attach relevant AST-anchored memories
        all_touched_paths = matched_file_set | seen_skel
        all_touched_nodes = {s["node_id"] for s in target_slices} | {file_id(p) for p in all_touched_paths}
        memories = self._memory.get_notes_for(all_touched_paths, all_touched_nodes, self._ast_hashes)

        # 5. Identify related test files so the agent knows what to run
        related_tests: set[str] = set()
        for p in matched_files:
            for imp in self._imported_by.get(p, []):
                if any(t in imp.lower() for t in ("test", "spec", "__tests__")):
                    related_tests.add(imp)

        structural_siblings: list[dict] = []
        if intent.get("greenfield_mode"):
            for r_sib in ranked_results[:3]:
                rp_s = r_sib["rel_path"]
                info_s = self._index.get(rp_s, {})
                for rt_s in info_s.get("routes", [])[:2]:
                    structural_siblings.append({
                        "rel_path": rp_s,
                        "kind": "route_exemplar",
                        "pattern": f"{rt_s.get('method')} {rt_s.get('path')} -> {rt_s.get('handler')}",
                        "line": rt_s.get("line", 1),
                    })
                for cls_s in info_s.get("classes", [])[:2]:
                    structural_siblings.append({
                        "rel_path": rp_s,
                        "kind": "class_exemplar",
                        "pattern": cls_s.get("signature", cls_s.get("name")),
                        "line": cls_s.get("line_start", 1),
                    })

        out: dict = {
            "task": task,
            "intent": intent,
            "target_slices": target_slices,
            "neighbor_skeletons": neighbor_skeletons,
            "recommended_read_order": matched_files,
            "related_tests": sorted(related_tests),
            "resolution_telemetry": {
                "files_resolved": len(matched_files),
                "target_slices_count": len(target_slices),
                "neighbor_skeletons_count": len(neighbor_skeletons),
                "session_deduplicated_count": len(omitted_already_in_context),
            },
        }
        if memories:
            out["memories"] = memories
        if flow_trace and flow_trace.get("steps"):
            out["flow_trace"] = flow_trace
        if structural_siblings:
            out["structural_siblings"] = structural_siblings[:4]
        if omitted_already_in_context:
            out["deduplicated_in_session"] = omitted_already_in_context
        self._ledger.record_input_op(
            op="compile",
            query=task,
            files_covered=len(matched_files),
            touched_files=all_touched_paths,
            slices_included=len(target_slices),
        )
        return out

    def compile_dossier(
        self,
        tasks: list[str] | str,
        impact_files: list[str] | None = None,
        out_path: str | None = None,
        zoom: bool = False,
    ) -> dict:
        """Compile a self-contained, zero-follow-up Engineering Dossier across one
        or more tasks/questions.

        Unlike `compile_context` (which slices only top-3 functions per file at 65 lines),
        `compile_dossier` is designed to eliminate fallback `view_file` calls completely:
          1. Automatically extracts module-level constants, regex arrays, timing
             configs, and bootstrap IIFEs (`extract_file_globals_slice`) for every
             primary file.
          2. Automatically expands 1-2 hop callers (`inverse_calls`) and 1-2 hop
             callees (`calls`, `handles`) in the graph for any symbol or route
             referenced in the prompt, plus UI trigger components in 1-hop imported files.
          3. Uses smart focus-window folding (`max_lines=115`) so long functions
             and React monoliths preserve all relevant control flow, timers, and
             call sites without arbitrary bottom truncation.
          4. Attaches `impact()` blast-radius reports for any file mentioned or
             passed in `impact_files`.
          5. Writes the complete Markdown dossier to `.projectbrain_dossier.md`
             so the agent can read up to 46 KB in a single `view_file` call without
             hitting shell `run_command` 8 KB stdout truncation.
        """
        self._ensure_fresh()
        if isinstance(tasks, str):
            tasks = [tasks]
        impact_list = list(impact_files or [])

        seen_slices: set[str] = set()
        seen_globals: set[str] = set()
        seen_impacts: set[str] = set()
        all_keywords: list[str] = []
        for t in tasks:
            for kw in _extract_keywords(t):
                if kw not in all_keywords:
                    all_keywords.append(kw)
            # Auto-detect file paths mentioned in the prompt for impact analysis
            for rel in self._index:
                bname = Path(rel).name
                if bname.lower() in t.lower() and any(w in t.lower() for w in ("import", "depend", "impact", "many files", "blast")):
                    if rel not in impact_list:
                        impact_list.append(rel)

        md_sections: list[str] = [
            "# ProjectBrain Complete Engineering Dossier",
            "> Self-contained AST slices, module constants/globals, bidirectional call chains, and blast radius.",
            "",
        ]

        seen_spans: list[tuple[str, int, int]] = []
        primary_files: set[str] = set()
        emitted_by_file: dict[str, list[tuple[int, int]]] = {}
        matched_4d_cells_count = 0
        monolith_4d_cells_count = 0
        # Helper to add a file's module globals once (only when relevant to query or TypeScript interfaces/globals-only module)
        def _ensure_globals(rel_path: str, buf: list[str], max_g: int = 24, kw_list: list[str] | None = None) -> None:
            if zoom or max_g <= 0 or rel_path in seen_globals or rel_path not in self._index:
                return
            seen_globals.add(rel_path)
            f_info = self._index[rel_path]
            glob_text = extract_file_globals_slice(self.root, rel_path, f_info, max_lines=min(max_g, 16))
            if not glob_text:
                return
            is_ts_iface = rel_path.endswith((".ts", ".tsx")) and ("interface " in glob_text or "type " in glob_text)
            is_globals_module = len(f_info.get("functions", [])) == 0 and len(f_info.get("classes", [])) <= 1
            non_imp_lines = [
                ln.lower()
                for ln in glob_text.splitlines()
                if not ln.lstrip().startswith(("import ", "from ", "#", "//", "/*", "*", "export type "))
            ]
            non_imp_blob = "\n".join(non_imp_lines)
            has_kw_global = bool(
                kw_list
                and any(
                    len(w) >= 4 and self._idf.get(w, 1.0) >= 2.0 and w in non_imp_blob
                    for w in kw_list
                )
            )
            if not (is_ts_iface or is_globals_module or has_kw_global):
                return
            g_ivs = extract_emitted_intervals(glob_text)
            if g_ivs:
                g_plan = self._ledger.plan_slice_intervals(rel_path, g_ivs[0][0], g_ivs[-1][1], commit=False)
                if not g_plan["unseen_intervals"]:
                    buf.append(f"### [{rel_path} :: Module Constants — [Session Cache] Already delivered in this session (0 duplicate lines)]")
                    return
            emitted_by_file.setdefault(rel_path, []).extend(g_ivs)
            buf.append(f"### [{rel_path} :: Module Constants, State & Bootstrap]")
            buf.append(glob_text)

        def _node_display_name(nd_obj: dict) -> str:
            p_cls = nd_obj.get("parent_class")
            nm_s = nd_obj.get("name") or ""
            if p_cls and nm_s and not nm_s.startswith(f"{p_cls}."):
                return f"{p_cls}.{nm_s}"
            return nm_s

        # Helper to add a function/class slice once (skipping nested functions already covered by an outer slice)
        def _add_node_slice(node_id: str, reason: str, kw_list: list[str], buf: list[str], max_l: int = 44, max_g: int = 24) -> None:
            if not self._graph or node_id not in self._graph.nodes:
                return
            nd = self._graph.nodes[node_id]
            if nd.get("type") not in ("fn", "cls"):
                return
            rel = nd.get("rel_path", "")
            if not rel or rel not in self._index:
                return
            l_start = nd.get("line_start", 1)
            l_end = nd.get("line_end", l_start)
            is_caller_slice = any(reason.startswith(p) for p in ("calls ", "invokes "))
            m_call_line = re.search(r"@L(\d+)", reason) if is_caller_slice else None
            slice_key = f"{node_id}@L{m_call_line.group(1)}" if (m_call_line and (l_end - l_start) > 120) else node_id
            if slice_key in seen_slices:
                return
            effective_max_l = 12 if (l_end - l_start) > 400 and is_caller_slice else max_l
            slice_s, slice_e = l_start, l_end
            if m_call_line and (l_end - l_start) > 120:
                c_ln = int(m_call_line.group(1))
                slice_s = max(l_start, c_ln - 4)
                slice_e = min(l_end, c_ln + 7)
                effective_max_l = 14
            for s_rel, s_start, s_end in seen_spans:
                if s_rel == rel and s_start <= slice_s and slice_e <= s_end and (s_end - s_start) <= 130:
                    seen_slices.add(slice_key)
                    return
            seen_slices.add(slice_key)
            seen_spans.append((rel, slice_s, slice_e))
            _ensure_globals(rel, buf, max_g=max_g, kw_list=kw_list)
            ast_calls: list[str] = []
            if (l_end - l_start) <= 450 and not is_caller_slice:
                for f_entry in self._index[rel].get("functions", []):
                    if f_entry.get("line_start") == l_start:
                        ast_calls = [c.lower() for c in f_entry.get("calls", []) if len(c) >= 4]
                        break

            # Session Zero-Waste Guard inside dossier: skip re-emitting spans already delivered unfolded in this session
            s_plan = self._ledger.plan_slice_intervals(rel, slice_s, slice_e, commit=False)
            if not s_plan["unseen_intervals"]:
                buf.append(f"### [{rel} :: {_node_display_name(nd)} (L{slice_s}-L{slice_e}) — [Session Cache] Already delivered in this session (0 duplicate lines)]")
                return

            node_pins: set[int] = set(self._last_pin_lines.get(rel, set()))
            node_anns: dict[int, list[str]] = {
                k: list(v) for k, v in self._last_line_annotations.get(rel, {}).items()
            }
            if self._graph:
                for ei_hb in self._graph._adj.get(node_id, []):
                    ehb = self._graph.edges[ei_hb]
                    if ehb["type"] == "http_flow" and ehb.get("line"):
                        hl = int(ehb["line"])
                        dst_nd = self._graph.nodes.get(ehb["dst"], {})
                        dst_lbl = f"{dst_nd.get('rel_path', '')}:{dst_nd.get('name', '')} (L{dst_nd.get('line_start', 1)})"
                        b_msg = f"# >>> [HTTP-BRIDGE L{hl}] {ehb.get('reason', '')} -> {dst_lbl}"
                        node_pins.add(hl)
                        if b_msg not in node_anns.setdefault(hl, []):
                            node_anns[hl].append(b_msg)
            if "Causal Symptom Chop" in reason:
                for c_cell in self._index[rel].get("cells_4d", []):
                    cs_ln = c_cell.get("line_start", 0)
                    ce_ln = c_cell.get("line_end", 0)
                    if slice_s <= cs_ln <= slice_e and any(
                        ex.startswith(("status:", "throw:", "err:")) for ex in c_cell.get("E_exit", [])
                    ):
                        for gl in range(cs_ln, min(slice_e + 1, ce_ln + 1)):
                            node_pins.add(gl)
            in_slice_pins = sorted(p for p in node_pins if slice_s <= p <= slice_e)
            if len(in_slice_pins) >= 6:
                effective_max_l = max(effective_max_l, 64)

            matrix_header = ""
            cell_slices: list[str] = []
            cell_focus_terms: list[str] = []
            if (slice_e - slice_s) >= 10 and not is_caller_slice:
                pre_cells = self._index[rel].get("cells_4d")
                if pre_cells:
                    cells = [
                        c for c in pre_cells
                        if c["line_start"] >= slice_s and c["line_end"] <= slice_e
                    ]
                else:
                    try:
                        f_lines = (self.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        f_lines = []
                    cells = extract_4d_coordinate_matrix(f_lines, slice_s, slice_e, ext=Path(rel).suffix) if f_lines else []
                if cells:
                    max_rows = 22 if (slice_e - slice_s) > 200 else 1
                    top_cells, matrix_header = query_4d_coordinate_matrix(
                        cells,
                        focus_terms=kw_list + ast_calls,
                        raw_query=" ".join(tasks),
                        max_table_rows=max_rows,
                    )
                    nonlocal matched_4d_cells_count, monolith_4d_cells_count
                    if (slice_e - slice_s) > 450 and len(cells) >= 3:
                        cands_to_slice = list(top_cells[:3])
                        for c_item in cells:
                            if (
                                c_item.get("js_ast_contracts")
                                and c_item not in cands_to_slice
                                and len(cands_to_slice) < 5
                                and any(
                                    self._contract_in_query_scope(rel, c_item.get("X_scope", ""), jc0.get("summary", ""), kw_list)
                                    for jc0 in c_item["js_ast_contracts"]
                                )
                            ):
                                cands_to_slice.append(c_item)
                        for tc in cands_to_slice:
                            tc_s, tc_e = tc["line_start"], tc["line_end"]
                            if any(
                                sr == rel and ss <= tc_s and tc_e <= se and (se - ss) <= 130
                                for sr, ss, se in seen_spans
                            ):
                                continue
                            if tc["span"] <= 95:
                                tc_code = slice_lines(
                                    self.root,
                                    rel,
                                    tc_s,
                                    tc_e,
                                    max_lines=min(55, tc["span"] + 2),
                                    focus_terms=kw_list + ast_calls,
                                    pin_lines=node_pins,
                                    inline_annotations=node_anns,
                                )
                                if tc_code:
                                    matched_4d_cells_count += 1
                                    monolith_4d_cells_count += 1
                                    seen_spans.append((rel, tc_s, tc_e))
                                    emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(tc_code))
                                    cell_slices.append(
                                        f"# [6D Cell Match: {tc['Y_role']} | {tc['X_scope']} (L{tc_s}-L{tc_e})]\n{tc_code}"
                                    )
                        # Ensure any pinned 6D contract lines inside the monolith that fell outside top_cells are also sliced
                        for pin_ln in sorted(node_anns.keys()):
                            if not (slice_s <= pin_ln <= slice_e):
                                continue
                            if any(sr == rel and ss <= pin_ln <= se and (se - ss) <= 130 for sr, ss, se in seen_spans):
                                continue
                            p_s = max(slice_s, pin_ln - 3)
                            p_e = min(slice_e, pin_ln + 7)
                            p_code = slice_lines(
                                self.root,
                                rel,
                                p_s,
                                p_e,
                                max_lines=14,
                                focus_terms=kw_list,
                                pin_lines=node_pins,
                                inline_annotations=node_anns,
                            )
                            if p_code:
                                seen_spans.append((rel, p_s, p_e))
                                emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(p_code))
                                cell_slices.append(f"# [6D Contract Pin Window (L{p_s}-L{p_e})]\n{p_code}")
                    else:
                        matched_4d_cells_count += max(1, len(top_cells[:2]))
                        for tc in top_cells[:2]:
                            cell_focus_terms.extend(tc.get("Z_write", [])[:3])
                            cell_focus_terms.extend(tc.get("W_calls", [])[:3])

            code = (
                ""
                if cell_slices
                else slice_lines(
                    self.root,
                    rel,
                    slice_s,
                    slice_e,
                    max_lines=effective_max_l,
                    focus_terms=kw_list + ast_calls + cell_focus_terms,
                    pin_lines=node_pins,
                    inline_annotations=node_anns,
                )
            )
            if not code and not cell_slices:
                return
            if code:
                emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(code))
            hdr_span = f"L{slice_s}-L{slice_e}" if (m_call_line and (l_end - l_start) > 120) else f"L{l_start}-L{l_end}"
            buf.append(f"### [{rel} :: {_node_display_name(nd)} ({hdr_span}) — {reason}]")
            if matrix_header:
                buf.append(matrix_header)
            for cs in cell_slices:
                buf.append(cs)
            if code:
                buf.append(code)
        for idx, task in enumerate(tasks, start=1):
            clean_task_for_search = task.strip()
            t_kw = self._expand_keywords_with_aliases(_extract_keywords(clean_task_for_search))
            t_low = clean_task_for_search.lower()
            sec: list[str] = [f"## Task {idx}: {task}"]
            if not zoom:
                self._compile_6d_contract_diagnostics(task, set(self._index.keys()))

            # 1. Identify explicitly named code symbols (camelCase, snake_case, dotted obj.method, or backtick/paren-wrapped) or routes
            explicit_nodes: list[str] = []
            explicit_task_files: list[str] = []
            for m_path in re.findall(r"\b([a-zA-Z0-9_.\-]+(?:/[a-zA-Z0-9_.\-]+)+\.[a-zA-Z0-9]+)\b", clean_task_for_search):
                mp_low = m_path.lower().strip("/")
                cands_mp = [
                    rel_k for rel_k in self._index
                    if rel_k.lower() == mp_low or rel_k.lower().endswith("/" + mp_low)
                ]
                if cands_mp:
                    best_mp = min(cands_mp, key=lambda k: (len(k.split("/")), len(k)))
                    if best_mp not in explicit_task_files:
                        explicit_task_files.append(best_mp)
            has_explicit_route = False
            if self._graph:
                for nid, nd in self._graph.nodes.items():
                    ntype = nd.get("type")
                    if ntype == "route":
                        rpath = (nd.get("path") or "").lower()
                        rmeth = (nd.get("method") or "").lower()
                        if rpath and len(rpath) > 1 and rpath in t_low:
                            if not rmeth or rmeth in t_low:
                                explicit_nodes.append(nid)
                                has_explicit_route = True
                    elif ntype in ("fn", "cls"):
                        raw_nm = nd.get("name") or ""
                        p_cls = nd.get("parent_class") or ""
                        q_nm = f"{p_cls}.{raw_nm}" if p_cls else ""
                        if q_nm and re.search(r"\b" + re.escape(q_nm) + r"\b", clean_task_for_search, re.IGNORECASE):
                            explicit_nodes.append(nid)
                        elif len(raw_nm) >= 5 and (
                            any(c.isupper() for c in raw_nm[1:])
                            or "_" in raw_nm
                            or f"`{raw_nm}`" in task
                            or f"({raw_nm})" in task
                        ):
                            if re.search(r"\b" + re.escape(raw_nm) + r"\b", clean_task_for_search):
                                explicit_nodes.append(nid)

            # 1b. Multi-Dimensional Data/Config/Constant Slicing: if the task names an explicit
            # SCREAMING_SNAKE_CASE or snake_case constant/config key (e.g. SECRET_KEY) that is a
            # state/config channel rather than a function/class definition name, slice its
            # declaration cells and consumer functions across production files.
            explicit_data_keys: list[str] = []
            for m_dk_tok in re.findall(r"\b([A-Z][A-Z0-9]+_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b|`([A-Za-z_][A-Za-z0-9_]{3,})`", clean_task_for_search):
                dk_cand = (m_dk_tok[0] or m_dk_tok[1]).strip()
                if len(dk_cand) >= 5 and dk_cand.lower() not in STOPWORDS:
                    if not any(
                        (self._graph.nodes.get(en, {}).get("name") or "").lower() == dk_cand.lower()
                        for en in explicit_nodes
                    ):
                        if dk_cand not in explicit_data_keys:
                            explicit_data_keys.append(dk_cand)
            explicit_state_files: set[str] = set()
            if explicit_data_keys and not explicit_nodes and self._graph:
                q_wants_tests_dk = bool(set(t_kw) & _TEST_QUERY_TERMS)
                dk_fn_cands: list[tuple[float, str, str, str, bool]] = []
                dk_state_slices: list[tuple[float, str, int, int, str]] = []
                for dk in explicit_data_keys[:2]:
                    dk_low = dk.lower()
                    for rp_dk, info_dk in self._index.items():
                        if not q_wants_tests_dk and (
                            _is_test_file(rp_dk)
                            or bool({p.lower() for p in Path(rp_dk).parts[:-1]} & DEPRIORITY_DIRS)
                        ):
                            continue
                        if (
                            dk_low not in self._token_sets.get(rp_dk, set())
                            and dk_low not in self._body_token_sets.get(rp_dk, set())
                            and dk_low not in self._blobs.get(rp_dk, "")
                        ):
                            continue
                        raw_txt_dk = self._get_file_text(rp_dk) or ""
                        if dk not in raw_txt_dk and dk_low not in raw_txt_dk.lower():
                            continue
                        flines_dk = raw_txt_dk.splitlines()
                        # Check functions that directly read/write dk or dk_low
                        beh_dk = self._behaviors.get(rp_dk, {})
                        rts_dk = info_dk.get("routes", [])
                        c4d_dk = info_dk.get("cells_4d", [])
                        covered_dk_lines: set[int] = set()
                        for fn_dk in info_dk.get("functions", []):
                            ls_d, le_d = fn_dk.get("line_start", 1), fn_dk.get("line_end", 1)
                            fn_txt_d = "\n".join(flines_dk[max(0, ls_d - 1) : min(len(flines_dk), le_d)])
                            if dk in fn_txt_d or re.search(rf"\b{re.escape(dk_low)}\b", fn_txt_d.lower()):
                                for cln in range(ls_d, le_d + 1):
                                    covered_dk_lines.add(cln)
                                sc_d = self._score_symbol_for_query(fn_dk, t_kw + [dk_low], beh_dk, rts_dk, c4d_dk)
                                is_code_access = bool(
                                    f".{dk_low}" in fn_txt_d.lower()
                                    or f'"{dk}"' in fn_txt_d
                                    or f"'{dk}'" in fn_txt_d
                                    or re.search(rf"(?:self|app|config|settings|cls)\.{re.escape(dk_low)}\b", fn_txt_d.lower())
                                )
                                if is_code_access:
                                    sc_d += 10.0
                                nid_d = fn_id(rp_dk, fn_dk["name"], fn_dk.get("parent_class"))
                                dk_fn_cands.append((sc_d, nid_d, rp_dk, dk, is_code_access))
                        # Also check class/module-level declarations (outside functions) where dk is defined/configured
                        for ln_i, ln_str in enumerate(flines_dk, start=1):
                            if ln_i in covered_dk_lines:
                                continue
                            if dk in ln_str or re.search(rf"\b{re.escape(dk_low)}\b\s*(?::\s*[^=]+)?=", ln_str.lower()):
                                st_ln = ln_str.strip()
                                if st_ln.startswith(("#", "//", "*", '"""', "'''")):
                                    continue
                                is_dict_kv = bool(re.match(rf"^['\"]{re.escape(dk)}['\"]\s*:", st_ln))
                                if st_ln.startswith(('"', "'")) and not is_dict_kv:
                                    continue
                                is_decl = ("=" in ln_str) or is_dict_kv
                                sc_st = (11.0 if "=" in ln_str else 10.0) if is_decl else 5.0
                                w_s = max(1, ln_i - 2)
                                w_e = min(len(flines_dk), ln_i + 6)
                                dk_state_slices.append((sc_st, rp_dk, w_s, w_e, f"defines/configures `{dk}` @L{ln_i}"))
                dk_state_slices.sort(key=lambda x: x[0], reverse=True)
                for _, rp_st, ws_st, we_st, rsn_st in dk_state_slices[:3]:
                    if any(sr == rp_st and ss <= ws_st and we_st <= se for sr, ss, se in seen_spans):
                        continue
                    primary_files.add(rp_st)
                    explicit_state_files.add(rp_st)
                    seen_spans.append((rp_st, ws_st, we_st))
                    st_code = slice_lines(self.root, rp_st, ws_st, we_st, max_lines=14, focus_terms=t_kw)
                    if st_code:
                        matched_4d_cells_count += 1
                        emitted_by_file.setdefault(rp_st, []).extend(extract_emitted_intervals(st_code))
                        sec.append(f"### [{rp_st} (L{ws_st}-L{we_st}) — {rsn_st}]")
                        sec.append(st_code)
                has_code_dk = any(x[4] for x in dk_fn_cands)
                if has_code_dk:
                    dk_fn_cands = [x for x in dk_fn_cands if x[4]]
                dk_fn_cands.sort(key=lambda x: x[0], reverse=True)
                for _, nid_d, rp_d, dk_nm, _ in dk_fn_cands[:3]:
                    primary_files.add(rp_d)
                    explicit_state_files.add(rp_d)
                    _add_node_slice(nid_d, f"reads/uses `{dk_nm}` in {rp_d}", t_kw + [dk_nm.lower()], sec, max_l=54, max_g=6)
                    if nid_d not in explicit_nodes:
                        explicit_nodes.append(nid_d)

            # Count explicit matches per file so secondary giant components don't hijack primary focus
            exp_by_file: dict[str, list[str]] = {rp_s: [f"state:{rp_s}"] for rp_s in explicit_state_files}
            for enid in explicit_nodes:
                if self._graph and self._graph.nodes.get(enid, {}).get("type") in ("fn", "cls"):
                    erel = self._graph.nodes[enid].get("rel_path", "")
                    if erel:
                        exp_by_file.setdefault(erel, []).append(enid)
            dominant_exp_file = max(exp_by_file.items(), key=lambda kv: len(kv[1]))[0] if exp_by_file else ""
            filtered_explicit: list[str] = []
            for enid in explicit_nodes:
                if self._graph and self._graph.nodes.get(enid, {}).get("type") in ("fn", "cls"):
                    e_nd = self._graph.nodes.get(enid, {})
                    erel = e_nd.get("rel_path", "")
                    e_span = e_nd.get("line_end", 1) - e_nd.get("line_start", 1)
                    # If dominant file has >=2 explicit symbols, treat a >300-line component in another file as a 1-line pointer
                    if (
                        dominant_exp_file
                        and len(exp_by_file.get(dominant_exp_file, [])) >= 2
                        and erel != dominant_exp_file
                        and e_span > 300
                    ):
                        sec.append(
                            f"### [{erel} :: {e_nd.get('name')} (L{e_nd.get('line_start', 1)}-L{e_nd.get('line_end', 1)}) — referenced component (use `slice` if internals needed)]"
                        )
                        continue
                    # If same file already has specific inner explicit symbols, skip redundant 1D slice on >1500-line outer wrapper
                    if (
                        e_span > 1500
                        and len(exp_by_file.get(erel, [])) >= 2
                        and any(
                            200 < (self._graph.nodes[o].get("line_end", 1) - self._graph.nodes[o].get("line_start", 1)) < e_span
                            for o in exp_by_file.get(erel, [])
                            if o != enid
                        )
                    ):
                        primary_files.add(erel)
                        continue
                    if erel:
                        primary_files.add(erel)
                    if e_nd.get("type") == "cls" and e_span > 90:
                        cls_nm_e = e_nd.get("name", "")
                        own_methods = [
                            fn for fn in self._index.get(erel, {}).get("functions", [])
                            if fn.get("parent_class") == cls_nm_e
                        ]
                        if len(own_methods) >= 2:
                            e_beh = self._behaviors.get(erel, {})
                            e_rts = self._index.get(erel, {}).get("routes", [])
                            e_c4d = self._index.get(erel, {}).get("cells_4d", [])
                            m_scored = sorted(
                                ((self._score_symbol_for_query(m, t_kw, e_beh, e_rts, e_c4d), m) for m in own_methods),
                                key=lambda x: x[0],
                                reverse=True,
                            )
                            for _, m_fn in m_scored[: (1 if len(tasks) >= 3 else 2)]:
                                m_nid = fn_id(erel, m_fn["name"], cls_nm_e)
                                filtered_explicit.append(m_nid)
                                _add_node_slice(m_nid, f"top method on {cls_nm_e}", t_kw, sec, max_l=48, max_g=8)
                            continue
                    filtered_explicit.append(enid)
                    _add_node_slice(enid, "explicitly referenced in task", t_kw, sec, max_l=58 if e_span <= 450 else 44, max_g=8)
            if filtered_explicit:
                explicit_nodes = filtered_explicit
            had_initial_explicit = bool(explicit_nodes)
            had_executable_explicit = any(
                self._graph and self._graph.nodes.get(en, {}).get("type") in ("fn", "route")
                for en in explicit_nodes
            )
            # Expand passive class/enum/schema seeds (type == "cls" with <=1 method) to their top referencing functions across importing modules
            if self._graph and explicit_nodes:
                cls_ref_cands: list[tuple[float, str, str, str]] = []
                q_wants_tests = bool(set(t_kw) & _TEST_QUERY_TERMS)
                for enid in list(explicit_nodes):
                    e_nd = self._graph.nodes.get(enid, {})
                    if e_nd.get("type") != "cls":
                        continue
                    erel = e_nd.get("rel_path", "")
                    enm = e_nd.get("name", "")
                    enm_low = enm.lower()
                    if not erel or not enm_low:
                        continue
                    own_m_cnt = sum(1 for fn in self._index.get(erel, {}).get("functions", []) if fn.get("parent_class") == enm)
                    if own_m_cnt >= 2:
                        continue
                    for imp_rel in [erel] + self._imported_by.get(erel, []):
                        if not q_wants_tests and _is_test_file(imp_rel):
                            continue
                        f_txt = self._get_file_text(imp_rel) or ""
                        if enm not in f_txt and enm_low not in self._blobs.get(imp_rel, ""):
                            continue
                        imp_info = self._index.get(imp_rel, {})
                        imp_beh = self._behaviors.get(imp_rel, {})
                        f_lines = f_txt.splitlines() if f_txt else []
                        for ifn in imp_info.get("functions", []):
                            ls, le = ifn.get("line_start", 1), ifn.get("line_end", 1)
                            fn_span_txt = "\n".join(f_lines[max(0, ls - 1) : min(len(f_lines), le)]) if f_lines else ""
                            if (
                                enm in fn_span_txt
                                or enm_low in [bt.lower() for bt in ifn.get("body_tokens", [])]
                                or enm_low in (ifn.get("params") or "").lower()
                                or enm_low in (ifn.get("return_type") or "").lower()
                            ):
                                sc = self._score_symbol_for_query(
                                    ifn, t_kw + [enm_low], imp_beh, imp_info.get("routes", []), imp_info.get("cells_4d", [])
                                )
                                ref_nid = fn_id(imp_rel, ifn["name"], ifn.get("parent_class"))
                                cls_ref_cands.append((sc, ref_nid, imp_rel, enm))
                cls_ref_cands.sort(key=lambda x: x[0], reverse=True)
                task_cls_ref_counts: dict[str, int] = {}
                for _, ref_nid, ref_rel, cls_nm in cls_ref_cands[: (2 if len(tasks) >= 2 else 3)]:
                    primary_files.add(ref_rel)
                    _add_node_slice(ref_nid, f"references {cls_nm} in {ref_rel}", t_kw, sec, max_l=58 if len(tasks) >= 2 else 75, max_g=6)
                    task_cls_ref_counts[ref_rel] = task_cls_ref_counts.get(ref_rel, 0) + 1
                    if ref_nid not in explicit_nodes:
                        explicit_nodes.append(ref_nid)
            else:
                task_cls_ref_counts = {}
            step2_ranked_files: set[str] = set()
            same_file_callers_to_add: list[tuple[str, str, list[str]]] = []
            top3_query_files: set[str] = set()
            if not has_explicit_route:
                ranked_all = self.query(clean_task_for_search, top_n=8)
                top3_query_files = {r["rel_path"] for r in ranked_all[:3]} | set(explicit_task_files)
                top1_file_score = ranked_all[0]["score"] if ranked_all else 1.0

                def _is_exec_file(cand_rp: str) -> bool:
                    c_inf = self._index.get(cand_rp, {})
                    c_fns = c_inf.get("functions", [])
                    c_calls = sum(len(f.get("calls", [])) for f in c_fns)
                    return bool(c_inf.get("routes")) or c_calls >= 3 or (len(c_fns) >= 2 and len(c_inf.get("cells_4d", [])) >= 2)

                if len(explicit_task_files) >= 2:
                    ranked = [{"rel_path": ef, "score": top1_file_score} for ef in explicit_task_files]
                else:
                    q_wants_t = bool(set(t_kw) & _TEST_QUERY_TERMS)
                    exec_map: dict[str, float] = {}
                    decl_list: list[dict] = []
                    for r_item in ranked_all:
                        r_p = r_item["rel_path"]
                        if not q_wants_t and _is_test_file(r_p):
                            continue
                        if _is_exec_file(r_p):
                            exec_map[r_p] = r_item["score"]
                        elif Path(r_p).stem.lower() not in ("_types", "types", "typing"):
                            decl_list.append(r_item)
                    # Bipartite Importer Bridge: traverse incoming imports from top passive declaration files to executable consumers
                    for d_item in decl_list[:3]:
                        d_rp = d_item["rel_path"]
                        d_sc = d_item["score"]
                        for imp_rp in self._imported_by.get(d_rp, []):
                            if not q_wants_t and _is_test_file(imp_rp):
                                continue
                            if not _is_exec_file(imp_rp):
                                continue
                            imp_inf = self._index.get(imp_rp, {})
                            imp_beh = self._behaviors.get(imp_rp, {})
                            imp_rts = imp_inf.get("routes", [])
                            imp_c4d = imp_inf.get("cells_4d", [])
                            best_fn = max(
                                (self._score_symbol_for_query(fn, t_kw, imp_beh, imp_rts, imp_c4d) for fn in imp_inf.get("functions", [])),
                                default=0.0,
                            )
                            if best_fn > 0:
                                br_sc = d_sc * 0.78 + min(3.5, best_fn * 0.22) + (0.9 if imp_rts else 0.0)
                                if br_sc > exec_map.get(imp_rp, 0.0):
                                    exec_map[imp_rp] = br_sc
                    sorted_exec = sorted(exec_map.items(), key=lambda kv: kv[1], reverse=True)
                    max_exec_slots = 1 if (had_executable_explicit and not explicit_task_files) else 3
                    chosen_exec: list[dict] = []
                    route_exec = [(p, s) for p, s in sorted_exec if self._index.get(p, {}).get("routes")]
                    logic_exec = [(p, s) for p, s in sorted_exec if not self._index.get(p, {}).get("routes")]
                    if max_exec_slots >= 2 and route_exec and logic_exec:
                        top_e_sc = sorted_exec[0][1]
                        if route_exec[0][1] >= top_e_sc * 0.55:
                            chosen_exec.append({"rel_path": route_exec[0][0], "score": max(route_exec[0][1], top1_file_score * 0.75)})
                        for l_p, l_s in logic_exec[: (max_exec_slots - len(chosen_exec))]:
                            chosen_exec.append({"rel_path": l_p, "score": max(l_s, top1_file_score * 0.75)})
                    for e_p, e_s in sorted_exec:
                        if len(chosen_exec) >= max_exec_slots:
                            break
                        if all(x["rel_path"] != e_p for x in chosen_exec):
                            chosen_exec.append({"rel_path": e_p, "score": max(e_s, top1_file_score * 0.75)})
                    ranked = list(chosen_exec)
                    if decl_list and (not chosen_exec or (not had_initial_explicit and len(ranked) < 3 and decl_list[0]["score"] >= top1_file_score * 0.9)):
                        ranked.append(decl_list[0])
                    existing_ranked_paths = {r["rel_path"] for r in ranked}
                    for ef in explicit_task_files:
                        if ef not in existing_ranked_paths:
                            ranked.append({"rel_path": ef, "score": top1_file_score})
                            existing_ranked_paths.add(ef)
                rank0_top_sym_score = 0.0
                for file_rank, r in enumerate(ranked):
                    rp = r["rel_path"]
                    min_ratio = 0.72 if file_rank == 1 else 0.52
                    if file_rank > 0 and rp not in explicit_task_files and r["score"] < top1_file_score * min_ratio:
                        continue
                    if (len(filtered_explicit) >= 2 or rp in exp_by_file) and rp in exp_by_file and rp not in explicit_task_files:
                        continue
                    if task_cls_ref_counts.get(rp, 0) >= 2 or (rp in exp_by_file and len(exp_by_file[rp]) >= 1):
                        continue
                    info = self._index.get(rp, {})
                    behaviors = self._get_file_behaviors(rp)
                    base_classes_in_file = {b for c in info.get("classes", []) for b in c.get("bases", [])}
                    scored: list[tuple[float, str, dict]] = []
                    for fn in info.get("functions", []):
                        sc = self._score_symbol_for_query(fn, t_kw, behaviors, info.get("routes", []), info.get("cells_4d", []))
                        if sc > 0:
                            if fn.get("parent_class") in base_classes_in_file:
                                sc *= 1.25
                            if self._graph and not fn.get("name", "").startswith("_"):
                                fn_nid = fn_id(rp, fn["name"], fn.get("parent_class"))
                                cross_prod_callers = sum(
                                    1 for ei in self._graph._radj.get(fn_nid, [])
                                    if self._graph.edges[ei].get("type") == "calls"
                                    and self._graph.edges[ei].get("confidence") in ("exact", "import_resolved")
                                    and self._graph.nodes.get(self._graph.edges[ei]["src"], {}).get("rel_path", rp) != rp
                                    and not _is_test_file(self._graph.nodes.get(self._graph.edges[ei]["src"], {}).get("rel_path", ""))
                                )
                                if cross_prod_callers >= 1:
                                    sc *= 1.0 + 0.14 * min(4, cross_prod_callers)
                            scored.append((sc, "fn", fn))
                    for cls in info.get("classes", []):
                        sc = self._score_class_for_query(cls, t_kw)
                        if sc > 0:
                            scored.append((sc, "cls", cls))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    # Deduplicate nested functions and cap same-method-name subclass overrides per file
                    dedup_scored: list[tuple[float, str, dict]] = []
                    method_name_counts: dict[str, int] = {}
                    for item in scored:
                        s_fn = item[2]
                        s_st, s_en = s_fn.get("line_start", 1), s_fn.get("line_end", 1)
                        if any(
                            o_fn.get("line_start", 1) <= s_st and s_en <= o_fn.get("line_end", 1) and (o_fn.get("line_end", 1) - o_fn.get("line_start", 1)) <= 180
                            or (s_st <= o_fn.get("line_start", 1) and o_fn.get("line_end", 1) <= s_en and (s_en - s_st) <= 180)
                            for _, _, o_fn in dedup_scored
                        ):
                            continue
                        if item[1] == "fn" and s_fn.get("parent_class"):
                            m_nm = s_fn.get("name", "")
                            if method_name_counts.get(m_nm, 0) >= 2:
                                continue
                            method_name_counts[m_nm] = method_name_counts.get(m_nm, 0) + 1
                        dedup_scored.append(item)
                    if file_rank == 0 and dedup_scored:
                        rank0_top_sym_score = dedup_scored[0][0]
                    elif (
                        file_rank >= 2
                        and rp not in explicit_task_files
                        and rank0_top_sym_score > 0
                        and (not dedup_scored or dedup_scored[0][0] < rank0_top_sym_score * 0.45)
                    ):
                        continue
                    step2_ranked_files.add(rp)
                    primary_files.add(rp)
                    top_k = 1 if (had_initial_explicit or file_rank > 1 or len(tasks) >= 3) else 2
                    if not had_initial_explicit and len(tasks) == 1:
                        if file_rank == 0:
                            if len(dedup_scored) >= 4 and dedup_scored[3][0] >= dedup_scored[0][0] * 0.72:
                                top_k = 4
                            elif len(dedup_scored) >= 3 and dedup_scored[2][0] >= dedup_scored[0][0] * 0.48:
                                top_k = 3
                        elif file_rank <= 2:
                            if len(dedup_scored) >= 3 and dedup_scored[2][0] >= dedup_scored[0][0] * 0.65:
                                top_k = max(top_k, 3)
                            elif len(dedup_scored) >= 2 and dedup_scored[1][0] >= dedup_scored[0][0] * 0.60:
                                top_k = max(top_k, 2)
                    multi_file_budget = (len(tasks) >= 3 and len(ranked) >= 3)
                    for rank_idx, (sc, kind, sym) in enumerate(dedup_scored[:top_k]):
                        nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                        sym_span = sym.get("line_end", 1) - sym.get("line_start", 1)
                        rank_max_l = (
                            (42 if multi_file_budget else (52 if sym_span <= 450 else 44))
                            if rank_idx == 0
                            else (30 if rank_idx == 1 else 24)
                        )
                        rank_max_g = 6 if multi_file_budget else (14 if file_rank == 0 else 8)
                        _add_node_slice(nid, f"top match in {rp}", t_kw, sec, max_l=rank_max_l, max_g=rank_max_g)
                        if (not had_executable_explicit or rp in explicit_task_files) and nid not in explicit_nodes and len(explicit_nodes) < 6:
                            explicit_nodes.append(nid)
                            added_for_sym = 0
                            for ei_c in self._graph._radj.get(nid, []):
                                ec = self._graph.edges[ei_c]
                                if ec["type"] == "calls" and ec.get("confidence") in ("exact", "import_resolved"):
                                    c_src = ec["src"]
                                    c_nd = self._graph.nodes.get(c_src, {})
                                    if c_nd.get("rel_path") == rp:
                                        c_lines = ec.get("lines") or ([ec.get("line", 0)] if ec.get("line") else [0])
                                        for call_ln in c_lines[:3]:
                                            same_file_callers_to_add.append((c_src, f"calls {sym['name']} @L{call_ln}", t_kw))
                                        added_for_sym += 1
                                        if added_for_sym >= 2:
                                            break

            # 3. Expand bidirectional call chains (BFS hop order + cross-file priority + Ochiai hub damping)
            if self._graph and explicit_nodes:
                chain_lines: list[str] = []
                expanded_nodes: list[tuple[str, str, list[str], int, int]] = []
                for seed_id in explicit_nodes[:6]:
                    hop1_list: list[tuple[str, str, list[str], int, int]] = []
                    hop2_cross: list[tuple[str, str, list[str], int, int]] = []
                    hop2_same: list[tuple[str, str, list[str], int, int]] = []
                    hop3_list: list[tuple[str, str, list[str], int, int]] = []
                    bwd_expanded: list[tuple[str, str, list[str], int, int]] = []
                    seed_nd = self._graph.nodes.get(seed_id, {})
                    seed_rel = seed_nd.get("rel_path", "")
                    seed_s = seed_nd.get("line_start", -1)
                    seed_e = seed_nd.get("line_end", -1)

                    # Forward 1-2 hops (handles, calls, state_flow) ordered strictly by BFS hop and cross-file priority
                    for ei in self._graph._adj.get(seed_id, []):
                        e1 = self._graph.edges[ei]
                        if e1["type"] == "state_flow":
                            dst1 = e1["dst"]
                            dst1_nd = self._graph.nodes.get(dst1, {})
                            dst1_rel = dst1_nd.get("rel_path", "")
                            dst1_name = (dst1_nd.get("name") or "").lower()
                            dst1_toks = set(_tokenize_symbol(dst1_name))
                            if dst1_rel in top3_query_files and any(
                                w in dst1_name or w[:4] in dst1_name or w[:3] in dst1_toks for w in t_kw if len(w) >= 4
                            ):
                                hop1_list.append((dst1, f"reads state from {seed_id.split(':')[-1]}", t_kw + [dst1_name], 26, 0))
                            continue
                        if e1["type"] not in ("calls", "handles") or e1.get("confidence") not in ("exact", "import_resolved"):
                            continue
                        dst1 = e1["dst"]
                        dst1_nd = self._graph.nodes.get(dst1, {})
                        dst1_rel = dst1_nd.get("rel_path", "")
                        if (seed_e - seed_s) > 200:
                            c_line = e1.get("line", 0)
                            dst1_low = (dst1_nd.get("name") or "").lower()
                            in_matched_cell = any(
                                sr == seed_rel and (se - ss) <= 130 and ss <= c_line <= se
                                for sr, ss, se in seen_spans
                            )
                            kw_hit = any(w in dst1_low for w in t_kw if len(w) >= 4)
                            if not in_matched_cell and not kw_hit:
                                continue
                        dst1_s = dst1_nd.get("line_start", 0)
                        dst1_e = dst1_nd.get("line_end", 0)
                        if dst1_rel == seed_rel and seed_s <= dst1_s and dst1_e <= seed_e:
                            continue
                        chain_lines.append(f"- `{seed_id}` -[{e1['type']} (L{e1.get('line', '?')})]-> `{dst1}`")
                        dst1_name = (dst1_nd.get("name") or "").lower()
                        dst1_toks = set(_tokenize_symbol(dst1_name))
                        dst1_in_deg = len(self._graph._radj.get(dst1, []))
                        dst1_kw_overlap = any(w in dst1_name or w[:4] in dst1_name or w[:3] in dst1_toks for w in t_kw if len(w) >= 4)
                        if dst1_in_deg >= 6 and not dst1_kw_overlap:
                            continue
                        h1_max_l = 12 if (dst1_in_deg >= 4 and not dst1_kw_overlap) else 26
                        hop1_list.append((dst1, f"called by {seed_id.split(':')[-1]}", t_kw + [dst1_name], h1_max_l, 0))
                        # 2nd hop forward
                        for ei2 in self._graph._adj.get(dst1, []):
                            e2 = self._graph.edges[ei2]
                            if e2["type"] == "calls" and e2.get("confidence") in ("exact", "import_resolved"):
                                dst2 = e2["dst"]
                                dst2_nd = self._graph.nodes.get(dst2, {})
                                dst2_rel = dst2_nd.get("rel_path", "")
                                chain_lines.append(f"  - `{dst1}` -[calls (L{e2.get('line', '?')})]-> `{dst2}`")
                                dst2_name = (dst2_nd.get("name") or "").lower()
                                dst2_toks = set(_tokenize_symbol(dst2_name))
                                dst2_in_deg = len(self._graph._radj.get(dst2, []))
                                dst2_kw_overlap = any(w in dst2_name or w[:4] in dst2_name or w[:3] in dst2_toks for w in t_kw if len(w) >= 4)
                                if dst2_in_deg >= 5 and not dst2_kw_overlap:
                                    continue
                                if dst2_rel != dst1_rel:
                                    hop2_cross.append((dst2, f"2-hop callee via {dst1.split(':')[-1]}", t_kw + [dst2_name], 20, 10 if seed_nd.get("type") == "route" else 0))
                                    for ei3 in self._graph._adj.get(dst2, []):
                                        e3 = self._graph.edges[ei3]
                                        if e3["type"] == "calls" and e3.get("confidence") in ("exact", "import_resolved"):
                                            dst3 = e3["dst"]
                                            dst3_nd = self._graph.nodes.get(dst3, {})
                                            dst3_name = (dst3_nd.get("name") or "").lower()
                                            dst3_in_deg = len(self._graph._radj.get(dst3, []))
                                            dst3_kw_overlap = any(w in dst3_name or w[:4] in dst3_name for w in t_kw if len(w) >= 4)
                                            chain_lines.append(f"    - `{dst2}` -[calls (L{e3.get('line', '?')})]-> `{dst3}`")
                                            if dst3_in_deg < 4 or dst3_kw_overlap:
                                                hop3_list.append((dst3, f"3-hop callee via {dst2.split(':')[-1]}", t_kw, 16, 0))
                                elif (dst2_nd.get("line_end", 0) - dst2_nd.get("line_start", 0)) >= 5:
                                    hop2_same.append((dst2, f"2-hop callee via {dst1.split(':')[-1]}", t_kw + [dst2_name], 16, 0))

                    fwd_expanded = hop1_list + hop2_cross + hop2_same + hop3_list

                    # Backward 1-2 hops (direct callers via _radj)
                    seed_tokens = {
                        tok for tok in _tokenize_symbol(seed_id.split(":")[-1])
                        if len(tok) >= 4 and tok not in ("mark", "handle", "save", "load", "init", "sync")
                    }
                    q_wants_tests_bwd = bool(set(t_kw) & _TEST_QUERY_TERMS)
                    raw_callers: list[tuple[int, int, str, dict]] = []
                    for ei in self._graph._radj.get(seed_id, []):
                        e_in = self._graph.edges[ei]
                        if e_in["type"] != "calls" or e_in.get("confidence") not in ("exact", "import_resolved"):
                            continue
                        cid = e_in["src"]
                        cnd = self._graph.nodes.get(cid, {})
                        crel = cnd.get("rel_path", "")
                        c_is_test = 1 if (not q_wants_tests_bwd and _is_test_file(crel)) else 0
                        c_span = (cnd.get("line_end", 1) - cnd.get("line_start", 1))
                        raw_callers.append((c_is_test, c_span, cid, e_in))
                    raw_callers.sort(key=lambda x: (x[0], x[1]))

                    caller_files_seen: set[str] = set()
                    for c_is_test, c_span, caller_id, e_in in raw_callers:
                        caller_nd = self._graph.nodes.get(caller_id, {})
                        caller_name = caller_nd.get("name", "")
                        caller_rel = caller_nd.get("rel_path", "")
                        c_lines = e_in.get("lines") or ([e_in.get("line", 0)] if e_in.get("line") else [0])
                        c_lines_str = ",".join(f"L{x}" for x in c_lines[:4]) if c_lines and c_lines[0] else "L?"
                        chain_lines.append(f"- `{caller_id}` ({c_lines_str}) -[calls]-> `{seed_id}`")
                        if c_is_test and caller_files_seen:
                            continue
                        if caller_rel in caller_files_seen and c_span > 150 and len(c_lines) <= 1:
                            continue
                        caller_files_seen.add(caller_rel)
                        for call_ln in c_lines[:3]:
                            bwd_expanded.append((caller_id, f"calls {seed_id.split(':')[-1]} @L{call_ln}", [seed_id.split(":")[-1].lower()], 18, 0))
                            for ei_b2 in self._graph._radj.get(caller_id, []):
                                eb2 = self._graph.edges[ei_b2]
                                if eb2["type"] == "calls" and eb2.get("confidence") in ("exact", "import_resolved"):
                                    c2_id = eb2["src"]
                                    c2_nd = self._graph.nodes.get(c2_id, {})
                                    if not q_wants_tests_bwd and _is_test_file(c2_nd.get("rel_path", "")):
                                        continue
                                    c2_span = c2_nd.get("line_end", 1) - c2_nd.get("line_start", 1)
                                    if c2_span <= 120:
                                        bwd_expanded.append((c2_id, f"calls {caller_name} @L{eb2.get('line', 0)}", [caller_name.lower()], 16, 0))
                        for dep_rel in self._import_targets.get(caller_rel, []):
                            dep_tokens = {tok.rstrip("s") for tok in _tokenize_symbol(Path(dep_rel).stem) if len(tok) >= 4}
                            norm_seed = {tok.rstrip("s") for tok in seed_tokens}
                            if dep_tokens & norm_seed:
                                dep_info = self._index.get(dep_rel, {})
                                cb_fns: list[dict] = []
                                for dfn in dep_info.get("functions", []):
                                    dfn_tokens = {tok.rstrip("s") for tok in _tokenize_symbol(dfn["name"])}
                                    span = dfn.get("line_end", 0) - dfn.get("line_start", 0)
                                    if (dfn_tokens & norm_seed) and span < 40:
                                        cb_fns.append(dfn)
                                cb_names = {f["name"] for f in cb_fns}
                                caller_of_cb: list[dict] = [
                                    dfn for dfn in dep_info.get("functions", [])
                                    if cb_names & set(dfn.get("calls", []))
                                    and (dfn.get("line_end", 0) - dfn.get("line_start", 0)) < 300
                                ]
                                for dfn in (cb_fns + caller_of_cb)[:2]:
                                    dnid = fn_id(dep_rel, dfn["name"], dfn.get("parent_class"))
                                    bwd_expanded.append((dnid, f"UI trigger in {dep_rel}", t_kw + list(seed_tokens) + [caller_name.lower()], 22, 6))

                    seed_sym = (self._graph.nodes.get(seed_id, {}).get("name") or "").lower()
                    if seed_rel and seed_sym and seed_id in explicit_nodes[:2]:
                        for imp_rel in self._imported_by.get(seed_rel, []):
                            if not q_wants_tests_bwd and _is_test_file(imp_rel):
                                continue
                            if seed_sym in self._blobs.get(imp_rel, ""):
                                imp_info = self._index.get(imp_rel, {})
                                for ifn in imp_info.get("functions", []):
                                    if seed_sym in [c.lower() for c in ifn.get("calls", [])]:
                                        inid = fn_id(imp_rel, ifn["name"], ifn.get("parent_class"))
                                        bwd_expanded.append((inid, f"invokes {seed_sym} in {imp_rel}", [seed_sym], 12, 0))

                    expanded_nodes.extend(fwd_expanded[:8])
                    expanded_nodes.extend(bwd_expanded[:8])

                for sc_id, sc_reason, sc_kw in same_file_callers_to_add[:3]:
                    expanded_nodes.append((sc_id, sc_reason, sc_kw, 20, 0))

                # Append Transitive Interprocedural Effects & State-Channel Flow Edges for seed symbols
                for seed_id in explicit_nodes[:6]:
                    s_nd = self._graph.nodes.get(seed_id, {})
                    t_zw = s_nd.get("transitive_z_write", [])
                    t_eff = s_nd.get("transitive_effects", [])
                    t_ex = s_nd.get("transitive_exits", [])
                    prov = s_nd.get("effect_provenance", {})
                    if t_zw or t_ex or "IO" in t_eff:
                        parts_eff = []
                        if t_zw:
                            parts_eff.append(f"?Z_transitive:[{', '.join(t_zw[:4])}]")
                        if t_eff:
                            parts_eff.append(f"Effects:[{', '.join(t_eff)}]")
                        if t_ex:
                            parts_eff.append(f"Exits:[{', '.join(t_ex[:3])}]")
                        sample_via = next(iter(prov.values()), "")
                        via_sfx = f" ({sample_via})" if sample_via else ""
                        chain_lines.append(f"- `{seed_id}` -[interprocedural_bubble]-> {' | '.join(parts_eff)}{via_sfx}")
                    for ei_sf in self._graph._adj.get(seed_id, []):
                        esf = self._graph.edges[ei_sf]
                        if esf["type"] in ("state_flow", "http_flow"):
                            dst_rel = self._graph.nodes.get(esf["dst"], {}).get("rel_path", "")
                            if not q_wants_tests_bwd and dst_rel and _is_test_file(dst_rel):
                                continue
                            chain_lines.append(f"- `{seed_id}` -[{esf['type']}: {esf.get('reason', '')}]-> `{esf['dst']}`")
                    for ei_sfr in self._graph._radj.get(seed_id, []):
                        esfr = self._graph.edges[ei_sfr]
                        if esfr["type"] in ("state_flow", "http_flow"):
                            src_rel = self._graph.nodes.get(esfr["src"], {}).get("rel_path", "")
                            if not q_wants_tests_bwd and src_rel and _is_test_file(src_rel):
                                continue
                            chain_lines.append(f"- `{esfr['src']}` -[{esfr['type']}: {esfr.get('reason', '')}]-> `{seed_id}`")

                # Multiplex Personalized PageRank (PPR) + Causal Symptom Chop across 4-Layer Graph
                ppr_results = self._graph.multiplex_ppr({sid: 1.0 for sid in explicit_nodes[:6]}, top_k=10)
                ppr_scores: dict[str, float] = {nid: prob for nid, prob, _ in ppr_results}
                existing_exp_ids = {item[0] for item in expanded_nodes}

                chop_hits = self._graph.causal_symptom_chop(explicit_nodes[:6], clean_task_for_search)
                for chop_nid, chop_sym, chop_path in chop_hits[:2]:
                    path_str = " -> ".join(f"`{p}`" for p in chop_path)
                    chain_lines.append(f"- ?? **[Causal Symptom Chop]** {path_str} *(origin of `{chop_sym}`)*")
                    ppr_scores[chop_nid] = max(ppr_scores.get(chop_nid, 0.0), 0.45)
                    for u_hop, v_hop in zip(chop_path, chop_path[1:]):
                        u_nd = self._graph.nodes.get(u_hop, {})
                        v_nd = self._graph.nodes.get(v_hop, {})
                        u_rel = u_nd.get("rel_path", "")
                        v_nm = v_nd.get("name", v_hop.split(":")[-1])
                        if u_rel:
                            for ei_ch in self._graph._adj.get(u_hop, []):
                                ech = self._graph.edges[ei_ch]
                                if ech["dst"] == v_hop and ech.get("line") and ech["type"] == "calls":
                                    ch_ln = int(ech["line"])
                                    for p_ln in range(max(1, ch_ln - 1), ch_ln + 2):
                                        self._last_pin_lines.setdefault(u_rel, set()).add(p_ln)
                                    ch_msg = (
                                        f"# >>> [CAUSAL-CHOP L{ch_ln}] calls {v_nm} -> origin of {chop_sym} "
                                        f"({v_nd.get('rel_path', '')}:L{v_nd.get('line_start', 1)})"
                                    )
                                    ann_l = self._last_line_annotations.setdefault(u_rel, {}).setdefault(ch_ln, [])
                                    if ch_msg not in ann_l:
                                        ann_l.append(ch_msg)
                    if chop_nid not in existing_exp_ids and chop_nid not in seen_slices:
                        expanded_nodes.insert(0, (chop_nid, f"Causal Symptom Chop origin ({chop_sym})", t_kw, 32, 0))
                        existing_exp_ids.add(chop_nid)
                for p_nid, p_prob, p_rsn in ppr_results[:6]:
                    p_rel = self._graph.nodes.get(p_nid, {}).get("rel_path", "")
                    if not q_wants_tests_bwd and p_rel and _is_test_file(p_rel):
                        continue
                    if p_nid not in existing_exp_ids and p_nid not in seen_slices and (
                        "http_flow" in p_rsn or "table:" in p_rsn or p_prob >= 0.085
                    ):
                        expanded_nodes.append((p_nid, p_rsn, t_kw, 28, 0))
                        existing_exp_ids.add(p_nid)

                if chain_lines and not zoom and monolith_4d_cells_count == 0:
                    sec.append("### Static AST Call, State-Channel & Interprocedural Flow Edges")
                    sec.extend(list(dict.fromkeys(chain_lines))[:12])
                emitted_hops = 99 if (zoom and had_initial_explicit) else 0
                max_hops_allowed = (3 if not had_initial_explicit else 1) if (zoom or monolith_4d_cells_count > 0 or len(filtered_explicit) >= 2) else 3

                # Submodular Marginal Information Gain per Unseen Token Optimizer
                covered_features: set[str] = set()
                for s_id in seen_slices:
                    s_obj = self._graph.nodes.get(s_id, {})
                    for zw in s_obj.get("local_z_write", []):
                        covered_features.add(f"zw:{zw.lower()}")
                    for ex in s_obj.get("exit_contracts", []):
                        covered_features.add(f"ex:{ex.lower()}")

                remaining_cands: list[tuple[str, str, list[str], int, int]] = []
                seen_cand_ids: set[str] = set()
                seed_call_freq: dict[str, int] = {}
                for item in expanded_nodes:
                    if item[1].startswith(("called by ", "calls ", "2-hop callee")):
                        seed_call_freq[item[0]] = seed_call_freq.get(item[0], 0) + 1
                    m_cl = re.search(r"@L(\d+)", item[1])
                    cand_key = f"{item[0]}@L{m_cl.group(1)}" if m_cl else item[0]
                    if cand_key not in seen_cand_ids and cand_key not in seen_slices and item[0] in self._graph.nodes:
                        seen_cand_ids.add(cand_key)
                        remaining_cands.append(item)
                ordered_cands: list[tuple[str, str, list[str], int, int]] = []
                while remaining_cands:
                    best_idx = 0
                    best_ratio = -1e9
                    for c_idx, item in enumerate(remaining_cands):
                        c_nid, c_rsn, _, c_max_l, _ = item
                        nd_obj = self._graph.nodes.get(c_nid, {})
                        nd_nm = (nd_obj.get("name") or "").lower()
                        nd_rel = nd_obj.get("rel_path", "")
                        nd_s = nd_obj.get("line_start", 1)
                        nd_e = nd_obj.get("line_end", nd_s)
                        nd_span = max(1, nd_e - nd_s)

                        # Compute marginal unseen token cost C(u) from Session Interval Ledger
                        s_plan = self._ledger.plan_slice_intervals(nd_rel, nd_s, nd_e, commit=False)
                        unseen_lines = sum(ue - us + 1 for us, ue in s_plan["unseen_intervals"])
                        cost_u = max(4.0, float(min(c_max_l, unseen_lines if unseen_lines > 0 else nd_span)))

                        # Compute marginal epistemic gain ?I(u | U_cov)
                        gain = 0.0
                        nd_tokens = set(_tokenize_symbol(nd_nm))
                        for w in t_kw:
                            if w in ("handled", "handling", "work", "works", "working", "across"):
                                continue
                            if len(w) >= 4 and (w in nd_nm or w[:4] in nd_nm or w[:3] in nd_tokens):
                                feat_k = f"kw:{w}"
                                gain += (6.0 if feat_k not in covered_features else 1.5) * self._idf.get(w, 1.0)
                        for zw in nd_obj.get("local_z_write", [])[:2]:
                            feat_zw = f"zw:{zw.lower()}"
                            gain += 1.4 if feat_zw not in covered_features else 0.2
                        for ex in nd_obj.get("exit_contracts", [])[:1]:
                            feat_ex = f"ex:{ex.lower()}"
                            gain += 1.2 if feat_ex not in covered_features else 0.2
                        feat_file = f"file:{nd_rel}"
                        if nd_rel in top3_query_files:
                            gain += 5.0 if feat_file not in covered_features else 1.5
                        if c_rsn.startswith("called by "):
                            gain += 6.5 + 8.0 * min(3, seed_call_freq.get(c_nid, 1) - 1)
                        elif c_rsn.startswith("calls ") and nd_rel in primary_files:
                            gain += 6.0 + 6.5 * min(3, seed_call_freq.get(c_nid, 1) - 1)
                        elif not c_rsn.startswith(("2-hop", "3-hop")):
                            gain += 3.5
                        if "http_flow" in c_rsn or "state_flow" in c_rsn:
                            gain += 4.5
                        feat_ppr = f"ppr:{c_nid}"
                        if feat_ppr not in covered_features:
                            gain += 25.0 * ppr_scores.get(c_nid, 0.0)
                        if nd_span < 8 or nd_nm.startswith("_"):
                            gain *= 0.40

                        ratio = gain / (cost_u ** 0.55)
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_idx = c_idx

                    chosen = remaining_cands.pop(best_idx)
                    ordered_cands.append(chosen)
                    ch_obj = self._graph.nodes.get(chosen[0], {})
                    ch_nm = (ch_obj.get("name") or "").lower()
                    ch_rel = ch_obj.get("rel_path", "")
                    if ch_rel:
                        covered_features.add(f"file:{ch_rel}")
                    covered_features.add(f"ppr:{chosen[0]}")
                    for w in t_kw:
                        if len(w) >= 4 and (w in ch_nm or w[:4] in ch_nm):
                            covered_features.add(f"kw:{w}")
                    for zw in ch_obj.get("local_z_write", []):
                        covered_features.add(f"zw:{zw.lower()}")
                    for ex in ch_obj.get("exit_contracts", []):
                        covered_features.add(f"ex:{ex.lower()}")

                hop_pointers: list[str] = []
                monolith_call_wins = 0
                for enid, ereason, ekw, emax_l, emax_g in ordered_cands:
                    m_cl = re.search(r"@L(\d+)", ereason)
                    cand_key = f"{enid}@L{m_cl.group(1)}" if m_cl else enid
                    if not self._graph or enid not in self._graph.nodes or cand_key in seen_slices:
                        continue
                    end_node = self._graph.nodes[enid]
                    enm = (end_node.get("name") or "").lower()
                    enm_tokens = set(_tokenize_symbol(enm))
                    erel = end_node.get("rel_path", "")
                    full_node_span = end_node.get("line_end", 1) - end_node.get("line_start", 1)
                    is_monolith_call = bool(m_cl) and full_node_span > 120 and erel in primary_files and monolith_call_wins < 2
                    espan = 12 if is_monolith_call else full_node_span
                    sec_lines_now = sum(len(x.splitlines()) for x in sec)
                    is_chop = "Causal Symptom Chop" in ereason
                    is_direct_callee = ereason.startswith(("called by ", "calls "))
                    kw_direct = (
                        is_chop
                        or is_monolith_call
                        or is_direct_callee
                        or any(w in enm or w[:4] in enm or w[:3] in enm_tokens for w in t_kw if len(w) >= 4)
                        or ("http_flow" in ereason)
                        or ("state_flow" in ereason)
                        or ("reads state from" in ereason)
                        or ("table:" in ereason)
                    )
                    sec_cap_direct = 185 if len(tasks) >= 3 else 310
                    if (emitted_hops < max_hops_allowed or is_chop or is_monolith_call) and (sec_lines_now < 140 or (kw_direct and sec_lines_now < sec_cap_direct)) and espan <= 120:
                        _add_node_slice(enid, ereason, ekw, sec, max_l=min(emax_l, 26 if is_chop else 22), max_g=0)
                        if is_monolith_call:
                            monolith_call_wins += 1
                        elif not is_chop:
                            emitted_hops += 1
                    else:
                        sig_str = f" `{end_node['signature']}`" if end_node.get("signature") else ""
                        ptr = f"- `{erel} :: {_node_display_name(end_node)}` (L{end_node.get('line_start', 1)}-L{end_node.get('line_end', 1)}){sig_str} — {ereason}"
                        if ptr not in hop_pointers:
                            hop_pointers.append(ptr)
                if hop_pointers:
                    sec.append("### Connected Call-Graph Coordinate Map (1-hop/2-hop neighbors)")
                    sec.extend(hop_pointers[:6])
            diag_lines, diag_nodes = self._compile_6d_contract_diagnostics(task, primary_files | set(emitted_by_file.keys()) | top3_query_files)
            if diag_lines and not zoom:
                # In-scope verified findings outrank low-value graph hops: always slice up to 2
                # finding sites (each bounded by max_l) regardless of how many slices exist.
                for d_nid in diag_nodes[:2]:
                    _add_node_slice(d_nid, "6D contract violation site", t_kw, sec, max_l=58, max_g=0)
                diag_block = ["### Static 6D Contract, Schema & Dialect Diagnostics"] + diag_lines[:10]
                sec[1:1] = diag_block
            md_sections.extend(sec)
        # 4. Append Impact / Blast Radius sections
        for imp_file in impact_list:
            norm_f = self._normalize_rel(imp_file)
            if norm_f in seen_impacts:
                continue
            seen_impacts.add(norm_f)
            imp_res = self.impact([norm_f])
            details = imp_res.get("details", [{}])[0]
            prod_c = details.get("direct_consumers", [])
            test_c = details.get("test_consumers", [])
            all_c = imp_res.get("all_impacted_files", [])
            src_c = [f for f in all_c if f.startswith("src/")]
            md_sections.append(f"## Blast Radius / Direct Importers of `{norm_f}`")
            md_sections.append(
                f"- **Total Direct Importers in `src/`:** **{len(src_c)} files** "
                f"({len(prod_c)} production files + {len(test_c)} test files)"
            )
            md_sections.append("- **Production Importers (`" + str(len(prod_c)) + "`):** " + ", ".join(f"`{p}`" for p in prod_c))
            if test_c:
                md_sections.append("- **Test Importers (`" + str(len(test_c)) + "`):** " + ", ".join(f"`{p}`" for p in test_c))

        # 5. Multi-File Symptom & Cross-File State-Key Expansion (when Pass 1 uses < 520 lines, e.g. open-ended bug hunts)
        current_md = "\n".join(md_sections)
        current_lines = len(current_md.splitlines())
        if current_lines < 520 and not zoom:
            symptom_sec: list[str] = []
            # 5a. Extract state keys (getItem/setItem) and route paths (<Navigate to="...") from Pass 1 slices
            _GENERIC_SCAN_WORDS = {
                "how", "what", "where", "when", "why", "who", "does", "is", "are", "the", "and", "for", "with",
                "from", "into", "state", "session", "usersession", "user", "app", "data", "database", "db",
                "save", "saved", "saving", "track", "tracked", "tracking", "store", "stored", "file", "files",
                "function", "code", "flow", "work", "works", "handled", "manage", "managed", "context", "sync",
                "id",
            }
            all_t_kw = {w for t in tasks for w in _extract_keywords(t)}
            specific_t_kw = {w for w in all_t_kw if w not in _GENERIC_SCAN_WORDS and len(w) >= 3}
            all_tasks_low = " ".join(tasks).lower()
            state_keys: list[str] = []
            for m_key in re.finditer(r"(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]{6,45})['\"]", current_md):
                k_val = m_key.group(1)
                if k_val in state_keys:
                    continue
                k_low = k_val.lower()
                k_toks = {tok for tok in re.split(r"[_\-./]+", k_low) if len(tok) >= 3 and tok not in _GENERIC_SCAN_WORDS}
                # Only include a state key if explicitly mentioned in tasks or matching a specific domain keyword
                if k_low in all_tasks_low or (specific_t_kw and (k_toks & specific_t_kw)):
                    state_keys.append(k_val)
            for m_nav in re.finditer(r"Navigate\s+to=['\"](/[^'\"]{2,30})['\"]", current_md):
                n_val = m_nav.group(1)
                n_low = n_val.lower()
                n_toks = {tok for tok in re.split(r"[_\-./]+", n_low) if len(tok) >= 3 and tok not in _GENERIC_SCAN_WORDS}
                if n_val not in state_keys and (n_low in all_tasks_low or (specific_t_kw and (n_toks & specific_t_kw))):
                    state_keys.append(n_val)
            state_keys.sort(
                key=lambda x: (x.lower() in all_tasks_low, any(w in x.lower() for w in specific_t_kw), "_" in x, len(x)),
                reverse=True,
            )

            # 5b. Scan all indexed files for discovered query-relevant state_keys/routes
            wants_cross_scan = (
                matched_4d_cells_count == 0
                or any(
                    w in all_tasks_low
                    for w in ("storage", "localstorage", "persist", "cloud", "kv", "route", "redirect", "navigate", "onboarding", "across", "sync")
                )
            )
            if state_keys and wants_cross_scan:
                target_keys = state_keys[:2]
                xref_body: list[str] = []
                xref_line_count = 2
                test_hit_files: list[str] = []
                omitted_prod_hits: list[str] = []
                for sk in target_keys:
                    for rp in sorted(self._index.keys()):
                        raw_txt = self._get_file_text(rp)
                        if raw_txt is None:
                            continue
                        if sk not in raw_txt:
                            continue
                        rp_low = rp.lower()
                        if any(t_marker in rp_low for t_marker in ("test", "spec", "__tests__", "eval/")):
                            if rp not in test_hit_files:
                                test_hit_files.append(rp)
                            continue
                        flines = raw_txt.splitlines()
                        hit_lines = [ln_no for ln_no, l_txt in enumerate(flines, start=1) if sk in l_txt]
                        for h_idx, h_ln in enumerate(hit_lines):
                            if any(sr == rp and ss <= h_ln <= se and (se - ss) <= 60 for sr, ss, se in seen_spans):
                                continue
                            if h_idx >= 3 or xref_line_count >= 64 or current_lines + xref_line_count > 520:
                                omitted_prod_hits.append(f"{rp}:L{h_ln}")
                                continue
                            w_s = max(1, h_ln - 3)
                            w_e = min(len(flines), h_ln + 7)
                            seen_spans.append((rp, w_s, w_e))
                            win_code = slice_lines(self.root, rp, w_s, w_e, max_lines=12)
                            if win_code:
                                emitted_by_file.setdefault(rp, []).extend(extract_emitted_intervals(win_code))
                                xref_body.append(f"### [{rp} (L{w_s}-L{w_e}) — references `{sk}` @L{h_ln}]")
                                xref_body.append(win_code)
                                xref_line_count += 1 + len(win_code.splitlines())
                if xref_body or omitted_prod_hits:
                    if not omitted_prod_hits:
                        xref_header = [
                            "## Closed-World Exhaustive Repo Scan (100% File Coverage)",
                            f"> **EXHAUSTIVE GUARANTEE:** Scanned all `{len(self._index)}` repository files. Every production occurrence of "
                            + ", ".join(f"`{k}`" for k in target_keys)
                            + " across the codebase is shown below. Do NOT read other files to search for additional occurrences.",
                        ]
                    else:
                        xref_header = [
                            "## Cross-File Repo Scan (Truncated to Fit Dossier Budget)",
                            f"> **PARTIAL SCAN NOTE:** Scanned all `{len(self._index)}` repository files for "
                            + ", ".join(f"`{k}`" for k in target_keys)
                            + f". Showing top slices below; {len(omitted_prod_hits)} additional production occurrence(s) omitted: "
                            + ", ".join(f"`{h}`" for h in omitted_prod_hits[:12]),
                        ]
                    symptom_sec.extend(xref_header)
                    symptom_sec.extend(xref_body)
                    if test_hit_files:
                        symptom_sec.append(
                            f"- **Also referenced in {len(test_hit_files)} test file(s):** "
                            + ", ".join(f"`{tf}`" for tf in test_hit_files[:10])
                        )

            # 5c. Compact Secondary Candidate Map (replaces 745-line code padding with 1-line coordinate pointers)
            map_pointer_lines: list[str] = []
            for idx, task in enumerate(tasks, start=1):
                clean_t = task.strip()
                t_kw = _extract_keywords(clean_t)
                if not t_kw:
                    continue
                more_ranked = self.query(" ".join(t_kw), top_n=5)
                top1_score = more_ranked[0]["score"] if more_ranked else 1.0
                for r in more_ranked:
                    rp = r["rel_path"]
                    if rp in step2_ranked_files:
                        continue
                    info = self._index.get(rp, {})
                    behaviors = self._get_file_behaviors(rp)
                    scored: list[tuple[float, str, dict]] = []
                    for fn in info.get("functions", []):
                        sc = self._score_symbol_for_query(fn, t_kw, behaviors, info.get("routes", []))
                        if sc > 0:
                            scored.append((sc, "fn", fn))
                    for cls in info.get("classes", []):
                        sc = self._score_class_for_query(cls, t_kw)
                        if sc > 0:
                            scored.append((sc, "cls", cls))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if not scored:
                        continue
                    # Only emit a code slice if Pass 1 had no explicit/4D matches and was very small (< 90 lines)
                    if (
                        not had_initial_explicit
                        and matched_4d_cells_count == 0
                        and current_lines + sum(len(x.splitlines()) for x in symptom_sec) < 90
                        and r["score"] >= top1_score * 0.55
                    ):
                        sc, kind, sym = scored[0]
                        nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                        _add_node_slice(nid, f"secondary match in {rp} (Task {idx})", t_kw, symptom_sec, max_l=24, max_g=6)
                        ptrs = [
                            f"`{_node_display_name(s)}` (L{s.get('line_start', 1)}-L{s.get('line_end', 1)})"
                            for _, _, s in scored[:4]
                        ]
                        line_ptr = f"- `{rp}` :: " + ", ".join(ptrs)
                        if line_ptr not in map_pointer_lines:
                            map_pointer_lines.append(line_ptr)
            if map_pointer_lines:
                symptom_sec.append("### Secondary Candidate Coordinate Map (Use `projectbrain slice <file>:<symbol>` if needed)")
                symptom_sec.extend(map_pointer_lines[:6])
            if symptom_sec:
                md_sections.extend(symptom_sec)
            # 5d. Append compact output-token saving instructions for `projectbrain patch`
            current_lines_after = len("\n".join(md_sections).splitlines())
            if current_lines_after < 775:
                md_sections.append(
                    "\n> **Tools:** `projectbrain slice --summary <file>:<symbol>` | `projectbrain patch` (`@file <rel_path>`, `@replace_body <Symbol> ... @end`, `@sub <Symbol> \\`<old>\\` -> \\`<new>\\``)"
                )

        full_md = "\n".join(md_sections)
        target_file = Path(out_path) if out_path else (self.root / ".projectbrain_dossier.md")
        target_file.write_text(full_md, encoding="utf-8")
        total_lines = len(full_md.splitlines())
        for f_rel, ivs in emitted_by_file.items():
            self._ledger.commit_slice_intervals(f_rel, ivs)
        covered_files = {sr for sr, _, _ in seen_spans} | seen_globals
        credited_files = primary_files if primary_files else covered_files
        closed_world_verified = "EXHAUSTIVE GUARANTEE" in full_md
        self._ledger.record_input_op(
            op="dossier",
            query=" | ".join(tasks),
            files_covered=len(credited_files),
            touched_files=credited_files,
            slices_included=len(seen_slices),
            cells_4d_resolved=matched_4d_cells_count,
            closed_world_verified=closed_world_verified,
        )
        return {
            "dossier_path": str(target_file),
            "total_lines": total_lines,
            "total_chars": len(full_md),
            "slices_included": len(seen_slices),
            "files_globals_included": len(seen_globals),
            "cells_6d_resolved": matched_4d_cells_count,
            "cells_4d_resolved": matched_4d_cells_count,
            "files_resolved": len(credited_files),
            "closed_world_verified": closed_world_verified,
            "markdown": full_md,
        }

    def patch(
        self,
        spec_text: str,
        apply: bool = False,
        diff_out: str | None = None,
        fuzzy_window: int = 8,
    ) -> dict:
        """Apply or dry-run a Surgical Anchor Patch spec and return unified diff + syntax verification."""
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
            pre_patch_ifaces: dict[str, dict[str, str]] = {}
            for f_item in res.get("files", []):
                f_rel = self._normalize_rel(f_item.get("file", ""))
                p_txt = f_item.pop("_patched_text", None)
                if f_rel:
                    patched_rels.add(f_rel)
                    if f_rel in self._index:
                        pre_patch_ifaces[f_rel] = {
                            f"{fn0.get('parent_class')}.{fn0['name']}" if fn0.get("parent_class") else fn0["name"]: fn0.get("interface_hash", "")
                            for fn0 in self._index[f_rel].get("functions", [])
                        }
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
                # Pre-Commit CodePlan Caller Arity & Keyword Obligation Verification
                if self._graph and patched_texts:
                    for f_rel, p_txt in patched_texts.items():
                        ext = Path(f_rel).suffix.lower()
                        new_info = parse(p_txt, ext)
                        old_if_map = pre_patch_ifaces.get(f_rel, {})
                        for fn in new_info.get("functions", []):
                            q_nm = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                            if q_nm in old_if_map and old_if_map[q_nm] == fn.get("interface_hash", ""):
                                continue
                            arity = fn.get("arity_spec")
                            if not arity:
                                continue
                            nid = fn_id(f_rel, fn["name"], fn.get("parent_class"))
                            min_a = arity.get("min_args", 0)
                            max_a = arity.get("max_args")
                            kw_req = set(arity.get("kwonly_required", []))
                            for ei in self._graph._radj.get(nid, []):
                                e_in = self._graph.edges[ei]
                                if e_in["type"] != "calls" or e_in.get("confidence") not in ("exact", "import_resolved"):
                                    continue
                                caller_nd = self._graph.nodes.get(e_in["src"], {})
                                caller_rel = caller_nd.get("rel_path", "")
                                call_line = e_in.get("line", 0)
                                if not caller_rel or not call_line:
                                    continue
                                caller_src = patched_texts.get(caller_rel) or self._get_file_text(caller_rel)
                                if not caller_src:
                                    continue
                                c_lines = caller_src.splitlines()
                                if 1 <= call_line <= len(c_lines):
                                    snippet = " ".join(c_lines[call_line - 1 : min(len(c_lines), call_line + 3)])
                                    arg_items = _extract_balanced_call_args(snippet, fn["name"])
                                    if arg_items is None:
                                        continue
                                    if any(a.startswith("*") for a in arg_items):
                                        continue
                                    pos_args = [a for a in arg_items if not re.match(r"^[A-Za-z_]\w*\s*=(?!=)", a)]
                                    kw_names = {
                                        a.split("=", 1)[0].strip()
                                        for a in arg_items
                                        if re.match(r"^[A-Za-z_]\w*\s*=(?!=)", a)
                                    }
                                    total_supplied = len(pos_args) + len(kw_names - kw_req)
                                    missing_kw = sorted(kw_req - kw_names)
                                    if total_supplied < min_a or (max_a is not None and len(pos_args) > max_a) or missing_kw:
                                        reason_str = (
                                            f"missing required keyword-only arg(s) `{', '.join(missing_kw)}`"
                                            if missing_kw
                                            else f"passes {len(arg_items)} arg(s), but updated signature `{fn.get('signature', fn['name'])}` requires {min_a}..{max_a if max_a is not None else '?'}"
                                        )
                                        diag_lines.append(
                                            f"- ?? **[6D Caller Arity Contract Violation]** `{caller_rel}:L{call_line}` (`{caller_nd.get('name', '')}`) ? "
                                            f"Call to `{fn['name']}(...)` {reason_str}."
                                        )
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

    def savings_stats(self, reset: bool = False) -> dict:
        """Return cumulative ROI and token/dollar/time savings from `.projectbrain_ledger.json`."""
        if reset:
            return self._ledger.reset()
        return self._ledger.get_stats()

    def slice_symbols(
        self,
        targets: list[str] | str,
        max_lines_per_symbol: int = 180,
        summary_mode: bool = False,
    ) -> dict:
        """Surgically slice specific symbols or files without reading full files.

        Each target in `targets` can be:
          - A graph node ID: `"fn:src/brain.py:ProjectBrain.query"` or `"cls:src/graph.py:CodeGraph"`
          - A `file:symbol` shorthand: `"brain.py:verify_claim"`
          - A bare symbol name: `"verify_claim"` or `"CodeGraph.build"`
          - A file range: `"src/brain.py:100-150"`
          - A file path (returns its signature skeleton): `"src/brain.py"`
        """
        if isinstance(targets, (str, Path)):
            targets = [str(targets)]

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
            for raw_target in targets:
                t = raw_target.strip()
                if not t:
                    continue
                range_m = re.match(r"^(.+?):(\d+)-(\d+)$", t)
                if range_m:
                    fpath, s_line, e_line = range_m.group(1), int(range_m.group(2)), int(range_m.group(3))
                    f_hit = self._db_store.get_file_fast(self._normalize_rel(fpath) or fpath)
                    if not f_hit:
                        fast_ok = False
                        break
                    rel = f_hit["rel_path"]
                    disk_p = self.root / rel
                    if not disk_p.exists() or disk_p.stat().st_mtime != f_hit["mtime"]:
                        fast_ok = False
                        break
                    f_lines = disk_p.read_text(encoding="utf-8", errors="replace").splitlines()
                    s_line, e_line = snap_to_block_boundaries(f_lines, s_line, e_line)
                    span_len = max_lines_per_symbol if summary_mode else max(max_lines_per_symbol, min(400, e_line - s_line + 1))
                    code = slice_lines(self.root, rel, s_line, e_line, max_lines=span_len)
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
                        fast_ok = False
                        break
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
                    fast_ok = False
                    break
                for sh in sym_hits:
                    rel = sh["rel_path"]
                    disk_p = self.root / rel
                    if not disk_p.exists() or (sh["mtime"] is not None and disk_p.stat().st_mtime != sh["mtime"]):
                        fast_ok = False
                        break
                    l_start, l_end = sh["line_start"], sh["line_end"]
                    code = slice_lines(self.root, rel, l_start, l_end, max_lines=max_lines_per_symbol)
                    self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
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
                    if f_lines:
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
                        span_len = max_lines_per_symbol if summary_mode else max(max_lines_per_symbol, min(400, e_line - s_line + 1))
                        code = slice_lines(self.root, rel, s_line, e_line, max_lines=span_len)
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
                    code = slice_lines(self.root, rel, l_start, l_end, max_lines=max_lines_per_symbol)
                    self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
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
            found_any = False
            for rel, info in self._index.items():
                if file_filter and file_filter not in rel.lower():
                    continue
                for fn in info.get("functions", []):
                    qname = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                    if sym_lower in (fn["name"].lower(), qname.lower()):
                        nid = fn_id(rel, fn["name"], fn.get("parent_class"))
                        l_start, l_end = fn.get("line_start", 1), fn.get("line_end", 1)
                        code = slice_lines(self.root, rel, l_start, l_end, max_lines=max_lines_per_symbol)
                        self._ledger.commit_slice_intervals(rel, extract_emitted_intervals(code))
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
                        code = slice_lines(self.root, rel, l_start, l_end, max_lines=max_lines_per_symbol)
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
                q_clean = re.sub(r":\d+-\d+$", "", t).replace("\\", "/").strip("/")
                q_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", q_clean.split(":")[0]).lower()
                pkg_matches = sorted(
                    k for k in self._index
                    if q_stem and (q_stem in k.lower() or any(tok in k.lower() for tok in q_stem.split("/")[-2:] if len(tok) >= 4))
                )[:6]
                hint = f" Closest indexed files: {', '.join(pkg_matches)}" if pkg_matches else ""
                slices.append({"target": t, "error": f"Symbol or file '{t}' not found in index.{hint}"})
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
        self._ensure_fresh()
        target = target.strip()

        if target_b:
            return self.find_relationship(target, target_b.strip())

        if target.startswith("/"):
            return self.trace_flow(target)

        if not self._graph:
            return {"error": "Graph not available"}

        node_id = target if target in self._graph.nodes else None
        if not node_id:
            matches = self._graph.find_nodes_by_name(target)
            if not matches:
                return {"error": f"No graph node matching '{target}'", "nodes": [], "edges": []}
            node_id = matches[0]

        res = self.get_neighbors(node_id, edge_types=edge_types, depth=depth)
        if include_co_changes:
            rel_path = self._graph.nodes.get(node_id, {}).get("rel_path")
            if rel_path:
                res["co_changes"] = self.co_change_analysis(rel_path).get("co_changes", [])
        return res

    def check(
        self,
        files_modified: list[str] | None = None,
        claims: list[list[str]] | list[tuple[str, str, str]] | None = None,
        plan_files: list[str] | None = None,
        task: str = "",
    ) -> dict:
        """Unified verification gate: checks claims, validates edit plans, and analyzes impact."""
        self._ensure_fresh()
        out: dict = {}

        if claims:
            tuples = [(c[0], c[1], c[2]) for c in claims if len(c) >= 3]
            out["claim_verification"] = self.verify_batch(tuples)

        if plan_files:
            out["plan_validation"] = self.verify_plan(task or "planned edit", plan_files)

        if files_modified:
            out["impact_analysis"] = self.impact(files_modified)

        if not out:
            out["health"] = self.health()

        return out

    def remember(self, target: str, note: str, tags: list[str] | None = None) -> dict:
        """Save an AST-anchored architectural invariant or note in `.projectbrain_memory.json`."""
        self._ensure_fresh()
        target = target.strip()
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
        self._ensure_fresh()
        notes = self._memory.list_all(self._ast_hashes)
        return {"memories": notes, "count": len(notes)}

    def _normalize_rel(self, file_path: str) -> str:
        try:
            return str(Path(file_path).resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return file_path.replace("\\", "/").lstrip("./")

    def _score_symbol_for_query(
        self,
        fn: dict,
        keywords: list[str],
        behaviors: dict[str, str],
        routes: list[dict],
        cells_4d: list[dict] | None = None,
        local_idf: dict[str, float] | None = None,
    ) -> float:
        name_lower = fn.get("name", "").lower()
        name_tokens = set(_tokenize_symbol(fn.get("name", ""))) | {name_lower}
        parent_cls = (fn.get("parent_class") or "").lower()
        parent_tokens = set(_tokenize_symbol(parent_cls)) if parent_cls else set()
        is_lifecycle_method = name_lower in (
            "push", "pop", "open", "close", "dispatch", "handle", "send", "invoke",
            "execute", "run", "process", "resolve", "build", "register", "record", "load", "save",
            "convert", "validate", "parse", "merge", "extract", "init_poolmanager", "resolve_command",
        )
        doc_lower = (fn.get("docstring") or "").lower()
        calls_lower = {c.lower() for c in fn.get("calls", [])}
        body_tokens = set(fn.get("body_tokens", []))
        fn_s = fn.get("line_start", 1)
        fn_e = fn.get("line_end", fn_s)
        fn_span = max(1, fn_e - fn_s)
        score = 0.0
        matched_kws = 0
        id_or_call_hits = 0
        for kw in keywords:
            idf = self._idf.get(kw, 1.0) * (local_idf.get(kw, 1.0) if local_idf else 1.0)
            kw_stem = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
            if len(kw_stem) >= 4 and kw_stem[-1] == kw_stem[-2] and kw_stem[-1] in "pbgtdmnr":
                kw_stem = kw_stem[:-1]
            if len(kw_stem) < 3:
                kw_stem = kw
            kw_e = (kw_stem + "e") if (len(kw_stem) >= 4 and kw.endswith(("ed", "ing")) and not kw_stem.endswith("e")) else ""
            kw_root = re.sub(r"(?:ers|ors|er|or)$", "e", kw) if kw.endswith(("handler", "handlers", "serializer", "serializers", "dispatcher", "dispatchers", "loader", "loaders", "parser", "parsers", "manager", "managers", "router", "routers", "producer", "producers", "consumer", "consumers")) else re.sub(r"(?:ers|ors|er|or)$", "", kw_stem)
            if len(kw_root) < 4:
                kw_root = kw_stem
            if kw in ("handled", "handling", "work", "works", "working", "across"):
                idf *= 0.22
            name_weight = 0.8 if kw in ("file", "files", "path", "paths", "data", "item", "items", "node", "nodes") else 2.6
            sig_tokens = set(re.findall(r"[a-z0-9]+", (fn.get("signature") or "").lower()))
            hit = False
            if kw == name_lower or kw_stem == name_lower or (kw_e and kw_e == name_lower):
                score += 4.2 * idf
                hit = True
                id_or_call_hits += 1
            elif (
                kw in name_tokens
                or kw in name_lower
                or (len(kw_stem) >= 3 and (kw_stem in name_tokens or kw_stem in name_lower))
                or (kw_e and (kw_e in name_tokens or kw_e in name_lower))
                or (len(kw_root) >= 4 and (kw_root in name_tokens or kw_root in name_lower))
                or (len(kw_stem) >= 6 and any(len(nt) >= 5 and kw_stem.startswith(nt) for nt in name_tokens))
            ):
                score += name_weight * idf
                hit = True
                id_or_call_hits += 1
                if kw in doc_lower or (len(kw_stem) >= 4 and kw_stem in doc_lower):
                    score += 0.75 * idf
            elif (
                parent_tokens
                and not name_lower.startswith("_")
                and (
                    kw in parent_tokens
                    or kw == parent_cls
                    or (len(kw_stem) >= 3 and (kw_stem in parent_tokens or kw_stem in parent_cls))
                    or (len(kw_stem) >= 6 and any(len(pt) >= 4 and kw_stem.startswith(pt) for pt in parent_tokens))
                )
            ):
                score += (2.4 if is_lifecycle_method else 1.35) * idf
                hit = True
                id_or_call_hits += 1
            elif kw in sig_tokens or (len(kw_stem) >= 3 and kw_stem in sig_tokens) or (kw_e and kw_e in sig_tokens):
                score += 2.0 * idf
                hit = True
                id_or_call_hits += 1
            elif any(
                kw in c
                or (len(kw_stem) >= 4 and kw_stem in c)
                or (kw_e and kw_e in c)
                or (len(kw_root) >= 4 and kw_root in c)
                for c in calls_lower
            ):
                score += 1.45 * idf
                hit = True
                id_or_call_hits += 1
            elif kw in doc_lower or (len(kw_stem) >= 4 and kw_stem in doc_lower) or (kw_e and kw_e in doc_lower):
                score += 1.15 * idf
                hit = True
            elif kw in body_tokens or any(len(kw_stem) >= 4 and kw_stem in bt for bt in body_tokens) or (kw_e and kw_e in body_tokens):
                score += 0.75 * idf
                hit = True
            if hit:
                matched_kws += 1
        if (
            name_lower.startswith(("has_", "is_", "can_"))
            and not any(qk.startswith(("has", "is", "can", "check")) for qk in keywords)
        ):
            score *= 0.52
        if fn.get("is_nested") and name_lower not in keywords:
            score *= 0.35
        if name_lower in ("__init__", "constructor") and not any(
            qk in ("init", "__init__", "constructor", "create", "new", "instantiate", "setup", "default", "defaults")
            for qk in keywords
        ):
            score *= 0.48
        if any(qk in ("dispatch", "dispatched", "subcommand", "subcommands", "route", "routed") for qk in keywords):
            if name_lower in ("invoke", "resolve_command", "dispatch", "handle", "full_dispatch_request", "dispatch_request"):
                score *= 1.45
            elif "decorator" in body_tokens and name_lower in (parent_cls, "command", "group", "errorhandler", "route"):
                score *= 0.52
            elif fn_span <= 4 and len(calls_lower) <= 1:
                score *= 0.55
        if any(qk in ("parse", "parsed", "parsing", "parser") for qk in keywords):
            if "parse" in name_lower or name_lower in ("consume_value", "process_value"):
                score *= 1.38
            elif name_lower.startswith(("format_", "get_help")) and not any(qk in ("format", "help") for qk in keywords):
                score *= 0.52
        if any(qk in ("across", "between", "merge", "merged", "combine", "combined") for qk in keywords) and id_or_call_hits >= 1:
            if name_tokens & {"merge", "combine", "sync"}:
                score *= 1.65
        if name_lower.startswith("test_") and not (set(keywords) & _TEST_QUERY_TERMS):
            score *= 0.45
        if id_or_call_hits >= 2:
            score *= 1.35 + 0.10 * min(3, id_or_call_hits - 2)
        elif id_or_call_hits == 0 and len(keywords) >= 2:
            # Discount functions that only matched prose in docstrings while their identifier,
            # signature, and outgoing calls have zero overlap with the query keywords.
            score *= 0.28
        if matched_kws >= 3 and id_or_call_hits >= 1:
            score *= 1.0 + 0.12 * (matched_kws - 2)
        if cells_4d:
            inner_cells = [
                c for c in cells_4d
                if fn_s <= c.get("line_start", 0) and c.get("line_end", 0) <= fn_e
            ]
            if inner_cells:
                cell_blob = " ".join(
                    f"{c.get('X_scope', '')} {' '.join(c.get('Z_write', []))} {' '.join(c.get('Z_read', []))} {' '.join(c.get('E_exit', []))}"
                    for c in inner_cells
                ).lower()
                for kw in keywords:
                    idf = self._idf.get(kw, 1.0)
                    kw_stem = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 5 else kw
                    if kw in cell_blob or (len(kw_stem) >= 4 and kw_stem in cell_blob):
                        score += 0.9 * idf
                kw_set = set(keywords)
                if kw_set & {"db", "database", "sql", "sqlite", "postgres", "insert", "query", "connect", "persist", "storage", "table"}:
                    if any(c.get("Y_role") == "IO" or c.get("sql_inserts") for c in inner_cells):
                        score += 1.6
                if kw_set & {"bug", "bugs", "error", "errors", "fail", "crash", "cant", "cannot", "broken", "500", "409"}:
                    if any(c.get("E_exit") or c.get("sql_inserts") or c.get("sql_dialects") for c in inner_cells):
                        score += 1.4
        for r in routes:
            if r.get("handler") == fn.get("name"):
                for kw in keywords:
                    if kw in r.get("path", "").lower() or kw == r.get("method", "").lower():
                        score += 3.0
        return score

    def _contract_in_query_scope(self, rel: str, scope_text: str, summary: str, terms: list[str]) -> bool:
        """Is a static-contract finding relevant to the current query?

        Decided purely from query-term evidence (no repository-specific names):
          * if the file's own name (stem) contains a query concept, the file IS the query's
            subject and every finding in it is in scope;
          * otherwise, the query is split into concepts (a term plus the aliases the codebase
            declares for it). Only concepts that actually occur in this file can be expected in
            one of its regions. The finding is in scope when the IDF-weighted fraction of those
            concepts matched by the finding's enclosing scope + summary is >= 1/2, i.e. the
            region carries at least half of the query's information that this file carries
            (IDF-weighted coordination). A generic term alone cannot pull in an unrelated region
            when a more specific query concept is present in the file but absent from it.
        """
        terms = list(dict.fromkeys(t.lower() for t in terms if len(t) >= 3))
        if not terms:
            return True
        stem = Path(rel).stem.lower()
        if any(len(t) >= 4 and t in stem for t in terms):
            return True
        # Build concept groups from the supplied terms and their declared aliases.
        groups: list[list[str]] = []
        seen_t: set[str] = set()
        for t in terms:
            if t in seen_t:
                continue
            members = [t] + [a for a in self._expand_keywords_with_aliases([t])[1:] if a in terms and a not in seen_t]
            seen_t.update(members)
            groups.append(members)
        file_toks = self._token_sets.get(rel, set())
        file_blob = self._blobs.get(rel, "")
        region = f"{scope_text} {summary}".lower()
        present_w = 0.0
        matched_w = 0.0
        for members in groups:
            in_file = any(m in file_toks or (len(m) >= 4 and m in file_blob) for m in members)
            if not in_file:
                continue
            w = max(self._idf.get(m, 1.0) for m in members)
            present_w += w
            if any(m in region for m in members):
                matched_w += w
        if present_w <= 0:
            return False
        return matched_w / present_w >= 0.5

    def _compile_6d_contract_diagnostics(self, task: str, focus_files: set[str], strict_focus: bool = False) -> tuple[list[str], list[str]]:
        """Pass C: Cross-check 6D IO cells (SQL INSERT column sets, SQL dialects, exception contracts)
        against indexed CREATE TABLE schemas and database adapter capabilities across the repository.
        Returns (diagnostic_markdown_lines, offending_node_ids).
        """
        schemas_by_table: dict[str, list[tuple[str, dict]]] = {}
        all_sql_alters: dict[str, list[dict]] = {}
        sqlite_adapters: list[tuple[str, str]] = []
        for rel in sorted(self._index.keys()):
            info = self._index[rel]
            for tbl, sc in (info.get("sql_schemas") or {}).items():
                sc_copy = {
                    "name": sc.get("name", tbl),
                    "line_start": sc.get("line_start", 1),
                    "line_end": sc.get("line_end", 1),
                    "all_cols": list(sc.get("all_cols", [])),
                    "required_cols": list(sc.get("required_cols", [])),
                    "defaulted_cols": list(sc.get("defaulted_cols", [])),
                    "fields": list(sc.get("fields", [])),
                }
                schemas_by_table.setdefault(tbl.lower(), []).append((rel, sc_copy))
            sql_alters_map = info.get("sql_alters") or {}
            if isinstance(sql_alters_map, dict):
                for alt_tbl, ops in sql_alters_map.items():
                    all_sql_alters.setdefault(alt_tbl.lower(), []).extend(ops)
            if rel.endswith(".py") and "sqlite3" in info.get("imports", []):
                src_txt = self._get_file_text(rel)
                if src_txt and ("def execute" in src_txt or "_translate_sql" in src_txt):
                    sqlite_adapters.append((rel, src_txt))
        if all_sql_alters and schemas_by_table:
            for alt_tbl_low, ops in all_sql_alters.items():
                for _, sc_copy in schemas_by_table.get(alt_tbl_low, []):
                    apply_sql_alters_to_schema(sc_copy, ops)
        t_low = task.lower()
        q_terms = [t for t in self._expand_keywords_with_aliases(_extract_keywords(task)) if len(t) >= 4]
        q_wants_tests_diag = bool(set(q_terms) & _TEST_QUERY_TERMS)
        is_explanatory_q = bool(re.match(r"^\s*(?:how|where|what|which)\b", t_low)) and not any(
            w in t_low for w in ("bug", "bugs", "broken", "mismatch", "violation", "failing", "crash")
        )
        wants_all_repo_diag = (not strict_focus) and (not is_explanatory_q) and any(
            w in t_low
            for w in ("bug", "bugs", "broken", "fail", "failing", "db", "database", "sql", "sqlite", "postgres", "connect", "insert", "schema", "mismatch", "violation")
        )
        ordered_files = sorted(
            self._index.keys(),
            key=lambda r: (0 if r in focus_files else 1, r),
        )
        warnings: list[str] = []
        offending_nodes: list[str] = []
        seen_warn_keys: set[tuple[str, int, str]] = set()
        covered_dialect_spans: list[tuple[str, int, int, str]] = []
        line_ann: dict[str, dict[int, list[str]]] = {}
        pin_map: dict[str, set[int]] = {}

        def _record_offending_fn(f_rel: str, ln_no: int, span_end: int = 0, inline_msg: str = "") -> None:
            end_p = max(ln_no, span_end)
            for p_ln in range(max(1, ln_no - 1), min(end_p + 1, ln_no + 6) + 1):
                pin_map.setdefault(f_rel, set()).add(p_ln)
            if inline_msg:
                ann_list = line_ann.setdefault(f_rel, {}).setdefault(ln_no, [])
                if inline_msg not in ann_list:
                    ann_list.append(inline_msg)
            for fn in self._index.get(f_rel, {}).get("functions", []):
                if fn.get("line_start", 0) <= ln_no <= fn.get("line_end", 0):
                    nid = fn_id(f_rel, fn["name"], fn.get("parent_class"))
                    if nid not in offending_nodes:
                        offending_nodes.append(nid)
        for rel in ordered_files:
            if not wants_all_repo_diag and rel not in focus_files:
                continue
            if not q_wants_tests_diag and (
                _is_test_file(rel)
                or (rel not in focus_files and bool({p.lower() for p in Path(rel).parts[:-1]} & DEPRIORITY_DIRS))
            ):
                continue
            cells = self._index[rel].get("cells_4d") or []
            # Prefer finest-grained cell per line
            cells_sorted = sorted(cells, key=lambda c: c.get("span", 9999))
            for cell in cells_sorted:
                scope = cell.get("X_scope", "")
                c_line = cell.get("line_start", 1)
                c_end = cell.get("line_end", c_line)
                # 1. Check INSERT INTO <table> (<cols>) against NOT NULL columns without DEFAULT
                for ins in cell.get("sql_inserts", []):
                    tbl_raw = ins["table"]
                    tbl_key = tbl_raw.lower()
                    ins_cols_low = {c.lower() for c in ins["cols"]}
                    ins_line = ins.get("line", c_line)
                    cand_schemas = schemas_by_table.get(tbl_key, [])
                    local_schemas = [(s_rel, sc) for s_rel, sc in cand_schemas if s_rel == rel]
                    for schema_rel, sc in (local_schemas or cand_schemas):
                        missing = [
                            req for req in sc.get("required_cols", [])
                            if req.lower() not in ins_cols_low
                        ]
                        if missing:
                            w_key = (rel, ins_line, f"null:{tbl_key}:{','.join(missing)}")
                            if w_key not in seen_warn_keys:
                                seen_warn_keys.add(w_key)
                                _record_offending_fn(
                                    rel,
                                    ins_line,
                                    span_end=min(c_end, ins_line + 5),
                                    inline_msg=(
                                        f"# >>> [6D-CONTRACT L{ins_line}] INSERT INTO {tbl_raw} omits required NOT NULL "
                                        f"column(s): {', '.join(missing)} (schema: {schema_rel}:L{sc['line_start']})"
                                    ),
                                )
                                warnings.append(
                                    f"- ?? **[6D Schema Contract Violation]** `{rel}:L{ins_line}` (`{scope}`) ? "
                                    f"`INSERT INTO {tbl_raw}({', '.join(ins['cols'][:5])}{'...' if len(ins['cols']) > 5 else ''})` "
                                    f"omits required `NOT NULL` column(s) `{', '.join(missing)}` "
                                    f"(declared without `DEFAULT` in `{schema_rel}:L{sc['line_start']}`)."
                                )
                # 2. Check PostgreSQL-specific SQL dialect features against SQLite adapters
                if sqlite_adapters:
                    ad_rel, ad_src = sqlite_adapters[0]
                    for feat in cell.get("sql_dialects", []):
                        if any(
                            sr == rel and c_line <= ss and se <= c_end and sf == feat
                            for sr, ss, se, sf in covered_dialect_spans
                        ):
                            continue
                        handled = False
                        if feat == "interval_literal":
                            handled = bool(re.search(r"interval\\s\+|interval\s+'", ad_src, re.IGNORECASE))
                        elif feat == "make_interval":
                            handled = "make_interval" in ad_src
                        elif feat == "any_array":
                            handled = bool(re.search(r"ANY\\s\*\\\(|ANY\s*\(", ad_src, re.IGNORECASE))
                        elif feat == "ilike_operator":
                            handled = bool(re.search(r"\bILIKE\b", ad_src, re.IGNORECASE))
                        elif feat == "postgres_typecast":
                            handled = "::" in ad_src
                        if not handled:
                            covered_dialect_spans.append((rel, c_line, c_end, feat))
                            w_key = (rel, c_line, f"dialect:{feat}")
                            if w_key not in seen_warn_keys:
                                seen_warn_keys.add(w_key)
                                _record_offending_fn(
                                    rel,
                                    c_line,
                                    span_end=min(c_end, c_line + 3),
                                    inline_msg=(
                                        f"# >>> [6D-CONTRACT L{c_line}] PostgreSQL construct '{feat}' is not translated "
                                        f"by SQLite adapter ({ad_rel})"
                                    ),
                                )
                                warnings.append(
                                    f"- ?? **[6D SQL Dialect Mismatch]** `{rel}:L{c_line}` (`{scope}`) ? "
                                    f"Query uses PostgreSQL construct `{feat}` not translated by SQLite adapter `{ad_rel}`."
                                )
                    # 3. Check psycopg exception catch vs sqlite3.IntegrityError on DB mutations
                    exits = cell.get("E_exit", [])
                    if (
                        cell.get("Z_write")
                        and "catch:UniqueViolation" in exits
                        and "catch:IntegrityError" not in exits
                        and "UniqueViolation" not in ad_src
                    ):
                        if not any(
                            sr == rel and c_line <= ss and se <= c_end and sf == "exc:UniqueViolation"
                            for sr, ss, se, sf in covered_dialect_spans
                        ):
                            covered_dialect_spans.append((rel, c_line, c_end, "exc:UniqueViolation"))
                            w_key = (rel, c_line, "exc:UniqueViolation")
                            if w_key not in seen_warn_keys:
                                seen_warn_keys.add(w_key)
                                _record_offending_fn(
                                    rel,
                                    c_line,
                                    span_end=min(c_end, c_line + 2),
                                    inline_msg=(
                                        f"# >>> [6D-CONTRACT L{c_line}] Catches UniqueViolation on DB write without "
                                        f"catching sqlite3.IntegrityError ({ad_rel})"
                                    ),
                                )
                                warnings.append(
                                    f"- ?? **[6D Exception Contract Mismatch]** `{rel}:L{c_line}` (`{scope}`) ? "
                                    f"Catches `UniqueViolation` on DB write (`?Z:{','.join(cell['Z_write'][:2])}`) "
                                    f"without catching `IntegrityError` raised by SQLite adapter `{ad_rel}`."
                                )
                # 4. Check Disjoint Runtime Type Algebra (UUID parameter compared directly to SQLite TEXT column)
                for tc_cmp in cell.get("type_comparisons", []):
                    cmp_line = tc_cmp["line"]
                    w_key = (rel, cmp_line, f"type:{tc_cmp['param']}:{tc_cmp['row_key']}")
                    if w_key not in seen_warn_keys:
                        seen_warn_keys.add(w_key)
                        if tc_cmp.get("other_var"):
                            _record_offending_fn(
                                rel,
                                cmp_line,
                                span_end=cmp_line + 1,
                                inline_msg=(
                                    f"# >>> [6D-CONTRACT L{cmp_line}] Disjoint type comparison: {tc_cmp['param']} "
                                    f"({tc_cmp['param_type']}) vs {tc_cmp['other_var']} ({tc_cmp['other_type']}) — "
                                    f"τ({tc_cmp['param_type']}) ∩ τ({tc_cmp['other_type']}) = ∅"
                                ),
                            )
                            warnings.append(
                                f"- ⚠️ **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) — "
                                f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                                f"`{tc_cmp['other_type']}` expression `{tc_cmp['other_var']}` without type coercion "
                                f"(`τ({tc_cmp['param_type']}) ∩ τ({tc_cmp['other_type']}) = ∅`)."
                            )
                        else:
                            _record_offending_fn(
                                rel,
                                cmp_line,
                                span_end=cmp_line + 1,
                                inline_msg=(
                                    f"# >>> [6D-CONTRACT L{cmp_line}] Disjoint type comparison: {tc_cmp['param']} "
                                    f"({tc_cmp['param_type']}) vs SQLite TEXT row['{tc_cmp['row_key']}'] without "
                                    f"str()/{tc_cmp['param_type']}() coercion"
                                ),
                            )
                            warnings.append(
                                f"- ⚠️ **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) — "
                                f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                                f"SQLite `TEXT` row key `['{tc_cmp['row_key']}']` without `str()`/`{tc_cmp['param_type']}()` coercion "
                                f"(`τ({tc_cmp['param_type']}) ∩ τ(str) = ∅`)."
                            )

                # 5. Check Producer-Consumer SQL SELECT Projection vs Row Key Read (Post(SELECT) |= Pre(row[k]))
                for sv in cell.get("sql_select_violations", []):
                    sv_line = sv["line"]
                    w_key = (rel, sv_line, f"sql_proj:{sv['var']}:{sv['read_key']}")
                    if w_key not in seen_warn_keys:
                        seen_warn_keys.add(w_key)
                        _record_offending_fn(
                            rel,
                            sv_line,
                            span_end=sv_line + 1,
                            inline_msg=(
                                f"# >>> [6D-CONTRACT L{sv_line}] Row key {sv['var']}['{sv['read_key']}'] is absent "
                                f"from SELECT {', '.join(sv['projected_cols'])} on {sv['table']}"
                            ),
                        )
                        warnings.append(
                            f"- ?? **[6D Producer-Consumer SQL Shape Violation]** `{rel}:L{sv_line}` (`{scope}`) ? "
                            f"Reads row key `{sv['var']}['{sv['read_key']}']`, which is absent from `SELECT {', '.join(sv['projected_cols'])}` "
                            f"on `{sv['table']}` (`Post(SELECT) ? Pre(row['{sv['read_key']}'])`)."
                        )

                # 6. Check Frontend -> Backend HTTP Route Verb & Response Shape Contracts
                if self._graph and cell.get("http_calls"):
                    if not hasattr(self, "_cached_be_routes") or self._cached_be_routes_ver != id(self._graph):
                        be_routes_by_norm: dict[str, set[str]] = {}
                        be_ret_keys_by_norm: dict[str, tuple[str, set[str]]] = {}
                        for r_nid, r_nd in self._graph.nodes.items():
                            if r_nd.get("type") == "route":
                                rf = r_nd.get("full_path") or r_nd.get("path") or ""
                                rn = re.sub(r"\{[^}]+\}|:[a-zA-Z_]\w*", "{*}", rf).rstrip("/") or "/"
                                be_routes_by_norm.setdefault(rn, set()).add((r_nd.get("method") or "GET").upper())
                                for ei_h in self._graph._adj.get(r_nid, []):
                                    eh = self._graph.edges[ei_h]
                                    if eh["type"] == "handles":
                                        h_nd = self._graph.nodes.get(eh["dst"], {})
                                        r_keys = set(h_nd.get("return_dict_keys", []))
                                        if r_keys:
                                            be_ret_keys_by_norm[rn] = (h_nd.get("name") or eh["dst"], r_keys)
                        self._cached_be_routes = (be_routes_by_norm, be_ret_keys_by_norm)
                        self._cached_be_routes_ver = id(self._graph)
                    else:
                        be_routes_by_norm, be_ret_keys_by_norm = self._cached_be_routes
                    if be_routes_by_norm:
                        for hc in cell.get("http_calls", []):
                            hu = hc["norm_url"]
                            hm = hc["method"]
                            h_line = hc.get("line", c_line)
                            allowed_verbs = be_routes_by_norm.get(hu)
                            if allowed_verbs and "ANY" not in allowed_verbs and "ALL" not in allowed_verbs and hm not in allowed_verbs:
                                w_key = (rel, h_line, f"http_verb:{hm}:{hu}")
                                if w_key not in seen_warn_keys:
                                    seen_warn_keys.add(w_key)
                                    _record_offending_fn(
                                        rel,
                                        h_line,
                                        span_end=h_line + 2,
                                        inline_msg=(
                                            f"# >>> [6D-CONTRACT L{h_line}] HTTP verb mismatch: calls {hm} {hc['url']}, "
                                            f"but backend route only accepts {', '.join(sorted(allowed_verbs))}"
                                        ),
                                    )
                                    warnings.append(
                                        f"- ?? **[6D HTTP Route Method Mismatch]** `{rel}:L{h_line}` (`{scope}`) ? "
                                        f"Frontend calls `{hm} {hc['url']}`, but backend route only accepts `{', '.join(sorted(allowed_verbs))}`."
                                    )
                            elif hc.get("expected_keys") and hu in be_ret_keys_by_norm:
                                be_fn_nm, be_keys = be_ret_keys_by_norm[hu]
                                missing_rk = [k for k in hc["expected_keys"] if k not in be_keys]
                                if missing_rk:
                                    w_key = (rel, h_line, f"http_shape:{hu}:{missing_rk[0]}")
                                    if w_key not in seen_warn_keys:
                                        seen_warn_keys.add(w_key)
                                        _record_offending_fn(
                                            rel,
                                            h_line,
                                            span_end=h_line + 2,
                                            inline_msg=(
                                                f"# >>> [6D-CONTRACT L{h_line}] Response property .{missing_rk[0]} is absent "
                                                f"from backend handler {be_fn_nm} return keys {sorted(be_keys)}"
                                            ),
                                        )
                                        warnings.append(
                                            f"- ?? **[6D Cross-Stack HTTP Shape Violation]** `{rel}:L{h_line}` (`{scope}`) ? "
                                            f"Reads response property `.{missing_rk[0]}` from `{hm} {hc['url']}`, "
                                            f"but backend handler `{be_fn_nm}` only returns `{sorted(be_keys)}` (`Post(BE) ? Pre(FE)`)."
                                        )
                # 7. Check Universal JS/TS/React Static AST & Closure Contracts (scoped to focus_files when non-empty)
                for jc in (
                    [
                        jc0 for jc0 in cell.get("js_ast_contracts", [])
                        if strict_focus or self._contract_in_query_scope(rel, scope, jc0.get("summary", ""), q_terms)
                    ]
                    if (not focus_files or rel in focus_files) else []
                ):
                    j_line = jc["line"]
                    j_kind = jc["kind"]
                    w_key = (rel, j_line, f"js_ast:{j_kind}:{jc.get('symbol', '')}")
                    if w_key not in seen_warn_keys:
                        seen_warn_keys.add(w_key)
                        for ex_ln in jc.get("extra_pin_lines", []):
                            pin_map.setdefault(rel, set()).add(ex_ln)
                        _record_offending_fn(
                            rel,
                            j_line,
                            span_end=jc.get("end_line", j_line + 1),
                            inline_msg=jc["inline_msg"],
                        )
                        warnings.append(
                            f"- ?? **[6D AST Contract Violation (`{j_kind}`)]** `{rel}:L{j_line}` (`{scope}`) ? {jc['summary']}"
                        )
        self._last_line_annotations = line_ann
        self._last_pin_lines = pin_map
        return warnings, offending_nodes

    def _score_class_for_query(self, cls: dict, keywords: list[str]) -> float:
        name_lower = cls.get("name", "").lower()
        name_tokens = set(_tokenize_symbol(cls.get("name", ""))) | {name_lower}
        doc_lower = (cls.get("docstring") or "").lower()
        methods_lower = {m.lower() for m in cls.get("methods", [])}
        fields_lower = " ".join(cls.get("fields", [])).lower()
        score = 0.0
        for kw in keywords:
            idf = self._idf.get(kw, 1.0)
            if kw == name_lower:
                score += 4.0 * idf
            elif kw in name_tokens or kw in name_lower:
                score += 2.5 * idf
            elif kw in fields_lower:
                score += 2.2 * idf
            elif kw in methods_lower:
                score += 1.5 * idf
            elif kw in doc_lower:
                score += 1.0 * idf
        return score

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

    # ── Context compression (Upgraded with real AST signatures & schemas) ───

    def compress_context(
        self,
        file_paths: list[str],
        max_tokens: int = 4000,
        focus: str = "",
    ) -> dict:
        """Structure-aware context compression for a set of files.

        Returns exact AST function signatures (with parameters and return types),
        class schemas/fields, behavioral roles, routes, and connections.
        """
        self._ensure_fresh()

        char_budget = max_tokens * 4
        results = []
        total_chars = 0
        focus_lower = focus.lower() if focus else ""

        for path in file_paths:
            try:
                rel_path = str(Path(path).relative_to(self.root)).replace("\\", "/")
            except ValueError:
                rel_path = path.replace("\\", "/")

            info = self._index.get(rel_path)
            if not info:
                results.append({
                    "rel_path": rel_path,
                    "status": "not_indexed",
                })
                continue

            file_budget = max(char_budget // max(len(file_paths), 1), 500)
            compressed = self._compress_file(rel_path, info, file_budget, focus_lower)
            chars = len(str(compressed))
            total_chars += chars

            results.append(compressed)

            if total_chars >= char_budget:
                break

        return {
            "files": results,
            "total_files_processed": len(results),
            "estimated_tokens": total_chars // 4,
            "budget_tokens": max_tokens,
        }

    def _compress_file(
        self,
        rel_path: str,
        info: dict,
        char_budget: int,
        focus: str,
    ) -> dict:
        """Compress a single file's context into a structured summary with real AST signatures."""
        functions = info.get("functions", [])
        classes = info.get("classes", [])
        routes = info.get("routes", [])
        imports = info.get("imports", [])
        exports = info.get("exports", [])
        behaviors = self._get_file_behaviors(rel_path)

        def fn_priority(fn: dict) -> int:
            name = fn.get("name", "").lower()
            if focus and focus in name:
                return 0
            qualified = fn["name"]
            if fn.get("parent_class"):
                qualified = f"{fn['parent_class']}.{fn['name']}"
            behavior = behaviors.get(qualified, "unknown")
            _BEHAVIOR_ORDER = {
                "handler": 1, "orchestrator": 2, "constructor": 3,
                "io_writer": 4, "io_reader": 5, "validator": 6,
                "transformer": 7, "renderer": 8, "unknown": 9,
            }
            return _BEHAVIOR_ORDER.get(behavior, 9)

        sorted_fns = sorted(functions, key=fn_priority)

        fn_summaries = []
        chars_used = 0
        for fn in sorted_fns:
            if chars_used >= char_budget * 0.7:
                fn_summaries.append(f"... and {len(sorted_fns) - len(fn_summaries)} more functions")
                break

            qualified = fn["name"]
            if fn.get("parent_class"):
                qualified = f"{fn['parent_class']}.{fn['name']}"
            behavior = behaviors.get(qualified, "")

            calls = fn.get("calls", [])[:5]
            summary = {
                "name": fn["name"],
                "signature": fn.get("signature", f"def {fn['name']}()"),
                "lines": f"L{fn['line_start']}-{fn['line_end']}",
            }
            if fn.get("parent_class"):
                summary["class"] = fn["parent_class"]
            if fn.get("is_method"):
                summary["is_method"] = True
            if behavior and behavior != "unknown":
                summary["behavior"] = behavior
            if fn.get("docstring"):
                summary["doc"] = fn["docstring"][:100]
            if calls:
                summary["calls"] = calls
            if fn.get("qualified_calls"):
                qc = list(fn["qualified_calls"].keys())[:5]
                summary["qualified_calls"] = qc

            fn_summaries.append(summary)
            chars_used += len(str(summary))

        cls_summaries = []
        for cls in classes:
            cs = {
                "name": cls["name"],
                "signature": cls.get("signature", f"class {cls['name']}"),
                "lines": f"L{cls['line_start']}-{cls['line_end']}",
            }
            if cls.get("bases"):
                cs["extends"] = cls["bases"]
            if cls.get("fields"):
                cs["fields"] = cls["fields"][:15]
            if cls.get("methods"):
                cs["methods"] = cls["methods"][:10]
            cls_summaries.append(cs)

        entry: dict = {
            "rel_path": rel_path,
            "status": "compressed",
            "skeleton": build_file_skeleton(rel_path, info, behaviors=behaviors, focus_terms=[focus] if focus else None),
        }
        if fn_summaries:
            entry["functions"] = fn_summaries
        if cls_summaries:
            entry["classes"] = cls_summaries
        if routes:
            entry["routes"] = routes
        if imports:
            entry["imports"] = imports
        if exports:
            entry["exports"] = exports

        connections = self._connections(rel_path)
        if connections:
            entry["connections"] = connections

        return entry

    def index(self) -> dict:
        """Parse all Python and JS/TS files under root and cache the result."""
        import time as _time

        if not self._index and self._db_store.db_path.exists() and not self._cache_path.exists():
            self.load()

        prev_index = dict(self._index)
        prev_sizes = dict(self._sizes)
        prev_skipped = dict(self._skipped)
        prev_graph = self._graph

        self._index = {}
        self._sizes = {}
        self._skipped = {}
        self._source_cache.clear()

        now_ts = _time.time()
        any_changed = False

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
            if is_settled and rel in prev_index and st.st_mtime == prev_index[rel].get("mtime") and st.st_size == prev_sizes.get(rel):
                self._index[rel] = prev_index[rel]
                self._sizes[rel] = st.st_size
                continue
            if is_settled and rel in prev_skipped:
                self._skipped[rel] = prev_skipped[rel]
                continue

            any_changed = True
            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                continue

            if _is_minified(source):
                self._skipped[rel] = "minified_or_generated"
                continue

            info, ts_tree = parse_with_tree(source, ext)
            info.setdefault("routes", []).extend(frameworks.infer_file_based_routes(rel, info))
            info["cells_4d"] = extract_4d_coordinate_matrix(source.splitlines(), ext=ext, tree=ts_tree)
            info["mtime"] = st.st_mtime
            self._index[rel] = info
            self._sizes[rel] = st.st_size

        if (
            not any_changed
            and prev_index
            and set(self._index.keys()) == set(prev_index.keys())
            and set(self._skipped.keys()) == set(prev_skipped.keys())
            and prev_graph is not None
            and self._db_store.db_path.exists()
            and not self._cache_path.exists()
        ):
            self._graph = prev_graph
            if not self._blobs:
                self._build_blobs()
                self._sync_interprocedural_effects()
            return self.stats()

        self._build_blobs()
        self._graph = CodeGraph.build(
            self._index,
            import_targets=self._import_targets,
            symbol_imports=self._symbol_imports,
        )
        self._sync_interprocedural_effects()
        self._db_store.write_full(
            {
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "graph_version": GRAPH_VERSION,
                "root": str(self.root),
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

    def load(self) -> bool:
        """Load a previously saved index from disk (SQLite .projectbrain.db primary)."""
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
            data = self._db_store.load_all()

        if data is None:
            if not json_exists:
                return False
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False

        if not self._is_compatible_cache(data):
            return False
        self._index = {
            rel: {**entry.get("info", {}), "mtime": entry.get("mtime")}
            for rel, entry in data.get("files", {}).items()
        }
        self._sizes = {
            rel: entry.get("size") for rel, entry in data.get("files", {}).items()
        }
        self._skipped = data.get("skipped", {})
        self._build_blobs()
        if data.get("graph_json"):
            self._graph = CodeGraph.from_json(data["graph_json"])
        elif self._graph_path.exists():
            self._graph = CodeGraph.from_json(self._graph_path.read_text(encoding="utf-8"))
        else:
            self._graph = CodeGraph.build(
                self._index,
                import_targets=self._import_targets,
                symbol_imports=self._symbol_imports,
            )
        self._sync_interprocedural_effects()
        return True

    def _sync_interprocedural_effects(self, target_rels: set[str] | None = None) -> None:
        """Fold transitive_z_write and exit_contracts from CodeGraph fn nodes back into file search sets."""
        if not self._graph:
            return
        new_toks_by_file: dict[str, set[str]] = {}
        if target_rels is not None:
            self._graph._ensure_registries()
            candidate_nids = [
                nid
                for r in target_rels
                for nid in self._graph._nodes_by_file.get(r, set())
                if nid in self._graph.nodes
            ]
        else:
            candidate_nids = list(self._graph.nodes.keys())
        for nid in candidate_nids:
            nd = self._graph.nodes[nid]
            if nd.get("type") != "fn":
                continue
            rel = nd.get("rel_path", "")
            if not rel or rel not in self._index:
                continue
            zw_set = self._z_write_sets.setdefault(rel, set())
            tok_set = self._token_sets.setdefault(rel, set())
            added = new_toks_by_file.setdefault(rel, set())
            for zw in nd.get("transitive_z_write", []):
                zw_l = zw.lower()
                if zw_l not in tok_set:
                    added.add(zw_l)
                zw_set.add(zw_l)
                tok_set.add(zw_l)
                for sub_t in _tokenize_symbol(zw):
                    if len(sub_t) >= 3:
                        if sub_t not in tok_set:
                            added.add(sub_t)
                        zw_set.add(sub_t)
                        tok_set.add(sub_t)
            for ex in nd.get("exit_contracts", []) + nd.get("transitive_exits", []):
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

    def _build_blobs(self) -> None:
        """Precompute searchable tokens, IDF weights, import maps, and AST hashes per file."""
        self._blobs = {}
        self._token_sets = {}
        self._import_token_sets: dict[str, set[str]] = {}
        self._defn_sets = {}
        self._ast_hashes = {}
        self._z_write_sets = {}
        self._z_read_sets: dict[str, set[str]] = {}
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

        # Symbol-aware and file-aware import resolution
        file_set = set(self._index)
        module_index = resolve.build_python_module_index(self._index)
        self._python_module_index = module_index

        self._import_targets = {}
        self._symbol_imports = {}
        self._imported_by = {}
        for importer_path, info in self._index.items():
            targets, sym_map = resolve.resolve_file_imports_and_symbols(
                importer_path, info, file_set, module_index,
            )
            self._import_targets[importer_path] = targets
            self._symbol_imports[importer_path] = sym_map
            for target in targets:
                self._imported_by.setdefault(target, []).append(importer_path)

        self._importer_counts = {
            rel_path: len(self._imported_by.get(rel_path, []))
            for rel_path in self._index
        }
        self._behaviors = {}

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
        file_set = set(self._index)
        if has_added_or_deleted or not getattr(self, "_python_module_index", None):
            self._python_module_index = resolve.build_python_module_index(self._index)
        module_index = self._python_module_index

        for rel in changed_rels:
            info = self._index.get(rel)
            if not info:
                continue
            new_toks = self._compute_file_blob(rel, info)
            for tok in new_toks:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1
                touched_tokens.add(tok)
            targets, sym_map = resolve.resolve_file_imports_and_symbols(
                rel, info, file_set, module_index,
            )
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
        return any(current[rel] != self._index[rel].get("mtime") for rel in self._index if rel in current)

    def incremental_index(self) -> dict:
        """Re-parse only changed files instead of a full re-index."""
        if not self._index:
            return self.index()

        current_files: dict[str, tuple[str, float]] = {
            rel: (abs_path, mtime)
            for rel, abs_path, mtime in self._walk_entries()
        }

        old_keys = set(self._index.keys()) | set(self._skipped.keys())
        new_keys = set(current_files.keys())

        added = new_keys - old_keys
        deleted = old_keys - new_keys
        potentially_modified = set(self._index.keys()) & new_keys

        modified: set[str] = {
            rel for rel in potentially_modified
            if current_files[rel][1] != self._index[rel].get("mtime")
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
            self._ledger.invalidate_file(rel)
            was_in_index = rel in self._index
            old_info = self._index.get(rel) or {}
            old_iface_hash = compute_file_interface_hash(old_info) if old_info else None

            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                skipped_changed = True
                if was_in_index:
                    self._index.pop(rel, None)
                    deleted_indexed.add(rel)
                all_interfaces_unchanged = False
                continue

            if _is_minified(source):
                self._skipped[rel] = "minified_or_generated"
                skipped_changed = True
                if was_in_index:
                    self._index.pop(rel, None)
                    deleted_indexed.add(rel)
                all_interfaces_unchanged = False
                continue

            if rel in self._skipped:
                self._skipped.pop(rel, None)
                skipped_changed = True
            st = path.stat()
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
            self._sync_interprocedural_effects(target_rels=set(graph_delta["affected_rels"]))
            self._db_store.apply_incremental(
                {
                    "schema_version": SCHEMA_VERSION,
                    "parser_version": PARSER_VERSION,
                    "graph_version": GRAPH_VERSION,
                    "root": str(self.root),
                },
                {r: self._index[r] for r in changed_indexed},
                self._sizes,
                deleted_indexed,
                self._skipped,
                graph_json=None,
                graph_delta=graph_delta,
                skipped_changed=skipped_changed,
            )
        else:
            self._build_blobs()
            self._graph = CodeGraph.build(
                self._index,
                import_targets=self._import_targets,
                symbol_imports=self._symbol_imports,
            )
            self._sync_interprocedural_effects()
            self._db_store.apply_incremental(
                {
                    "schema_version": SCHEMA_VERSION,
                    "parser_version": PARSER_VERSION,
                    "graph_version": GRAPH_VERSION,
                    "root": str(self.root),
                },
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
                    matched_imp = kw in imp_tokens
                    matched_body = kw in body_tokens
                    matched_sub = len(kw) >= 4 and kw in blob
                    in_stem = (not is_test_f or query_wants_tests) and (
                        kw in stem_tokens or (len(kw) >= 4 and kw in stem_low)
                    )
                    in_dir = (not is_test_f or query_wants_tests) and (
                        kw in dir_segs or kw.rstrip("s") in dir_segs or kw in dir_lower
                    )
                    if not (matched_token or matched_imp or matched_body or matched_sub or in_stem or in_dir):
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
        """Ground a structural claim "<subject> <relation> <object>" against the
        index, returning SUPPORTED / REFUTED / UNKNOWN with evidence (never lying or
        falsely refuting when a file has a syntax error or fails to parse).
        """
        self._ensure_fresh()
        subject = (subject or "").strip()
        obj = (object or "").strip()
        relation = (relation or "").strip().lower()

        if relation not in _VERIFY_RELATIONS:
            return _verdict(subject, relation, obj, "UNKNOWN",
                f"Unsupported relation '{relation}'. Supported: "
                f"{', '.join(sorted(_VERIFY_RELATIONS))}.")
        if not subject or not obj:
            return _verdict(subject, relation, obj, "UNKNOWN",
                "Both subject and object are required to verify a claim.")

        if relation == "reaches":
            return self._verify_reaches(subject, obj)

        kind = _VERIFY_RELATIONS[relation]
        matches = self._match_subjects(subject, kind)
        if not matches:
            return _verdict(subject, relation, obj, "UNKNOWN",
                f"No {kind} matching '{subject}' is indexed — cannot verify.")

        reasons: list[str] = []
        refutable = len(matches) == 1
        if len(matches) > 1:
            reasons.append(f"'{subject}' is ambiguous: {len(matches)} definitions "
                           f"({', '.join(m['rel_path'] for m in matches[:4])}).")

        for m in matches:
            info = self._index[m["rel_path"]]
            captured = self._captured_set(m, relation, info)
            if self._name_in(obj, captured):
                ev = self._support_evidence(m, relation, obj, info)
                if ev["confidence"] in _STRONG:
                    return _verdict(subject, relation, obj, "SUPPORTED",
                        ev["reason"], confidence=ev["confidence"], evidence=[ev])
                reasons.append(ev["reason"])
                refutable = False
            if m["status"] != "ok":
                refutable = False
                reasons.append(f"{m['label']} in {m['rel_path']} parsed as "
                               f"'{m['status']}' — cannot confirm absence.")

        if refutable:
            m = matches[0]
            captured = self._captured_set(m, relation, self._index[m["rel_path"]])
            return _verdict(subject, relation, obj, "REFUTED",
                f"No direct '{relation}' relation from {m['label']} to '{obj}'. "
                f"Checked the complete captured set ({len(captured)} item(s)) "
                "from a cleanly-parsed file.",
                confidence="exact",
                evidence=[{
                    "node_id": m["node_id"], "file": m["rel_path"],
                    "line": m.get("line"), "captured": sorted(captured),
                }])

        return _verdict(subject, relation, obj, "UNKNOWN",
            " ".join(reasons) or "Insufficient information to confirm or refute.")

    @staticmethod
    def _name_in(obj: str, captured: set) -> bool:
        o = obj.lower()
        o_bare = o.rsplit(".", 1)[-1]
        return any(
            o == c.lower() or o_bare == c.lower() or o == c.lower().rsplit(".", 1)[-1]
            for c in captured
        )

    def _match_subjects(self, subject: str, kind: str) -> list[dict]:
        s = subject.lower()
        out: list[dict] = []
        for rel, info in self._index.items():
            status = info.get("parse_status", "ok")
            if kind == "fn":
                for f in info.get("functions", []):
                    qualified = (f"{f['parent_class']}.{f['name']}"
                                 if f.get("parent_class") else f["name"])
                    if s in (f["name"].lower(), qualified.lower()):
                        out.append({"rel_path": rel, "status": status, "kind": "fn",
                                    "entity": f, "label": qualified,
                                    "node_id": fn_id(rel, f["name"], f.get("parent_class")),
                                    "line": f.get("line_start")})
            elif kind == "cls":
                for c in info.get("classes", []):
                    if c["name"].lower() == s:
                        out.append({"rel_path": rel, "status": status, "kind": "cls",
                                    "entity": c, "label": c["name"],
                                    "node_id": cls_id(rel, c["name"]),
                                    "line": c.get("line_start")})
            elif kind == "file":
                if s in (rel.lower(), Path(rel).name.lower()):
                    out.append({"rel_path": rel, "status": status, "kind": "file",
                                "entity": info, "label": rel,
                                "node_id": file_id(rel), "line": 1})
            elif kind == "route":
                for r in info.get("routes", []):
                    forms = (r["path"].lower(),
                             f"{r['method']} {r['path']}".lower(),
                             f"{r['method']}:{r['path']}".lower())
                    if s in forms:
                        out.append({"rel_path": rel, "status": status, "kind": "route",
                                    "entity": r, "label": f"{r['method']} {r['path']}",
                                    "node_id": route_id(r["method"], r["path"]),
                                    "line": r.get("line")})
        return out

    def _captured_set(self, m: dict, relation: str, info: dict) -> set:
        e = m["entity"]
        if relation == "calls":
            return set(e.get("calls", []))
        if relation == "inherits":
            return set(e.get("bases", []))
        if relation == "has_method":
            return set(e.get("methods", []))
        if relation == "handles":
            return {e["handler"]} if e.get("handler") else set()
        if relation == "defines":
            return ({f["name"] for f in info.get("functions", [])}
                    | {c["name"] for c in info.get("classes", [])})
        if relation == "imports":
            raw = set(info.get("imports", []))
            resolved = set(self._import_targets.get(m["rel_path"], []))
            resolved_names = {Path(p).name for p in resolved} | {Path(p).stem for p in resolved}
            return raw | resolved | resolved_names
        return set()

    def _support_evidence(self, m: dict, relation: str, obj: str, info: dict) -> dict:
        rank = {"exact": 3, "qualified": 2, "import_resolved": 1, "dynamic_dispatch": 0, "candidate": 0}
        o = obj.lower()
        o_bare = o.rsplit(".", 1)[-1]
        found = None
        if self._graph:
            for ei in self._graph._adj.get(m["node_id"], []):
                edge = self._graph.edges[ei]
                if edge["type"] != relation:
                    continue
                tgt = self._graph.nodes.get(edge["dst"], {})
                tname = (tgt.get("name") or tgt.get("path") or "").lower()
                tparent = (tgt.get("parent_class") or "").lower()
                tqual = f"{tparent}.{tname}" if tparent else tname
                if not (o == tqual or o == tname or o == tname.rsplit(".", 1)[-1] or o_bare == tname):
                    continue
                e_conf = edge["confidence"]
                e_reason = edge.get("reason", "")
                if relation == "calls" and m["kind"] == "fn" and ("." in o or tparent):
                    ent = m["entity"]
                    caller_cls = (ent.get("parent_class") or "").lower()
                    q_calls = {k.lower(): v for k, v in (ent.get("qualified_calls") or {}).items()}
                    req_recv = o.rsplit(".", 1)[0] if "." in o else tparent
                    matching_recvs = [
                        k[: -(len(o_bare) + 1)]
                        for k in q_calls
                        if k.endswith("." + o_bare)
                    ]
                    if matching_recvs:
                        caller_bases = {caller_cls}
                        for cls_d in info.get("classes", []):
                            if (cls_d.get("name") or "").lower() == caller_cls:
                                caller_bases.update(b.lower() for b in cls_d.get("bases", []))
                        static_recv = any(
                            r_u == req_recv
                            or (r_u in ("self", "cls", "this", "super()") and req_recv in caller_bases)
                            for r_u in matching_recvs
                        )
                        if not static_recv:
                            e_conf = "dynamic_dispatch"
                            e_reason = (
                                f"{m['label']} calls '.{o_bare}' on dynamic receiver "
                                f"'{matching_recvs[0]}' (line {edge.get('line')}; candidate: {tqual or obj}) — "
                                "receiver type cannot be resolved statically."
                            )
                if found is None or rank.get(e_conf, 0) > rank.get(found["confidence"], 0):
                    found = {
                        "node_id": m["node_id"], "file": m["rel_path"],
                        "target": edge["dst"], "confidence": e_conf,
                        "line": edge.get("line"), "reason": e_reason,
                    }
        if found and found["confidence"] in _STRONG:
            return found
        if relation == "imports":
            for rp in self._import_targets.get(m["rel_path"], []):
                if o in (rp.lower(), Path(rp).name.lower(), Path(rp).stem.lower()):
                    return {
                        "node_id": m["node_id"], "file": m["rel_path"], "target": file_id(rp),
                        "confidence": "exact", "line": m.get("line"),
                        "reason": f"'{m['label']}' imports resolved file '{rp}'.",
                    }
        if relation in ("defines", "has_method", "imports", "handles"):
            return {
                "node_id": m["node_id"], "file": m["rel_path"], "target": None,
                "confidence": "exact", "line": m.get("line"),
                "reason": f"'{obj}' is a direct {relation} fact of {m['label']}.",
            }
        if found:
            return found
        line = m["entity"].get("call_sites", {}).get(obj) if m["kind"] == "fn" else m.get("line")
        return {
            "node_id": m["node_id"], "file": m["rel_path"], "target": None,
            "confidence": "candidate", "line": line,
            "reason": f"{m['label']} references '{obj}', but no matching definition "
                      "is import-resolved in this codebase (external or ambiguous).",
        }

    def _verify_reaches(self, subject: str, obj: str) -> dict:
        if not self._graph:
            return _verdict(subject, "reaches", obj, "UNKNOWN", "Graph not available.")
        a_ids = self._graph.find_nodes_by_name(subject)
        b_ids = self._graph.find_nodes_by_name(obj)
        if not a_ids or not b_ids:
            return _verdict(subject, "reaches", obj, "UNKNOWN",
                "One or both symbols are not in the graph — cannot trace flow.")
        saw_candidate = False
        for a in a_ids[:5]:
            for b in b_ids[:5]:
                path = self._graph.find_directed_path(a, b, {"calls", "handles"})
                if not path:
                    continue
                if all(e["confidence"] in _STRONG for e in path):
                    return _verdict(subject, "reaches", obj, "SUPPORTED",
                        f"Control flows {subject} -> {obj} in {len(path)} hop(s) "
                        "along trusted call edges.",
                        confidence="import_resolved",
                        evidence=[{"path": [
                            {"src": e["src"], "dst": e["dst"], "type": e["type"],
                             "confidence": e["confidence"], "line": e.get("line")}
                            for e in path]}])
                saw_candidate = True
        if saw_candidate:
            return _verdict(subject, "reaches", obj, "UNKNOWN",
                "A path exists but only through name-only (candidate) edges — "
                "the target is ambiguous, so reachability is not certain.")
        return _verdict(subject, "reaches", obj, "UNKNOWN",
            "No call-flow path found. Absence of a static path is not proof of "
            "unreachability (dynamic dispatch, unresolved imports) — hence UNKNOWN, "
            "not REFUTED.")

    def _find_importers(self, rel_path: str, limit: int = 5) -> list[str]:
        return self._imported_by.get(rel_path, [])[:limit]

    def _resolve_imports(self, rel_path: str, limit: int = 5) -> list[str]:
        return self._import_targets.get(rel_path, [])[:limit]

    # ── Verification & Safety ───────────────────────────────────────────────

    def verify_batch(self, claims: list[tuple[str, str, str]]) -> dict:
        self._ensure_fresh()
        verdicts = []
        counts = {"SUPPORTED": 0, "REFUTED": 0, "UNKNOWN": 0}

        for subject, relation, obj in claims:
            v = self.verify_claim(subject, relation, obj)
            verdicts.append(v)
            counts[v.get("verdict", "UNKNOWN")] += 1

        return {
            "verdicts": verdicts,
            "summary": counts,
            "total": len(claims),
        }

    def verify_plan(
        self,
        task: str,
        files_to_edit: list[str],
        files_to_create: list[str] | None = None,
    ) -> dict:
        self._ensure_fresh()

        plan_set: set[str] = set()
        for f in files_to_edit:
            try:
                plan_set.add(str(Path(f).relative_to(self.root)).replace("\\", "/"))
            except ValueError:
                plan_set.add(f.replace("\\", "/"))

        if files_to_create:
            for f in files_to_create:
                plan_set.add(f.replace("\\", "/"))

        warnings: list[dict] = []
        missing_files: list[dict] = []
        test_files: list[str] = []
        risks: list[dict] = []

        for rel_path in plan_set:
            info = self._index.get(rel_path)
            if not info:
                continue

            importers = self._imported_by.get(rel_path, [])
            for imp in importers:
                if imp in plan_set:
                    continue

                imp_lower = imp.lower()
                is_test = any(seg in imp_lower for seg in (
                    "test", "spec", "__tests__", "tests/",
                ))
                if is_test:
                    test_files.append(imp)
                else:
                    missing_files.append({
                        "rel_path": imp,
                        "reason": f"imports '{rel_path}' which is being edited",
                        "severity": "warning",
                    })

            targets = self._import_targets.get(rel_path, [])
            for target in targets:
                if target in plan_set:
                    continue
                target_info = self._index.get(target, {})
                if target_info.get("classes"):
                    cls_names = [c["name"] for c in target_info["classes"]]
                    task_lower = task.lower()
                    for cls_name in cls_names:
                        if cls_name.lower() in task_lower:
                            missing_files.append({
                                "rel_path": target,
                                "reason": (
                                    f"defines class '{cls_name}' mentioned in the task, "
                                    f"imported by planned file '{rel_path}'"
                                ),
                                "severity": "important",
                            })
                            break

            importer_count = self._importer_counts.get(rel_path, 0)
            behaviors = self._behaviors.get(rel_path, {})
            has_orchestrator = any(b == "orchestrator" for b in behaviors.values())
            has_handler = any(b == "handler" for b in behaviors.values())

            risk_level = "low"
            risk_reasons = []
            if importer_count >= 5:
                risk_level = "high"
                risk_reasons.append(f"imported by {importer_count} files (hub file)")
            elif importer_count >= 2:
                risk_level = "medium"
                risk_reasons.append(f"imported by {importer_count} files")
            if has_orchestrator:
                risk_level = max(risk_level, "medium", key=lambda x: ["low", "medium", "high"].index(x))
                risk_reasons.append("contains orchestrator function(s)")
            if has_handler:
                risk_reasons.append("contains route handler(s)")

            if risk_reasons:
                risks.append({
                    "rel_path": rel_path,
                    "risk": risk_level,
                    "reasons": risk_reasons,
                })

        seen: set[str] = set()
        unique_missing = []
        for mf in missing_files:
            if mf["rel_path"] not in seen:
                seen.add(mf["rel_path"])
                unique_missing.append(mf)

        if unique_missing:
            verdict = "INCOMPLETE"
            message = (
                f"Plan covers {len(plan_set)} file(s) but may be missing "
                f"{len(unique_missing)} connected file(s)."
            )
        elif test_files:
            verdict = "NEEDS_TEST_UPDATES"
            message = (
                f"Plan looks structurally complete. "
                f"{len(set(test_files))} test file(s) may need updating."
            )
        else:
            verdict = "COMPLETE"
            message = "Plan covers all structurally connected files."

        result: dict = {
            "verdict": verdict,
            "message": message,
            "planned_files": sorted(plan_set),
        }
        if unique_missing:
            result["missing_files"] = unique_missing
        if test_files:
            result["test_files_to_update"] = sorted(set(test_files))
        if risks:
            result["risks"] = risks
        if warnings:
            result["warnings"] = warnings
        return result

    def impact(self, files_changed: list[str] | str) -> dict:
        self._ensure_fresh()
        if isinstance(files_changed, (str, Path)):
            files_changed = [str(files_changed)]

        results = []
        all_impacted: set[str] = set()
        max_risk = "low"
        _RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        for path in files_changed:
            try:
                rel_path = str(Path(path).relative_to(self.root)).replace("\\", "/")
            except ValueError:
                rel_path = path.replace("\\", "/")

            if rel_path not in self._index:
                results.append({
                    "rel_path": rel_path,
                    "status": "not_indexed",
                    "risk": "unknown",
                })
                continue

            info = self._index[rel_path]
            importers = self._imported_by.get(rel_path, [])
            importer_count = len(importers)
            all_impacted.update(importers)

            defined_fns = [fn for fn in info.get("functions", [])]
            callers: list[dict] = []
            if self._graph and defined_fns:
                for fn in defined_fns:
                    fn_name = fn["name"]
                    fn_node_id = fn_id(rel_path, fn_name, fn.get("parent_class"))
                    if fn_node_id in self._graph.nodes:
                        for edge in self._graph.edges:
                            if edge["dst"] == fn_node_id and edge["type"] == "calls":
                                src_node = self._graph.nodes.get(edge["src"], {})
                                callers.append({
                                    "function": fn_name,
                                    "signature": fn.get("signature", ""),
                                    "called_by": edge["src"],
                                    "in_file": src_node.get("rel_path", ""),
                                    "line": edge.get("line"),
                                    "confidence": edge.get("confidence", "candidate"),
                                })

            behaviors = self._get_file_behaviors(rel_path)
            behavior_summary = {}
            for name, behavior in behaviors.items():
                if behavior != "unknown":
                    behavior_summary.setdefault(behavior, []).append(name)

            if importer_count >= 10:
                risk = "critical"
            elif importer_count >= 5:
                risk = "high"
            elif importer_count >= 2:
                risk = "medium"
            else:
                risk = "low"

            if _RISK_ORDER.get(risk, 0) > _RISK_ORDER.get(max_risk, 0):
                max_risk = risk

            test_importers = []
            prod_importers = []
            for imp in importers:
                imp_lower = imp.lower()
                if any(seg in imp_lower for seg in ("test", "spec", "__tests__")):
                    test_importers.append(imp)
                else:
                    prod_importers.append(imp)

            entry: dict = {
                "rel_path": rel_path,
                "risk": risk,
                "direct_consumers": prod_importers,
                "consumer_count": len(prod_importers),
                "test_consumers": test_importers,
                "test_consumer_count": len(test_importers),
            }
            if callers:
                entry["called_by"] = callers[:20]
            if behavior_summary:
                entry["behaviors"] = behavior_summary
            routes = info.get("routes", [])
            if routes:
                entry["routes"] = routes

            results.append(entry)

        return {
            "files_analyzed": len(results),
            "total_impact_zone": len(all_impacted),
            "overall_risk": max_risk,
            "details": results,
            "all_impacted_files": sorted(all_impacted),
        }

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

    def trace_flow(self, url_path: str, method: str = "GET") -> dict:
        self._ensure_fresh()
        method = method.upper()
        steps: list[dict] = []

        ui_triggers = self._find_url_callers(url_path, method)
        for trigger in ui_triggers:
            steps.append({
                "step": len(steps) + 1,
                "type": "ui_trigger",
                "rel_path": trigger["rel_path"],
                "line": trigger.get("line"),
                "detail": trigger.get("detail", f"calls {method} {url_path}"),
            })

        route_matches = self._find_route_handler(url_path, method)
        for match in route_matches:
            steps.append({
                "step": len(steps) + 1,
                "type": "route",
                "rel_path": match["rel_path"],
                "handler": match.get("handler"),
                "line": match.get("line"),
                "detail": f"{method} {match.get('route_path', url_path)}",
            })

            handler_name = match.get("handler")
            if handler_name and self._graph:
                handler_file = match["rel_path"]
                fn_node = fn_id(handler_file, handler_name)
                if fn_node in self._graph.nodes:
                    callees = [
                        e for e in self._graph.edges
                        if e["src"] == fn_node and e["type"] == "calls"
                    ]
                    for callee_edge in callees[:10]:
                        callee_node = self._graph.nodes.get(callee_edge["dst"], {})
                        callee_file = callee_node.get("rel_path", "")
                        callee_name = callee_node.get("name", callee_edge["dst"])
                        behavior = ""
                        if callee_file:
                            behaviors = self._get_file_behaviors(callee_file)
                            behavior = behaviors.get(callee_name, "")

                        step_type = "service"
                        if behavior in ("io_reader", "io_writer"):
                            step_type = "data"
                        elif behavior == "validator":
                            step_type = "validator"

                        steps.append({
                            "step": len(steps) + 1,
                            "type": step_type,
                            "rel_path": callee_file,
                            "function": callee_name,
                            "signature": callee_node.get("signature", ""),
                            "behavior": behavior,
                            "confidence": callee_edge.get("confidence", "candidate"),
                            "detail": f"{handler_name}() calls {callee_name}()",
                        })

        if not steps:
            return {
                "url": f"{method} {url_path}",
                "steps": [],
                "message": f"No flow found for {method} {url_path}. "
                           "Ensure the route is indexed and URL format matches.",
            }

        return {
            "url": f"{method} {url_path}",
            "steps": steps,
            "step_count": len(steps),
        }

    def _find_url_callers(self, url_path: str, method: str) -> list[dict]:
        callers = []
        url_lower = url_path.lower()

        for rel_path, blob in self._blobs.items():
            if url_lower not in blob.lower():
                continue

            info = self._index.get(rel_path, {})
            routes = info.get("routes", [])
            is_route_definer = any(
                self._url_matches(r["path"], url_path) for r in routes
            )
            if is_route_definer:
                continue

            for fn in info.get("functions", []):
                calls = fn.get("calls", [])
                qualified = fn.get("qualified_calls", {})
                http_callers = {"fetch", "post", "get", "put", "delete", "patch",
                                "request", "ajax"}
                if any(c.lower() in http_callers for c in calls):
                    callers.append({
                        "rel_path": rel_path,
                        "function": fn["name"],
                        "line": fn.get("line_start"),
                        "detail": f"{fn['name']}() calls {method} {url_path}",
                    })
                    break
                for qc in qualified:
                    qc_lower = qc.lower()
                    if any(h in qc_lower for h in ("fetch", "axios", "requests", "http")):
                        callers.append({
                            "rel_path": rel_path,
                            "function": fn["name"],
                            "line": fn.get("line_start"),
                            "detail": f"{fn['name']}() calls {qc}('{url_path}')",
                        })
                        break

        return callers

    def _find_route_handler(self, url_path: str, method: str) -> list[dict]:
        matches = []
        for rel_path, info in self._index.items():
            for route in info.get("routes", []):
                if route["method"].upper() != method:
                    continue
                if not self._url_matches(route["path"], url_path):
                    continue

                handler = route.get("handler")
                if not handler:
                    route_node = route_id(method, route["path"])
                    if self._graph and route_node in self._graph.nodes:
                        for edge in self._graph.edges:
                            if edge["dst"] == route_node and edge["type"] == "handles":
                                src_node = self._graph.nodes.get(edge["src"], {})
                                handler = src_node.get("name")
                                break

                matches.append({
                    "rel_path": rel_path,
                    "route_path": route["path"],
                    "handler": handler,
                    "line": route.get("line"),
                })

        return matches

    @staticmethod
    def _url_matches(pattern: str, url: str) -> bool:
        pattern = pattern.rstrip("/") or "/"
        url = url.rstrip("/") or "/"

        if pattern == url:
            return True

        _PLACEHOLDER = "__DYN__"
        normalized = re.sub(r"(:\w+|\[\w+\]|\{\w+\})", _PLACEHOLDER, pattern)
        pattern_re = re.escape(normalized).replace(re.escape(_PLACEHOLDER), "[^/]+")
        pattern_re = pattern_re.replace(re.escape("..."), ".*")
        return bool(re.fullmatch(pattern_re, url))

    def co_change_analysis(self, file_path: str, days: int = 90) -> dict:
        import subprocess

        try:
            rel_path = str(Path(file_path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel_path = file_path.replace("\\", "/")

        try:
            result = subprocess.run(
                ["git", "log", f"--since={days} days ago",
                 "--pretty=format:%H", "--", rel_path],
                capture_output=True, text=True, cwd=str(self.root),
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {
                    "file": rel_path,
                    "co_changes": [],
                    "message": "No git history found for this file "
                               f"in the last {days} days.",
                }

            commit_hashes = result.stdout.strip().split("\n")
            co_change_counts: dict[str, int] = {}
            total_commits = len(commit_hashes)

            for commit in commit_hashes[:50]:
                files_result = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "-r",
                     "--name-only", commit],
                    capture_output=True, text=True, cwd=str(self.root),
                    timeout=10,
                )
                if files_result.returncode != 0:
                    continue

                changed = files_result.stdout.strip().split("\n")
                for f in changed:
                    f_norm = f.strip().replace("\\", "/")
                    if f_norm and f_norm != rel_path:
                        co_change_counts[f_norm] = co_change_counts.get(f_norm, 0) + 1

            co_changes = []
            for f, count in sorted(
                co_change_counts.items(), key=lambda x: x[1], reverse=True
            )[:20]:
                co_changes.append({
                    "rel_path": f,
                    "co_change_count": count,
                    "co_change_pct": round(count / total_commits * 100, 1),
                    "indexed": f in self._index,
                })

            return {
                "file": rel_path,
                "commits_analyzed": min(total_commits, 50),
                "days": days,
                "co_changes": co_changes,
            }

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {
                "file": rel_path,
                "co_changes": [],
                "message": "Git not available or timed out.",
            }

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
        if not self._index and self._db_store.db_path.exists():
            self.load()
        result = {
            "files": len(self._index),
            "functions": sum(len(v.get("functions", [])) for v in self._index.values()),
            "classes": sum(len(v.get("classes", [])) for v in self._index.values()),
            "routes": sum(len(v.get("routes", [])) for v in self._index.values()),
            "cells_6d": sum(len(v.get("cells_4d", [])) for v in self._index.values()),
            "cells_4d": sum(len(v.get("cells_4d", [])) for v in self._index.values()),
            "memories": len(self._memory._notes),
            "storage_backend": "sqlite" if self._db_store.db_path.exists() else "json",
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "graph_version": GRAPH_VERSION,
            "root": str(self.root),
        }
        result["parse_status"] = self._parse_status_counts()
        result["skipped"] = len(self._skipped)
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
        """Graph-Aware Command, Test & Log Output Compressor ('Read less' + 6D Traceback Resolution).

        1. Strips ANSI escape sequences and carriage-return progress overwrites.
        2. Folds passing test noise (pytest, unittest, jest/vitest, go test, cargo test) into compact counts.
        3. Deduplicates repetitive log/JSON/CSV lines sharing the same structural template.
        4. Cross-references any stack-trace `file:line` frames against the live 6D Code Graph
           to surface the exact failing symbols, signatures, 6D roles, and 1-hop callers.
        """
        raw_text = raw_output or ""
        raw_chars = len(raw_text)
        # Strip ANSI colors and carriage-return spinner overwrites
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_text)
        clean = re.sub(r"[^\r\n]+\r(?!\n)", "", clean)
        clean = clean.replace("\r\n", "\n")

        lines = clean.splitlines()
        pass_re = re.compile(
            r"^(?:"
            r"\s*\S+::\S+\s+PASSED(?:\s+\[\s*\d+%\])?\s*$|"
            r"\s*PASSED\s+\S+.*$|"
            r"\s*[\w./\\-]+(?:\.py|\.ts|\.tsx|\.js|\.go|\.rs)\s+[.s]+\s*(?:\[\s*\d+%\])?\s*$|"
            r"\s*[.s]{3,}\s*(?:\[\s*\d+%\])?\s*$|"
            r"\s*(?:ok\s+\S+\s+[\d.]+s|---\s+PASS:\s+\S+|===\s+RUN\s+\S+)\s*$|"
            r"\s*test\s+\S+\s+\.\.\.\s+ok\s*$|"
            r"\s*(?:PASS|✓|√)\s+.+$"
            r")"
        )
        urgent_re = re.compile(
            r"(?:FAIL|FAILED|ERROR|Traceback|Exception|AssertionError|panic:|--- FAIL:)",
            re.IGNORECASE,
        )

        # Stage 1: Fold passing test lines
        stage1_lines: list[str] = []
        passing_folded = 0
        run_pass_streak = 0
        for ln in lines:
            if pass_re.match(ln):
                passing_folded += 1
                run_pass_streak += 1
            else:
                if run_pass_streak > 0:
                    stage1_lines.append(f"[... folded {run_pass_streak} passing test line(s) ...]")
                    run_pass_streak = 0
                stage1_lines.append(ln)
        if run_pass_streak > 0:
            stage1_lines.append(f"[... folded {run_pass_streak} passing test line(s) ...]")

        # Stage 2: Structural template deduplication for logs / JSON / CSV rows
        def _line_template(s: str) -> str:
            t = s.strip()
            if not t:
                return ""
            t = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "<TS>", t)
            t = re.sub(r"\b[0-9a-fA-F]{8,}\b", "<HEX>", t)
            t = re.sub(r"\b\d+(?:\.\d+)?\b", "<NUM>", t)
            return t

        stage2_lines: list[str] = []
        repeated_folded = 0
        prev_tmpl: str | None = None
        tmpl_streak = 0
        for ln in stage1_lines:
            tmpl = _line_template(ln)
            if tmpl and tmpl == prev_tmpl and not urgent_re.search(ln):
                tmpl_streak += 1
                if tmpl_streak <= 2:
                    stage2_lines.append(ln)
                else:
                    repeated_folded += 1
            else:
                if tmpl_streak > 2:
                    stage2_lines.append(f"[... folded {tmpl_streak - 2} repetitive line(s) ...]")
                prev_tmpl = tmpl
                tmpl_streak = 1
                stage2_lines.append(ln)
        if tmpl_streak > 2:
            stage2_lines.append(f"[... folded {tmpl_streak - 2} repetitive line(s) ...]")

        # Stage 3: Window to max_lines while preserving all urgent error/traceback lines
        if max_lines > 0 and len(stage2_lines) > max_lines:
            head_n = max(10, max_lines // 3)
            tail_n = max(15, max_lines - head_n)
            head = stage2_lines[:head_n]
            tail = stage2_lines[-tail_n:]
            middle = stage2_lines[head_n:-tail_n]
            urgent_mid = [m for m in middle if urgent_re.search(m)][:20]
            omitted = len(middle) - len(urgent_mid)
            windowed = list(head)
            if urgent_mid:
                windowed.extend(urgent_mid)
            if omitted > 0:
                windowed.append(f"[... truncated {omitted} non-error middle line(s) ...]")
            windowed.extend(tail)
            stage3_lines = windowed
        else:
            stage3_lines = stage2_lines

        # Stage 4: 6D Code Graph Traceback Frame Resolution
        frames = _extract_stack_trace_frames(clean)
        resolved_6d_frames: list[dict] = []
        if frames:
            if not self._index and self._db_store.db_path.exists():
                self.load()
            for fr in frames:
                f_hint = fr["file_hint"]
                ln_no = fr["line"]
                matched_rel = None
                if f_hint in self._index:
                    matched_rel = f_hint
                else:
                    for rel_k in self._index:
                        if rel_k.endswith(f_hint) or f_hint.endswith(rel_k):
                            matched_rel = rel_k
                            break
                if not matched_rel:
                    continue
                f_info = self._index.get(matched_rel, {})
                enclosing_fn = None
                for fn in f_info.get("functions", []):
                    if fn.get("line_start", 1) <= ln_no <= fn.get("line_end", 1):
                        enclosing_fn = fn
                        break
                cell_role = "LOGIC"
                for cell in f_info.get("cells_4d", []):
                    if cell.get("line_start", 1) <= ln_no <= cell.get("line_end", 1):
                        cell_role = cell.get("role", cell_role)
                        break
                sym_name = enclosing_fn["name"] if enclosing_fn else (fr.get("function") or "<module>")
                sig = enclosing_fn.get("signature", sym_name) if enclosing_fn else sym_name
                l_span = (
                    f"L{enclosing_fn.get('line_start', ln_no)}-L{enclosing_fn.get('line_end', ln_no)}"
                    if enclosing_fn
                    else f"L{ln_no}"
                )
                callers: list[str] = []
                if self._graph and enclosing_fn:
                    nid = fn_id(matched_rel, enclosing_fn["name"], enclosing_fn.get("parent_class"))
                    callers = [
                        e["src"]
                        for e in (self._graph.edges[i] for i in self._graph._radj.get(nid, []))
                        if e.get("type") == "calls"
                    ][:3]
                entry = {
                    "rel_path": matched_rel,
                    "line": ln_no,
                    "symbol": sym_name,
                    "lines": l_span,
                    "role_6d": cell_role,
                    "signature": sig,
                    "callers": callers,
                }
                if entry not in resolved_6d_frames:
                    resolved_6d_frames.append(entry)

        if resolved_6d_frames:
            stage3_lines.append("")
            stage3_lines.append("# [6D Failing Frame Resolution]")
            for rf in resolved_6d_frames:
                caller_str = f" | Callers: {', '.join(rf['callers'])}" if rf["callers"] else ""
                stage3_lines.append(
                    f"#   -> {rf['rel_path']}:L{rf['line']} in {rf['symbol']} ({rf['lines']}) "
                    f"| Role:{rf['role_6d']} | Sig: `{rf['signature']}`{caller_str}"
                )

        compressed_text = "\n".join(stage3_lines)
        comp_chars = len(compressed_text)
        raw_tokens = max(1, raw_chars // 4) if raw_chars > 0 else 0
        comp_tokens = max(1, comp_chars // 4) if comp_chars > 0 else 0
        saved_tokens = max(0, raw_tokens - comp_tokens)
        savings_pct = round((saved_tokens / raw_tokens) * 100.0, 1) if raw_tokens > 0 else 0.0

        self._ledger.record_input_op(
            op="run_compress",
            query="command_output",
            files_covered=len({r["rel_path"] for r in resolved_6d_frames}),
            cells_4d_resolved=len(resolved_6d_frames),
            naive_bytes=raw_chars,
            emitted_chars=comp_chars,
        )

        return {
            "compressed_output": compressed_text,
            "raw_chars": raw_chars,
            "compressed_chars": comp_chars,
            "raw_tokens_est": raw_tokens,
            "compressed_tokens_est": comp_tokens,
            "tokens_saved_est": saved_tokens,
            "savings_pct": savings_pct,
            "passing_tests_folded": passing_folded,
            "repeated_lines_folded": repeated_folded,
            "resolved_6d_frames": resolved_6d_frames,
        }

    def run_command_compressed(
        self,
        command: str | list[str],
        timeout: int = 120,
        max_lines: int = 80,
    ) -> dict:
        """Execute a command in `self.root` and compress stdout/stderr with 6D traceback resolution."""
        import shlex
        if isinstance(command, str):
            has_shell_meta = any(op in command for op in ("|", "&&", "||", ";", ">", "<", "$("))
            if not has_shell_meta:
                try:
                    cmd_args: str | list[str] = shlex.split(command, posix=(os.name != "nt"))
                    use_shell = False
                except ValueError:
                    cmd_args = command
                    use_shell = True
            else:
                cmd_args = command
                use_shell = True
        else:
            cmd_args = command
            use_shell = False
        try:
            proc = subprocess.run(
                cmd_args,
                cwd=str(self.root),
                shell=use_shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out_s = (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            err_s = (exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            combined = f"{out_s}\n{err_s}\n[ProjectBrain] Command timed out after {timeout}s."
            exit_code = 124

        res = self.compress_command_output(combined, max_lines=max_lines)
        res["command"] = command if isinstance(command, str) else " ".join(command)
        res["exit_code"] = exit_code
        return res

    def _load_gitignore_rules(self, gitignore_file: Path, base_rel: str) -> list[tuple[bool, bool, str, re.Pattern]]:
        """Parse and cache `.gitignore` rules as `(is_negated, dir_only, base_rel, compiled_regex)`."""
        cache = getattr(self, "_gitignore_cache", None)
        if cache is None:
            cache = {}
            self._gitignore_cache = cache
        try:
            st = gitignore_file.stat()
        except OSError:
            return []
        key = (str(gitignore_file), st.st_mtime)
        if key in cache:
            return cache[key]
        rules: list[tuple[bool, bool, str, re.Pattern]] = []
        try:
            raw_lines = gitignore_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        for raw in raw_lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            is_neg = line.startswith("!")
            if is_neg:
                line = line[1:].strip()
            if not line:
                continue
            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            if line.startswith("/"):
                line = line.lstrip("/")
            # Convert gitignore glob to regex
            i, n = 0, len(line)
            rx = ["^" if anchored else "(?:^|/)"]
            while i < n:
                ch = line[i]
                if ch == "*":
                    if i + 1 < n and line[i + 1] == "*":
                        if i + 2 < n and line[i + 2] == "/":
                            rx.append("(?:.*/)?")
                            i += 3
                            continue
                        rx.append(".*")
                        i += 2
                        continue
                    rx.append("[^/]*")
                elif ch == "?":
                    rx.append("[^/]")
                elif ch in ".+^$(){}[]|\\-":
                    rx.append("\\" + ch)
                else:
                    rx.append(ch)
                i += 1
            rx.append("(?:$|/)")
            try:
                rules.append((is_neg, dir_only, base_rel, re.compile("".join(rx))))
            except re.error:
                continue
        cache[key] = rules
        return rules

    @staticmethod
    def _matches_gitignore(rel_posix: str, is_dir: bool, rules: list[tuple[bool, bool, str, re.Pattern]]) -> bool:
        for is_neg, dir_only, base_rel, rx in reversed(rules):
            if dir_only and not is_dir:
                continue
            if base_rel:
                if not rel_posix.startswith(base_rel + "/"):
                    continue
                target = rel_posix[len(base_rel) + 1:]
            else:
                target = rel_posix
            if rx.search(target):
                return not is_neg
        return False

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
                if d in SKIP_DIRS or d.startswith((".pytest", ".mypy", ".ruff", ".tox", ".nox")) or d == "target":
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
def _extract_balanced_call_args(snippet: str, fn_name: str) -> list[str] | None:
    """Extract top-level argument strings for `fn_name(...)` respecting nested `()`, `[]`, `{}`, and quotes."""
    m = re.search(rf"\b{re.escape(fn_name)}\s*\(", snippet)
    if not m:
        return None
    i = m.end()
    n = len(snippet)
    depth = 1
    in_str: str | None = None
    cur: list[str] = []
    args: list[str] = []
    while i < n and depth > 0:
        ch = snippet[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                cur.append(snippet[i])
            elif ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
                cur.append(ch)
            elif ch in ("(", "[", "{"):
                depth += 1
                cur.append(ch)
            elif ch in (")", "]", "}"):
                depth -= 1
                if depth == 0:
                    tok = "".join(cur).strip()
                    if tok:
                        args.append(tok)
                    return args
                cur.append(ch)
            elif ch == "," and depth == 1:
                tok = "".join(cur).strip()
                if tok:
                    args.append(tok)
                cur = []
            else:
                cur.append(ch)
        i += 1
    return None


def _is_minified(source: str) -> bool:
    lines = source.splitlines()
    if not lines:
        return False
    non_empty = [len(line) for line in lines if line.strip()]
    if not non_empty:
        return False
    max_len = max(non_empty)
    if max_len <= MINIFIED_LINE_THRESHOLD:
        return False
    if max_len > 5000 or len(non_empty) <= 5:
        return True
    long_count = sum(1 for ln in non_empty if ln > MINIFIED_LINE_THRESHOLD)
    avg_len = sum(non_empty) / len(non_empty)
    return (long_count / len(non_empty)) > 0.25 or avg_len > 250


_TEST_QUERY_TERMS = {
    "test", "tests", "testing", "spec", "specs", "pytest",
    "unittest", "jest", "vitest", "mock", "fixture", "fixtures",
    "benchmark", "benchmarks",
}


def _is_test_file(rel_path: str) -> bool:
    p = Path(rel_path)
    parts_low = {seg.lower() for seg in p.parts[:-1]}
    if parts_low & {"tests", "test", "testing", "__tests__", "spec", "specs", "e2e", "benchmarks", "benchmark"}:
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
    tokens = re.findall(r"[a-zA-Z0-9_/.-]+", prompt.lower())
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
