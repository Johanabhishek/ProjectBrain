# ProjectBrain: Future Architecture & Strategic Roadmap (`v0.3` -> `v1.0`)

## 1. Key Insights from Google Tech Lead (`davenportjw/cosm`)
Reference: `https://github.com/davenportjw/cosm` ("AI-Native Polyglot AST Source Control Management & Target Compilation System")

- **What Cosm proved:**
  1. **SQLite WAL Graph Storage (`.cosm/graph.db`):** Storing AST symbol nodes and dependency edges in an embedded SQLite WAL database with content-addressed SHA-256 Merkle hashes instead of flat files.
  2. **Polyglot Language Codec Dispatcher (`pkg/codecs/`):** Splitting AST parsing into 21 isolated language codecs (`python`, `typescript`, `golang`, `rust`, `java`, `kotlin`, `cpp`, `csharp`, `sql`, `hcl`, `graphql`, `protobuf`, `dockerfile`, etc.) to trace cross-boundary contracts (Frontend -> Backend API -> SQL -> Terraform Infra).
  3. **Declarative AST Surgery (`cosm-ast-surgeon`):** Executing mutations via symbol-anchored geometric verbs (`replace_function_body`, `replace_function`, `add_method`, `add_before`, `add_after`, `delete`, `add_import`, `replace_global`) so agents never need exact line numbers to edit a function.
- **What ProjectBrain proved:**
  1. **Zero-friction drop-in on any Git repo (`90ms` index)** with MCP + CLI support.
  2. **Closed-World Engineering Dossier (`-67%` to `-73%` input token reduction)** in a single `<800`-line read.
  3. **Sub-line Anchor Patching (`-49%` to `-77%` output token reduction)** with Tree-sitter syntax verification and a live Token Reduction Ledger.

---

## 2. Technical Roadmap

### Phase 1 (`v0.3`): SQLite WAL Graph DB Storage (`.projectbrain/graph.db`) + Symbol-Scoped Patching
1. **Replace flat JSON deserialization (`.projectbrain.json` / `.projectbrain_graph.json`) with SQLite WAL + FTS5:**
   - Tables: `files`, `symbols` (with per-symbol SHA-256 `merkle_hash`), `edges` (`calls`, `imports`, `reads_state`, `routes_to`), `fts_symbols` (FTS5 BM25 index), and `ledger_events`.
   - Sub-millisecond 1–3 hop graph traversal via `WITH RECURSIVE` SQL CTEs without loading the entire repo graph into Python RAM.
   - Benchmark lookup latency at `100`, `1,000`, `10,000`, and `50,000` files (`JSON file load` vs `SQLite WAL Graph DB`).
2. **Symbol-Scoped AST Surgery in `projectbrain patch` (eliminating folded-line friction):**
   - Support symbol-anchored operations without line numbers:
     - `@sub <SymbolName> \`<old>\` -> \`<new>\`` (searches only inside `<SymbolName>`'s AST line span, even if folded in the dossier!)
     - `@replace_body <SymbolName>` ... `@end` (replaces function/method body while preserving signature & decorators)
     - `@add_method <ClassName>` ... `@end`
     - `@add_import \`import ...;\``

### Phase 2 (`v0.4`): Modular Polyglot Codec Architecture (`src/projectbrain/codecs/`)
1. Break `parsers.py` into a `codecs/` package with a unified `LanguageCodec` interface.
2. Add first-class codecs for `go`, `rust`, `java`, `sql`, `graphql`, `protobuf`, and `hcl` (Terraform).
3. Add **Cross-Boundary Contract Edges**: link frontend `fetch("/api/...")` calls to backend route handlers and SQL/schema definitions.

### Phase 3 (`v0.5`): Hook-Enforced Interception & Ephemeral Micro-Universes
1. **`PreToolUse` IDE Hooks (`projectbrain init --hooks`):** Automatically intercept expensive full-file `Read`/`Grep` calls on monoliths in Claude Code, Cursor, and Antigravity.
2. **Graph-Backed Micro-Universes:** Allow parallel agents to apply and syntax-check AST patches inside isolated SQLite branches before materializing changes to the working tree.

---

## 3. Strategic & Commercial Playbook
1. **Build & Reply Loop with `davenportjw` & Richard Seroter:** Ship Phase 1 (`SQLite WAL Graph DB` + `Symbol-Scoped AST Surgery` + `JSON vs DB Lookup Benchmark`) and share the concrete latency & token curves.
2. **Public SWE-Bench Verified Benchmark:** Publish a reproducible 50-task SWE-bench Verified comparison (`Baseline Agent` vs `ProjectBrain Agent`: Pass@1 accuracy, input/output tokens, wall-clock time).
3. **Open-Core Monetization:** Free local CLI/MCP (`projectbrain-mcp` on PyPI) for individual developers; paid **Team Brain** tier (`$25/seat/mo`) for shared cross-branch Graph DB, team-wide causal bug memory, and PR blast-radius review automation.
