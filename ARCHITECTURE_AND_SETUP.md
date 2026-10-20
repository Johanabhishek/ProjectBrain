# ProjectBrain (`projectbrain-mcp`) — Setup & Architecture Guide

---

## 1. Installation & Setup (60 Seconds)

### Step 1: Install from PyPI
Install `projectbrain-mcp` once on your machine:

```bash
pip install projectbrain-mcp
```
* **What this installs:** Two global CLI commands: `projectbrain` (the CLI) and `projectbrain-mcp` (the MCP server for AI coding agents).
* **Zero bloat:** Uses pre-compiled C wheels (`tree-sitter` + `mcp`). No external database, no Docker, and no GPU/embeddings required.

---

### Step 2: Initialize Any Project in 1 Command
Run `init` on any repository where you want your AI agent to use ProjectBrain:

```bash
projectbrain --path /path/to/your/repo init
```
* **What `init` does automatically in ~0.5 seconds:**
  * Scans and indexes your codebase into `.projectbrain.json` and `.projectbrain_graph.json`.
  * Updates `.gitignore` so ephemeral index caches never pollute Git.
  * Creates `.mcp.json` in the repo root so MCP-compatible editors (Claude Code, Cursor, Windsurf, Antigravity) auto-detect the `projectbrain` server.
  * Creates `.agents/rules/projectbrain.md` instructing the AI agent to compile a single-read Engineering Dossier (`brain_dossier` / `projectbrain dossier`) first instead of running blind `grep` searches or reading entire files, and to apply edits with `brain_patch`.

---

### Step 3: Parent / Monorepo Workspace Setup (If Opening a Parent Folder)
If your IDE workspace is opened at a **parent folder** containing sub-repositories (e.g. `D:\workspace\my-app`), place an `AGENTS.md` (or `.agents/rules/projectbrain.md`) at the **parent workspace root** and pass `--path "<subfolder>"` in the rules so the agent automatically invokes ProjectBrain on the child repo:

```markdown
# ProjectBrain Mandatory Codebase Investigation & Editing Rules

Whenever investigating, answering questions about, or modifying `<subfolder>`, you **MUST ALWAYS use ProjectBrain FIRST** before running shell searches (`rg`, `Get-ChildItem`, `grep`) or multiple `read_file` calls:

1. **Compile a Single-Read Engineering Dossier (`projectbrain dossier`):**
   - Run `projectbrain --path "<subfolder>" dossier "<question_or_bug>"`
   - Immediately read `<subfolder>/.projectbrain_dossier.md` in **one single `read_file` call** (`StartLine=1, EndLine=800`).
2. **Surgical Symbol Slicing (`projectbrain slice`):**
   - Run `projectbrain --path "<subfolder>" slice --summary <file:symbol_or_start-end>` instead of reading full files.
3. **Surgical Anchor Patching (`projectbrain patch`):**
   - Run `projectbrain --path "<subfolder>" patch --spec "<Anchor Patch DSL>" --apply`
```

---

### Step 4: Keep Git Clean (`.gitignore`)
Add these entries to `.gitignore` to keep all local ProjectBrain files and agent rules untracked:

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

---

### Step 5: Daily CLI & Live ROI Dashboard Commands

```bash
# 1. Compile a Single-Read Engineering Dossier (<800 lines in .projectbrain_dossier.md)
projectbrain --path /path/to/repo dossier "how does onboarding questionnaire assign persona"

# 2. Surgically slice specific symbols or line ranges
projectbrain --path /path/to/repo slice --summary src/App.tsx:ProtectedRoute

# 3. Apply a Surgical Anchor Patch (cuts output tokens by 65-80%)
projectbrain --path /path/to/repo patch --apply --spec "@file src/App.tsx
@sub L84 \`!profileCompleted\` -> \`isHydrated && !profileCompleted\`"

# 4. View the Live ROI & Token Savings Dashboard
projectbrain --path /path/to/repo stats --summary

# 5. Reset the savings ledger
projectbrain --path /path/to/repo stats --reset
```

---

## 2. Architecture Diagram

```mermaid
flowchart TD
    subgraph Repo["1. Your Codebase (Python / JS / TS / JSX / TSX)"]
        Files["Source Files (.py, .ts, .tsx, .js)"]
    end

    subgraph Indexer["2. Local Indexing & AST Extraction (~90ms, mtime-cached)"]
        Walk["File Walker (brain.py)\n• Skips node_modules, .git, minified files\n• Checks file mtime for instant incremental updates"]
        Parser["Tree-Sitter AST Parser (parsers.py)\n• Function & class signatures (params + return types)\n• Class fields/schemas & TS interfaces\n• Exact start_line / end_line & AST hash\n• Qualified calls (self.fn, Class.fn, mod.fn)"]
        Resolver["Import & Route Resolver (resolve.py + frameworks.py)\n• Python relative & symbol imports (from x import y)\n• TS/JS path aliases (@/, ~/, #/)\n• FastAPI, Flask, Django, Express, Next.js routes"]
    end

    subgraph Storage["3. Local Brain State (JSON on Disk)"]
        Graph["Typed Symbol Graph (graph.py)\n• Nodes: file, fn, cls, route, mod\n• Edges: calls, imports, inherits, handles, defines\n• Confidence tiers: exact, qualified, import_resolved, candidate"]
        Memory["AST-Anchored Repo Memory & Ledger (memory.py + ledger.py)\n• Stores invariants in .projectbrain_memory.json\n• Tracks cumulative token savings in .projectbrain_ledger.json"]
    end

    subgraph Compiler["4. Dossier Compiler & Surgical Patcher (slicer.py + patcher.py)"]
        Dossier["Single-Read Dossier Compiler\n• Module globals + smart-folded AST slices\n• 1-3 hop bidirectional call chains\n• Closed-World Exhaustive Repo Scan (<800 lines)"]
        Patcher["Surgical Anchor Patch Engine\n• @file, @sub, @replace, @insert_after, @delete\n• Tree-sitter syntax validation in <5ms"]
    end

    subgraph Agent["5. AI Agent CLI & MCP Interface (cli.py + server.py)"]
        MCP["CLI & MCP Power Tools\n1. brain_dossier / projectbrain dossier\n2. brain_patch / projectbrain patch\n3. brain_slice / projectbrain slice\n4. brain_stats / projectbrain stats\n5. brain_explore / brain_check / brain_remember"]
    end

    Files --> Walk
    Walk --> Parser
    Parser --> Resolver
    Resolver --> Graph
    Parser --> Memory
    Graph --> Dossier
    Memory --> Dossier
    Dossier --> MCP
    Patcher --> MCP
```
