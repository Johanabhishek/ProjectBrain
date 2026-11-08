# ProjectBrain — Agent Context & Token-Saving Rules

This repository is indexed by **ProjectBrain** (`projectbrain`).
To avoid burning context tokens on blind `grep` loops and full-file `view_file` reads, follow this workflow:

1. **Compile Context First (`brain_context` / `projectbrain compile`):**
   Before opening unfamiliar files for a task, call `brain_context(task="<your task>")`.
   It returns the **exact AST source code slices** of target functions/classes plus **signature-only skeletons** (parameters, return types, class schemas) of 1-hop dependencies in a single call.
2. **Read Symbols Surgically (`brain_slice` / `projectbrain slice`):**
   When you need to inspect a specific function, method, or class in a large file, call `brain_slice(targets=["file.py:symbol_name"])` instead of reading the entire file.
3. **Navigate Relationships (`brain_explore`):**
   Use `brain_explore(target="A", target_b="B")` to trace multi-hop call/import chains or `brain_explore(target="/api/route")` to trace frontend-to-backend request flows.
4. **Verify Before & After Edits (`brain_check`):**
   - Before editing multiple files, call `brain_check(plan_files=[...], task="...")` to catch missing connected files or tests.
   - After modifying signatures or models, call `brain_check(files_modified=[...])` to inspect downstream callers and affected tests.
5. **Persist Architectural Insights (`brain_remember`):**
   When you discover a non-obvious architectural invariant or bug root-cause, call `brain_remember(target="symbol_or_file", note="...")` so future sessions and subagents inherit that knowledge automatically.
