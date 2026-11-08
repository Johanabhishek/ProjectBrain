# ProjectBrain — Workspace Scientific Integrity & Agent Rules

1. **Zero Overfitting / Pure Computer Science Mandate:**
   - Do **NOT** curate, hardcode, or bias ProjectBrain toward any local project on this machine (`Saral-core`, `saralvidhya-mvp`, etc.) or any specific test fixture.
   - All indexing, graph construction, slicing, and static verification in `src/projectbrain/` must be grounded in **true, generalizable computer science and static program analysis** (AST/Tree-sitter structural analysis, general dataflow/control-flow slicing, general type/shape lattice checking, and schema/migration state-machine folding) rather than brittle regexes or scenario-specific heuristics.
   - Always audit new features critically for false positives, narrow regex assumptions, or benchmark-specific overfitting before claiming generality.

2. **Dogfooding Workflow:**
   - Always use `projectbrain dossier`, `projectbrain slice`, and `projectbrain patch` (`@' ... '@ | projectbrain --path "d:\projects\Brain\projectbrain" patch --apply`) when investigating and editing this repository.
