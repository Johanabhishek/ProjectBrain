import json
import math
import os
import re
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
from .parsers import parse, apply_sql_alters_to_schema
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

MAX_FUNCTIONS_PER_FILE = int(os.environ.get("PROJECTBRAIN_MAX_FUNCTIONS", "40"))
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


PARSED_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".sql"}

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
}


class ProjectBrain:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self._index: dict = {}
        self._blobs: dict[str, str] = {}
        self._token_sets: dict[str, set[str]] = {}
        self._idf: dict[str, float] = {}
        self._cache_path = self.root / ".projectbrain.json"
        self._db_store = SQLiteIndexStore(self.root / ".projectbrain.db")
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
        if not fpath.exists():
            return None
        try:
            txt = fpath.read_text(encoding="utf-8", errors="ignore")
            self._source_cache[rel_path] = txt
            return txt
        except OSError:
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
        keywords = _extract_keywords(task)
        ranked_results = self.query(task, top_n=top_n)

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
        for file_idx, r in enumerate(ranked_results):
            if file_idx > 0 and omitted_already_in_context and r.get("score", 0.0) < top_file_score * 0.45:
                continue
            rel_path = r["rel_path"]
            info = self._index.get(rel_path, {})
            behaviors = self._behaviors.get(rel_path, {})

            # Find which specific functions or classes in this file matched the query
            scored_symbols: list[tuple[float, str, dict]] = []
            for fn in info.get("functions", []):
                fn_score = self._score_symbol_for_query(fn, keywords, behaviors, info.get("routes", []))
                if fn_score > 0:
                    scored_symbols.append((fn_score, "fn", fn))

            for cls in info.get("classes", []):
                cls_score = self._score_class_for_query(cls, keywords)
                if cls_score > 0:
                    scored_symbols.append((cls_score, "cls", cls))

            scored_symbols.sort(key=lambda x: x[0], reverse=True)

            # If no specific symbol matched keywords (e.g. filename match), pick main entrypoints
            if not scored_symbols:
                for fn in info.get("functions", [])[:2]:
                    scored_symbols.append((1.0, "fn", fn))
                for cls in info.get("classes", [])[:1]:
                    scored_symbols.append((1.0, "cls", cls))

            # Slice top 3 symbols per file within token budget
            for sym_score, kind, sym in scored_symbols[:3]:
                if chars_used >= char_budget * 0.65:
                    break
                parent = sym.get("parent_class")
                nid = fn_id(rel_path, sym["name"], parent) if kind == "fn" else cls_id(rel_path, sym["name"])

                if deduplicate_session and nid in self._served_slices:
                    omitted_already_in_context.append(
                        f"{nid} (L{sym.get('line_start')}-{sym.get('line_end')})"
                    )
                    est_lines = min(max(sym.get("line_end", 1) - sym.get("line_start", 1) + 1, 8), 65)
                    chars_used += est_lines * 55 + 120
                    continue

                l_start = sym.get("line_start", 1)
                l_end = sym.get("line_end", l_start)
                # For large classes, prefer class signature + fields + matched methods instead of 500 lines
                max_slice_lines = 45 if kind == "cls" else 65
                code_text = slice_lines(
                    self.root, rel_path, l_start, l_end, max_lines=max_slice_lines, focus_terms=keywords,
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
        # Helper to add a file's module globals once (only when max_g > 0 so expanded callees don't prematurely lock out primary globals)
        def _ensure_globals(rel_path: str, buf: list[str], max_g: int = 24) -> None:
            if zoom or max_g <= 0 or rel_path in seen_globals or rel_path not in self._index:
                return
            seen_globals.add(rel_path)
            glob_text = extract_file_globals_slice(self.root, rel_path, self._index[rel_path], max_lines=max_g)
            if glob_text:
                g_ivs = extract_emitted_intervals(glob_text)
                if g_ivs:
                    g_plan = self._ledger.plan_slice_intervals(rel_path, g_ivs[0][0], g_ivs[-1][1], commit=False)
                    if not g_plan["unseen_intervals"]:
                        buf.append(f"### [{rel_path} :: Module Constants — [Session Cache] Already delivered in this session (0 duplicate lines)]")
                        return
                emitted_by_file.setdefault(rel_path, []).extend(g_ivs)
                buf.append(f"### [{rel_path} :: Module Constants, State & Bootstrap]")
                buf.append(glob_text)

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
            _ensure_globals(rel, buf, max_g=max_g)
            ast_calls: list[str] = []
            if (l_end - l_start) <= 450 and not is_caller_slice:
                for f_entry in self._index[rel].get("functions", []):
                    if f_entry.get("line_start") == l_start:
                        ast_calls = [c.lower() for c in f_entry.get("calls", []) if len(c) >= 4]
                        break

            # Session Zero-Waste Guard inside dossier: skip re-emitting spans already delivered unfolded in this session
            s_plan = self._ledger.plan_slice_intervals(rel, slice_s, slice_e, commit=False)
            if not s_plan["unseen_intervals"]:
                buf.append(f"### [{rel} :: {nd.get('name')} (L{slice_s}-L{slice_e}) ? [Session Cache] Already delivered in this session (0 duplicate lines)]")
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
                    max_rows = 22 if (slice_e - slice_s) > 200 else 8
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
                                        f"# [4D Cell Match: {tc['Y_role']} | {tc['X_scope']} (L{tc_s}-L{tc_e})]\n{tc_code}"
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
            buf.append(f"### [{rel} :: {nd.get('name')} ({hdr_span}) ? {reason}]")
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

            # 1. Identify explicitly named code symbols (camelCase, snake_case, or backtick/paren-wrapped) or routes
            explicit_nodes: list[str] = []
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
                        if len(raw_nm) >= 5 and (
                            any(c.isupper() for c in raw_nm[1:])
                            or "_" in raw_nm
                            or f"`{raw_nm}`" in task
                            or f"({raw_nm})" in task
                        ):
                            if re.search(r"\b" + re.escape(raw_nm) + r"\b", clean_task_for_search):
                                explicit_nodes.append(nid)

            # Count explicit matches per file so secondary giant components don't hijack primary focus
            exp_by_file: dict[str, list[str]] = {}
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
                    filtered_explicit.append(enid)
                    _add_node_slice(enid, "explicitly referenced in task", t_kw, sec, max_l=95 if e_span <= 450 else 55, max_g=8)
            if filtered_explicit:
                explicit_nodes = filtered_explicit
            had_initial_explicit = bool(explicit_nodes)
            step2_ranked_files: set[str] = set()
            same_file_callers_to_add: list[tuple[str, str, list[str]]] = []
            top3_query_files: set[str] = set()
            if not has_explicit_route:
                ranked_all = self.query(clean_task_for_search, top_n=3)
                top3_query_files = {r["rel_path"] for r in ranked_all}
                ranked = ranked_all[: (1 if had_initial_explicit else 3)]
                top1_file_score = ranked[0]["score"] if ranked else 1.0
                for file_rank, r in enumerate(ranked):
                    min_ratio = 0.72 if file_rank == 1 else 0.52
                    if file_rank > 0 and r["score"] < top1_file_score * min_ratio:
                        continue
                    rp = r["rel_path"]
                    step2_ranked_files.add(rp)
                    primary_files.add(rp)
                    if len(filtered_explicit) >= 2 and rp in exp_by_file:
                        continue
                    info = self._index.get(rp, {})
                    behaviors = self._behaviors.get(rp, {})
                    scored: list[tuple[float, str, dict]] = []
                    for fn in info.get("functions", []):
                        sc = self._score_symbol_for_query(fn, t_kw, behaviors, info.get("routes", []), info.get("cells_4d", []))
                        if sc > 0:
                            scored.append((sc, "fn", fn))
                    for cls in info.get("classes", []):
                        sc = self._score_class_for_query(cls, t_kw)
                        if sc > 0:
                            scored.append((sc, "cls", cls))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    # Deduplicate nested functions so inner helpers don't consume top_k slots of the same outer function
                    dedup_scored: list[tuple[float, str, dict]] = []
                    for item in scored:
                        s_fn = item[2]
                        s_st, s_en = s_fn.get("line_start", 1), s_fn.get("line_end", 1)
                        if any(
                            o_fn.get("line_start", 1) <= s_st and s_en <= o_fn.get("line_end", 1) and (o_fn.get("line_end", 1) - o_fn.get("line_start", 1)) <= 180
                            or (s_st <= o_fn.get("line_start", 1) and o_fn.get("line_end", 1) <= s_en and (s_en - s_st) <= 180)
                            for _, _, o_fn in dedup_scored
                        ):
                            continue
                        dedup_scored.append(item)
                    top_k = 1 if (had_initial_explicit or file_rank > 0) else 2
                    for rank_idx, (sc, kind, sym) in enumerate(dedup_scored[:top_k]):
                        nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                        sym_span = sym.get("line_end", 1) - sym.get("line_start", 1)
                        rank_max_l = (95 if sym_span <= 450 else 55) if rank_idx == 0 else 48
                        _add_node_slice(nid, f"top match in {rp}", t_kw, sec, max_l=rank_max_l, max_g=16 if file_rank == 0 else 8)
                        if not had_initial_explicit and nid not in explicit_nodes and len(explicit_nodes) < 4:
                            explicit_nodes.append(nid)
                            added_for_sym = 0
                            for ei_c in self._graph._radj.get(nid, []):
                                ec = self._graph.edges[ei_c]
                                if ec["type"] == "calls" and ec.get("confidence") in ("exact", "import_resolved"):
                                    c_src = ec["src"]
                                    c_nd = self._graph.nodes.get(c_src, {})
                                    c_nm = (c_nd.get("name") or "").lower()
                                    if c_nd.get("rel_path") == rp and not c_nm.startswith(("clear", "remove")):
                                        c_lines = ec.get("lines") or ([ec.get("line", 0)] if ec.get("line") else [0])
                                        for call_ln in c_lines[:3]:
                                            same_file_callers_to_add.append((c_src, f"calls {sym['name']} @L{call_ln}", t_kw))
                                        added_for_sym += 1
                                        if added_for_sym >= 2:
                                            break

            # 3. Expand bidirectional call chains (BFS hop order + cross-file priority)
            wants_auth = any(w in t_kw for w in ("auth", "header", "headers", "token"))
            wants_load = any(w.startswith(("load", "restore", "resume", "fetch", "cache")) for w in t_kw)
            if self._graph and explicit_nodes:
                chain_lines: list[str] = []
                expanded_nodes: list[tuple[str, str, list[str], int, int]] = []
                for seed_id in explicit_nodes[:4]:
                    hop1_list: list[tuple[str, str, list[str], int, int]] = []
                    hop2_cross: list[tuple[str, str, list[str], int, int]] = []
                    hop2_same: list[tuple[str, str, list[str], int, int]] = []
                    hop3_list: list[tuple[str, str, list[str], int, int]] = []
                    bwd_expanded: list[tuple[str, str, list[str], int, int]] = []
                    seed_nd = self._graph.nodes.get(seed_id, {})
                    seed_rel = seed_nd.get("rel_path", "")
                    seed_s = seed_nd.get("line_start", -1)
                    seed_e = seed_nd.get("line_end", -1)

                    # Forward 1-2 hops (handles, calls) ordered strictly by BFS hop and cross-file priority
                    for ei in self._graph._adj.get(seed_id, []):
                        e1 = self._graph.edges[ei]
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
                        if not wants_auth and ("header" in dst1_name or "token" in dst1_name):
                            continue
                            continue
                        h1_max_l = 10 if (dst1_name.startswith("load") and not wants_load) else 42
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
                                if not wants_auth and ("header" in dst2_name or "token" in dst2_name):
                                    continue
                                if dst2_rel != dst1_rel:
                                    hop2_cross.append((dst2, f"2-hop callee via {dst1.split(':')[-1]}", t_kw + [dst2_name], 26, 12 if seed_nd.get("type") == "route" else 0))
                                    for ei3 in self._graph._adj.get(dst2, []):
                                        e3 = self._graph.edges[ei3]
                                        if e3["type"] == "calls" and e3.get("confidence") in ("exact", "import_resolved"):
                                            dst3 = e3["dst"]
                                            dst3_name = (self._graph.nodes.get(dst3, {}).get("name") or "").lower()
                                            chain_lines.append(f"    - `{dst2}` -[calls (L{e3.get('line', '?')})]-> `{dst3}`")
                                            if (
                                                "safe" not in dst3_name
                                                and "notify" not in dst3_name
                                                and "header" not in dst3_name
                                                and "token" not in dst3_name
                                                and not dst3_name.endswith("fetch")
                                            ):
                                                hop3_list.append((dst3, f"3-hop callee via {dst2.split(':')[-1]}", t_kw, 20, 0))
                                elif (dst2_nd.get("line_end", 0) - dst2_nd.get("line_start", 0)) >= 5:
                                    hop2_same.append((dst2, f"2-hop callee via {dst1.split(':')[-1]}", t_kw + [dst2_name], 20, 0))

                    fwd_expanded = hop1_list + hop2_cross + hop2_same + hop3_list

                    # Backward 1-2 hops (direct callers via _radj)
                    seed_tokens = {
                        tok for tok in _tokenize_symbol(seed_id.split(":")[-1])
                        if len(tok) >= 4 and tok not in ("mark", "handle", "save", "load", "init", "sync")
                    }
                    raw_callers: list[tuple[int, str, dict]] = []
                    for ei in self._graph._radj.get(seed_id, []):
                        e_in = self._graph.edges[ei]
                        if e_in["type"] != "calls" or e_in.get("confidence") not in ("exact", "import_resolved"):
                            continue
                        cid = e_in["src"]
                        cnd = self._graph.nodes.get(cid, {})
                        c_span = (cnd.get("line_end", 1) - cnd.get("line_start", 1))
                        raw_callers.append((c_span, cid, e_in))
                    raw_callers.sort(key=lambda x: x[0])

                    caller_files_seen: set[str] = set()
                    for c_span, caller_id, e_in in raw_callers:
                        caller_nd = self._graph.nodes.get(caller_id, {})
                        caller_name = caller_nd.get("name", "")
                        caller_rel = caller_nd.get("rel_path", "")
                        c_lines = e_in.get("lines") or ([e_in.get("line", 0)] if e_in.get("line") else [0])
                        c_lines_str = ",".join(f"L{x}" for x in c_lines[:4]) if c_lines and c_lines[0] else "L?"
                        chain_lines.append(f"- `{caller_id}` ({c_lines_str}) -[calls]-> `{seed_id}`")
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
                                    if (dfn_tokens & norm_seed) and span < 40 and "watch" in dfn["name"].lower():
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
                for seed_id in explicit_nodes[:4]:
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
                            chain_lines.append(f"- `{seed_id}` -[{esf['type']}: {esf.get('reason', '')}]-> `{esf['dst']}`")
                    for ei_sfr in self._graph._radj.get(seed_id, []):
                        esfr = self._graph.edges[ei_sfr]
                        if esfr["type"] in ("state_flow", "http_flow"):
                            chain_lines.append(f"- `{esfr['src']}` -[{esfr['type']}: {esfr.get('reason', '')}]-> `{seed_id}`")

                # Multiplex Personalized PageRank (PPR) + Causal Symptom Chop across 4-Layer Graph
                ppr_results = self._graph.multiplex_ppr({sid: 1.0 for sid in explicit_nodes[:4]}, top_k=10)
                ppr_scores: dict[str, float] = {nid: prob for nid, prob, _ in ppr_results}
                existing_exp_ids = {item[0] for item in expanded_nodes}

                chop_hits = self._graph.causal_symptom_chop(explicit_nodes[:4], clean_task_for_search)
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
                for p_nid, p_prob, p_rsn in ppr_results[:4]:
                    if p_nid not in existing_exp_ids and p_nid not in seen_slices and (
                        "http_flow" in p_rsn or "table:" in p_rsn or p_prob >= 0.085
                    ):
                        expanded_nodes.append((p_nid, p_rsn, t_kw, 28, 0))
                        existing_exp_ids.add(p_nid)

                if chain_lines and not zoom and monolith_4d_cells_count == 0:
                    sec.append("### Static AST Call, State-Channel & Interprocedural Flow Edges")
                    sec.extend(list(dict.fromkeys(chain_lines))[:12])
                emitted_hops = 99 if (zoom and had_initial_explicit) else 0
                max_hops_allowed = (2 if not had_initial_explicit else 1) if (zoom or monolith_4d_cells_count > 0 or len(filtered_explicit) >= 2) else 2

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
                for item in expanded_nodes:
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
                        for w in t_kw:
                            if len(w) >= 4 and (w in nd_nm or w[:4] in nd_nm):
                                feat_k = f"kw:{w}"
                                gain += (6.0 if feat_k not in covered_features else 1.5) * self._idf.get(w, 1.0)
                        for zw in nd_obj.get("local_z_write", []):
                            feat_zw = f"zw:{zw.lower()}"
                            gain += 3.2 if feat_zw not in covered_features else 0.4
                        for ex in nd_obj.get("exit_contracts", []):
                            feat_ex = f"ex:{ex.lower()}"
                            gain += 2.2 if feat_ex not in covered_features else 0.3
                        if nd_rel in top3_query_files and "analytics" not in nd_rel.lower():
                            gain += 5.0
                        if not c_rsn.startswith(("2-hop", "3-hop")):
                            gain += 3.5
                        if "http_flow" in c_rsn or "state_flow" in c_rsn:
                            gain += 4.5
                        gain += 25.0 * ppr_scores.get(c_nid, 0.0)
                        if nd_span < 6:
                            gain *= 0.35

                        ratio = gain / (cost_u ** 0.55)
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_idx = c_idx

                    chosen = remaining_cands.pop(best_idx)
                    ordered_cands.append(chosen)
                    ch_obj = self._graph.nodes.get(chosen[0], {})
                    ch_nm = (ch_obj.get("name") or "").lower()
                    for w in t_kw:
                        if len(w) >= 4 and (w in ch_nm or w[:4] in ch_nm):
                            covered_features.add(f"kw:{w}")
                    for zw in ch_obj.get("local_z_write", []):
                        covered_features.add(f"zw:{zw.lower()}")
                    for ex in ch_obj.get("exit_contracts", []):
                        covered_features.add(f"ex:{ex.lower()}")

                hop_pointers: list[str] = []
                for enid, ereason, ekw, emax_l, emax_g in ordered_cands:
                    m_cl = re.search(r"@L(\d+)", ereason)
                    cand_key = f"{enid}@L{m_cl.group(1)}" if m_cl else enid
                    if not self._graph or enid not in self._graph.nodes or cand_key in seen_slices:
                        continue
                    end_node = self._graph.nodes[enid]
                    enm = (end_node.get("name") or "").lower()
                    erel = end_node.get("rel_path", "")
                    espan = 12 if m_cl else (end_node.get("line_end", 1) - end_node.get("line_start", 1))
                    sec_lines_now = sum(len(x.splitlines()) for x in sec)
                    is_chop = "Causal Symptom Chop" in ereason
                    kw_direct = is_chop or bool(m_cl) or any(w in enm or w[:4] in enm for w in t_kw if len(w) >= 4) or ("http_flow" in ereason) or ("table:" in ereason)
                    if (emitted_hops < max_hops_allowed or is_chop or bool(m_cl)) and (sec_lines_now < 160 or (kw_direct and sec_lines_now < 310)) and espan <= 120:
                        _add_node_slice(enid, ereason, ekw, sec, max_l=min(emax_l, 30 if is_chop else 26), max_g=0)
                        if not is_chop and not m_cl:
                            emitted_hops += 1
                    else:
                        ptr = f"- `{erel} :: {end_node.get('name')}` (L{end_node.get('line_start', 1)}-L{end_node.get('line_end', 1)}) ? {ereason}"
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
                "completed", "counts", "open", "id",
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
                    behaviors = self._behaviors.get(rp, {})
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
                            f"`{s['name']}` (L{s.get('line_start', 1)}-L{s.get('line_end', 1)})"
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
                md_sections.extend([
                    "",
                    "## Surgical Anchor Patch Tool (`projectbrain patch`)",
                    "> Use `projectbrain --path \"<repo>\" patch --spec \"...\"` (or `--apply`) to generate verified unified diffs (`<repo>/.projectbrain_patch.diff`) without repeating full before/after code blocks:",
                    "> `@file <rel_path>` | `@sub L<line> \\`<old>\\` -> \\`<new>\\`` | `@replace L<s-e> ... @end` | `@insert_after L<line> ... @end` | `@delete L<s-e>`",
                ])

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
            for f_item in res.get("files", []):
                f_rel = self._normalize_rel(f_item.get("file", ""))
                p_txt = f_item.pop("_patched_text", None)
                if f_rel:
                    patched_rels.add(f_rel)
                    if not apply and p_txt is not None and f_rel in self._index:
                        saved_cells[f_rel] = self._index[f_rel].get("cells_4d", [])
                        info_f = self._index[f_rel]
                        self._index[f_rel]["cells_4d"] = extract_4d_coordinate_matrix(p_txt.splitlines(), ext=Path(f_rel).suffix.lower())
            if apply:
                self.index()
            diag_lines, _ = self._compile_6d_contract_diagnostics(
                "patch verification", patched_rels, strict_focus=True
            )
            for f_rel, orig_c in saved_cells.items():
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
        max_lines_per_symbol: int = 120,
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
        self._ensure_fresh()
        if isinstance(targets, (str, Path)):
            targets = [str(targets)]
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
                            if summary_mode:
                                span_req = e_line - s_line + 1
                                target_cap = max_lines_per_symbol if max_lines_per_symbol < 110 else max(max_lines_per_symbol, min(260, span_req))
                            else:
                                # Explicit non-summary line-range request: unfold up to 400 lines per target
                                target_cap = max(max_lines_per_symbol, 400)
                            emitted_ivs: list[tuple[int, int]] = []
                            for u_s, u_e in unseen:
                                u_len = (
                                    min(400, u_e - u_s + 1)
                                    if not summary_mode
                                    else min(target_cap, max(65, target_cap // max(1, len(unseen))))
                                )
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
                slices.append({"target": t, "error": f"Symbol or file '{t}' not found in index."})
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
    ) -> float:
        name_lower = fn.get("name", "").lower()
        name_tokens = set(_tokenize_symbol(fn.get("name", ""))) | {name_lower}
        doc_lower = (fn.get("docstring") or "").lower()
        calls_lower = {c.lower() for c in fn.get("calls", [])}
        body_tokens = set(fn.get("body_tokens", []))
        fn_s = fn.get("line_start", 1)
        fn_e = fn.get("line_end", fn_s)
        score = 0.0
        for kw in keywords:
            idf = self._idf.get(kw, 1.0)
            kw_stem = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 5 else kw
            if len(kw_stem) < 3:
                kw_stem = kw
            if kw == name_lower:
                score += 4.0 * idf
            elif kw in name_tokens or kw in name_lower or (len(kw_stem) >= 4 and kw_stem in name_lower):
                score += 2.5 * idf
            elif kw in doc_lower or (len(kw_stem) >= 4 and kw_stem in doc_lower):
                score += 1.0 * idf
            elif any(kw in c or (len(kw_stem) >= 4 and kw_stem in c) for c in calls_lower):
                score += 0.8 * idf
            elif kw in body_tokens or any(len(kw_stem) >= 4 and kw_stem in bt for bt in body_tokens):
                score += 0.6 * idf
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
        wants_all_repo_diag = (not strict_focus) and any(
            w in t_low
            for w in ("bug", "bugs", "error", "fail", "db", "database", "sql", "sqlite", "postgres", "connect", "insert", "schema", "backend")
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
                                    f"({tc_cmp['param_type']}) vs {tc_cmp['other_var']} ({tc_cmp['other_type']}) ? "
                                    f"?({tc_cmp['param_type']}) ? ?({tc_cmp['other_type']}) = ?"
                                ),
                            )
                            warnings.append(
                                f"- ?? **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) ? "
                                f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                                f"`{tc_cmp['other_type']}` expression `{tc_cmp['other_var']}` without type coercion "
                                f"(`?({tc_cmp['param_type']}) ? ?({tc_cmp['other_type']}) = ?`)."
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
                                f"- ?? **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) ? "
                                f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                                f"SQLite `TEXT` row key `['{tc_cmp['row_key']}']` without `str()`/`{tc_cmp['param_type']}()` coercion "
                                f"(`?({tc_cmp['param_type']}) ? ?(str) = ?`)."
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
        score = 0.0
        for kw in keywords:
            idf = self._idf.get(kw, 1.0)
            if kw == name_lower:
                score += 4.0 * idf
            elif kw in name_tokens or kw in name_lower:
                score += 2.5 * idf
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
        behaviors = self._behaviors.get(rel_path, {})

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
        self._index = {}
        self._sizes = {}
        self._skipped = {}

        for path in self._walk():
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            ext = path.suffix.lower()

            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                continue

            if _is_minified(source):
                self._skipped[rel] = "minified_or_generated"
                continue

            st = path.stat()
            info = parse(source, ext)
            info.setdefault("routes", []).extend(frameworks.infer_file_based_routes(rel, info))
            info["cells_4d"] = extract_4d_coordinate_matrix(source.splitlines(), ext=ext)
            info["mtime"] = st.st_mtime
            self._index[rel] = info
            self._sizes[rel] = st.st_size

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
            graph_json=self._graph.to_json(),
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

    def _sync_interprocedural_effects(self) -> None:
        """Fold transitive_z_write and exit_contracts from CodeGraph fn nodes back into file search sets."""
        if not self._graph:
            return
        new_toks_by_file: dict[str, set[str]] = {}
        for nid, nd in self._graph.nodes.items():
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

    def _build_blobs(self) -> None:
        """Precompute searchable tokens, IDF weights, import maps, and AST hashes per file."""
        self._blobs = {}
        self._token_sets = {}
        self._defn_sets = {}
        self._ast_hashes = {}
        self._z_write_sets = {}
        self._cell_4d_tokens = {}
        self._const_aliases: dict[str, list[str]] = {}
        doc_freq: dict[str, int] = {}

        for rel_path, info in self._index.items():
            for ak, a_vals in (info.get("const_aliases") or {}).items():
                dst_list = self._const_aliases.setdefault(ak, [])
                for av in a_vals:
                    if av not in dst_list:
                        dst_list.append(av)
            symbol_names = (
                [f["name"] for f in info.get("functions", [])]
                + [c["name"] for c in info.get("classes", [])]
                + info.get("exports", [])
            )
            tokenized = []
            for name in symbol_names:
                tokenized.extend(_tokenize_symbol(name))

            z_write_toks: set[str] = set()
            cell_4d_toks: set[str] = set()
            for cell in info.get("cells_4d", []):
                for zw in cell.get("Z_write", []):
                    zw_l = zw.lower()
                    z_write_toks.add(zw_l)
                    cell_4d_toks.add(zw_l)
                    for sub_t in _tokenize_symbol(zw):
                        if len(sub_t) >= 4:
                            cell_4d_toks.add(sub_t)
                for zr in cell.get("Z_read", []):
                    zr_l = zr.lower()
                    cell_4d_toks.add(zr_l)
                    for sub_t in _tokenize_symbol(zr):
                        if len(sub_t) >= 4:
                            cell_4d_toks.add(sub_t)
                x_sc = cell.get("X_scope", "")
                if x_sc.startswith(("useEffect", "useLayoutEffect", "useMemo", "useCallback", "case:")):
                    for x_id in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", x_sc):
                        if x_id not in ("useEffect", "useLayoutEffect", "useMemo", "useCallback", "case"):
                            cell_4d_toks.add(x_id.lower())
                            for sub_t in _tokenize_symbol(x_id):
                                if len(sub_t) >= 4:
                                    cell_4d_toks.add(sub_t)
            self._z_write_sets[rel_path] = z_write_toks
            self._cell_4d_tokens[rel_path] = cell_4d_toks

            for f in info.get("functions", []):
                nid = fn_id(rel_path, f["name"], f.get("parent_class"))
                if f.get("ast_hash"):
                    self._ast_hashes[nid] = f["ast_hash"]
            for c in info.get("classes", []):
                nid = cls_id(rel_path, c["name"])
                if c.get("ast_hash"):
                    self._ast_hashes[nid] = c["ast_hash"]

            parts = [
                rel_path,
                *[f["name"] for f in info.get("functions", [])],
                *[c["name"] for c in info.get("classes", [])],
                *[r["path"] for r in info.get("routes", [])],
                *info.get("imports", []),
                *info.get("exports", []),
                *tokenized,
                *sorted(cell_4d_toks),
                *[f["docstring"] for f in info.get("functions", []) if f.get("docstring")],
                *[c["docstring"] for c in info.get("classes", []) if c.get("docstring")],
            ]
            blob_str = " ".join(parts).lower()
            self._blobs[rel_path] = blob_str

            file_tokens = set(re.findall(r"[a-z0-9_]+", blob_str)) | set(tokenized) | cell_4d_toks
            for p_seg in re.split(r"[/._\-]", rel_path.lower()):
                if p_seg:
                    file_tokens.add(p_seg)
                    file_tokens.update(_tokenize_symbol(p_seg))
            self._token_sets[rel_path] = file_tokens
            for tok in file_tokens:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1

            self._defn_sets[rel_path] = {
                tok
                for name in symbol_names
                for tok in ([name.lower()] + _tokenize_symbol(name))
            } | {r["path"].lower() for r in info.get("routes", [])}

        # Precompute BM25-style IDF weights so specific domain symbols outrank common words
        n_docs = max(len(self._index), 1)
        self._idf = {
            tok: math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)) + 1.0
            for tok, df in doc_freq.items()
        }

        # Symbol-aware and file-aware import resolution
        file_set = set(self._index)
        module_index = resolve.build_python_module_index(self._index)

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

        _JSX_EXTS = {".tsx", ".jsx"}
        self._behaviors = {}
        for rel_path, info in self._index.items():
            ext = Path(rel_path).suffix.lower()
            self._behaviors[rel_path] = classify_file_functions(
                info, is_jsx=(ext in _JSX_EXTS),
            )

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
            str(path.relative_to(self.root)).replace("\\", "/"): path.stat().st_mtime
            for path in self._walk()
        }
        if current.keys() != self._index.keys():
            return True
        return any(current[rel] != self._index[rel].get("mtime") for rel in current)

    def incremental_index(self) -> dict:
        """Re-parse only changed files instead of a full re-index."""
        if not self._index:
            return self.index()

        current_files: dict[str, Path] = {}
        for path in self._walk():
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            current_files[rel] = path

        old_keys = set(self._index.keys())
        new_keys = set(current_files.keys())

        added = new_keys - old_keys
        deleted = old_keys - new_keys
        potentially_modified = old_keys & new_keys

        modified: set[str] = set()
        for rel in potentially_modified:
            path = current_files[rel]
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime != self._index[rel].get("mtime"):
                modified.add(rel)

        if not added and not deleted and not modified:
            return {
                "status": "up_to_date",
                "added": 0, "modified": 0, "deleted": 0,
                "total_files": len(self._index),
            }

        for rel in deleted:
            self._index.pop(rel, None)
            self._sizes.pop(rel, None)
            self._skipped.pop(rel, None)

        for rel in added | modified:
            path = current_files[rel]
            ext = path.suffix.lower()

            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                self._skipped[rel] = f"read_error:{e.__class__.__name__}"
                self._index.pop(rel, None)
                continue

            if _is_minified(source):
                self._skipped[rel] = "minified_or_generated"
                self._index.pop(rel, None)
                continue

            st = path.stat()
            info = parse(source, ext)
            info.setdefault("routes", []).extend(frameworks.infer_file_based_routes(rel, info))
            info["cells_4d"] = extract_4d_coordinate_matrix(source.splitlines(), ext=ext)
            info["mtime"] = st.st_mtime
            self._index[rel] = info
            self._sizes[rel] = st.st_size
            # Invalidate served session cache for modified files
            self._served_skeletons.discard(rel)

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
            {r: self._index[r] for r in (added | modified) if r in self._index},
            self._sizes,
            deleted,
            self._skipped,
            graph_json=self._graph.to_json(),
        )
        for legacy in (self._cache_path, self._graph_path):
            if legacy.exists():
                legacy.unlink(missing_ok=True)

        return {
            "status": "incremental_update",
            "added": len(added),
            "modified": len(modified),
            "deleted": len(deleted),
            "total_files": len(self._index),
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

    def query(self, prompt: str, top_n: int = 10) -> list[dict]:
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

        scores: dict[str, float] = {}
        for rel_path, blob in self._blobs.items():
            p_obj = Path(rel_path)
            # Field split (BM25F-style): the filename stem is the "title" field of a code
            # file and carries far stronger topical evidence than directory names.
            stem_low = p_obj.stem.lower()
            stem_tokens = set(_tokenize_symbol(p_obj.stem)) | {stem_low}
            dir_lower = "/".join(p_obj.parts[:-1]).lower()
            defns = self._defn_sets.get(rel_path, set())
            z_writes = self._z_write_sets.get(rel_path, set())
            tokens = self._token_sets.get(rel_path, set())
            score = 0.0
            for members in concept_groups:
                best = 0.0
                for kw in members:
                    # Prefer exact token match (avoids "id" matching "middleware"),
                    # fall back to substring only for longer keywords (>= 4 chars)
                    matched_token = kw in tokens
                    matched_sub = len(kw) >= 4 and kw in blob
                    in_stem = kw in stem_tokens or (len(kw) >= 4 and kw in stem_low)
                    if not (matched_token or matched_sub or in_stem):
                        continue
                    # Gentle IDF scaling when multiple keywords exist so rare terms win ties
                    idf_weight = (0.7 + 0.3 * self._idf.get(kw, 1.0)) if multi_kw else 1.0
                    if kw in defns:
                        base_hit = 2.0
                    elif kw in z_writes:
                        base_hit = 1.5
                    elif matched_token:
                        base_hit = 1.0
                    elif matched_sub:
                        base_hit = 0.6
                    else:
                        base_hit = 0.0
                    field_bonus = 1.5 if in_stem else (0.5 if kw in dir_lower else 0.0)
                    best = max(best, (base_hit + field_bonus) * idf_weight)
                score += best

            if score > 0:
                score *= _path_multiplier(rel_path)

                # Centrality is a query-independent prior: it should break ties, not
                # dominate relevance. Log-damped so a hub imported by dozens of files is
                # not doubled on every query merely for being widely imported.
                importer_count = self._importer_counts.get(rel_path, 0)
                if importer_count:
                    score *= 1.0 + 0.06 * math.log2(1 + importer_count)

                score = self._apply_intent_boost(
                    score, rel_path, intent, keywords,
                )

                scores[rel_path] = score

        if self._graph and scores:
            scores = self._graph_boost(scores, top_n)

        ranked = sorted(scores, key=lambda p: scores[p], reverse=True)[:top_n]
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
            for entity in intent.get("entities", []):
                ent_lower = entity.lower()
                defns = self._defn_sets.get(rel_path, set())
                if ent_lower in defns:
                    score *= 1.3

        elif intent_type == UNDERSTAND_FLOW:
            behaviors = self._behaviors.get(rel_path, {})
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
            behaviors = self._behaviors.get(rel_path, {})
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
        behaviors = self._behaviors.get(rel_path, {})
        functions = []
        for fn in all_functions[:MAX_FUNCTIONS_PER_FILE]:
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
        index, returning SUPPORTED / REFUTED / UNKNOWN with evidence.
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
        return any(o == c.lower() or o == c.lower().rsplit(".", 1)[-1] for c in captured)

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
        rank = {"exact": 3, "qualified": 2, "import_resolved": 1, "candidate": 0}
        o = obj.lower()
        found = None
        if self._graph:
            for ei in self._graph._adj.get(m["node_id"], []):
                edge = self._graph.edges[ei]
                if edge["type"] != relation:
                    continue
                tgt = self._graph.nodes.get(edge["dst"], {})
                tname = (tgt.get("name") or tgt.get("path") or "").lower()
                if not (o == tname or o == tname.rsplit(".", 1)[-1]):
                    continue
                if found is None or rank.get(edge["confidence"], 0) > rank.get(found["confidence"], 0):
                    found = {
                        "node_id": m["node_id"], "file": m["rel_path"],
                        "target": edge["dst"], "confidence": edge["confidence"],
                        "line": edge.get("line"), "reason": edge.get("reason", ""),
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

            behaviors = self._behaviors.get(rel_path, {})
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
                            behaviors = self._behaviors.get(callee_file, {})
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
            "skeleton": build_file_skeleton(rel, info, behaviors=self._behaviors.get(rel, {})),
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

    def _walk(self):
        # Detect if this workspace has sibling application projects (e.g. package.json in a child dir)
        # so nested copies of projectbrain itself (e.g. brian/projectbrain) are skipped.
        has_app_project = (self.root / "package.json").exists() or any(
            (child / "package.json").exists()
            for child in self.root.iterdir()
            if child.is_dir() and child.name not in SKIP_DIRS
        )
        for dirpath, dirs, files in os.walk(self.root, topdown=True):
            dp = Path(dirpath)
            filtered_dirs = []
            for d in dirs:
                if d in SKIP_DIRS:
                    continue
                cand = dp / d
                if has_app_project and (cand / "src" / "projectbrain" / "brain.py").exists():
                    continue
                filtered_dirs.append(d)
            dirs[:] = sorted(filtered_dirs)
            for fname in sorted(files):
                if fname in SKIP_FILES:
                    continue
                if any(fname.endswith(s) for s in SKIP_SUFFIXES):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in PARSED_EXTS:
                    continue
                yield dp / fname

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


def _path_multiplier(rel_path: str) -> float:
    parts = {p.lower() for p in Path(rel_path).parts[:-1]}
    if parts & DEPRIORITY_DIRS:
        return 0.3
    return 1.0


def _tokenize_symbol(name: str) -> list[str]:
    parts = re.split(r"[_\-]", name)
    tokens = []
    for part in parts:
        sub = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", part).split()
        tokens.extend(sub)
    return [t.lower() for t in tokens if len(t) > 1]


def _extract_keywords(prompt: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_/.-]+", prompt.lower())
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if "-" in token:
            expanded.extend(token.split("-"))
    seen: set[str] = set()
    result: list[str] = []
    for w in expanded:
        if w not in seen and w not in STOPWORDS and len(w) > 1:
            seen.add(w)
            result.append(w)
    return result
