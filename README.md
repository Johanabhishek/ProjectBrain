# ProjectBrain (`projectbrain-mcp` v0.8.9)

[![PyPI](https://img.shields.io/pypi/v/projectbrain-mcp.svg)](https://pypi.org/project/projectbrain-mcp/)
[![License: MIT](https://img.shields.io/badge/License: MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Local SQLite Code Graph, Program Slicer, and Syntax-Verified Symbol Patcher for AI Coding Agents.**

ProjectBrain indexes a repository locally into `.projectbrain.db` (SQLite WAL + FTS5) using `tree-sitter` and language AST parsers. It gives CLI and MCP coding agents four concrete primitives in a single dependency-light package—with no external database server, no Docker, no cloud embeddings, and no API keys:

1. **Program & Symbol Slicing (`projectbrain slice` / `brain_slice`):**
   - Slices exact functions, classes, methods, CommonJS/prototype assignments (`res.send`, `app.use`), or line ranges (`file.py:ClassName.method` or `file.py:120-160`) directly from SQLite in `< 20ms` without loading full files into the context window.
   - For large functions sliced around a target variable or call, computes an AST/Program Dependence Graph (PDG) slice across three orthogonal dimensions: **backward data dependence (`def -> use`)**, **forward data dependence (`def -> use`)**, and **enclosing control-flow guards (`if`/`try`/`for`/`with`)**, collapsing unrelated lines while keeping governing logic intact.
2. **One-Turn Code Dossiers (`projectbrain dossier` / `brain_dossier`):**
   - Given a bug report, stack trace, or architectural question, ranks relevant symbols using sub-identifier BM25 + call/import/state-flow graph traversal and returns class-qualified (`ClassName.method`) target code slices plus 1-hop caller/callee signatures in a single read.
3. **Syntax-Verified Symbol Patching (`projectbrain patch` / `brain_patch`):**
   - Applies symbol-anchored edits (`@replace_body <Symbol>`, `@add_method <ClassName>`, `@replace <Symbol>`, `@sub <Symbol>`) validated against Tree-sitter + language syntax compilers and caller arity contracts before touching disk, with automatic rollback on syntax errors and zero-change dry runs.
4. **Closed-World Structural Verification (`projectbrain verify` / `projectbrain_verify_claim`):**
   - Verifies structural claims (`calls`, `reaches`, `imports`, `handles`) with explicit evidence provenance (`exact`, `import_resolved`, `candidate`) and refuses to guess (`UNKNOWN`) when any traversed file has a syntax error or when a call goes through a dynamic local receiver (`adapter.send(...)`) whose type cannot be resolved statically.

---

## Language-Support Tiers

ProjectBrain indexes 29 file extensions across three explicit capability tiers:

| Tier | Languages / Formats | Capabilities |
| :--- | :--- | :--- |
| **Tier 1: Deep AST + PDG + Contracts** | **Python** (`.py`), **TypeScript / JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`) | Full Tree-sitter + Python `ast` parsing (including CommonJS `obj.method = function`, chained assignments, `Class.prototype.method`, `exports.foo`, `defineGetter`, and `@typing.overload` stub deduplication); intra-function PDG data/control-dependence slicing (`def -> use` + enclosing guards); cross-file relative import, `require('./mod')`, & `tsconfig` path-alias resolution; class/module config & descriptor tracking (`ConfigAttribute`, `Field`, `Column`, constants); route extraction (Flask, FastAPI, Django, Next.js, Express, Hono); caller arity & SQL schema contract checks. |
| **Tier 2: Tree-sitter Structural + Call Graph** | **Go** (`.go`), **Rust** (`.rs`), **Java** (`.java`), **Kotlin** (`.kt`), **C / C++** (`.c`, `.h`, `.cpp`, `.hpp`), **C#** (`.cs`), **Ruby** (`.rb`), **PHP** (`.php`) | Structural symbol extraction (functions, methods, structs, classes, interfaces, traits); intra-file and cross-file call/import graph edges; signature skeletons; symbol & line-range slicing; syntax-verified symbol patching. |
| **Tier 3: Schema, Query & Component Indexing** | **SQL** (`.sql`), **GraphQL** (`.graphql`, `.gql`), **Prisma** (`.prisma`), **Protobuf** (`.proto`), **Vue / Svelte** (`.vue`, `.svelte`), **Scala** (`.scala`), **Swift** (`.swift`) | DDL table/column/constraint extraction (`CREATE TABLE`, `NOT NULL`), schema-to-code linking, component script extraction, and symbol/line slicing. |

---

## When to Use ProjectBrain vs. Plain `grep`

- **When plain `rg` / `grep` is faster:** Looking up a single literal string or constant when you already know the exact identifier name and just want matching line numbers.
- **When ProjectBrain wins:**
  - **Multi-hop call and dispatch questions** (e.g., *"how are exceptions routed to error handlers"* or *"how are redirects handled and when is auth stripped"*), where following imports, mixins, and helper methods across files takes multiple turns of manual search/read loops.
  - **Reading specific methods inside large files** (`projectbrain slice django/db/models/sql/query.py:Query.build_filter` or `projectbrain slice res.send`) without dumping a 2,500-line file into the prompt.
  - **Safe symbol-anchored patching** (`projectbrain patch --apply`) where line numbers have drifted or where syntax errors must be rejected before writing to disk.

---

## Quick Start

### 1. Install

```bash
pip install "projectbrain-mcp>=0.8.9"
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
projectbrain --path /path/to/repo slice Query.build_filter res.send should_strip_auth

# 2. Compile a 1-turn code dossier (target slices + callers + callees)
projectbrain --path /path/to/repo dossier "how are exceptions routed to error handlers"

# 3. Apply a syntax-verified, symbol-anchored patch (--apply to write, omit for dry-run diff)
projectbrain --path /path/to/repo patch --apply --spec-file patch.spec

# 4. Verify closed-world structural claims (calls, reaches, imports, handles)
projectbrain --path /path/to/repo verify "Session.send" calls "HTTPAdapter.send"
```

---

## Reproducible Evaluation & Scale Benchmarks (`projectbrain/eval/`)

All correctness, retrieval, claim-verification, and external repository benchmarks can be run locally from the repository root:

```bash
# 1. Fixture regression guards, self-dogfooding, and zero-ID adversarial evaluation
python projectbrain/eval/run_eval.py

# 2. 200-file polyglot scale, incremental Salsa early-cutoff, FTS5, and recursive CTE latency benchmark
python projectbrain/eval/bench_scale.py

# 3. External repository benchmarks: pallets/flask, django/django, psf/requests, pallets/click, expressjs/express
python projectbrain/eval/bench_external.py --suite all

# 4. Full unit, integration, security, and regression test suite (202 tests)
pytest projectbrain/tests -q
```

### 1. Held-Out External Repository Benchmark (`python projectbrain/eval/bench_external.py --suite heldout`)

Tested on three open-source repositories across Python and CommonJS JavaScript (`psf/requests`, `pallets/click`, `expressjs/express`):

| Repository | Dossier Questions | Target Symbol Slice Recall | Cost per Hit | Additional Structural Checks |
| :--- | :---: | :---: | :---: | :--- |
| **`psf/requests`** | 4 questions (`proxies`, `redirects/auth`, `cookies`, `adapters/pool`) | **`15 / 15` (`100%`)** | `3.71 KB` | `verify_claim("Session.send", "calls", "HTTPAdapter.send")` -> **`UNKNOWN`** (dynamic receiver `adapter.send`, never false `REFUTED`); `slice_symbols(["should_strip_auth", "get_environ_proxies"])` -> `2 / 2` |
| **`pallets/click`** | 3 questions (`option/arg parsing`, `Group subcommand dispatch`, `ParamType converters`) | **`9 / 11` (`81.8%`)** | `4.95 KB` | **`0` bare `__init__` headers** (all methods qualified as `ClassName.method`, e.g. `Group.invoke`, `Group.resolve_command`, `ParamType.convert`, `Parameter.type_cast_value`) |
| **`expressjs/express`** | 3 questions (`res.send content-type`, `app.use mounting`, `app.handle dispatch`) | **`6 / 6` (`100%`)** | `2.34 KB` | `lib/response.js`: **32 functions** indexed; `lib/application.js`: **18 functions** indexed; `slice_symbols(["res.send", "app.use", "app.handle"])` -> **`3 / 3`** |
| **Combined Held-Out Total** | **10 questions** | **`30 / 32` (`93.8%`)** | **`3.81 KB`** | — |

### 2. Tuned External Repository Benchmarks (`pallets/flask` & `django/django`)

*(Note: `pallets/flask` and `django/django` were used during `v0.8.5`–`v0.8.8` development to tune dossier token budgets, PDG slicing, and incremental indexing latency.)*

| Benchmark | `v0.8.4` | `v0.8.6` | `v0.8.9` |
| :--- | :---: | :---: | :---: |
| **`pallets/flask` (6 Architectural Questions) — Target Function Slice Recall** | `9 / 19` (`47.4%`) | `12 / 19` (`63.2%`) | **`21 / 21` (`100%`, `19 / 19` subset)** |
| **`pallets/flask` (6 Architectural Questions) — Total Dossier Output** | `68.0 KB` | `113.0 KB` | **`55.8 KB` (`-50.6%` vs `v0.8.6`)** |
| **`pallets/flask` (6 Architectural Questions) — Cost per Correct Hit** | `7.6 KB` | `9.4 KB` | **`2.66 KB`** |
| **`pallets/flask` — End-to-End Bug-Fix (`dossier` -> `slice` -> `patch` -> `test`)** | — | — | **`PASS` (`red -> green` in `1.51 s`, `0` syntax/contract errors)** |
| **`django/django` (`3,051` files) — Cold `brain.load()` / Cold Walk Latency** | ~8,300 ms / 820 ms | ~8,300 ms / 820 ms | **`~4,400 ms` / `~245 ms`** |
| **`django/django` (`3,051` files) — Single-File Edit `incremental_index()` Latency** | ~7,000 ms (global rebuild) | ~7,000 ms (global rebuild) | **`515 ms` body edit (`salsa_early_cutoff: True`) / `894 ms` interface edit** |

### 3. Built-In Verification & Dogfooding Suite (`python projectbrain/eval/run_eval.py`)

| Suite | Scope | Metric | Result | Notes & Grep Comparison |
| :--- | :--- | :--- | :---: | :--- |
| **File Localization (`eval/tasks.json`)** | Small fixture repos (10 tasks) | Precision / Recall | **`100%` / `100%`** | Regression guard on basic file ranking (many are grep-answerable) |
| **Multi-Hop Graph vs. Grep (`eval/relationship_tasks.json`)** | Small fixture repos (8 tasks, incl. negative controls) | Pass Rate | **`8 / 8` (`100%`)** | Tests 2-hop call paths where single-file `grep` misses the intermediate module |
| **Closed-World Claim Verification (`eval/claim_tasks.json`)** | Small fixture repos (22 tasks) | Exact Verdict Accuracy | **`22 / 22` (`100%`)** | `0` false supports, `0` false refutations; returns `UNKNOWN` on syntax-broken files |
| **Self-Dogfooding (`compile_context`)** | ProjectBrain repo (3 tasks, 6 target symbols) | Symbol Recall & Token Savings | **`100%` (`6 / 6` symbols)** | **`22.2%–29.9%` fewer tokens** than disciplined `grep + 120-line windows` (`93.1%–97.6%` vs full files); `grep` recall is `100%` on 2 tasks and `0%` on 1 task |
| **Adversarial Zero-Identifier (`compile_context`)** | ProjectBrain repo (4 tasks, 8 target symbols; zero function names in prompt) | Symbol Recall & Token Savings | **`87.5%` (`7 / 8` symbols)** | `100%` recall on 3 tasks and `50%` on 1 task vs **`50.0%` (`4 / 8`)** for disciplined `grep`; token delta vs `grep` ranges from `-6.0%` to `+26.4%` |

---

## Changelog (`v0.8.5` – `v0.8.9`)

1. **CommonJS / Prototype Method Indexing, Dynamic Dispatch Verification, and Held-Out Benchmarks (`v0.8.9`):**
   - **JS CommonJS & Prototype Indexing:** `_parse_js` now indexes `obj.method = function`, chained assignments (`res.contentType = res.type = function`), `Class.prototype.method = function`, `exports.foo = function`, `module.exports = function`, and `defineGetter(obj, 'prop', function)`, plus CommonJS `require('./mod')` symbol bindings. On `expressjs/express`, `lib/response.js` indexes **32 functions** (up from 10), `lib/application.js` indexes **18 functions** (up from 2), and `res.send`, `app.use`, and `app.handle` slice cleanly.
   - **Dynamic Receiver Dispatch -> `UNKNOWN` in `verify_claim`:** When a function calls `.method()` on a local variable or dynamic receiver (`adapter.send(request)` in `Session.send`), `verify_claim("Session.send", "calls", "HTTPAdapter.send")` now returns **`UNKNOWN`** (explaining that the dynamic receiver cannot be resolved statically) instead of a false `REFUTED`.
   - **Class-Qualified Headers & Retrieval:** All dossier headers and call-graph pointers now format methods as `ClassName.method` (eliminating ambiguous bare `__init__` or bare `convert` headers), `@typing.overload` stubs are filtered during Python indexing, and [`projectbrain/eval/bench_external.py`](projectbrain/eval/bench_external.py) now includes a held-out benchmark suite across `psf/requests`, `pallets/click`, and `expressjs/express`.
2. **Tree-sitter CST Program Dependence Graph (PDG) Slicing for JS/TS/Go/Rust/Java (`v0.8.8`):**
   - Added [`_analyze_treesitter_cst_pdg`](projectbrain/src/projectbrain/slicer.py) so [`compute_multidim_program_slice`](projectbrain/src/projectbrain/slicer.py) uses real Tree-sitter Concrete Syntax Trees for `.ts`, `.tsx`, `.js`, `.jsx`, `.go`, `.rs`, and `.java` in addition to Python's `ast` module.
3. **Dossier Token Budget, Docstring Folding, Visible PDG Provenance, and $O(\Delta)$ Incremental Graph Updates (`v0.8.7`):**
   - Folded multi-line Python docstrings (`> 3` lines) in dossier slices, wired [`compute_multidim_program_slice`](projectbrain/src/projectbrain/slicer.py) into `compile_dossier()` with `# [PDG Slice: ...]` banners, and added incremental `CodeGraph.update_files` + SQLite `graph_delta` persistence.
