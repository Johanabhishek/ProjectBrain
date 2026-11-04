# ProjectBrain (`projectbrain-mcp`)

[![PyPI](https://img.shields.io/pypi/v/projectbrain-mcp.svg)](https://pypi.org/project/projectbrain-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Local-First 4D AST Coordinate Matrix, Surgical Engineering Dossier, and Anchor Patch Engine for AI Coding Agents.**

Instead of forcing LLMs to search repositories as flat 1D text files (running `rg`/`grep` and reading dozens of overlapping `read_file` windows), ProjectBrain indexes your codebase into a **4D AST Coordinate Matrix** and typed symbol graph in `~90ms` using pre-compiled `tree-sitter` C wheels—no external database, no Docker, no vector embeddings, and no LLM required:

- **4D AST Coordinate Matrix (`X × Y × Z × W`):** Decomposes monolith files and giant components (`>200` lines) into atomic sub-symbol cells indexed across four orthogonal axes:
  - **`X` (Scope / Branch):** Outer function, `switch` case branch (`case 'flashcards'`), `if/elif` dispatch, or JSX conditional block.
  - **`Y` (Execution Role):** `STATE` (`useState`/`useRef`/`self.x`), `EFFECT` (`useEffect[deps]`/`useMemo`/`useCallback`), `ACTION` (handlers/mutations), `IO` (`fetch`/API/storage), or `VIEW` (JSX render blocks).
  - **`Z_write` / `Z_read` (State & Storage Dependency):** Directional dataflow tracking which state variables and storage keys each cell mutates vs. reads.
  - **`W` (Component & Call Wiring):** Outgoing function calls and rendered sub-components per cell.
- **Single-Read Surgical Dossier (`projectbrain dossier`):** Intersects all 4 axes at once to unfold only the exact matching sub-symbol cells, module constants, 1-line `Connected Call-Graph` and `Secondary Candidate` coordinate maps, and a **Closed-World Exhaustive Repo Scan** into `.projectbrain_dossier.md`—with automatic **Session Cache deduplication** across follow-up queries.
- **Surgical Anchor Patch Engine (`projectbrain patch`):** Replaces verbatim `Target` + `Replacement` repetition (`edit_file`) with a compact Anchor Patch DSL (`@file`, `@sub`, `@replace`, `@insert_after`, `@delete`) validated by Tree-sitter in `<5ms`.
- **Page-Accurate Token Savings Ledger (`projectbrain stats --summary`):** Tracks real delivered tokens against 800-line `read_file` page baselines and `edit_file` payloads in `.projectbrain_ledger.json`.
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

Run `projectbrain init` inside any repository (or pass `--path` and optional `--ide {all,antigravity,claude,cursor,vscode}`):

```bash
projectbrain --path /path/to/your/repo init
```

**What `projectbrain init` does automatically across all IDEs:**
1. **Builds the AST & Call-Graph Index:** Writes `.projectbrain.json` and `.projectbrain_graph.json` in `~90ms`.
2. **Updates `.gitignore`:** Ensures ephemeral index/cache files stay out of Git.
3. **Configures MCP Across IDEs:** Writes/merges `.mcp.json` (Claude Code / Antigravity), `.cursor/mcp.json` (Cursor), and `.vscode/mcp.json` (VS Code Agent Mode).
4. **Installs Agent Rules Across IDEs:** Writes `AGENTS.md`, `.agents/rules/projectbrain.md` (Antigravity), `CLAUDE.md` (Claude Code), `.cursor/rules/projectbrain.mdc` (Cursor), and `.github/copilot-instructions.md` + `.clinerules` (VS Code Copilot / Cline / Roo Code).
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

### 1. Compile a Single-Read Engineering Dossier (`dossier` / `dossier --zoom`)
Before investigating a question or bug, compile a self-contained `<800`-line dossier (`.projectbrain_dossier.md`) with module globals, 4D AST Coordinate Matrix cells, 1–3 hop call chains, and a Closed-World Exhaustive Repo Scan (or pass `--zoom` for ultra-compact pure 4D coordinate slices + 1-line pointer tables):

```bash
projectbrain --path /path/to/repo dossier "what role does questionnaire have in onboarding"
projectbrain --path /path/to/repo dossier --zoom "how does credit card validation and countdown redirect work"
```
### 2. Surgically Slice Symbols or Line Ranges (`slice`)
Fetch exact source lines (`start_line`–`end_line`) for 1–2 symbols without reading the entire file:

```bash
projectbrain --path /path/to/repo slice --summary src/App.tsx:ProtectedRoute src/data/questionnaireData.ts:calculatePersona
```

### 3. Apply Surgical Anchor Patches (`patch`)
Apply multi-file edits using the Anchor Patch DSL (`@file`, `@sub`, `@replace`, `@insert_after`, `@insert_before`, `@delete`), automatically syntax-checked by Tree-sitter:
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

## How the 4D Coordinate Matrix Works (1D Search vs. 4D Intersection)

Traditional coding agents search in **1D**: they run `rg -n <keyword>` and call `read_file(StartLine, EndLine)` around every match. On large real-world files (e.g., a 3,000–6,000 line React view or Python service where 90%+ of the file lives inside one outer function), a single state variable or route identifier appears 20–50 times across declarations, effects, handlers, and JSX branches—forcing the agent to read thousands of lines of surrounding noise.

ProjectBrain projects every large function/component into a **4D Coordinate Matrix** inside `.projectbrain_dossier.md`:

```text
# [4D Coordinate Matrix] StudyTable (L74-L5923, 86 sub-cells indexed)
#   [L152-L162] Y:STATE  X:StudyTable.state        Z_w:flashcards,flashcardIdx  Z_r:-
#   [L482-L547] Y:EFFECT X:StudyTable.useEffect[]  Z_w:savedCourseParams        Z_r:localStorage
#   [L1736-L1854] Y:VIEW X:switch:case 'flashcards' Z_w:flashcardIdx            Z_r:flashcards  W:FlashcardDeck
```

When the agent queries a bug or feature, ProjectBrain computes the **coordinate intersection** across `(X, Y, Z, W)`, unfolds the exact matching cell lines verbatim, and leaves the rest of the monolith as a compact 1-line-per-cell coordinate map so the agent knows the exact `file:start-end` address of every other branch without reading the whole file.

### Measured Real-World Subagent A/B Benchmark (v0.4.0)

Evaluated across two production TypeScript/React + Python repositories (`142` files / `1,238` 4D cells and `162` files / `887` 4D cells) with a live head-to-head subagent A/B audit (`rg + read_file` vs. `projectbrain dossier --zoom` + `slice`):

| Benchmark Metric | Baseline Agent (`rg` + `read_file`) | ProjectBrain 4D `--zoom` Agent | Measured Difference |
| :--- | :---: | :---: | :---: |
| **Ground-Truth Unfolded Code Recall (`20` spans across `12` tasks)** | `20/20` (`100%`) | **`20/20` (`100%`)** | Tied (`100%` recall) |
| **Wall-Clock Investigation Time (3-Part Multi-File Audit)** | `152.0s` (`2m 32s`) | **`58.0s` (`0m 58s`)** | **`2.62x` faster (`-94.0s`)** |
| **LLM Planner Turns** | `12` turns | **`5` turns** | **`2.40x` fewer turns (`-58.3%`)** |
| **Total Tool Calls** | `17` calls | **`4` calls** | **`4.25x` fewer calls (`-76.5%`)** |
| **Raw Tool Output Ingested** | `~24,759` tok (`99,037` chars) | **`~6,322` tok (`25,291` chars)** | **`3.92x` smaller (`-74.5%`)** |
| **Cumulative Prompt Context Billed Across Turns** | `~198,796` tok (`795,187` chars) | **`~24,773` tok (`99,095` chars)** | **`8.02x` smaller (`-87.5%`)** |
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
