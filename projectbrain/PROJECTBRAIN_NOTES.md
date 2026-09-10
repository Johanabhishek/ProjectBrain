# ProjectBrain Notes: Scope, Roadmap, and Product Direction

## One-Line Vision

ProjectBrain should be a lightweight, local codebase intelligence layer that helps AI coding agents and developers quickly answer:

> "Which files matter for this task, why do they matter, and how are they connected?"

It should not try to become a full IDE, a full static analyzer, or a replacement for reading code. Its job is to reduce blind scanning and give agents a good starting map.

## What The Tool Should Be

ProjectBrain should be:

- **Fast**: index a medium repo quickly and answer queries almost instantly.
- **Local-first**: no required LLM, cloud service, database, or external indexing server.
- **Explainable**: every result should include a reason, not just a path.
- **Graph-aware**: it should understand files, symbols, imports, routes, handlers, calls, classes, and relationships.
- **Honest about uncertainty**: dynamic dispatch and name-based matching should be marked as approximate, not presented as fact.
- **Agent-friendly**: output should be easy for Codex, Claude Code, Cursor, Copilot, and other agents to consume.
- **Small by default**: return focused context, not a dump of the whole project.

The ideal feel is: a teammate who already skimmed the repo and says, "Start here. This file owns the route, this service is called next, and this model is probably involved."

## Core Scope

### In Scope

- Index Python, JavaScript, TypeScript, TSX, and JSX files.
- Extract functions, classes, methods, imports, exports, routes, docstrings, calls, inheritance, and line ranges.
- Build a typed graph of code relationships.
- Query with natural language task descriptions.
- Return relevant files, reasons, connections, and graph context.
- Expose the functionality through:
  - Python API
  - CLI
  - MCP server for coding agents
- Cache index results for fast repeated use.

### Out Of Scope For Now

- Perfect compiler-level type inference.
- Full dynamic runtime behavior.
- A hosted web service.
- Heavy databases.
- Required LLM-based ranking.
- Auto-editing code.
- Replacing IDE language servers.

ProjectBrain can use language-server-like ideas, but it should stay much smaller and easier to install.

## Current Strong Points

- The project already has a useful package shape.
- Tree-sitter parsing is a good foundation.
- The graph layer is the right direction.
- MCP tools make it agent-friendly.
- CLI and MCP query paths are now closer together.
- The tool already indexes itself and produces a symbol graph.

## Current Weak Points To Improve

- There is no real test suite yet.
- Build artifacts can get stale if not regenerated.
- Query ranking is still basic keyword matching.
- Dynamic dispatch is mostly name-based.
- Import resolution is shallow.
- Output can become verbose for humans.
- There is no evaluation harness to prove token/time savings.

## Main Improvement Aspects

### 1. Staleness And Cache Reliability

This is the foundation. If the cache lies, the whole tool becomes hard to trust.

Useful features:

- Cache schema version.
- Parser version.
- Graph version.
- Per-file `mtime`.
- Per-file size.
- Optional content hash for stricter mode.
- Root path stored in cache metadata.
- Incremental reparse of changed files.
- Full graph rebuild from parsed index when graph logic changes.

Recommended cache metadata shape:

```json
{
  "schema_version": 2,
  "parser_version": "2026-07-06.1",
  "graph_version": "2026-07-06.1",
  "root": "D:/projects/Brain/projectbrain",
  "files": {
    "src/projectbrain/brain.py": {
      "mtime": 1781624399.88,
      "size": 12345,
      "hash": null,
      "info": {}
    }
  }
}
```

Use cases unlocked:

- Agents can trust cached context.
- Large repos do not need full reindexing every time.
- Parser upgrades do not silently reuse old incorrect data.
- CI or preflight tools can detect stale code maps.

Priority: **high**.

### 2. Better Parsing

The parser should keep improving coverage without becoming a massive compiler project.

Useful features:

- Direct class method detection.
- Nested function detection without misclassifying methods.
- Better import forms:
  - `import x`
  - `import x as y`
  - `from x import y`
  - relative imports
- Better JS/TS imports:
  - default imports
  - named imports
  - namespace imports
  - re-exports
- Framework-specific route extraction:
  - FastAPI
  - Flask
  - Django urls
  - Express
  - Next.js route handlers
  - React Router
- Database model extraction:
  - SQLAlchemy
  - Django models
  - Prisma schema
  - Mongoose
  - TypeORM

Use cases unlocked:

- "Why is `/users` returning 404?"
- "Where is checkout total calculated?"
- "Add a field to user profile."
- "Find the model behind this API response."
- "What service owns this route?"

Priority: **high for imports/routes**, **medium for database models**.

### 3. Graph Quality

The graph is the product's real moat. File search is easy; relationship search is the useful part.

Current graph ideas:

- file nodes
- function nodes
- class nodes
- route nodes
- module nodes
- `defines` edges
- `imports` edges
- `calls` edges
- `inherits` edges
- `handles` edges
- `has_method` edges
- `exports` edges

Recommended next graph improvements:

- Add edge confidence:
  - `exact`
  - `qualified`
  - `import_resolved`
  - `candidate`
  - `unknown`
- Add edge reason:
  - "same-file function call"
  - "import alias resolved"
  - "method name matched across class"
  - "route decorator handler"
- Add source line for edges where possible.
- Add read-order suggestions.
- Add entrypoint detection by query type.

Example edge:

```json
{
  "src": "fn:src/routes/users.py:get_user",
  "dst": "fn:src/services/users.py:find_user",
  "type": "calls",
  "confidence": "import_resolved",
  "reason": "find_user imported from src.services.users",
  "line": 42
}
```

Use cases unlocked:

- Agents can decide which connections are worth reading.
- Relationship search becomes more trustworthy.
- Dynamic dispatch uncertainty is visible.
- Query output can explain itself better.

Priority: **high**.

### 4. Dynamic Dispatch Handling

ProjectBrain should not pretend to fully solve dynamic dispatch. It should resolve what it can and expose uncertainty.

Suggested confidence model:

| Confidence | Meaning | Example |
| --- | --- | --- |
| `exact` | Same-file direct function/class/method resolved | `helper()` defined in same file |
| `qualified` | Receiver or class name gives strong signal | `UserService.find()` |
| `import_resolved` | Imported symbol points to a known file | `from users import find_user` |
| `candidate` | Name match but multiple possible targets | `handle()` exists in several files |
| `unknown` | Runtime/dynamic target cannot be known | `getattr(obj, name)()` |

The output should say:

> "This call probably connects to these candidates."

Not:

> "This call definitely connects here."

Use cases unlocked:

- Agents avoid false certainty.
- Graph traversal stays useful in dynamic languages.
- The tool can support Python and JS without becoming too heavy.

Priority: **high**.

### 5. Query Engine

The query engine should start simple, then become layered.

Recommended ranking layers:

1. Exact path and filename matches.
2. Route/path matches.
3. Symbol name matches.
4. Import/export matches.
5. Docstring/comment matches.
6. Tokenized camelCase/snake_case matches.
7. Graph expansion from high-confidence hits.
8. Optional recency or centrality boost.
9. Optional local embedding/LLM reranker later.

Important: v0 should stay self-contained and deterministic.

Good query output should include:

- file path
- line range
- reason
- relevant symbols
- connections
- graph entrypoints
- confidence
- suggested read order

Use cases unlocked:

- Bug investigation.
- Feature implementation.
- Onboarding to unfamiliar code.
- PR review prep.
- Agent context planning.

Priority: **high**.

### 6. Output Design

Agents do not need pretty output. Humans do.

Support both:

- JSON output for tools/agents.
- Summary output for terminal users.

Recommended CLI modes:

```text
projectbrain query "add discount code to checkout"
projectbrain query "add discount code to checkout" --json
projectbrain query "add discount code to checkout" --summary
projectbrain query "add discount code to checkout" --graph
projectbrain context src/routes/users.py --json
projectbrain nodes --type route
projectbrain relationship CheckoutService Coupon
```

Human summary should look like:

```text
Top files:
1. src/routes/checkout.ts
   Reason: defines POST /checkout
   Connections: imports CheckoutService, used by app.ts

2. src/services/checkout.ts
   Reason: defines applyDiscount and calculateTotal
   Connections: imports CouponModel
```

Use cases unlocked:

- Developers can use it directly.
- Agents can consume structured JSON.
- Debugging the tool itself becomes easier.

Priority: **medium-high**.

### 7. MCP Integration

MCP should be one of ProjectBrain's strongest surfaces.

Useful MCP tools:

- `projectbrain_query`
- `projectbrain_get_context`
- `projectbrain_get_neighbors`
- `projectbrain_find_relationship`
- `projectbrain_list_nodes`
- `projectbrain_stats`
- `projectbrain_health`
- `projectbrain_reindex`

The MCP tool descriptions matter a lot. Agents decide when to call tools based on those descriptions.

Good MCP guidance:

- Use ProjectBrain when the target file is not obvious.
- Skip ProjectBrain for tiny obvious edits.
- Query first, then read only the top suggested files.
- Use neighbors when a known node needs deeper context.
- Use relationship search when comparing two known concepts.

Use cases unlocked:

- Codex/Claude/Cursor can avoid blind file scanning.
- The tool becomes a context router for agents.
- Agents can make better first reads.

Priority: **high**.

### 8. Evaluation And Proof

This project needs proof, not just features.

Create a benchmark folder with sample projects and fixed tasks.

Example tasks:

- "Why is `/users` returning 404?"
- "Add discount code to checkout."
- "Profile avatar upload fails."
- "Cart total does not update after removing item."
- "Find where JWT auth is checked."

Measure:

- Expected relevant files.
- Files returned by ProjectBrain.
- Precision.
- Recall.
- Query time.
- Index time.
- Files an agent would otherwise scan.
- Estimated token savings.

Simple evaluation result:

```text
Task: add discount code to checkout
Expected files: 4
Returned files: 5
Correct returned: 4
Precision: 80%
Recall: 100%
Query time: 14ms
Estimated avoided reads: 18 files
```

Use cases unlocked:

- You can prove the tool works.
- You can compare versions.
- You can avoid regressions.
- You can show a convincing demo.

Priority: **very high**.

## Feature-To-Usecase Matrix

| Feature | Useful For | Main User |
| --- | --- | --- |
| Route extraction | API bugs, 404s, endpoint changes | Agent + developer |
| Import graph | Finding dependency chains | Agent |
| Function/class extraction | Locating business logic | Agent + developer |
| Call graph | Understanding flow | Agent |
| Confidence labels | Avoiding false certainty | Agent |
| Cache staleness metadata | Trusting repeated queries | Agent + developer |
| Incremental indexing | Large repos | Developer |
| CLI summary mode | Human inspection | Developer |
| JSON output | Tool automation | Agent |
| MCP server | Agent integration | Agent |
| Relationship search | "How does A connect to B?" | Agent + developer |
| Git history layer | Finding why code changed | Developer + reviewer |
| Test/eval harness | Proving value | You / maintainer |

## Roadmap

### Phase 1: Make The Current Tool Trustworthy

Goal: the existing feature set should be reliable.

Build:

- Parser tests.
- Graph tests.
- Query smoke tests.
- Cache schema metadata.
- Build artifact cleanup.
- CLI `--json` and human summary modes.
- `projectbrain stats` command.

Do not build yet:

- Web UI.
- Heavy semantic search.
- File watcher.

Success criteria:

- A new user can install, index, query, and understand output.
- Tests catch parser regressions.
- Cache invalidation is predictable.

### Phase 2: Make It Better Than Grep

Goal: prove the graph gives value plain search cannot.

Build:

- Import alias resolution.
- Better route extraction.
- Better JS/TS export resolution.
- Edge confidence and reasons.
- Relationship search improvements.
- Suggested read order.
- Evaluation harness with sample repos/tasks.

Success criteria:

- For realistic tasks, ProjectBrain returns the right 3-7 files.
- Output explains why each file matters.
- Agents can use the result directly.

### Phase 3: Make It Agent-Native

Goal: ProjectBrain becomes a standard first tool call for unclear coding tasks.

Build:

- Strong MCP tool descriptions.
- `projectbrain_health`.
- `projectbrain_reindex`.
- Query result size controls.
- Context budget controls.
- Agent-specific output profiles.

Example profiles:

```text
--profile codex
--profile claude
--profile human
```

Success criteria:

- Agents call it only when useful.
- Agents read fewer irrelevant files.
- Tool output fits within context budgets.

### Phase 4: Add Historical Intelligence

Goal: add "why did this exist/change?" on top of "where is this?"

Build:

- Optional git blame.
- Recent commits touching returned files.
- Files often changed together.
- PR/change impact hints.

Use cases:

- Debug regressions.
- Review PR risk.
- Understand ownership.
- Find likely related files that static imports miss.

Priority: **later**. This is powerful, but it should not distract from routing-to-the-right-file first.

### Phase 5: Optional Advanced Intelligence

Only build these after the core is clearly useful.

Possibilities:

- Local embeddings.
- LLM reranking.
- Watch mode.
- IDE extension.
- Web visualization.
- Language-server integration.

These are useful, but they are not the heart of the product.

## What To Avoid

- Do not turn ProjectBrain into a huge static analyzer.
- Do not require a database too early.
- Do not require an LLM for core functionality.
- Do not return too many files.
- Do not hide uncertainty.
- Do not prioritize UI before the engine is proven.
- Do not chase every framework at once.

## Good Default Behavior

When queried, ProjectBrain should:

1. Ensure the cache is fresh enough.
2. Extract keywords and path-like tokens from the prompt.
3. Match routes, files, symbols, imports, exports, and docs.
4. Expand through the graph from strong matches.
5. Rank results by relevance and confidence.
6. Return a small result set.
7. Explain every result.
8. Include connections and suggested next reads.

Example response shape:

```json
{
  "results": [
    {
      "file_path": "src/routes/checkout.ts",
      "rel_path": "src/routes/checkout.ts",
      "score": 8.5,
      "confidence": "high",
      "reason": "defines POST /checkout and imports CheckoutService",
      "line_ranges": [[12, 58]],
      "symbols": ["POST /checkout", "checkoutHandler"],
      "connections": [
        {
          "rel_path": "src/services/checkout.ts",
          "relation": "imports",
          "confidence": "import_resolved"
        }
      ]
    }
  ],
  "entrypoints": ["file:src/routes/checkout.ts"],
  "suggested_read_order": [
    "src/routes/checkout.ts",
    "src/services/checkout.ts",
    "src/models/coupon.ts"
  ]
}
```

## Best Near-Term Tasks

If choosing what to build next, do this order:

1. Add parser and graph tests.
2. Add cache metadata with schema/parser/graph versions.
3. Add human-friendly CLI summary mode.
4. Add edge confidence and reason fields.
5. Improve import alias resolution.
6. Add evaluation fixtures and token-savings metrics.
7. Improve route extraction by framework.

This order gives the project stability, usefulness, and proof.

## Final Product Shape

ProjectBrain should become a small, reliable "repo map" package:

- Installable with `pip`.
- Runnable from CLI.
- Importable from Python.
- Exposed through MCP.
- Fast enough to use before every unclear coding task.
- Accurate enough to reduce blind reads.
- Honest enough to show uncertainty.
- Simple enough that an intern can maintain and demo it.

The strongest version of this tool is not flashy. It is boring in the best way: install it, index a repo, ask a task question, get the right starting files with reasons.

