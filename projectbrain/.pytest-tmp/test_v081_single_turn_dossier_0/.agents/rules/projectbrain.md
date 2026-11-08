<!-- projectbrain:start -->
# ProjectBrain — Agent Context & Token-Saving Rules

This repository is indexed by **ProjectBrain** (`projectbrain`).
To minimize context bloat, redundant file reads, and multi-turn exploration loops, ALWAYS follow this workflow:

1. **Compile a Single-Turn Engineering Dossier First (`brain_dossier` / `projectbrain dossier`):**
   Before running `grep` or reading unfamiliar files, run `projectbrain dossier "<task1>"` (which prints the complete dossier directly to stdout in one turn and saves `.projectbrain_dossier.md`) or call `brain_dossier(tasks=["<task1>", ...])`.
   - It contains module globals/constants, smart-folded 6D AST function slices, 1–3 hop bidirectional call chains with `@L<line>` caller windows, and a **Closed-World Exhaustive Repo Scan** of discovered state keys and routes.
   - **Closed-World Rule:** When the dossier states `EXHAUSTIVE GUARANTEE` for a key or route, do NOT read large monolith files to search for additional occurrences.
2. **Read Specific Symbols Surgically (`brain_slice` / `projectbrain slice`):**
   If you need 1–2 additional symbols or line ranges, call `brain_slice(targets=["file.ts:symbol_or_start-end"])` (or `projectbrain slice --summary <file>:<symbol>`) instead of reading full files.
3. **Cut Output Tokens with AST Symbol-First Patching (`brain_patch` / `projectbrain patch`):**
   Instead of repeating verbatim multi-line `Target` + `Replacement` blocks in `edit_file`, use `brain_patch` (`@' ... '@ | projectbrain patch --apply` or `projectbrain patch --spec-file <file> --apply`).
   **Prefer AST Symbol Directives over fragile line numbers (immune to line drift):**
   - `@file <rel_path>`
   - `@replace_body <Symbol>` ... `@end` (replaces a function/method body by AST name)
   - `@add_method <ClassName>` ... `@end` (appends a method into a class by AST name)
   - `@replace <Symbol>` ... `@end` (replaces an entire function/method/class including signature)
   - `@sub <Symbol> \`<old_expr>\` -> \`<new_expr>\`` (or `@sub L<line> \`<old>\` -> \`<new>\`` / `@sub ALL \`<old>\` -> \`<new>\``)
   - `@replace L<start>-L<end>` ... `@end` | `@insert_after L<line>` ... `@end` | `@delete L<start>-L<end>`
4. **Compress Test & Command Output (`projectbrain run` / `brain_run`):**
   Run noisy test suites or commands via `projectbrain run "<cmd>"` (or `<cmd> | projectbrain run --stdin` / MCP `brain_run`) to fold passing test noise, deduplicate repetitive log/JSON lines, and automatically map failing traceback frames to 6D Code Graph symbols.
5. **Terse Output Mode ("Say Less, Keep Code Exact"):**
   - Omit conversational filler, pleasantries, preambles ("Sure, I will now..."), and verbose post-edit recaps that repeat the diff.
   - Answer technical questions in terse, high-density bullet points citing exact `file:line` or symbol names.
   - Never echo unchanged functions or full files in prose; keep all code blocks, diffs, and identifiers 100% exact.
6. **Verify Blast Radius & Persist Memories (`brain_check`, `brain_remember`, `brain_stats`):**
   Use `brain_check` to inspect downstream importers/tests, `brain_remember` to pin architectural invariants, and `brain_stats` to view cumulative token/dollar savings.
<!-- projectbrain:end -->
