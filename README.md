# ProjectBrain (`projectbrain-mcp` v0.8.6)

[![PyPI](https://img.shields.io/pypi/v/projectbrain-mcp.svg)](https://pypi.org/project/projectbrain-mcp/)
[![License: MIT](https://img.shields.io/badge/License: MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Local SQLite Code Graph, Program Slicer, and Syntax-Verified Symbol Patcher for AI Coding Agents.**

ProjectBrain indexes a repository locally into `.projectbrain.db` (SQLite WAL + FTS5) using `tree-sitter` and language AST parsers. It gives CLI and MCP coding agents four concrete primitives in a single dependency-light package—with no external database server, no Docker, no cloud embeddings, and no API keys:

1. **Program & Symbol Slicing (`projectbrain slice` / `brain_slice`):**
   - Slices exact functions, classes, methods, or line ranges (`file.py:ClassName.method` or `file.py:120-160`) directly from SQLite in `< 20ms` without loading full files into the context window.
   - For large functions sliced around a target variable or call, computes an AST/Program Dependence Graph (PDG) slice across three orthogonal dimensions: **backward data dependence (`def -> use`)**, **forward data dependence (`def -> use`)**, and **enclosing control-flow guards (`if`/`try`/`for`/`with`)**, collapsing unrelated lines while keeping governing logic intact.
2. **One-Turn Code Dossiers (`projectbrain dossier` / `brain_dossier`):**
   - Given a bug report, stack trace, or architectural question, ranks relevant symbols using sub-identifier BM25 + call/import/state-flow graph traversal and returns target code slices plus 1-hop caller/callee signatures in a single read.
3. **Syntax-Verified Symbol Patching (`projectbrain patch` / `brain_patch`):**
   - Applies symbol-anchored edits (`@replace_body <Symbol>`, `@add_method <ClassName>`, `@replace <Symbol>`, `@sub <Symbol>`) validated against Tree-sitter + language syntax compilers and caller arity contracts before touching disk, with automatic rollback on syntax errors and zero-change dry runs.
4. **Closed-World Structural Verification (`projectbrain verify` / `projectbrain_verify_claim`):**
   - Verifies structural claims (`calls`, `reaches`, `imports`, `handles`) with explicit evidence provenance (`exact`, `import_resolved`, `candidate`) and refuses to guess (`UNKNOWN`) when any traversed file has a syntax error.

---

## Language-Support Tiers

ProjectBrain indexes 29 file extensions across three explicit capability tiers:

| Tier | Languages / Formats | Capabilities |
| :--- | :--- | :--- |
| **Tier 1: Deep AST + PDG + Contracts** | **Python** (`.py`), **TypeScript / JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`) | Full Tree-sitter + Python `ast` parsing; intra-function PDG data/control-dependence slicing (`def -> use` + enclosing guards); cross-file relative import & `tsconfig` path-alias resolution; class/module config & descriptor tracking (`ConfigAttribute`, `Field`, `Column`, constants); route extraction (Flask, FastAPI, Django, Next.js, Express, Hono); caller arity & SQL schema contract checks. |
| **Tier 2: Tree-sitter Structural + Call Graph** | **Go** (`.go`), **Rust** (`.rs`), **Java** (`.java`), **Kotlin** (`.kt`), **C / C++** (`.c`, `.h`, `.cpp`, `.hpp`), **C#** (`.cs`), **Ruby** (`.rb`), **PHP** (`.php`) | Structural symbol extraction (functions, methods, structs, classes, interfaces, traits); intra-file and cross-file call/import graph edges; signature skeletons; symbol & line-range slicing; syntax-verified symbol patching. |
| **Tier 3: Schema, Query & Component Indexing** | **SQL** (`.sql`), **GraphQL** (`.graphql`, `.gql`), **Prisma** (`.prisma`), **Protobuf** (`.proto`), **Vue / Svelte** (`.vue`, `.svelte`), **Scala** (`.scala`), **Swift** (`.swift`) | DDL table/column/constraint extraction (`CREATE TABLE`, `NOT NULL`), schema-to-code linking, component script extraction, and symbol/line slicing. |

---

## When to Use ProjectBrain vs. Plain `grep`

- **When plain `rg` / `grep` is faster:** Looking up a single literal string or constant when you already know the exact identifier name and just want matching line numbers.
- **When ProjectBrain wins:**
  - **Multi-hop call and dispatch questions** (e.g., *"how are exceptions routed to error handlers"* or *"trace POST /checkout to database validation"*), where following imports, base classes, and helper methods across files takes 10–30 turns of manual search/read loops.
  - **Reading specific methods inside large files** (`projectbrain slice django/db/models/sql/query.py:Query.build_filter`) without dumping a 2,500-line file into the prompt.
  - **Safe symbol-anchored patching** (`projectbrain patch --apply`) where line numbers have drifted or where syntax errors must be rejected before writing to disk.

---

## Quick Start

### 1. Install

```bash
pip install "projectbrain-mcp>=0.8.6"
```

### 2. Initialize a Repository (Minimal & Non-Destructive)

```bash
projectbrain --path /path/to/your/repo init
```

- **Honors `.gitignore`:** Automatically respects root and nested `.gitignore` and `.git/info/exclude` rules.
- **Minimal Default Setup (`--ide auto`):** Creates `.projectbrain.db` (SQLite WAL), updates `.gitignore`, and writes `.mcp.json` and `AGENTS.md` at the repo root using non-destructive `<!-- projectbrain:start --> ... <!-- projectbrain:end -->` markers. Only writes `.cursor/`, `.vscode/`, or `.claude/` configs if those directories already exist in your repository (or when `--ide all` / `--ide claude` is explicitly passed).
- **Safe Execution Defaults:** Direct shell execution over MCP (`brain_run`) is disabled by default unless explicitly opted in via `PROJECTBRAIN_ALLOW_EXEC=1`.

### 3. MCP Server Configuration

```json
{
  "mcpServers": {
    "projectbrain": {
      "command": "projectbrain-mcp",
      "args": ["--lean", "/path/to/your/repo"]
    }
  }
}
```

### 4. Core CLI Commands

```bash
# 1. Slice exact symbols or line ranges (fast <20ms SQLite lookup on indexed repos)
projectbrain --path /path/to/repo slice Query.build_filter src/flask/app.py:Flask.wsgi_app

# 2. Compile a 1-turn code dossier (target slices + callers + callees)
projectbrain --path /path/to/repo dossier "how are exceptions routed to error handlers"

# 3. Apply a syntax-verified, symbol-anchored patch (--apply to write, omit for dry-run diff)
projectbrain --path /path/to/repo patch --apply --spec-file patch.spec

# 4. Verify closed-world structural claims (calls, reaches, imports, handles)
projectbrain --path /path/to/repo verify "checkout" reaches "validate_coupon"
```

---

## Reproducible Evaluation & Scale Benchmarks (`eval/`)

All correctness, retrieval, claim-verification, and scale benchmarks in this repository can be run locally:

```bash
# 1. Retrieval precision/recall, multi-hop graph vs. grep baseline, claim verification, and zero-ID slicing
python eval/run_eval.py

# 2. 200-file polyglot scale, incremental Salsa early-cutoff, FTS5, and recursive CTE latency benchmark
python eval/bench_scale.py

# 3. Full unit, integration, security, and regression test suite (197 tests)
pytest projectbrain/tests -q
```

### Built-In Verification Suite Results (`python eval/run_eval.py`)

| Suite | Metric | Result | What It Tests |
| :--- | :--- | :---: | :--- |
| **File Localization (`eval/tasks.json`)** | Precision / Recall (10 tasks) | **`100%` Precision / `100%` Recall** | Regression guard on single-file and multi-file retrieval ranking |
| **Multi-Hop Graph vs. Grep (`eval/relationship_tasks.json`)** | Pass Rate (8 tasks, incl. negative controls) | **`8 / 8 (100%)`** | 2-hop call paths where keyword `grep` misses the intermediate module |
| **Closed-World Claim Verification (`eval/claim_tasks.json`)** | Exact Verdict Accuracy (22 tasks) | **`22 / 22 (100%)`** | `SUPPORTED` / `REFUTED` / `UNKNOWN` accuracy (`0` false supports, `0` false refutations; returns `UNKNOWN` on syntax-broken files) |
| **Self-Dogfooding & Zero-ID Slicing** | Symbol Recall (7 tasks) | **`7 / 7 (100% Recall)`** | Natural-language prompts with zero function names leaked |

### What Changed in `v0.8.5` – `v0.8.6` (From `pallets/flask` & `django/django` Sandbox Reviews)

1. **Fixed Perpetual Graph Rebuild on Skipped/Vendored Files (`I-02`):**
   - Previously, files skipped as minified/vendored (e.g., 60 vendored `.js` files in Django) were recorded in `_skipped` but omitted from `is_stale()` and `incremental_index()` known-file sets, causing every warm query on Django to treat them as 60 newly added files and rebuild/re-serialize the entire graph. `is_stale()` and `incremental_index()` now track `_skipped` alongside `_index`, reducing unchanged warm `incremental_index()` checks to `0` added files (`up_to_date` in `< 100ms`).
2. **Fast Cold-Start SQLite Slicing & Zero-Copy Graph Persistence (`I-03`):**
   - `projectbrain slice <Symbol>` now queries the indexed `symbols` and `files` tables in `.projectbrain.db` directly before loading the full repository graph into memory, and `CodeGraph` persistence passes in-memory dictionaries directly to SQLite instead of round-tripping through `json.dumps(..., indent=2)` -> `json.loads()`.
   - Capped 3-hop `effect_provenance` dictionaries and replaced $O(N_{\text{calls}} \times N_{\text{candidates}})$ candidate scans in `CodeGraph.build()` with $O(1)$ `(file, symbol)` + $O(|\text{imports}|)$ lookups.
3. **AST/PDG Multi-Dimensional Program Slicing & Config/State Indexing (`I-05`):**
   - Implemented [`compute_multidim_program_slice`](src/projectbrain/slicer.py) (tracing backward `def -> use`, forward `def -> use`, and upward control-flow guards) and indexed class/module-level config declarations (`ConfigAttribute`, `ImmutableDict`, constants) so queries like `"where is SECRET_KEY used"` and `"how are exceptions routed to error handlers"` on `pallets/flask` return the complete chain (`ConfigAttribute("SECRET_KEY")`, `get_signing_serializer`, `_get_exc_class_and_code`, `_find_error_handler`) with zero test/example noise.
4. **Flask Tuple Route Methods & Scoped Contract Checks (`I-04`):**
   - `_flask_methods` now parses `tuple` and `set` literals (`methods=("GET", "POST")`), and `.py` pytest `client.post(...)` calls are no longer mistaken for frontend HTTP calls.
5. **Pinned `mcp>=1.2.0,<2.0.0`, Minimal `init --ide auto`, and Root `pyproject.toml` Fix (`I-01`, `I-06`, `I-07`):**
   - Pinned `mcp>=1.2.0,<2.0.0` so `mcp.server.fastmcp` is always present, changed `projectbrain init` default to `--ide auto`, and pointed the root `pyproject.toml` at `projectbrain/src`.
