import json
import os
import re
from pathlib import Path

from . import frameworks, resolve
from .classify import classify_file_functions
from .graph import CodeGraph, fn_id, cls_id, file_id, route_id
from .intent import classify_intent, LOCATE_SYMBOL, LOCATE_ROUTE, UNDERSTAND_FLOW, IMPACT_ANALYSIS, ADD_FEATURE
from .parsers import parse
from .session import SessionManager
from .versions import SCHEMA_VERSION, PARSER_VERSION, GRAPH_VERSION

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", ".turbo", "coverage",
    ".cache", ".parcel-cache", ".webpack", "out", ".output",
    "dist-deploy", ".vite-cache",
}

# Exact filenames that are always bundles/artifacts
SKIP_FILES = {
    "out.js", "out.cjs", "out.mjs", "out.min.js",
    "bundle.js", "bundle.cjs", "bundle.min.js",
    "index.min.js", "main.min.js",
    "vendor.js", "vendors.js", "chunk.js",
}

# Filename patterns treated as minified/generated
SKIP_SUFFIXES = {".min.js", ".min.cjs", ".min.mjs", ".min.ts"}

# If any line in the file exceeds this, treat it as minified
MINIFIED_LINE_THRESHOLD = 500

# Response budgets. A single god-file (a 5k-line React page with 80+ functions)
# can exceed an MCP client's token limit on its own, which fails the whole query
# no matter how small top_n is. These cap the payload so a query always returns.
MAX_FUNCTIONS_PER_FILE = int(os.environ.get("PROJECTBRAIN_MAX_FUNCTIONS", "40"))
MAX_SUBGRAPH_NODES = int(os.environ.get("PROJECTBRAIN_MAX_NODES", "120"))
MAX_SUBGRAPH_EDGES = int(os.environ.get("PROJECTBRAIN_MAX_EDGES", "200"))

# Least-to-most expendable when trimming a subgraph to MAX_SUBGRAPH_NODES.
_NODE_PRIORITY = {"file": 0, "route": 1, "cls": 2, "mod": 3, "fn": 4}

# Edge types worth keeping even when both ends sit in the same file: they are
# low-volume and describe structure a reader cannot infer from a function list.
_STRUCTURAL_EDGES = {"handles", "inherits", "has_method", "exports"}

# verify_claim: relation → the kind of node the subject must be. "calls" is a
# DIRECT call (honest, and what makes REFUTED tractable); "reaches" is transitive
# call flow answered by a forward graph path.
_VERIFY_RELATIONS = {
    "calls": "fn",
    "reaches": "any",
    "inherits": "cls",
    "has_method": "cls",
    "defines": "file",
    "imports": "file",
    "handles": "route",
}

# Confidences strong enough to SUPPORT a claim. A "candidate" (name-only) match
# is deliberately excluded — it becomes UNKNOWN, never a confident SUPPORTED.
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
    """Strip an edge to what a reader cannot already infer.

    "exact" confidence and its boilerplate reason ("direct definition in file")
    are the overwhelmingly common case, so they are implied by omission. A
    "candidate" edge is a name-only guess, and there the reason is the whole
    point — keep both.
    """
    out = {k: edge[k] for k in ("src", "dst", "type")}
    if edge.get("confidence") != "exact":
        out["confidence"] = edge.get("confidence")
        if edge.get("reason"):
            out["reason"] = edge["reason"]
    return out


def _trim_subgraph(subgraph: dict) -> dict:
    """Reduce a subgraph to the cross-file structure, within the response budget.

    Purely intra-file wiring is dropped: a file → its own function ("defines"),
    or a function → another function beside it ("calls"). Neither helps a caller
    decide which file to open, both are visible the moment the file is opened,
    and on a god-file they dominate the payload. What survives is how the
    returned files actually reach each other, plus the structural edges
    (handles/inherits/has_method/exports) that stay cheap. Function nodes are
    kept only where a surviving edge references them, so nothing dangles.
    """
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
                "Use projectbrain_get_neighbors on a node ID for the full neighborhood."
            ),
        }
    return result

PARSED_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java"}

# Path segments that indicate non-core files — examples, tests, docs, etc.
# Files with any of these as a directory component get a score penalty.
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
        self._cache_path = self.root / ".projectbrain.json"
        self._graph: CodeGraph | None = None
        self._graph_path = self.root / ".projectbrain_graph.json"
        self._defn_sets: dict[str, set[str]] = {}      # rel_path → lowercase symbol names
        self._importer_counts: dict[str, int] = {}     # rel_path → number of files that import it
        self._sizes: dict[str, int] = {}               # rel_path → file size in bytes
        self._import_targets: dict[str, list[str]] = {}  # rel_path → resolved files it imports
        self._imported_by: dict[str, list[str]] = {}     # rel_path → resolved files that import it
        self._skipped: dict[str, str] = {}               # rel_path → reason a walked file was not indexed
        self._behaviors: dict[str, dict[str, str]] = {}  # rel_path → {func_name: behavior}
        self._session_manager = SessionManager()

    # ── Session management ──────────────────────────────────────────────────

    def start_session(self, task: str) -> dict:
        """Start an iterative routing session for a coding task.

        Returns session_id plus initial file suggestions ranked by relevance.
        The agent should read these files one at a time, calling mark_read()
        after each, and Brain will route to the next relevant file.
        """
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
        rel_path = str(Path(file_path).as_posix())
        # Normalize: if it's an absolute path under root, make relative
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

    # ── Context compression ─────────────────────────────────────────────────

    def compress_context(
        self,
        file_paths: list[str],
        max_tokens: int = 4000,
        focus: str = "",
    ) -> dict:
        """Structure-aware context compression for a set of files.

        Instead of dumping entire file contents, returns a compressed view
        showing function/class signatures, key lines, docstrings, and
        structural relationships — enough for an agent to understand the
        architecture without reading every line.

        Args:
            file_paths: List of file paths (absolute or relative).
            max_tokens: Approximate token budget (1 token ≈ 4 chars).
            focus: Optional focus keyword — prioritize functions/classes
                   matching this term.

        Returns:
            dict with compressed context per file and total token estimate.
        """
        self._ensure_fresh()

        char_budget = max_tokens * 4  # Rough char-to-token ratio
        results = []
        total_chars = 0
        focus_lower = focus.lower() if focus else ""

        for path in file_paths:
            # Normalize to relative path
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

            # Build compressed representation
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
        """Compress a single file's context into a structured summary."""
        functions = info.get("functions", [])
        classes = info.get("classes", [])
        routes = info.get("routes", [])
        imports = info.get("imports", [])
        exports = info.get("exports", [])
        behaviors = self._behaviors.get(rel_path, {})

        # Sort functions: focused ones first, then by importance
        def fn_priority(fn: dict) -> int:
            name = fn.get("name", "").lower()
            if focus and focus in name:
                return 0  # Focused functions come first
            qualified = fn["name"]
            if fn.get("parent_class"):
                qualified = f"{fn['parent_class']}.{fn['name']}"
            behavior = behaviors.get(qualified, "unknown")
            # Handlers and orchestrators are most important
            _BEHAVIOR_ORDER = {
                "handler": 1, "orchestrator": 2, "constructor": 3,
                "io_writer": 4, "io_reader": 5, "validator": 6,
                "transformer": 7, "renderer": 8, "unknown": 9,
            }
            return _BEHAVIOR_ORDER.get(behavior, 9)

        sorted_fns = sorted(functions, key=fn_priority)

        # Build compressed function summaries
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

            calls = fn.get("calls", [])[:5]  # Top 5 callees
            summary = {
                "name": fn["name"],
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

        # Build class summaries
        cls_summaries = []
        for cls in classes:
            cs = {"name": cls["name"], "lines": f"L{cls['line_start']}-{cls['line_end']}"}
            if cls.get("bases"):
                cs["extends"] = cls["bases"]
            if cls.get("methods"):
                cs["methods"] = cls["methods"][:10]
            cls_summaries.append(cs)

        entry: dict = {
            "rel_path": rel_path,
            "status": "compressed",
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

        # Add connection summary
        connections = self._connections(rel_path)
        if connections.get("imports_files") or connections.get("imported_by"):
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
                # Record instead of silently dropping — a file we couldn't read
                # is a blind spot verify_claim must not mistake for "no symbols".
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
        self._graph = CodeGraph.build(self._index, import_targets=self._import_targets)
        self._graph_path.write_text(self._graph.to_json(), encoding="utf-8")
        return self.stats()

    def load(self) -> bool:
        """Load a previously saved index from disk.

        Returns False (never raises) for a missing, corrupt, or
        schema/parser/graph-version-mismatched cache — the caller
        (_ensure_fresh) treats that identically to "no cache" and
        triggers a full reindex.
        """
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
            self._graph = CodeGraph.build(self._index, import_targets=self._import_targets)
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
        """Precompute a lowercase searchable string per file, once per index/load."""
        self._blobs = {}
        self._defn_sets = {}
        for rel_path, info in self._index.items():
            symbol_names = (
                [f["name"] for f in info.get("functions", [])]
                + [c["name"] for c in info.get("classes", [])]
                + info.get("exports", [])
            )
            tokenized = []
            for name in symbol_names:
                tokenized.extend(_tokenize_symbol(name))

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
            self._blobs[rel_path] = " ".join(parts).lower()

            # Definition names only — used to distinguish "implements X" from "mentions X"
            self._defn_sets[rel_path] = {
                tok
                for name in symbol_names
                for tok in ([name.lower()] + _tokenize_symbol(name))
            } | {r["path"].lower() for r in info.get("routes", [])}

        # Import resolution: resolve each raw import string to an actual
        # indexed file (Python dotted-module / JS relative-path resolution),
        # rather than the old stem-substring guess. Unresolvable or ambiguous
        # imports (bare npm packages, ambiguous absolute Python imports,
        # imports outside the indexed set) contribute nothing — precision
        # over recall, consistent with the "honest about uncertainty" design.
        file_set = set(self._index)
        module_index = resolve.build_python_module_index(self._index)

        self._import_targets = {}
        self._imported_by = {}
        for importer_path, info in self._index.items():
            targets: list[str] = []
            for imp in info.get("imports", []):
                target = resolve.resolve_import(importer_path, imp, file_set, module_index)
                if target and target != importer_path and target not in targets:
                    targets.append(target)
            self._import_targets[importer_path] = targets
            for target in targets:
                self._imported_by.setdefault(target, []).append(importer_path)

        self._importer_counts = {
            rel_path: len(self._imported_by.get(rel_path, []))
            for rel_path in self._index
        }

        # Behavioral classification of all functions in the index
        _JSX_EXTS = {".tsx", ".jsx"}
        self._behaviors = {}
        for rel_path, info in self._index.items():
            ext = Path(rel_path).suffix.lower()
            self._behaviors[rel_path] = classify_file_functions(
                info, is_jsx=(ext in _JSX_EXTS),
            )

    def _graph_boost(self, base_scores: dict[str, float], top_n: int) -> dict[str, float]:
        """Expand keyword hits through graph edges for graph-boosted ranking.

        For each file that scored > 0 in keyword matching, walk 1-2 hops
        outward through import/call edges.  Files reachable from *multiple*
        scored files get a boost proportional to how many scored files reach
        them.  This surfaces "bridge" files that connect the query concepts.
        """
        if not self._graph:
            return base_scores

        boosted = dict(base_scores)
        # Only expand from the top candidates to keep it fast
        top_files = sorted(base_scores, key=lambda p: base_scores[p], reverse=True)[:top_n * 2]

        reach_count: dict[str, float] = {}  # rel_path → sum of source scores
        for rel_path in top_files:
            score = base_scores[rel_path]
            # Walk outward through imports (both directions)
            for connected in self._import_targets.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.3
            for connected in self._imported_by.get(rel_path, []):
                reach_count[connected] = reach_count.get(connected, 0) + score * 0.2

        # Apply the boost: files that are connected to multiple keyword hits
        # get their score increased
        for rel_path, reach_score in reach_count.items():
            if rel_path in boosted:
                boosted[rel_path] += reach_score * 0.5
            elif reach_score > 0:
                # File wasn't in keyword results but is connected to multiple
                # results — include it with a fraction of the reach score
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
        """Re-parse only changed files instead of a full re-index.

        Detects which files are new, modified, or deleted since the last
        index, and only re-parses those. Updates the graph, blobs, and
        cache accordingly.

        Returns stats about what changed.
        """
        if not self._index:
            # No existing index — fall back to full index
            return self.index()

        # Scan current files
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

        # Remove deleted files
        for rel in deleted:
            self._index.pop(rel, None)
            self._sizes.pop(rel, None)
            self._skipped.pop(rel, None)

        # Re-parse added + modified files
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

        # Rebuild derived data structures
        self._build_blobs()
        self._graph = CodeGraph.build(self._index, import_targets=self._import_targets)

        # Save updated cache
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

        # Classify the query intent to enable strategy-specific scoring
        intent = classify_intent(prompt)

        scores: dict[str, float] = {}
        for rel_path, blob in self._blobs.items():
            path_lower = rel_path.lower()
            defns = self._defn_sets.get(rel_path, set())
            score = 0.0
            for kw in keywords:
                if kw in blob:
                    # +2.0 if the file *defines* a symbol with this name;
                    # +1.0 if it merely mentions it (docstring, import, etc.)
                    score += 2.0 if kw in defns else 1.0
                    if kw in path_lower:
                        score += 0.5

            if score > 0:
                # Penalise files in non-core directories (examples, tests, docs…)
                score *= _path_multiplier(rel_path)

                # Boost files that are heavily imported — they're more likely
                # to be engine/library code than leaf scripts or examples.
                importer_count = self._importer_counts.get(rel_path, 0)
                if importer_count:
                    score *= 1.0 + 0.2 * min(importer_count, 5)

                # Intent-specific scoring adjustments
                score = self._apply_intent_boost(
                    score, rel_path, intent, keywords,
                )

                scores[rel_path] = score

        # Graph-boosted ranking: expand through import edges
        if self._graph and scores:
            scores = self._graph_boost(scores, top_n)

        ranked = sorted(scores, key=lambda p: scores[p], reverse=True)[:top_n]

        # Reason-building inspects per-file structure (functions/classes/routes/
        # imports/exports) — only worth doing for the handful of files we're
        # actually about to return, not the whole index.
        return [self._build_result(rel_path, scores[rel_path], keywords) for rel_path in ranked]

    def _apply_intent_boost(
        self, score: float, rel_path: str, intent: dict, keywords: list[str],
    ) -> float:
        """Apply intent-specific scoring adjustments."""
        intent_type = intent["intent"]
        info = self._index.get(rel_path, {})

        if intent_type == LOCATE_ROUTE:
            # Boost files that define routes matching the query
            for r in info.get("routes", []):
                for kw in keywords:
                    if kw in r["path"].lower() or kw == r["method"].lower():
                        score *= 1.5
                        break

        elif intent_type == LOCATE_SYMBOL:
            # Boost files that *define* (not just mention) the target symbol
            for entity in intent.get("entities", []):
                ent_lower = entity.lower()
                defns = self._defn_sets.get(rel_path, set())
                if ent_lower in defns:
                    score *= 1.3

        elif intent_type == UNDERSTAND_FLOW:
            # Boost orchestrator and handler functions (they're the flow)
            behaviors = self._behaviors.get(rel_path, {})
            flow_roles = {"orchestrator", "handler"}
            if any(b in flow_roles for b in behaviors.values()):
                score *= 1.3

        elif intent_type == IMPACT_ANALYSIS:
            # Boost heavily-imported files (high fan-in = high impact)
            importer_count = self._importer_counts.get(rel_path, 0)
            if importer_count >= 3:
                score *= 1.2

        elif intent_type == ADD_FEATURE:
            # Boost files that are import targets (extension points)
            # and files with route handlers (where features get wired in)
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
        # Drop each function's "calls" list: it is the single largest contributor
        # to response size and is already carried by the subgraph's "calls" edges.
        behaviors = self._behaviors.get(rel_path, {})
        functions = []
        for fn in all_functions[:MAX_FUNCTIONS_PER_FILE]:
            fn_entry = {k: fn[k] for k in ("name", "line_start", "line_end") if k in fn}
            # Add behavioral classification
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
            "reason": "; ".join(deduped[:3]) if deduped else "keyword match",
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

    def find_relationship(self, name_a: str, name_b: str) -> dict:
        self._ensure_fresh()
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
        for a_id in candidates_a[:3]:
            for b_id in candidates_b[:3]:
                p = self._graph.find_path(a_id, b_id)
                # find_path returns [] for both "unreachable" and "same node" —
                # neither is a real path, so only a non-empty p is a candidate.
                # (Using `is not None` here previously let the first unreachable
                # pair's [] win permanently, since no length is ever < 0.)
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

        Conservative by design — the anti-hallucination contract:
          * SUPPORTED only on a structurally-exact or import-resolved fact.
          * REFUTED only when the closed world is provably complete: the subject
            is unambiguous, its file parsed cleanly ("ok"), and the relation is
            exhaustively captured. Absence in that captured set is then decisive.
          * UNKNOWN for everything else, each with a specific reason (not indexed,
            parse errors, unsupported language, ambiguous name, ambiguous/external
            target). The tool never guesses in order to look decisive.

        relation ∈ {calls (direct), reaches (transitive call flow), inherits,
        has_method, defines, imports, handles}.
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
                # Name is present but the target is ambiguous/external: honest
                # UNKNOWN rather than a false SUPPORTED.
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
        """Resolve a subject name to the concrete parsed entities of the right kind.

        Kind is fixed by the relation, so a function named like a class never
        collides. Returns each match with its raw entity, node ID, file, and the
        file's parse_status (which gates whether a REFUTED is even allowed)."""
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

    @staticmethod
    def _captured_set(m: dict, relation: str, info: dict) -> set:
        """The COMPLETE set of names the parser captured for this relation on the
        subject. Drawn from raw parsed info (not the lossy graph edges) so that
        an absence is authoritative for a REFUTED."""
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
            return set(info.get("imports", []))
        return set()

    def _support_evidence(self, m: dict, relation: str, obj: str, info: dict) -> dict:
        """Best available evidence that m --relation--> obj holds.

        For calls/inherits the confidence comes from the resolved graph edge
        (exact / import_resolved / candidate). For the purely-structural relations
        (defines / has_method / imports / handles) membership in the captured set
        IS an exact fact, so those are exact by construction."""
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
        if relation in ("defines", "has_method", "imports", "handles"):
            return {
                "node_id": m["node_id"], "file": m["rel_path"], "target": None,
                "confidence": "exact", "line": m.get("line"),
                "reason": f"'{obj}' is a direct {relation} fact of {m['label']}.",
            }
        if found:  # candidate-only edge — real, but not decisive
            return found
        # Name is in the captured set but resolves to nothing indexed (external).
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
        for a in a_ids[:3]:
            for b in b_ids[:3]:
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
        """Files that import rel_path (1-hop, reverse direction, import-resolved)."""
        return self._imported_by.get(rel_path, [])[:limit]

    def _resolve_imports(self, rel_path: str, limit: int = 5) -> list[str]:
        """Files rel_path imports, resolved to index entries (1-hop, forward direction)."""
        return self._import_targets.get(rel_path, [])[:limit]

    # ── Sprint 4: Verification & Safety ─────────────────────────────────────

    def verify_batch(self, claims: list[tuple[str, str, str]]) -> dict:
        """Verify multiple structural claims in a single call.

        Args:
            claims: List of (subject, relation, object) tuples.

        Returns:
            dict with per-claim verdicts and a summary.
        """
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
        """Validate an agent's plan BEFORE execution.

        Checks whether the plan covers all files that would plausibly need
        changes by tracing graph edges from each planned file.  Surfaces:
        - Missing files that the plan doesn't cover but are connected
        - Test files that may need updating
        - Risk assessment per planned file

        Args:
            task: Description of the task.
            files_to_edit: List of file paths the agent plans to modify.
            files_to_create: Optional list of new files to create.

        Returns:
            dict with warnings, missing_files, risk assessment, and verdict.
        """
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
                continue  # New file or not indexed — can't analyze

            # Check importers: files that import a planned-edit file
            # may need updates themselves
            importers = self._imported_by.get(rel_path, [])
            for imp in importers:
                if imp in plan_set:
                    continue  # Already in plan

                # Check if it's a test file
                imp_lower = imp.lower()
                is_test = any(seg in imp_lower for seg in (
                    "test", "spec", "__tests__", "tests/",
                ))
                if is_test:
                    test_files.append(imp)
                else:
                    # Non-test file that imports something we're editing
                    # but isn't in the plan — potential miss
                    missing_files.append({
                        "rel_path": imp,
                        "reason": f"imports '{rel_path}' which is being edited",
                        "severity": "warning",
                    })

            # Check what this file imports — if we're editing something
            # that depends on a model/type file, the model might need changes too
            targets = self._import_targets.get(rel_path, [])
            for target in targets:
                if target in plan_set:
                    continue
                target_info = self._index.get(target, {})
                # If the dependency defines classes (models/types), flag it
                if target_info.get("classes"):
                    cls_names = [c["name"] for c in target_info["classes"]]
                    # Check if any class is mentioned in the task
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

            # Risk assessment per file
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

        # Deduplicate missing files
        seen: set[str] = set()
        unique_missing = []
        for mf in missing_files:
            if mf["rel_path"] not in seen:
                seen.add(mf["rel_path"])
                unique_missing.append(mf)

        # Build verdict
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

    def impact(self, files_changed: list[str]) -> dict:
        """Analyze the impact of changing a set of files.

        For each changed file, finds:
        - All files that import it (direct consumers)
        - All functions that call functions defined in it
        - Risk level based on fan-in (how many files depend on it)
        - Behavioral summary of what the changed file does

        Args:
            files_changed: List of file paths that were changed.

        Returns:
            dict with per-file impact analysis, total impact zone, and risk.
        """
        self._ensure_fresh()

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

            # Find which functions in this file are called by other files
            defined_fns = {fn["name"] for fn in info.get("functions", [])}
            callers: list[dict] = []
            if self._graph and defined_fns:
                for fn_name in defined_fns:
                    fn_node_id = fn_id(rel_path, fn_name)
                    if fn_node_id in self._graph.nodes:
                        # Find reverse call edges
                        for edge in self._graph.edges:
                            if edge["dst"] == fn_node_id and edge["type"] == "calls":
                                src_node = self._graph.nodes.get(edge["src"], {})
                                callers.append({
                                    "function": fn_name,
                                    "called_by": edge["src"],
                                    "in_file": src_node.get("rel_path", ""),
                                    "confidence": edge.get("confidence", "candidate"),
                                })

            # Behavioral summary
            behaviors = self._behaviors.get(rel_path, {})
            behavior_summary = {}
            for name, behavior in behaviors.items():
                if behavior != "unknown":
                    behavior_summary.setdefault(behavior, []).append(name)

            # Risk assessment
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

            # Test vs production importers
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
            # Add routes defined in this file
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
        """1-hop graph neighbors: what this file imports and who imports it."""
        connections = [
            {"rel_path": other, "relation": "imports", "confidence": "import_resolved"}
            for other in self._resolve_imports(rel_path)
        ]
        connections += [
            {"rel_path": other, "relation": "imported_by", "confidence": "import_resolved"}
            for other in self._find_importers(rel_path)
        ]
        return connections

    # ── Sprint 5: Cross-Boundary Intelligence ───────────────────────────────

    def trace_flow(self, url_path: str, method: str = "GET") -> dict:
        """Trace a request flow from frontend trigger to backend handler to data layer.

        Given a URL path and HTTP method, finds:
        1. Frontend triggers — files containing fetch/axios calls to this URL
        2. Backend route — the handler registered for this URL
        3. Handler chain — what the handler calls (services, data access)

        Args:
            url_path: The URL path to trace (e.g., "/api/checkout")
            method: HTTP method (GET, POST, PUT, DELETE, PATCH). Default: GET.

        Returns:
            dict with ordered flow steps from UI trigger → route → handler → data.
        """
        self._ensure_fresh()
        method = method.upper()
        steps: list[dict] = []

        # Step 1: Find frontend triggers (fetch/axios/http calls to this URL)
        ui_triggers = self._find_url_callers(url_path, method)
        for trigger in ui_triggers:
            steps.append({
                "step": len(steps) + 1,
                "type": "ui_trigger",
                "rel_path": trigger["rel_path"],
                "line": trigger.get("line"),
                "detail": trigger.get("detail", f"calls {method} {url_path}"),
            })

        # Step 2: Find the backend route handler
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

            # Step 3: Trace handler's call chain
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
        """Find files containing fetch/axios/http client calls to a URL."""
        callers = []
        url_lower = url_path.lower()

        for rel_path, blob in self._blobs.items():
            if url_lower not in blob.lower():
                continue

            # Check if this file actually makes HTTP calls (not just defines routes)
            info = self._index.get(rel_path, {})
            # Skip files that define routes for this URL (they're the backend)
            routes = info.get("routes", [])
            is_route_definer = any(
                self._url_matches(r["path"], url_path) for r in routes
            )
            if is_route_definer:
                continue

            # Check for HTTP client call patterns in functions
            for fn in info.get("functions", []):
                calls = fn.get("calls", [])
                qualified = fn.get("qualified_calls", {})
                # Check for fetch, axios, requests, http patterns
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
                # Check qualified calls too (e.g., axios.post, requests.get)
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
        """Find the route handler(s) for a URL path and method."""
        matches = []
        for rel_path, info in self._index.items():
            for route in info.get("routes", []):
                if route["method"].upper() != method:
                    continue
                if not self._url_matches(route["path"], url_path):
                    continue

                # Find the handler function for this route
                handler = route.get("handler")
                if not handler:
                    # Try to find a handler connected via graph
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
        """Check if a route pattern matches a URL path.

        Handles dynamic segments like :id, [id], {id}, and wildcards.
        """
        pattern = pattern.rstrip("/") or "/"
        url = url.rstrip("/") or "/"

        if pattern == url:
            return True

        # Normalize dynamic segments BEFORE escaping so {}, [], : aren't escaped
        import re
        # Replace :param, [param], {param} with a placeholder
        _PLACEHOLDER = "__DYN__"
        normalized = re.sub(r"(:\w+|\[\w+\]|\{\w+\})", _PLACEHOLDER, pattern)
        # Now escape the rest and swap placeholder for regex
        pattern_re = re.escape(normalized).replace(re.escape(_PLACEHOLDER), "[^/]+")
        pattern_re = pattern_re.replace(re.escape("..."), ".*")  # catch-all
        return bool(re.fullmatch(pattern_re, url))

    def co_change_analysis(self, file_path: str, days: int = 90) -> dict:
        """Find files that historically change together with the given file.

        Parses git log to find co-change patterns — when file A changes,
        which other files tend to change in the same commit?

        Args:
            file_path: The file to analyze co-changes for.
            days: How far back to look in git history (default: 90 days).

        Returns:
            dict with co-change partners ranked by frequency.
        """
        import subprocess

        try:
            rel_path = str(Path(file_path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel_path = file_path.replace("\\", "/")

        try:
            # Get commits that touched this file
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

            # For each commit, get the list of changed files
            co_change_counts: dict[str, int] = {}
            total_commits = len(commit_hashes)

            for commit in commit_hashes[:50]:  # Cap at 50 commits
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

            # Rank by frequency and compute percentage
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
        """Return parsed info for a file plus its 1-hop import/importer chain."""
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
            "imported_by": self._find_importers(rel, limit=999),
            "imports_resolved": self._resolve_imports(rel, limit=999),
        }

    def stats(self) -> dict:
        """Summary counts for the current index."""
        result = {
            "files": len(self._index),
            "functions": sum(len(v.get("functions", [])) for v in self._index.values()),
            "classes": sum(len(v.get("classes", [])) for v in self._index.values()),
            "routes": sum(len(v.get("routes", [])) for v in self._index.values()),
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
        return result

    def _parse_status_counts(self) -> dict:
        counts = {"ok": 0, "partial": 0, "unsupported": 0}
        for info in self._index.values():
            status = info.get("parse_status", "ok")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def health(self) -> dict:
        """Coverage/honesty report — what the index actually knows vs. doesn't.

        This is the closed-world boundary verify_claim consults: a REFUTED
        verdict is only trustworthy for files reported here as parsed "ok".
        Partial parses, unsupported languages, and skipped files are the
        blind spots where the honest answer is UNKNOWN, not "no".
        """
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
        }

    def _walk(self):
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in SKIP_DIRS for p in path.parts):
                continue
            # Extension check first: never even open images/binaries/lockfiles/
            # etc. — only files we'd actually parse are worth reading at all.
            ext = path.suffix.lower()
            if ext not in PARSED_EXTS:
                continue
            if path.name in SKIP_FILES:
                continue
            if any(path.name.endswith(s) for s in SKIP_SUFFIXES):
                continue
            yield path


def _is_minified(source: str) -> bool:
    return any(len(line) > MINIFIED_LINE_THRESHOLD for line in source.splitlines())


def _path_multiplier(rel_path: str) -> float:
    """Return < 1.0 for files that live in non-core directories."""
    parts = {p.lower() for p in Path(rel_path).parts[:-1]}
    if parts & DEPRIORITY_DIRS:
        return 0.3
    return 1.0


def _tokenize_symbol(name: str) -> list[str]:
    """Split camelCase/snake_case symbol names into individual searchable tokens."""
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
