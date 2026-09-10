# ProjectBrain

A lightweight, local codebase-intelligence layer for AI coding agents and developers. ProjectBrain indexes a repository into a typed **symbol graph** (files, functions, classes, routes, modules, and the edges between them), then answers natural-language queries with a small, ranked, explained set of relevant files — instead of an agent blindly grepping and reading dozens of files to find the same thing.

It is local-first: no database, no required LLM, no external service. Indexing writes two JSON files next to your project (`.projectbrain.json`, `.projectbrain_graph.json`) and everything else runs from those.

## Why a graph, not just search

A keyword/filename search (`grep`, ripgrep, an agent's own file search) can answer "which files mention X?". It cannot answer questions that require **assembling a relationship across files**:

- "How does `POST /checkout` reach the `Coupon` model?" — a route → handler → service → model chain, where the intermediate service file never mentions "Coupon" in its text.
- "What does `apply_discount` call two hops out?" — a transitive call that never appears in the querying function's own source.

ProjectBrain's `find_relationship` and `get_neighbors` tools answer exactly these questions by traversing the graph. The `eval/relationship_tasks.json` harness (see [Evaluation harness](#evaluation-harness)) verifies this concretely: for each such task, a live grep baseline over the same fixture is run and asserted to **miss** the connection that the graph finds.

Everything else — keyword-ranked file search, `context` lookups — is useful, but is closer to "a smarter grep": treat it as a convenience layer, not the reason the tool is unique.

## Install

```bash
cd projectbrain
pip install -e .          # runtime only
pip install -e ".[dev]"   # + pytest, for running the test suite
```

Requires Python ≥ 3.9. Dependencies: `mcp`, `tree-sitter`, and the Python/JavaScript/TypeScript tree-sitter grammars.

## Quick start (CLI)

```bash
projectbrain --path . index                       # build/refresh the index + graph
projectbrain --path . query "add discount code to checkout"
projectbrain --path . query "add discount code to checkout" --summary
projectbrain --path . context src/routes/checkout.py --summary
```

## Quick start (Python API)

```python
from projectbrain import ProjectBrain

brain = ProjectBrain("./my-project")
brain.index()                                   # builds and caches the graph

results = brain.query("why is /users returning a 404")
# -> [{file_path, rel_path, score, reason, functions, classes, routes,
#      imports, exports, connections}, ...]

ctx = brain.get_context_for_file("src/routes/users.py")
# -> parsed info for the file + its 1-hop import/importer chain

graph_result = brain.query_with_graph("add discount code to checkout")
# -> {"results": [...], "subgraph": {"nodes": [...], "edges": [...]}, "entrypoints": [...]}
```

`index()` and `load()` are the only ways the in-memory index gets populated; every other method calls `_ensure_fresh()` internally, which loads the cache if present and reindexes automatically if it's missing or stale (see [Cache & staleness](#cache--staleness)) — you don't normally need to call `index()` yourself after the first time.

## CLI reference

```
projectbrain --path <dir> index

projectbrain --path <dir> query "<prompt>" [--top-n N] [--json | --summary]

projectbrain --path <dir> context <file> [--json | --summary]
```

- `--path` (global, default `.`) — project root to index/query.
- `index` — builds the index and graph, writes the cache files, prints the resulting `stats()` as JSON.
- `query` — natural-language task description in, ranked file list + subgraph out. `--top-n` caps result count (default 10). `--json` (default) prints the raw structure; `--summary` prints a human-readable "Top files / Reason / Connections" listing.
- `context <file>` — one file's parsed info (functions/classes/routes/imports/exports) plus its resolved 1-hop import chain in both directions. `--json`/`--summary` as above.

## Python API reference

All methods are on `projectbrain.brain.ProjectBrain`:

| Method | Purpose |
| --- | --- |
| `index()` | Full parse of every matching file under root; writes cache + graph to disk; returns `stats()`. |
| `load()` | Load a previously written cache from disk. Returns `False` (never raises) if missing, corrupt, or version-mismatched. |
| `is_stale()` | Cheap `stat()`-only check for whether any indexed file's mtime has changed. |
| `query(prompt, top_n=10)` | Keyword-ranked file search. Returns a list of result dicts with `reason` and `connections`. |
| `query_with_graph(prompt, top_n=5)` | Same as `query()`, plus a typed subgraph spanning the returned files and an `entrypoints` list. |
| `get_context_for_file(file_path)` | Parsed info for one file + its resolved importers/imports. |
| `get_neighbors(node_id, edge_types=None, depth=1)` | BFS traversal outward from a known graph node (depth 1–3, bidirectional). |
| `find_relationship(name_a, name_b)` | Shortest typed path between two named symbols/files/routes. |
| `list_nodes(node_type=None, query=None)` | Enumerate/filter graph nodes (capped at 100). |
| `stats()` | File/function/class/route counts + graph size + cache version metadata. |

## MCP server

```bash
projectbrain-mcp /path/to/project     # or set PROJECTBRAIN_ROOT env var
```

Registers five tools over stdio (via `mcp`'s `FastMCP`), each with a docstring written to help an agent decide when to call it:

| Tool | Use it for |
| --- | --- |
| `projectbrain_query` | "Which files matter for this task?" — the default first call for an unclear coding task. |
| `projectbrain_get_context` | "What does this specific file connect to?" once you already know the file. |
| `projectbrain_get_neighbors` | Traverse outward from a known node ID (deeper than `get_context`'s 1-hop). |
| `projectbrain_find_relationship` | "How does A reach B?" — the graph-relationship question grep can't answer. |
| `projectbrain_list_nodes` | Browse/enumerate the graph without a specific task in mind. |

## Architecture

### Indexing pipeline

1. `_walk()` in `brain.py` enumerates files under root, skipping build/dependency directories (`.git`, `node_modules`, `dist`, etc.), known bundle/minified filenames, and any file with a line over 500 chars (heuristic minified-file skip).
2. Each remaining `.py`/`.js`/`.jsx`/`.ts`/`.tsx`/`.mjs`/`.cjs` file is parsed by `parsers.py` (tree-sitter) into functions, classes, imports, exports, and routes.
3. `frameworks.py` adds file-path-convention routes (Next.js) on top of the parser's syntax-based routes.
4. `graph.py` builds a typed symbol graph from the combined per-file index.
5. Both the flat index and the graph are cached to disk as JSON.

### Graph model

**Node types:** `file`, `fn`, `cls`, `route`, `mod` — encoded by ID prefix, e.g. `fn:src/routes/users.py:get_user`, `cls:src/models.py:User`, `route:GET:/users`, `mod:fastapi`.

**Edge types:** `defines`, `imports`, `calls`, `inherits`, `handles`, `has_method`, `exports`.

**Edge confidence** (every edge carries `confidence` + a human-readable `reason`):

| Confidence | Meaning |
| --- | --- |
| `exact` | Structural fact, or same-file resolution (e.g. a function calling another function defined in the same file). |
| `import_resolved` | Cross-file match confirmed by resolving the caller's actual import statement to the candidate's file. |
| `candidate` | Name-only match across files with no confirming import — ambiguous, treat as a hint. |
| `qualified` *(defined, unused)* | Would require receiver-aware call tracking (e.g. `UserService.find()`); the parsers only capture the bare callee name today, so this tier is never assigned rather than guessed. |
| `unknown` *(defined, unused)* | A call/base-class/handler with zero name matches gets no edge at all — never a fabricated "unknown" edge. |

### Import resolution (`resolve.py`)

Replaces naive substring matching with real resolution:

- **Python**: relative imports (`.`, `..`, `.foo`) are resolved via directory arithmetic respecting `__init__.py` package semantics; absolute imports (`services.users`) are resolved via a dotted-module index built from every file's module path — but only when the target is **unambiguous** (0 or 2+ candidates → unresolved, not guessed).
- **JS/TS**: only relative imports (`./`, `../`) are resolved — bare specifiers are npm packages and correctly stay external/unresolved. Tries known extensions (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`) then `index.*` directory-fallback.

This feeds both the `import_resolved` edge-confidence tier and `get_context_for_file`'s `imported_by`/`imports_resolved` lists.

### Route extraction by framework

| Framework | Pattern | Notes |
| --- | --- | --- |
| FastAPI (and generic) | `@app.get/post/put/delete/patch/all(...)` | Method name taken directly from the decorator. |
| Flask | `@app.route(path, methods=[...])` | Defaults to `["GET"]` if `methods` is omitted; emits one route entry per method. |
| Django | `path(...)` / `re_path(...)` in `urls.py` | Not decorator-based — matched as bare calls. `method` is the sentinel `"ANY"` since Django doesn't declare a per-route verb. |
| Express | `app.get/post/put/delete/patch/all(path, ...)` | |
| Next.js App Router | `app/**/route.ts` with verb-named exports (`GET`, `POST`, ...) | Inferred from file path + exports, not decorators — lives in `frameworks.py`, not `parsers.py`, since it needs the file path. |
| Next.js Pages Router | `pages/api/**` | Single `method: "ANY"` entry per file (verb is decided at runtime via `req.method`). |
| React Router | — | Explicitly out of scope (component/JSX-based, not decorator/call/convention-based). |

Dynamic path segments are converted for both Next.js styles: `[id]` → `:id`, `[...slug]` → `*`.

### Cache & staleness

On-disk cache envelope (`.projectbrain.json`):

```json
{
  "schema_version": 2,
  "parser_version": "2026-07-09.2",
  "graph_version": "2026-07-09.3",
  "root": "/abs/path/to/project",
  "files": {
    "src/app.py": { "mtime": 1781624399.88, "size": 1234, "hash": null, "info": { "functions": [...], ... } }
  }
}
```

`SCHEMA_VERSION` / `PARSER_VERSION` / `GRAPH_VERSION` live in `versions.py`. Any mismatch — or a corrupt/pre-versioning cache file — makes `load()` return `False`, which the caller treats identically to "no cache present" and triggers a full reindex. There is no migration path and no crash path: an incompatible cache is simply invisible.

`is_stale()` does a cheap `stat()`-only pass over every indexed file's mtime (no re-reading or re-parsing) to decide whether `query`/`get_context_for_file`/etc. should silently reindex before answering.

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

88 tests across `tests/test_parsers.py`, `test_graph.py`, `test_brain.py`, `test_cli.py`, `test_resolve.py`, `test_frameworks.py`, and `test_eval_thresholds.py`. Several tests are deliberately **known-gap regression locks** — they assert the *current* (documented) limitation rather than desired behavior, so a future fix is a visible, intentional test flip rather than a silent behavior change. See [Known limitations](#known-limitations).

## Evaluation harness

```bash
python eval/run_eval.py                    # both fixture projects
python eval/run_eval.py --project sample_flask_app
python eval/run_eval.py --json             # machine-readable output
```

Two fixture projects (`eval/fixtures/sample_flask_app`, `sample_express_app`) and two task sets:

- **`eval/tasks.json`** — file-retrieval tasks (precision/recall/query-time/estimated-tokens-saved). Reported as a "keyword-ranking baseline" — several of these are grep-answerable, so this section guards against ranking regressions rather than proving graph value.
- **`eval/relationship_tasks.json`** — 8 graph-relationship tasks (4 per fixture: a two-hop `neighbors` case, a multi-hop `find_relationship` path, and two negative controls verifying the graph doesn't fabricate connections). Each positive case is paired with a live grep baseline over the same fixture, asserted to **miss** the connection the graph finds — this is the section that substantiates the "graph > grep" claim with a number instead of an assertion.

`tests/test_eval_thresholds.py` wires both scores into pytest as loose regression floors (recall ≥ 70%, relationship pass rate ≥ 80%, and grep must keep missing every relationship task it's paired against).

## Known limitations

Documented gaps, intentionally left unfixed (locked in by "known gap" tests so they can't regress silently) because they were outside the scope of the current roadmap pass:

- **JS/TS `class Foo extends Base`** — base class is never extracted. This tree-sitter-javascript grammar version puts the base identifier as a direct `class_heritage` child rather than inside an `extends_clause` wrapper, which `_js_bases()` doesn't handle.
- **`query_with_graph`'s `entrypoints`** — currently always includes every file in the result set. It checks for incoming `"imports"` edges targeting a `file:` node, but `"imports"` edges always go `file: -> mod:` (a module name), never `file: -> file:`, so the incoming-edge check never actually filters anything.
- **`from . import x`** — bare relative imports with no dotted module name only capture the `.` itself; the parser never captures the imported symbol name `x` for this specific form, so it resolves to the package's own `__init__.py` rather than the specific submodule. `from .module import x` (a named dotted module) resolves correctly.
- **React Router** — not supported (component/JSX-based route definitions, not decorator/call/file-convention based).
- **Receiver-aware call resolution** (`qualified` confidence tier) — not implemented; the parsers only capture the bare callee name (`find()`), not the receiver (`UserService.find()`), so this tier is defined in the confidence vocabulary but never assigned.

## Project layout

```
projectbrain/
  src/projectbrain/
    brain.py        # ProjectBrain: index/load/query/graph-query/neighbors/relationship/stats
    graph.py         # CodeGraph: typed nodes/edges, BFS traversal, path-finding, serialization
    parsers.py       # tree-sitter parsing for Python + JS/TS/TSX/JSX
    resolve.py       # Python module-index + JS relative-path import resolution
    frameworks.py    # Next.js file-based route inference
    versions.py      # SCHEMA_VERSION / PARSER_VERSION / GRAPH_VERSION
    cli.py           # projectbrain CLI entry point
    server.py        # projectbrain-mcp FastMCP server entry point
  tests/             # pytest suite (88 tests)
  eval/              # evaluation harness: fixtures, task definitions, run_eval.py
  PROJECTBRAIN_NOTES.md  # original roadmap/scope notes this implementation follows
```
