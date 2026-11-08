# ProjectBrain (`projectbrain-mcp` v0.8.4)

[![PyPI](https://img.shields.io/pypi/v/projectbrain-mcp.svg)](https://pypi.org/project/projectbrain-mcp/)
[![License: MIT](https://img.shields.io/badge/License: MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**For a real bug or refactor, ProjectBrain gives your coding agent the exact symbols, callers, and dependencies it needs in 1 turn—with fewer wrong files and 77–89% fewer tokens.**

Instead of forcing AI coding agents to explore repositories by running dozens of `rg`/`grep` searches and reading entire files line-by-line, ProjectBrain indexes your codebase locally into a **SQLite WAL Code Graph (`.projectbrain.db`)** using `tree-sitter` and structural parsers. No external database daemon, no Docker, no cloud vector embeddings, and no API keys required.

### What It Actually Does

1. **One-Turn Code Dossiers (`projectbrain dossier` / `brain_dossier`):**
   - Given a bug report, stack trace, or feature prompt, ProjectBrain ranks relevant symbols using sub-identifier BM25 + call/import graph traversal, automatically bridges passive schema/DTO files to their executable consumers, and returns exact statement/function code slices plus 1-hop caller/callee signatures in a single read.
2. **Surgical Symbol & Line Slicing (`projectbrain slice` / `brain_slice`):**
   - Slices exact functions, classes, methods, or line ranges (`file.py:ClassName.method` or `file.py:120-160`) across multiple files in a single call, with fuzzy symbol/path suggestions when an agent guesses an incomplete name.
3. **AST Symbol-Anchored Patching (`projectbrain patch` / `brain_patch`):**
   - Applies surgical edits (`@replace_body <Symbol>`, `@add_method <ClassName>`, `@replace <Symbol>`, `@sub L<n>`) validated against Tree-sitter + language syntax compilers and caller arity contracts before touching disk, with automatic rollback on syntax errors.
4. **`.gitignore`-Compliant Polyglot Indexing (29 File Extensions):**
   - Honors root and nested `.gitignore` / `.git/info/exclude` rules automatically.
   - Supports **Python, TypeScript, JavaScript, JSX/TSX, Vue, Svelte, Go, Rust, Java, Kotlin, Scala, C, C++, C#, Ruby, PHP, Swift, SQL, GraphQL, Prisma, and Protobuf**.
5. **Safe Output & Traceback Compression (`projectbrain run` / `brain_run`):**
   - Folds passing test noise (`pytest`, `jest`/`vitest`, `go test`, `cargo test`), deduplicates repetitive log lines, and maps stack-trace `file:line` frames directly to enclosing functions and 1-hop callers. Direct shell execution over MCP is disabled by default unless explicitly opted in via `PROJECTBRAIN_ALLOW_EXEC=1`.

---

## Quick Start (60 Seconds)

### Step 1: Install

```bash
pip install projectbrain-mcp
```

*(Or `pipx install projectbrain-mcp` / `uv tool install projectbrain-mcp`.)*

### Step 2: Initialize Your Repository (Non-Destructive)

```bash
projectbrain --path /path/to/your/repo init --hooks
```

**What `projectbrain init --hooks` does safely:**
1. **Builds the SQLite Code Graph Index:** Writes `.projectbrain.db` (WAL mode), honoring `.gitignore`.
2. **Updates `.gitignore`:** Appends ephemeral `.projectbrain*` cache patterns if not already present.
3. **Merges MCP Configs:** Safely merges `projectbrain` into `.mcp.json`, `.cursor/mcp.json`, and `.vscode/mcp.json` without overwriting other MCP servers.
4. **Appends Agent Rules Non-Destructively:** Uses `<!-- projectbrain:start --> ... <!-- projectbrain:end -->` section markers in `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/projectbrain.mdc`, and deep-merges `.claude/settings.local.json` `PreToolUse` hooks so existing team instructions and settings are never overwritten.

### Step 3: MCP Server Configuration

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

---

## Core CLI Workflow

```bash
# 1. Compile a 1-turn engineering dossier (target slices + callers + callees)
projectbrain --path /path/to/repo dossier "recover from a panic in an HTTP handler and abort with 500"

# 2. Slice multiple symbols or line ranges in one batch
projectbrain --path /path/to/repo slice src/App.tsx:ProtectedRoute recovery.go:CustomRecoveryWithWriter

# 3. Apply a syntax-verified, symbol-anchored patch
projectbrain --path /path/to/repo patch --apply --spec-file patch.spec

# 4. Verify closed-world structural claims (calls, reaches, imports, handles)
projectbrain --path /path/to/repo verify "checkout" reaches "validate_coupon"
```

---

## Reproducible Evaluation Suite (`eval/`)

Every retrieval, multi-hop graph traversal, closed-world claim verification, and scale benchmark in this repository can be reproduced locally with two commands:

```bash
# 1. Run retrieval precision/recall, multi-hop graph vs. grep baseline, claim verification, and zero-ID slicing
python eval/run_eval.py

# 2. Run 200-file polyglot scale, incremental Salsa early-cutoff, FTS5, and recursive CTE latency benchmark
python eval/bench_scale.py
```

### 1. Built-In Reproducible Harness Results (`python eval/run_eval.py`)

| Benchmark Suite | Metric | Score | Notes |
| :--- | :--- | :---: | :--- |
| **File Localization (`eval/tasks.json`)** | Precision / Recall (10 tasks) | **`100%` Precision / `100%` Recall** | Returns exact 1-file answers for 1-file tasks and 3-file answers for 3-file flows |
| **Multi-Hop Graph vs. Grep (`eval/relationship_tasks.json`)** | Pass Rate (8 tasks, incl. negative controls) | **`8 / 8 (100%)`** | Finds 2-hop call paths where naive `grep` misses the intermediate module |
| **Closed-World Claim Verification (`eval/claim_tasks.json`)** | Exact Verdict Accuracy (22 tasks) | **`22 / 22 (100%)`** | `0` false supports, `0` false refutations; returns `UNKNOWN` on syntax-broken files |
| **Self-Dogfooding Context Slicing** | Symbol Recall & Token Reduction | **`3 / 3 (100% Recall)`** | `96.6%–99.2%` token reduction vs. reading matched full files |
| **Adversarial Zero-Identifier Slicing** | Symbol Recall (prompts with zero symbol names) | **`4 / 4 (100% Recall)`** | Outperforms `grep` + 120-line windows (`0%–50%` recall on 3 of 4 tasks) |

---

### 2. Blind Cold-Start Agent Benchmarks (Production Open-Source Repositories)

Measured head-to-head by spawning two isolated subagents simultaneously on unmodified open-source repositories (**Naive Search Agent** using `rg`/`Select-String` + `read_file` vs. **ProjectBrain Agent** starting completely cold with no pre-built `.projectbrain.db` index and zero file/symbol hints in the prompt):

#### A. `paperless-ngx/paperless-ngx` (4-Question Deep Architectural Audit — Cold Start)

| Metric | Naive Search Agent | ProjectBrain Agent (Cold Start) | Measured Delta |
| :--- | :---: | :---: | :--- |
| **Wall-Clock Time** | `12m 00s` (`720.0s`) | **`6m 27s` (`387.0s`)** | **`5m 33s` faster (`-46.3%` / `1.86x` faster)** |
| **Model Turns** | `53` turns | **`15` turns** | **`38` fewer turns (`-71.7%` / `3.53x` fewer)** |
| **Tool Calls** | `51` calls | **`18` calls** (`1` dossier + `12` reads + `5` batched slices) | **`33` fewer calls (`-64.7%` / `2.83x` fewer)** |
| **Uncached Input Tokens** | `274,363` tokens | **`216,603` tokens** | **`57,760` tokens saved (`-21.1%`)** |
| **Cumulative Billed Input Tokens** | `6,506,996` tokens | **`1,499,886` tokens** | **`5,007,110` tokens saved (`-76.9%` / `4.34x` less)** |

#### B. `Netflix/dispatch` (879-File Repository — 3-Question Cold-Start Trace)

| Metric | Naive Search Agent | ProjectBrain Agent (Cold Start) | Measured Delta |
| :--- | :---: | :---: | :--- |
| **Wall-Clock Time** | `5m 52s` (`352.0s`) | **`3m 44s` (`224.0s`)** | **`2m 08s` faster (`-36.4%` / `1.57x` faster)** |
| **Model Turns** | `16` turns | **`8` turns** | **`8` fewer turns (`-50.0%` / `2.0x` fewer)** |
| **Tool Calls** | `41` calls (`14` shell + `27` reads) | **`5` calls** (`1` dossier + `1` read + `3` slices) | **`36` fewer calls (`-87.8%` / `8.2x` fewer)** |
| **Cumulative Billed Input Tokens** | `2,139,562` tokens | **`224,407` tokens** | **`1,915,155` tokens saved (`-89.5%` / `9.53x` less)** |

---

## Project Layout

```text
projectbrain/
├── src/projectbrain/
│   ├── brain.py        # Context Compiler, Dossier Engine, Slicer, Explorer & Contract Verifier
│   ├── store.py        # SQLite WAL Property Graph (.projectbrain.db), FTS5, and Delta Overlay
│   ├── slicer.py       # Statement/Symbol Slicer, Focus-Window Folding & Signature Skeletons
│   ├── patcher.py      # AST Symbol-Anchored & Line-Anchored Patch Engine + Syntax Verification
│   ├── parsers.py      # Tree-sitter & Structural Parsers (29 file extensions)
│   ├── graph.py        # Cross-File Call/Import Graph & Personalized PageRank Push
│   ├── ledger.py       # Session Token & ROI Telemetry Ledger
│   ├── memory.py       # AST-Anchored Repository Notes (.projectbrain_memory.json)
│   ├── resolve.py      # Cross-File Import & TypeScript Path Alias Resolution
│   ├── frameworks.py   # Route Inference (Next.js, FastAPI, Flask, Express, Hono, Gin, Actix, Spring, ASP.NET)
│   ├── cli.py          # CLI Entry Point (init, dossier, slice, patch, verify, run, stats)
│   └── server.py       # FastMCP Server Entry Point (standard and --lean modes)
├── tests/              # 194 Unit, Integration, Security, and Benchmark Threshold Tests
└── eval/               # Reproducible Evaluation & Scale Benchmark Harnesses
```
