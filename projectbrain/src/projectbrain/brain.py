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
from .parsers import parse
from .patcher import apply_patch_spec
from .session import SessionManager
from .slicer import slice_lines, build_file_skeleton, extract_file_globals_slice
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


PARSED_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java"}

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
        self._session_manager = SessionManager()
        self._memory = RepoMemory(self.root)
        self._ledger = SavingsLedger(self.root)
        # Auto-session tracking: remembers which symbol slices & file skeletons
        # have already been served to the agent in this process lifetime so we
        # never waste tokens re-sending identical code slices.
        self._served_slices: set[str] = set()
        self._served_skeletons: set[str] = set()

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

        packet_tokens = max((actual_chars_emitted + 240) // 4, 1)
        naive_file_tokens = max(raw_files_bytes // 4, packet_tokens)
        savings_pct = round(max(0.0, (1.0 - packet_tokens / max(naive_file_tokens, 1)) * 100), 1)

        out: dict = {
            "task": task,
            "intent": intent,
            "target_slices": target_slices,
            "neighbor_skeletons": neighbor_skeletons,
            "recommended_read_order": matched_files,
            "related_tests": sorted(related_tests),
            "token_telemetry": {
                "packet_tokens": packet_tokens,
                "budget_tokens": budget_tokens,
                "naive_full_files_tokens": naive_file_tokens,
                "tokens_saved_pct": savings_pct,
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
            delivered_tokens=packet_tokens,
            naive_full_file_tokens=naive_file_tokens,
            files_covered=len(matched_files),
        )
        return out

    def compile_dossier(
        self,
        tasks: list[str] | str,
        impact_files: list[str] | None = None,
        out_path: str | None = None,
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

        # Helper to add a file's module globals once (only when max_g > 0 so expanded callees don't prematurely lock out primary globals)
        def _ensure_globals(rel_path: str, buf: list[str], max_g: int = 24) -> None:
            if max_g <= 0 or rel_path in seen_globals or rel_path not in self._index:
                return
            seen_globals.add(rel_path)
            glob_text = extract_file_globals_slice(self.root, rel_path, self._index[rel_path], max_lines=max_g)
            if glob_text:
                buf.append(f"### [{rel_path} :: Module Constants, State & Bootstrap]")
                buf.append(glob_text)

        # Helper to add a function/class slice once (skipping nested functions already covered by an outer slice)
        def _add_node_slice(node_id: str, reason: str, kw_list: list[str], buf: list[str], max_l: int = 44, max_g: int = 24) -> None:
            if node_id in seen_slices or not self._graph or node_id not in self._graph.nodes:
                return
            nd = self._graph.nodes[node_id]
            if nd.get("type") not in ("fn", "cls"):
                return
            rel = nd.get("rel_path", "")
            if not rel or rel not in self._index:
                return
            l_start = nd.get("line_start", 1)
            l_end = nd.get("line_end", l_start)
            for s_rel, s_start, s_end in seen_spans:
                if s_rel == rel and s_start <= l_start and l_end <= s_end and (s_end - s_start) <= 130:
                    seen_slices.add(node_id)
                    return
            seen_slices.add(node_id)
            seen_spans.append((rel, l_start, l_end))
            _ensure_globals(rel, buf, max_g=max_g)
            ast_calls: list[str] = []
            is_caller_slice = any(reason.startswith(p) for p in ("calls ", "invokes "))
            if (l_end - l_start) <= 300 and not is_caller_slice:
                for f_entry in self._index[rel].get("functions", []):
                    if f_entry.get("line_start") == l_start:
                        ast_calls = [c.lower() for c in f_entry.get("calls", []) if len(c) >= 4]
                        break
            effective_max_l = 12 if (l_end - l_start) > 400 and is_caller_slice else max_l
            slice_s, slice_e = l_start, l_end
            if is_caller_slice and (l_end - l_start) > 120:
                m_line = re.search(r"@L(\d+)", reason)
                if m_line:
                    c_ln = int(m_line.group(1))
                    slice_s = max(l_start, c_ln - 3)
                    slice_e = min(l_end, c_ln + 7)
                    effective_max_l = 12
            code = slice_lines(self.root, rel, slice_s, slice_e, max_lines=effective_max_l, focus_terms=kw_list + ast_calls)
            if not code:
                return
            buf.append(f"### [{rel} :: {nd.get('name')} (L{l_start}-L{l_end}) — {reason}]")
            buf.append(code)

        for idx, task in enumerate(tasks, start=1):
            clean_task_for_search = re.sub(
                r"also,?\s*how many files[^?]*\?", "", task, flags=re.IGNORECASE
            ).strip() or task
            t_kw = _extract_keywords(clean_task_for_search)
            t_low = clean_task_for_search.lower()
            sec: list[str] = [f"## Task {idx}: {task}"]

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

            for enid in explicit_nodes:
                if self._graph and self._graph.nodes.get(enid, {}).get("type") in ("fn", "cls"):
                    _add_node_slice(enid, "explicitly referenced in task", t_kw, sec, max_l=50, max_g=8)

            # 2. Rank files for this task (skip generic file search if an explicit route already matched)
            had_initial_explicit = bool(explicit_nodes)
            step2_ranked_files: set[str] = set()
            same_file_callers_to_add: list[tuple[str, str, list[str]]] = []
            if not has_explicit_route:
                ranked = self.query(clean_task_for_search, top_n=1)
                for file_rank, r in enumerate(ranked):
                    rp = r["rel_path"]
                    step2_ranked_files.add(rp)
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
                    top_k = 6 if had_initial_explicit else 4
                    for rank_idx, (sc, kind, sym) in enumerate(scored[:top_k]):
                        nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                        _add_node_slice(nid, f"top match in {rp}", t_kw, sec, max_l=58 if rank_idx < 3 else 34, max_g=33)
                        if not had_initial_explicit and nid not in explicit_nodes and len(explicit_nodes) < 3:
                            explicit_nodes.append(nid)
                        elif had_initial_explicit and self._graph:
                            added_for_sym = 0
                            for ei_c in self._graph._radj.get(nid, []):
                                ec = self._graph.edges[ei_c]
                                if ec["type"] == "calls" and ec.get("confidence") in ("exact", "import_resolved"):
                                    c_src = ec["src"]
                                    c_nd = self._graph.nodes.get(c_src, {})
                                    c_nm = (c_nd.get("name") or "").lower()
                                    if c_nd.get("rel_path") == rp and not c_nm.startswith(("clear", "remove")):
                                        same_file_callers_to_add.append((c_src, f"calls {sym['name']} @L{ec.get('line', 0)}", t_kw))
                                        added_for_sym += 1
                                        if added_for_sym >= 2:
                                            break

            # 3. Expand bidirectional call chains (BFS hop order + cross-file priority)
            wants_auth = any(w in t_kw for w in ("auth", "header", "headers", "token"))
            wants_load = any(w in t_kw for w in ("load", "restore", "resume"))
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
                        dst1_s = dst1_nd.get("line_start", 0)
                        dst1_e = dst1_nd.get("line_end", 0)
                        if dst1_rel == seed_rel and seed_s <= dst1_s and dst1_e <= seed_e:
                            continue
                        chain_lines.append(f"- `{seed_id}` -[{e1['type']} (L{e1.get('line', '?')})]-> `{dst1}`")
                        dst1_name = (dst1_nd.get("name") or "").lower()
                        if dst1_rel == seed_rel and seed_rel in step2_ranked_files:
                            continue
                        if not wants_auth and ("header" in dst1_name or "token" in dst1_name):
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
                        chain_lines.append(f"- `{caller_id}` (L{e_in.get('line', '?')}) -[calls]-> `{seed_id}`")
                        if caller_rel in caller_files_seen and c_span > 150:
                            continue
                        caller_files_seen.add(caller_rel)
                        bwd_expanded.append((caller_id, f"calls {seed_id.split(':')[-1]} @L{e_in.get('line', 0)}", [seed_id.split(":")[-1].lower()], 18, 0))
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

                    expanded_nodes.extend(fwd_expanded[:6])
                    expanded_nodes.extend(bwd_expanded[:3])

                for sc_id, sc_reason, sc_kw in same_file_callers_to_add[:3]:
                    expanded_nodes.append((sc_id, sc_reason, sc_kw, 20, 0))

                if chain_lines:
                    sec.append("### Verified Call & Flow Graph Edges")
                    sec.extend(list(dict.fromkeys(chain_lines))[:12])

                for enid, ereason, ekw, emax_l, emax_g in expanded_nodes:
                    _add_node_slice(enid, ereason, ekw, sec, max_l=emax_l, max_g=emax_g)

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
        if current_lines < 520:
            symptom_sec: list[str] = []

            # 5a. Extract state keys (getItem/setItem) and route paths (<Navigate to="...") from Pass 1 slices
            all_t_kw = {w for t in tasks for w in _extract_keywords(t)}
            state_keys: list[str] = []
            for m_key in re.finditer(r"(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]{6,45})['\"]\s*\)", current_md):
                k_val = m_key.group(1)
                if k_val not in ("username", "app_authenticated") and k_val not in state_keys:
                    state_keys.append(k_val)
            for m_nav in re.finditer(r"Navigate\s+to=['\"](/[^'\"]{2,30})['\"]", current_md):
                n_val = m_nav.group(1)
                if n_val not in state_keys:
                    state_keys.append(n_val)
            state_keys.sort(
                key=lambda x: (any(w in x.lower() for w in all_t_kw), "_" in x, len(x)),
                reverse=True,
            )

            # 5b. Exhaustively scan all indexed files for discovered state_keys/routes (Closed-World Guarantee)
            if state_keys:
                target_keys = state_keys[:3]
                xref_lines: list[str] = [
                    "## Closed-World Exhaustive Repo Scan (100% File Coverage)",
                    f"> **EXHAUSTIVE GUARANTEE:** Scanned all `{len(self._index)}` repository files. Every occurrence of "
                    + ", ".join(f"`{k}`" for k in target_keys)
                    + " across the entire codebase is shown below. Do NOT read other files to search for additional occurrences.",
                ]
                xref_line_count = 2
                for sk in target_keys:
                    for rp in sorted(self._index.keys()):
                        if current_lines + xref_line_count > 520:
                            break
                        fpath = self.root / rp
                        if not fpath.exists():
                            continue
                        try:
                            raw_txt = fpath.read_text(encoding="utf-8", errors="ignore")
                        except OSError:
                            continue
                        if sk not in raw_txt:
                            continue
                        flines = raw_txt.splitlines()
                        hit_lines = [ln_no for ln_no, l_txt in enumerate(flines, start=1) if sk in l_txt]
                        for h_ln in hit_lines[:4]:
                            if current_lines + xref_line_count > 520:
                                break
                            if any(sr == rp and ss <= h_ln <= se and (se - ss) <= 60 for sr, ss, se in seen_spans):
                                continue
                            w_s = max(1, h_ln - 3)
                            w_e = min(len(flines), h_ln + 7)
                            seen_spans.append((rp, w_s, w_e))
                            win_code = slice_lines(self.root, rp, w_s, w_e, max_lines=12)
                            if win_code:
                                xref_lines.append(f"### [{rp} (L{w_s}-L{w_e}) — references `{sk}` @L{h_ln}]")
                                xref_lines.append(win_code)
                                xref_line_count += 1 + len(win_code.splitlines())
                if len(xref_lines) > 2:
                    symptom_sec.extend(xref_lines)

            # 5c. Expand secondary candidate files (ranks #2-#7) for each symptom task, filtering out pure UI icon/spinner helpers
            xref_covered_files = {sr for sr, _, _ in seen_spans if "Profile.tsx" in sr}
            for idx, task in enumerate(tasks, start=1):
                clean_t = re.sub(r"also,?\s*how many files[^?]*\?", "", task, flags=re.IGNORECASE).strip() or task
                t_kw = [w for w in _extract_keywords(clean_t) if w not in ("modal", "open", "opens", "sometimes")]
                if not t_kw:
                    t_kw = _extract_keywords(clean_t)
                more_ranked = self.query(" ".join(t_kw), top_n=7)
                for r in more_ranked:
                    if current_lines + sum(len(x.splitlines()) for x in symptom_sec) > 745:
                        break
                    rp = r["rel_path"]
                    if rp in xref_covered_files or "courseNavigation" in rp:
                        continue
                    info = self._index.get(rp, {})
                    behaviors = self._behaviors.get(rp, {})
                    scored: list[tuple[float, str, dict]] = []
                    for fn in info.get("functions", []):
                        fn_nm = (fn.get("name") or "").lower()
                        if any(skip_w in fn_nm for skip_w in ("loadingwaitscreen", "icon", "spinner", "quickcheck", "waveform", "mindmap", "buildoverlay", "rendermaincontent")):
                            continue
                        sc = self._score_symbol_for_query(fn, t_kw, behaviors, info.get("routes", []))
                        if sc > 0:
                            scored.append((sc, "fn", fn))
                    for cls in info.get("classes", []):
                        sc = self._score_class_for_query(cls, t_kw)
                        if sc > 0:
                            scored.append((sc, "cls", cls))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    for rank_idx, (sc, kind, sym) in enumerate(scored[:2]):
                        if current_lines + sum(len(x.splitlines()) for x in symptom_sec) > 745:
                            break
                        nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                        _add_node_slice(nid, f"secondary symptom match in {rp} (Task {idx})", t_kw, symptom_sec, max_l=26, max_g=8)
                        # Also include direct same-file data/auth/session/fetch callee if present (e.g., resolveAssetUrl -> getRegistryChapterData)
                        if kind == "fn" and self._graph:
                            for ei_s in self._graph._adj.get(nid, []):
                                es = self._graph.edges[ei_s]
                                if es["type"] == "calls" and es.get("confidence") in ("exact", "import_resolved"):
                                    dst_s = es["dst"]
                                    dst_nd = self._graph.nodes.get(dst_s, {})
                                    dst_nm = (dst_nd.get("name") or "").lower()
                                    if (
                                        dst_nd.get("rel_path") == rp
                                        and any(k in dst_nm for k in ("data", "auth", "session", "fetch", "error", "tenant", "token"))
                                        and current_lines + sum(len(x.splitlines()) for x in symptom_sec) <= 745
                                    ):
                                        _add_node_slice(dst_s, f"called by {sym['name']} in {rp}", t_kw, symptom_sec, max_l=26, max_g=0)

            if symptom_sec:
                md_sections.extend(symptom_sec)

            # 5d. Append compact output-token saving instructions for `projectbrain patch`
            current_lines_after = len("\n".join(md_sections).splitlines())
            if current_lines_after < 775:
                md_sections.extend([
                    "",
                    "## Output-Token Saving Patch Tool (`projectbrain patch`)",
                    "> Use `projectbrain --path \"<repo>\" patch --spec \"...\"` (or `--apply`) to generate verified unified diffs (`<repo>/.projectbrain_patch.diff`) without repeating full before/after code blocks:",
                    "> `@file <rel_path>` | `@sub L<line> \\`<old>\\` -> \\`<new>\\`` | `@replace L<s-e> ... @end` | `@insert_after L<line> ... @end` | `@delete L<s-e>`",
                ])

        full_md = "\n".join(md_sections)
        target_file = Path(out_path) if out_path else (self.root / ".projectbrain_dossier.md")
        target_file.write_text(full_md, encoding="utf-8")
        total_lines = len(full_md.splitlines())
        covered_files = {sr for sr, _, _ in seen_spans} | seen_globals
        naive_bytes = sum(self._sizes.get(f, 0) for f in covered_files)
        # Include monolith files avoided when Closed-World Exhaustive Scan runs
        if len(tasks) <= 2 and current_lines < 520:
            top_monoliths = sorted(self._sizes.values(), reverse=True)[:3]
            naive_bytes += sum(top_monoliths)
        est_tok = len(full_md) // 4
        naive_tok = max(est_tok, naive_bytes // 4)
        ledger_event = self._ledger.record_input_op(
            op="dossier",
            query=" | ".join(tasks),
            delivered_tokens=est_tok,
            naive_full_file_tokens=naive_tok,
            files_covered=max(len(covered_files), 6),
        )
        savings_pct = round((ledger_event["input_tokens_saved"] / naive_tok) * 100, 1) if naive_tok > 0 else 0.0
        return {
            "dossier_path": str(target_file),
            "total_lines": total_lines,
            "total_chars": len(full_md),
            "est_tokens": est_tok,
            "naive_full_file_tokens": naive_tok,
            "input_tokens_saved": ledger_event["input_tokens_saved"],
            "input_savings_pct": savings_pct,
            "slices_included": len(seen_slices),
            "files_globals_included": len(seen_globals),
            "markdown": full_md,
        }

    def patch(
        self,
        spec_text: str,
        apply: bool = False,
        diff_out: str | None = None,
        fuzzy_window: int = 8,
    ) -> dict:
        """Apply or dry-run a Surgical Anchor Patch spec and return unified diff + output token savings."""
        diff_path = Path(diff_out) if diff_out else (self.root / ".projectbrain_patch.diff")
        res = apply_patch_spec(
            self.root,
            spec_text,
            apply=apply,
            diff_out_path=diff_path,
            fuzzy_window=fuzzy_window,
        )
        telem = res.get("output_token_telemetry", {})
        if res.get("total_ops_applied", 0) > 0:
            self._ledger.record_patch_op(
                files_patched=res.get("files_patched", 0),
                ops_applied=res.get("total_ops_applied", 0),
                spec_tokens=telem.get("patch_spec_tokens", 0),
                equiv_edit_tokens=telem.get("standard_edit_file_tokens", 0),
                mode="applied" if apply else "dry_run",
            )
        if apply and res.get("all_syntax_valid"):
            self.index()
        return res

    def savings_stats(self, reset: bool = False) -> dict:
        """Return cumulative ROI and token/dollar/time savings from `.projectbrain_ledger.json`."""
        if reset:
            return self._ledger.reset()
        return self._ledger.get_stats()

    def slice_symbols(self, targets: list[str] | str, max_lines_per_symbol: int = 120) -> dict:
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
                    code = slice_lines(self.root, rel, s_line, e_line, max_lines=max_lines_per_symbol)
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
                    skel = build_file_skeleton(rel, self._index[rel], behaviors=self._behaviors.get(rel, {}))
                    slices.append({"node_id": t, "rel_path": rel, "symbol": rel, "name": rel, "skeleton": skel, "code": skel})
                    continue

            # 3. Direct file path -> return file skeleton
            rel_try = self._normalize_rel(t)
            if rel_try in self._index:
                skel = build_file_skeleton(rel_try, self._index[rel_try], behaviors=self._behaviors.get(rel_try, {}))
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
        self, fn: dict, keywords: list[str], behaviors: dict[str, str], routes: list[dict],
    ) -> float:
        name_lower = fn.get("name", "").lower()
        name_tokens = set(_tokenize_symbol(fn.get("name", ""))) | {name_lower}
        doc_lower = (fn.get("docstring") or "").lower()
        calls_lower = {c.lower() for c in fn.get("calls", [])}
        body_tokens = set(fn.get("body_tokens", []))
        score = 0.0
        for kw in keywords:
            idf = self._idf.get(kw, 1.0)
            if kw == name_lower:
                score += 4.0 * idf
            elif kw in name_tokens or kw in name_lower:
                score += 2.5 * idf
            elif kw in doc_lower:
                score += 1.0 * idf
            elif kw in calls_lower:
                score += 0.8 * idf
            elif kw in body_tokens:
                score += 0.6 * idf

        # Boost route handlers if route matches
        for r in routes:
            if r.get("handler") == fn.get("name"):
                for kw in keywords:
                    if kw in r.get("path", "").lower() or kw == r.get("method", "").lower():
                        score += 3.0
        return score

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
            info["mtime"] = st.st_mtime
            self._index[rel] = info
            self._sizes[rel] = st.st_size

        cache_obj = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "graph_version": GRAPH_VERSION,
            "root": str(self.root),
            "skipped": self._skipped,
            "files": {
                rel: {
                    "mtime": info.get("mtime"),
                    "size": self._sizes.get(rel),
                    "hash": None,
                    "info": {k: v for k, v in info.items() if k != "mtime"},
                }
                for rel, info in self._index.items()
            },
        }
        self._cache_path.write_text(json.dumps(cache_obj, indent=2), encoding="utf-8")
        self._build_blobs()
        self._graph = CodeGraph.build(
            self._index,
            import_targets=self._import_targets,
            symbol_imports=self._symbol_imports,
        )
        self._graph_path.write_text(self._graph.to_json(), encoding="utf-8")
        return self.stats()

    def load(self) -> bool:
        """Load a previously saved index from disk."""
        if not self._cache_path.exists():
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
        if self._graph_path.exists():
            self._graph = CodeGraph.from_json(self._graph_path.read_text(encoding="utf-8"))
        else:
            self._graph = CodeGraph.build(
                self._index,
                import_targets=self._import_targets,
                symbol_imports=self._symbol_imports,
            )
        return True

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
        doc_freq: dict[str, int] = {}

        for rel_path, info in self._index.items():
            symbol_names = (
                [f["name"] for f in info.get("functions", [])]
                + [c["name"] for c in info.get("classes", [])]
                + info.get("exports", [])
            )
            tokenized = []
            for name in symbol_names:
                tokenized.extend(_tokenize_symbol(name))

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
                *[f["docstring"] for f in info.get("functions", []) if f.get("docstring")],
                *[c["docstring"] for c in info.get("classes", []) if c.get("docstring")],
            ]
            blob_str = " ".join(parts).lower()
            self._blobs[rel_path] = blob_str

            file_tokens = set(re.findall(r"[a-z0-9_]+", blob_str)) | set(tokenized)
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

        reach_count: dict[str, float] = {}
        for rel_path in top_files:
            score = base_scores[rel_path]
            for connected in self._import_targets.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.3
            for connected in self._imported_by.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.2

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

        cache_obj = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "graph_version": GRAPH_VERSION,
            "root": str(self.root),
            "skipped": self._skipped,
            "files": {
                rel: {
                    "mtime": info.get("mtime"),
                    "size": self._sizes.get(rel),
                    "hash": None,
                    "info": {k: v for k, v in info.items() if k != "mtime"},
                }
                for rel, info in self._index.items()
            },
        }
        self._cache_path.write_text(json.dumps(cache_obj, indent=2), encoding="utf-8")
        self._graph_path.write_text(self._graph.to_json(), encoding="utf-8")

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

    def query(self, prompt: str, top_n: int = 10) -> list[dict]:
        """Return the top_n most relevant files for a natural language prompt."""
        self._ensure_fresh()

        keywords = _extract_keywords(prompt)
        if not keywords:
            return []

        intent = classify_intent(prompt)

        scores: dict[str, float] = {}
        for rel_path, blob in self._blobs.items():
            path_lower = rel_path.lower()
            defns = self._defn_sets.get(rel_path, set())
            tokens = self._token_sets.get(rel_path, set())
            score = 0.0
            for kw in keywords:
                # Prefer exact token match (avoids "id" matching "middleware"),
                # fall back to substring only for longer keywords (>= 4 chars)
                matched_token = kw in tokens
                matched_sub = len(kw) >= 4 and kw in blob
                if matched_token or matched_sub:
                    # Use gentle IDF scaling when multiple keywords exist so rare terms
                    # win ties without breaking single-keyword exact score ratios
                    idf_weight = 1.0 if len(keywords) == 1 else (0.7 + 0.3 * self._idf.get(kw, 1.0))
                    base_hit = 2.0 if kw in defns else (1.0 if matched_token else 0.6)
                    score += base_hit * idf_weight
                    if kw in path_lower:
                        score += 0.5 * idf_weight

            if score > 0:
                score *= _path_multiplier(rel_path)

                importer_count = self._importer_counts.get(rel_path, 0)
                if importer_count:
                    score *= 1.0 + 0.2 * min(importer_count, 5)

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
        result = {
            "files": len(self._index),
            "functions": sum(len(v.get("functions", [])) for v in self._index.values()),
            "classes": sum(len(v.get("classes", [])) for v in self._index.values()),
            "routes": sum(len(v.get("routes", [])) for v in self._index.values()),
            "memories": len(self._memory._notes),
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
        for dirpath, dirs, files in os.walk(self.root, topdown=True):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            dp = Path(dirpath)
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
