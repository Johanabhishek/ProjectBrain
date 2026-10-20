# ProjectBrain (`projectbrain-mcp`)

[![PyPI](https://img.shields.io/pypi/v/projectbrain-mcp.svg)](https://pypi.org/project/projectbrain-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Local-First AST Context Compiler, Closed-World Engineering Dossier, and Surgical Anchor Patch Engine for AI Coding Agents.**

ProjectBrain indexes your codebase into a typed AST & call-graph (`file`, `fn`, `cls`, `route`, `mod`) in `~90ms` using pre-compiled `tree-sitter` C wheels—no external database, no Docker, no vector embeddings, and no LLM required. It cuts coding-agent token bills by **60%–65%** while speeding up multi-file debugging and editing by **30%–42%**:

- **Input Token Reduction (`-81.3%` to `-88.3%` code tokens, `-73.2%` to `-76.3%` billed prompt tokens):** Compiles module constants, smart-folded AST function slices, 1–3 hop bidirectional call chains with `@L<line>` caller windows, and a **Closed-World Exhaustive Repo Scan** into a single `<800`-line `.projectbrain_dossier.md` read in **1 `read_file` call**.
- **Output Token Reduction (`-63.2%` to `-77.3%` edit output tokens):** Replaces verbatim `Target` + `Replacement` repetition (`edit_file`) with a compact Anchor Patch DSL (`@file`, `@sub`, `@replace`, `@insert_after`, `@delete`) validated by Tree-sitter in `<5ms`.
- **Live ROI & Token Savings Ledger (`projectbrain stats --summary`):** Tracks cumulative input/output tokens saved, file reads avoided, estimated USD saved, and developer wait-time saved in `.projectbrain_ledger.json`.

---

## Step-by-Step Setup Guide (60 Seconds)

### Step 1: Install from PyPI

Install `projectbrain-mcp` globally (or in your active Python environment):

```bash
pip install projectbrain-mcp
```

*(Alternatively, use `pipx install projectbrain-mcp` or `uv tool install projectbrain-mcp`.)*

Verify both the CLI and MCP server binaries are available:

```bash
projectbrain --help
projectbrain-mcp --help
```

---

### Step 2: Initialize Your Repository (One Command)

Run `projectbrain init` inside any repository (or pass `--path`):

```bash
projectbrain --path /path/to/your/repo init
```

**What `projectbrain init` does automatically:**
1. **Builds the AST & Call-Graph Index:** Writes `.projectbrain.json` and `.projectbrain_graph.json` in `~90ms`.
2. **Updates `.gitignore`:** Ensures ephemeral index/cache files stay out of Git.
3. **Configures MCP (`.mcp.json`):** Registers the `projectbrain-mcp` server for Claude Code, Cursor, Windsurf, and Google Antigravity.
4. **Installs Agent Rules (`.agents/rules/projectbrain.md` & `AGENTS.md`):** Instructs AI coding agents to compile a dossier (`brain_dossier` / `projectbrain dossier`) first before running blind `grep` or full-file reads, and to apply edits via `brain_patch`.

---

### Step 3: Zero-Install MCP Server Configuration (Optional)

If your IDE or agent loads MCP servers from a global config file (such as `.cursor/mcp.json`, `claude_desktop_config.json`, or Antigravity `mcp_config.json`), add:

```json
{
  "mcpServers": {
    "projectbrain": {
      "command": "projectbrain-mcp",
      "args": ["/path/to/your/repo"]
    }
  }
}
```

Or run directly with **`uvx`** (zero manual install required):

```json
{
  "mcpServers": {
    "projectbrain": {
      "command": "uvx",
      "args": ["projectbrain-mcp", "/path/to/your/repo"]
    }
  }
}
```

---

### Step 4: Parent / Monorepo Workspace Setup

If you open a **parent directory** in your IDE that contains multiple sub-projects (e.g., `/workspace/my-app-frontend` and `/workspace/my-app-backend`), place an `AGENTS.md` (or `.agents/rules/projectbrain.md`) at the **workspace root** and pass `--path "<subfolder>"` so the agent targets the indexed sub-repository:

```markdown
# ProjectBrain Mandatory Codebase Investigation & Editing Rules

Whenever investigating, answering questions about, or modifying `<subfolder>`, ALWAYS use ProjectBrain FIRST before running shell searches (`rg`, `grep`) or multiple `read_file` calls:

1. **Compile a Single-Read Engineering Dossier (`projectbrain dossier`):**
   - Run: `projectbrain --path "<subfolder>" dossier "<question_or_bug>"`
   - Read `<subfolder>/.projectbrain_dossier.md` in one single `read_file` call (`StartLine=1, EndLine=800`).
2. **Surgical Symbol Slicing (`projectbrain slice`):**
   - Run: `projectbrain --path "<subfolder>" slice --summary <file:symbol_or_start-end>`
3. **Surgical Anchor Patching (`projectbrain patch`):**
   - Run: `projectbrain --path "<subfolder>" patch --spec "<Anchor Patch DSL>" --apply`
```

---

### Step 5: Keep Git Clean (`.gitignore`)

If you want all local ProjectBrain caches, dossiers, diffs, and local agent configs completely ignored by Git, add the following to your `.gitignore`:

```gitignore
# ProjectBrain (local dev only)
.projectbrain*
*.projectbrain*
**/.projectbrain*
project_brain.py
test_projectbrain.json
.mcp.json
.agents/
AGENTS.md
CLAUDE.md
.claude/settings.json
```

*(Tip: If `AGENTS.md` is already tracked in your repo and you want to customize it locally without Git showing it as modified, run `git update-index --skip-worktree AGENTS.md`.)*

---

## Daily CLI Workflow & Commands

### 1. Compile a Single-Read Engineering Dossier (`dossier`)
Before investigating a question or bug, compile a self-contained `<800`-line dossier (`.projectbrain_dossier.md`) with module globals, smart-folded AST slices, 1–3 hop call chains, and a Closed-World Exhaustive Repo Scan:

```bash
projectbrain --path /path/to/repo dossier "what role does questionnaire have in onboarding"
```

### 2. Surgically Slice Symbols or Line Ranges (`slice`)
Fetch exact source lines (`start_line`–`end_line`) for 1–2 symbols without reading the entire file:

```bash
projectbrain --path /path/to/repo slice --summary src/App.tsx:ProtectedRoute src/data/questionnaireData.ts:calculatePersona
```

### 3. Apply Surgical Anchor Patches (`patch`)
Cut output tokens by 65%–80% using the Anchor Patch DSL (`@file`, `@sub`, `@replace`, `@insert_after`, `@insert_before`, `@delete`), automatically syntax-checked by Tree-sitter:

```bash
# Dry-run (writes unified diff to .projectbrain_patch.diff)
projectbrain --path /path/to/repo patch --spec "@file src/App.tsx
@sub L84 \`!profileCompleted\` -> \`isHydrated && !profileCompleted\`"

# Apply directly to disk
projectbrain --path /path/to/repo patch --apply --spec "@file src/App.tsx
@sub L84 \`!profileCompleted\` -> \`isHydrated && !profileCompleted\`"
```

### 4. View Live ROI & Token Savings Dashboard (`stats`)
Check how many input/output tokens and tool calls ProjectBrain has saved across your sessions:

```bash
projectbrain --path /path/to/repo stats --summary
```

Example output:
```text
===============================================================
  ProjectBrain Live ROI & Token Savings Ledger
===============================================================
  Repository         : /path/to/repo
  Total Operations   : 4
  Input Tokens Saved : 504,441 tok (16,156 delivered vs 520,597 baseline -> -96.9%)
  Output Tokens Saved: 190 tok (85 spec vs 275 edit_file -> -69.1%)
  Tool Calls Avoided : 22 file reads / multi-turn edits
===============================================================
```

**Live auto-refreshing dashboard in terminal:**
- **PowerShell (Windows):**
  ```powershell
  while ($true) { cls; projectbrain --path "/path/to/repo" stats --summary; Start-Sleep 2 }
  ```
- **Bash / Zsh (macOS / Linux):**
  ```bash
  watch -n 2 projectbrain --path /path/to/repo stats --summary
  ```
- **Reset the ledger:**
  ```bash
  projectbrain --path /path/to/repo stats --reset
  ```

### 5. Additional Granular CLI Tools

```bash
# Re-index repository manually (auto-updates via mtime on every command)
projectbrain --path /path/to/repo index

# Explore symbol neighborhood, route flows, or shortest path between A and B
projectbrain --path /path/to/repo explore Questionnaire setItem

# Compute blast radius (direct importers, callers, and affected tests)
projectbrain --path /path/to/repo impact src/context/CloudStateContext.tsx

# Pin an AST-anchored architectural invariant (auto-flags [STALE] if AST hash changes)
projectbrain --path /path/to/repo remember calculatePersona "Thresholds: >=22 Advanced, >=16 Intermediate, <16 Beginner"

# Fact-check a structural claim (SUPPORTED / REFUTED / UNKNOWN)
projectbrain --path /path/to/repo verify Questionnaire calls calculatePersona

# Check index coverage & parse health
projectbrain --path /path/to/repo health
```

---

## MCP Tools Reference (`projectbrain-mcp`)

| MCP Tool | Purpose |
| :--- | :--- |
| **`brain_dossier`** | **Primary entry point.** Compiles a `<800`-line `.projectbrain_dossier.md` containing module globals, smart-folded AST slices, 1–3 hop bidirectional call chains, and a Closed-World Exhaustive Repo Scan. |
| **`brain_patch`** | Applies or dry-runs a Surgical Anchor Patch spec (`@file`, `@sub`, `@replace`, `@insert_after`, `@delete`) with instant Tree-sitter syntax validation. |
| **`brain_slice`** | Fetches exact source code lines (`start_line`–`end_line`) for specific functions, classes, or `file:start-end` ranges without reading full files. |
| **`brain_stats`** | Returns cumulative input/output token savings, tool calls avoided, and ROI metrics from `.projectbrain_ledger.json`. |
| **`brain_context`** | Compiles a JSON surgical context packet (`target_slices` + `skeletons` + `memories` + session deduplication). |
| **`brain_explore`** | Unified graph explorer (`mode="neighbors"`, `"path"`, `"flow"`, or `"impact"`) for call/dependency traversal and blast-radius analysis. |
| **`brain_check`** | Unified 3-valued verification & safety checker (`mode="claim"`, `"batch"`, `"plan"`, or `"health"` returning `SUPPORTED` / `REFUTED` / `UNKNOWN`). |
| **`brain_remember`** | Saves or lists persistent AST-anchored architectural notes in `.projectbrain_memory.json`. |

---

## Verified A/B Benchmark Results (`142`-File Production Repo)

| Benchmark Scenario | Metric | Baseline Agent (No Brain) | ProjectBrain Agent | Improvement |
| :--- | :--- | :---: | :---: | :---: |
| **Scenario 1: Targeted 3-Part Engineering Analysis** | Code Output Ingested | `67,319` tok | **`12,569` tok** | **`-81.3%` (`5.35x` smaller)** |
| | Total Prompt Tokens Billed | `655,391` tok | **`155,052` tok** | **`-76.3%` (`4.23x` fewer)** |
| | File Reads (`read_file`) | `16` | **`2`** | **`-87.5%`** |
| | Net API Cost | `$0.6723` | **`$0.2590`** | **`-61.5%` (`2.60x` cheaper)** |
| **Scenario 2: Unseen Open-Ended Bug Report (Zero Identifiers)** | Code Output Ingested | `351,658` tok (`1.40M` chars) | **`40,982` tok (`163.9k` chars)** | **`-88.3%` (`8.58x` smaller)** |
| | Total Prompt Tokens Billed | `4,561,692` tok | **`1,220,579` tok** | **`-73.2%` (`-3.34M` tokens)** |
| | File Reads (`read_file`) | `84` (`19` re-reads) | **`6` (`0` re-reads)** | **`-92.9%` (`14.0x` fewer reads)** |
| | Multi-File Patch Output Tokens | `3,359` tok (`edit_file`) | **`529` – `1,237` tok** | **`-63.2%` to `-77.3%` edit output** |
| | Net API Cost | `$3.2712` | **`$1.1557`** | **`-64.7%` (`2.83x` cheaper)** |

---

## Project Layout

```text
projectbrain/
├── src/projectbrain/
│   ├── brain.py        # Primary API: compile_dossier, compile_context, slice_symbols, explore, check
│   ├── slicer.py       # Surgical AST line extraction, smart folding & signature-only skeletons
│   ├── patcher.py      # Surgical Anchor Patch DSL engine + Tree-sitter syntax verifier
│   ├── ledger.py       # Live ROI & Token Savings Ledger (.projectbrain_ledger.json)
│   ├── memory.py       # Persistent AST-anchored repo memory (.projectbrain_memory.json)
│   ├── graph.py        # Typed symbol graph, qualified call resolution, BFS & shortest path
│   ├── parsers.py      # Tree-sitter extraction (signatures, schemas, AST hashes, bindings)
│   ├── resolve.py      # Python module/symbol import resolution & TS path alias resolution
│   ├── frameworks.py   # Next.js App & Pages Router convention route inference
│   ├── cli.py          # CLI entry point (init, dossier, patch, slice, stats, compile, explore, etc.)
│   └── server.py       # FastMCP server entry point (brain_dossier, brain_patch, brain_stats, etc.)
├── tests/              # Comprehensive pytest suite
└── eval/               # Evaluation harness, fixtures, and self-dogfooding benchmark
```
