# ProjectBrain

**A local codebase map for people and AI coding agents.**

ProjectBrain indexes a repository into an explainable symbol graph, then answers questions such as:

> Which files matter for this task, why do they matter, and how are they connected?

It is designed to replace broad, repetitive scanning with a small, grounded starting set: routes, functions, classes, imports, exports, calls, and their relationships. Everything runs locally—there is no database, cloud service, or required LLM.

## What it does

- Maps Python, JavaScript, TypeScript, JSX, and TSX source files.
- Extracts functions, classes, methods, imports, exports, call sites, inheritance, and common web routes.
- Builds a typed graph for traversing dependencies and finding paths between symbols.
- Ranks files for natural-language tasks and explains each result.
- Gives agents tools for claim verification, planning, impact analysis, request-flow tracing, and incremental codebase exploration.
- Stores a versioned local cache and refreshes it when indexed files change.

ProjectBrain deliberately reports uncertainty. Cross-file name matches are marked as candidates unless import resolution confirms them; unsupported or partially parsed files never turn absence into a false claim.

## Install

ProjectBrain requires Python 3.9 or later.

From the project directory:

```bash
pip install -e .
```

For tests and development tools:

```bash
pip install -e ".[dev]"
```

The package installs two commands:

- `projectbrain` — command-line interface
- `projectbrain-mcp` — MCP server for coding agents

## Quick start

Index any project, then ask a task-oriented question:

```bash
projectbrain --path /path/to/your/project index
projectbrain --path /path/to/your/project query "add discount codes to checkout" --summary
```

Explore a known file or verify a statement before relying on it:

```bash
projectbrain --path /path/to/your/project context src/routes/checkout.py --summary
projectbrain --path /path/to/your/project verify checkout reaches Coupon
projectbrain --path /path/to/your/project health
```

`query` prints JSON by default, making it easy to consume from scripts and agents. Add `--summary` for a compact human-readable result.

## Why a graph?

Text search answers “which files mention this word?” ProjectBrain also follows the code paths that connect files which do not share that wording.

For example, a request such as “How does `POST /checkout` reach the coupon model?” can travel through a route, handler, service, and model. A graph can retain that route → handler → call chain, while a keyword search may never surface the service file at all.

The graph uses these node and edge types:

| Nodes | Edges |
| --- | --- |
| files, functions, classes, routes, modules | defines, imports, calls, inherits, handles, has_method, exports |

Edges include a confidence level. `exact` represents a structural fact; `import_resolved` represents a cross-file link corroborated by an import; `candidate` is a useful but ambiguous name match.

## Interfaces

### CLI

```text
projectbrain --path <project> index
projectbrain --path <project> query "<task>" [--top-n N] [--json | --summary]
projectbrain --path <project> context <file> [--json | --summary]
projectbrain --path <project> verify <subject> <relation> <object>
projectbrain --path <project> health
```

`verify` supports `calls`, `reaches`, `inherits`, `has_method`, `defines`, `imports`, and `handles`. Its output is intentionally three-valued:

- `SUPPORTED` — backed by strong graph evidence.
- `REFUTED` — disproved within a complete, cleanly parsed scope.
- `UNKNOWN` — insufficient, ambiguous, unsupported, or partial evidence; do not treat this as false.

### Python API

```python
from projectbrain import ProjectBrain

brain = ProjectBrain("./my-project")
brain.index()

results = brain.query("why is /users returning a 404?")
context = brain.get_context_for_file("src/routes/users.py")
graph_result = brain.query_with_graph("add discount code to checkout")

# Trace a relationship or validate a claim before presenting it as fact.
path = brain.find_relationship("POST /checkout", "Coupon")
claim = brain.verify_claim("checkout", "reaches", "Coupon")
```

Useful methods include `get_neighbors`, `list_nodes`, `trace_flow`, `verify_plan`, `impact`, `compress_context`, and `co_change_analysis`. Every lookup automatically loads a compatible cache and refreshes it when the source has changed.

### MCP server

Start the server with the repository to map:

```bash
projectbrain-mcp /path/to/your/project
```

Or set `PROJECTBRAIN_ROOT` and run `projectbrain-mcp` with no argument. The server communicates over stdio, so configure that command in your MCP-capable coding client.

The tools are organized around a practical agent workflow:

| Need | MCP tools |
| --- | --- |
| Discover relevant code | `projectbrain_query`, `projectbrain_get_context`, `projectbrain_list_nodes` |
| Navigate relationships | `projectbrain_get_neighbors`, `projectbrain_find_relationship`, `brain_trace_flow` |
| Ground assertions | `projectbrain_verify_claim`, `brain_verify_batch`, `projectbrain_health` |
| Plan and assess change | `brain_verify_plan`, `brain_impact`, `brain_co_changes` |
| Work progressively | `brain_start_session`, `brain_next`, `brain_mark_read`, `brain_mark_modified`, `brain_status`, `brain_compress` |

## How indexing works

1. ProjectBrain walks source files while skipping dependency, build, cache, and minified artifacts.
2. Tree-sitter parsers extract code structure and common framework routes.
3. Import resolution links Python modules and relative JavaScript/TypeScript imports where the target is unambiguous.
4. A graph is built from the parsed symbols and relationships.
5. The flat index and graph are written beside the indexed project as `.projectbrain.json` and `.projectbrain_graph.json`.

The cache is versioned and checks file metadata for staleness. An incompatible, missing, corrupt, or stale cache is safely rebuilt rather than trusted.

## Framework awareness

Route extraction supports FastAPI-style decorators, Flask routes, Django `urls.py`, Express route calls, and Next.js App and Pages router conventions. Dynamic Next.js segments are normalized (`[id]` → `:id`, `[...slug]` → `*`).

ProjectBrain does not aim to be a compiler or runtime tracer. Dynamic dispatch, receiver-aware calls, and unsupported languages can limit its certainty. Use `health` to see exactly what was indexed, skipped, partially parsed, or unsupported before treating a negative result as conclusive.

## Development

```bash
pytest -q
python eval/run_eval.py
```

The test suite covers parsing, graph construction, import resolution, CLI behavior, sessions, claim verification, flows, classification, incremental indexing, and evaluation thresholds. The evaluation fixtures exercise both file retrieval and graph relationships, including cases where a graph traversal finds a connection that text grep misses.

## Project layout

```text
projectbrain/
├── src/projectbrain/
│   ├── brain.py        # primary API, cache, queries, verification, analysis
│   ├── graph.py        # typed graph and traversal
│   ├── parsers.py      # Tree-sitter extraction
│   ├── resolve.py      # import resolution
│   ├── session.py      # guided exploration sessions
│   ├── cli.py          # command-line interface
│   └── server.py       # MCP server
├── tests/              # pytest suite
└── eval/               # evaluation harness and fixture repositories
```

## Scope

ProjectBrain is a lightweight intelligence layer, not a replacement for reading code, an IDE language server, or a full static analyzer. It is most useful for quickly locating the right area of an unfamiliar codebase, understanding the relationships around it, and communicating what is known with appropriate confidence.
