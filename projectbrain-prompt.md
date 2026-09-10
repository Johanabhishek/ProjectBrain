# Prompt for Claude Code

I want to build a Python pip package called `projectbrain` — a lightweight codebase intelligence layer that sits between AI coding agents and a project's file system. The goal is to eliminate blind file scanning by giving agents a pre-built, queryable map of the codebase so they only read the files they actually need.

## The problem it solves

AI coding agents (Cursor, Claude Code, Copilot, etc.) waste massive amounts of tokens and time doing blind file reads. A simple 5-line fix might trigger reading 40+ files because the agent doesn't know where things live or how they connect. This tool fixes that by indexing the codebase once and answering "which files matter for this task?" instantly.

This isn't just for debugging/error-style prompts ("why am I getting a 404 on /users"). It's for any task description. Some changes are trivial and an agent already handles them fine without help — e.g. "change this button's color" is pure CSS, the agent doesn't need to search anything. But for anything involving functionality — "add a discount code field to checkout," "the cart total isn't updating after a coupon is applied" — the agent often has to grep/search through a lot of files before it lands on the right one. ProjectBrain's job is to shortcut that: it should act like a teammate who already knows the codebase and tells the agent "hey, this is the file you're looking for, it's at this path, here's what it connects to" — instead of the agent discovering that after reading 15 wrong files first.

Routing to the right file/route is the core value. Anything else that saves the agent tokens (e.g. surfacing relevant git history/blame for a file, not just current-state structure) is a bonus on top of that, not a replacement for it — see "What to skip in v0" below for what's deferred.

## Core concept

The tool builds a graph-based index of a codebase — not just file names, but functions, classes, routes, imports, exports, and how they connect to each other. When queried with a natural language prompt (like "why am I getting a 404 on /users"), it returns a small, focused set of relevant file paths and line ranges instead of making the agent grep through everything.

## What I need from you

Generate a detailed implementation plan — not code yet, just the plan. Cover:

### 1. Project structure
- Python package layout (src structure, pyproject.toml, etc.)
- What goes where, keep it minimal

### 2. Indexing engine
- How to parse Python and JavaScript/TypeScript files into a graph
- What to extract from each file:
  - Functions, classes, methods (with line numbers)
  - Import/export relationships (edges in the graph)
  - Route definitions (Express routes, FastAPI/Flask routes, Next.js API routes)
  - Database model definitions (SQLAlchemy, Prisma, Mongoose, etc.)
- How to store the graph (start simple — consider NetworkX or a plain dict-based adjacency list, no external database)
- When/how to rebuild the index (on-demand for now, not file-watching)

### 3. Query engine
- How to go from a natural language prompt to the right nodes in the graph — and the prompt could be a bug report ("404 error on /users endpoint"), a feature request ("add a discount code field to checkout"), or any other task description. Don't special-case "errors" — treat all of these as the same kind of query.
- Start with keyword extraction + graph traversal (no LLM dependency in v0 — keep it self-contained)
- Fallback: if keyword matching fails, rank files by relevance using simple heuristics (recently modified, most connected in graph, filename matching)
- Output format: list of `{file_path, start_line, end_line, reason, connections}` objects — the "reason" matters here, since the point is to tell the agent *why* this file is the answer (e.g. "defines route /users", "imported by checkout.ts"), not just hand back a bare path

### 4. Public API design
Keep it dead simple. Something like:

```python
from projectbrain import ProjectBrain

brain = ProjectBrain("./my-project")
brain.index()  # builds the graph

# Natural language query — works for bugs AND feature/functionality tasks
context = brain.query("why am I getting a 404 on /users")
context = brain.query("add a discount code field to the checkout flow")
# Returns focused file paths, line ranges, and connection chains

# Direct lookup
context = brain.get_context_for_file("src/routes/users.ts")
# Returns the file + everything it connects to (controller, service, model)
```

### 5. What to skip in v0
- No file watching / live reindexing (manual `brain.index()` is fine)
- No git integration (Layer 3 — skip for now)
- No LLM dependency for querying (pure keyword + graph traversal)
- No VS Code extension
- No web UI
- No database (in-memory graph, optionally cached to a JSON file)

### 6. How to validate it works
- Suggest a simple test: index a sample project, run a few queries, compare the files returned vs what an agent would have blindly read
- How to measure token savings (files returned by projectbrain vs files an agent typically scans)

## Constraints
- Pure Python, minimal dependencies
- Must work on Python and JS/TS codebases as a starting point
- Should index a medium-sized project (500 files) in under 30 seconds
- Query response should be instant (under 100ms)
- The graph structure must capture relationships (imports, function calls, route→handler chains) — not just a flat file list. This is critical because Layer 2 (causality/dependency tracing) should emerge naturally from a well-structured Layer 1

## Tone
I'm an intern proving a concept. Keep the plan practical and buildable in 1-2 weeks. Don't over-engineer. Tell me what to build first, what to build second, and what to skip entirely.
