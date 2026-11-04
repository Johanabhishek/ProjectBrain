import argparse
import json
import sys
from pathlib import Path

from .brain import ProjectBrain

_AGENT_RULE_CONTENT = """\
# ProjectBrain — Agent Context & Token-Saving Rules

This repository is indexed by **ProjectBrain** (`projectbrain`).
To cut input tokens by 75–88% and output tokens by 65–80%, ALWAYS follow this workflow:

1. **Compile a Single-Read Engineering Dossier First (`brain_dossier` / `projectbrain dossier`):**
   Before running `grep` or reading unfamiliar files, call `brain_dossier(tasks=["<task1>", ...])` (or `projectbrain dossier "<task1>"`) and read `.projectbrain_dossier.md` in a single `read_file` call (`StartLine=1, EndLine=800`).
   - It contains module globals/constants, smart-folded AST function slices, 1–3 hop bidirectional call chains with `@L<line>` caller windows, and a **Closed-World Exhaustive Repo Scan** of discovered state keys and routes.
   - **Closed-World Rule:** When the dossier states `EXHAUSTIVE GUARANTEE` for a key or route, do NOT read large monolith files to search for additional occurrences.
2. **Read Specific Symbols Surgically (`brain_slice` / `projectbrain slice`):**
   If you need 1–2 additional symbols or line ranges, call `brain_slice(targets=["file.ts:symbol_or_start-end"])` instead of reading full files.
3. **Cut Output Tokens with Surgical Anchor Patching (`brain_patch` / `projectbrain patch`):**
   Instead of repeating verbatim multi-line `Target` + `Replacement` blocks in `edit_file` or writing verbose before/after code blocks, use `brain_patch` (`projectbrain patch --spec "..." [--apply]`):
   - `@file <rel_path>`
   - `@sub L<line> \\`<old_expr>\\` -> \\`<new_expr>\\`` (or `@sub ALL \\`<old>\\` -> \\`<new>\\`` / `@sub <symbol> \\`<old>\\` -> \\`<new>\\``)
   - `@replace L<start>-L<end>` ... `@end`
   - `@insert_after L<line>` / `@insert_before L<line>` ... `@end`
   - `@delete L<start>-L<end>`
4. **Verify Blast Radius & Persist Memories (`brain_check`, `brain_remember`, `brain_stats`):**
   Use `brain_check` to inspect downstream importers/tests, `brain_remember` to pin architectural invariants, and `brain_stats` to view cumulative token/dollar savings.
"""


def _print_query_summary(result: dict) -> None:
    results = result.get("results", [])
    if not results:
        print("No matching files.")
        return

    print("Top files:")
    for i, r in enumerate(results, start=1):
        print(f"{i}. {r['rel_path']}")
        print(f"   Reason: {r['reason']}")

        conn_parts = []
        for c in r.get("connections", []):
            verb = "imports" if c["relation"] == "imports" else "used by"
            conn_parts.append(f"{verb} {c['rel_path']}")
        if conn_parts:
            print(f"   Connections: {', '.join(conn_parts)}")
        print()


def _print_context_summary(ctx: dict) -> None:
    if not ctx:
        print("No indexed file matched that path.")
        return

    print(ctx.get("rel_path", ctx.get("file_path", "?")))

    fns = [f["name"] for f in ctx.get("functions", [])]
    if fns:
        print(f"  Functions: {', '.join(fns)}")

    clss = [c["name"] for c in ctx.get("classes", [])]
    if clss:
        print(f"  Classes: {', '.join(clss)}")

    for r in ctx.get("routes", []):
        print(f"  Route: {r['method']} {r['path']}")

    if ctx.get("imports_resolved"):
        print(f"  Imports: {', '.join(ctx['imports_resolved'])}")

    if ctx.get("imported_by"):
        print(f"  Imported by: {', '.join(ctx['imported_by'])}")


def _print_compiled_summary(packet: dict) -> None:
    telem = packet.get("resolution_telemetry", {})
    print(f"Task: {packet.get('task')}  [intent: {packet.get('intent', {}).get('intent')}]")
    print(
        f"Resolution: {telem.get('target_slices_count', 0)} AST slices, "
        f"{telem.get('neighbor_skeletons_count', 0)} neighbor skeletons across "
        f"{telem.get('files_resolved', 0)} files\n"
    )
    if packet.get("memories"):
        print("=== Active Repo Memories ===")
        for m in packet["memories"]:
            stale_str = " [STALE WARNING]" if m.get("stale") else ""
            print(f"  * [{m.get('target_id')}]{stale_str}: {m.get('note')}")
        print()

    print("=== Tier 1: Target AST Code Slices ===")
    for s in packet.get("target_slices", []):
        print(f"--- {s['rel_path']} :: {s['symbol']} ({s['lines']}) [{s.get('reason')}] ---")
        print(s["code"])
        print()

    print("=== Tier 2: Neighbor Signature Skeletons ===")
    for sk in packet.get("neighbor_skeletons", []):
        print(f"--- {sk['rel_path']} ({sk['reason']}) ---")
        print(sk["skeleton"])
        print()

    if packet.get("related_tests"):
        print(f"Related Tests: {', '.join(packet['related_tests'])}")


def _print_stats_summary(st: dict) -> None:
    res = st.get("resolution", {})
    print("===============================================================")
    print("  ProjectBrain Structural & Epistemic Telemetry")
    print("===============================================================")
    print(f"  Repository                : {st.get('repo', st.get('root'))}")
    print(f"  Indexed Files / Symbols   : {st.get('files', 0)} files, {st.get('functions', 0)} functions, {st.get('classes', 0)} classes")
    print(f"  4D Coordinate Cells       : {st.get('cells_4d', 0)} cells")
    print(f"  Total Session Operations  : {st.get('total_operations', 0)}")
    print(f"  AST Slices Compiled       : {res.get('ast_slices_compiled', 0)}")
    print(f"  4D Cells Resolved         : {res.get('cells_4d_resolved', 0)}")
    print(f"  Closed-World Proofs       : {res.get('closed_world_verifications', 0)}")
    print(f"  Anchor Patch Ops Applied  : {res.get('patch_ops_applied', 0)}")
    print(f"  Files in Working Memory   : {res.get('files_in_working_memory', 0)}")
    print("===============================================================")


_CURSOR_MDC_FRONTMATTER = """\
---
description: ProjectBrain Mandatory Codebase Investigation & Token-Saving Rules
globs: *
alwaysApply: true
---
"""


def _write_or_merge_json(file_path: Path, top_key: str, server_name: str, server_cfg: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {top_key: {}}
    if file_path.exists():
        try:
            loaded = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
                data.setdefault(top_key, {})
        except json.JSONDecodeError:
            pass
    data[top_key][server_name] = server_cfg
    file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_or_append_rule(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8", errors="ignore")
        if "ProjectBrain" not in existing:
            suffix = "\n\n" if not existing.endswith("\n") else "\n"
            file_path.write_text(existing + suffix + content, encoding="utf-8")
    else:
        file_path.write_text(content, encoding="utf-8")


def _run_init(
    root_path: Path,
    write_mcp: bool = True,
    write_rules: bool = True,
    ide: str = "all",
) -> dict:
    """One-command setup: index repo, update .gitignore, wire MCP config, and write agent rules."""
    brain = ProjectBrain(str(root_path))
    stats = brain.index()

    actions: list[str] = ["indexed_repository"]
    resolved_root = str(root_path.resolve())

    # 1. Update .gitignore to ignore cache & graph files while keeping memory.json tracked
    gitignore = root_path / ".gitignore"
    ignore_entries = [
        ".projectbrain.json",
        ".projectbrain_graph.json",
        ".projectbrain.db",
        ".projectbrain.db-wal",
        ".projectbrain.db-shm",
        ".projectbrain_dossier.md",
        ".projectbrain_patch.diff",
        ".projectbrain_ledger.json",
    ]
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8", errors="ignore")
        missing = [e for e in ignore_entries if e not in content]
        if missing:
            suffix = "\n" if not content.endswith("\n") else ""
            gitignore.write_text(
                content + suffix + "# ProjectBrain cache\n" + "\n".join(missing) + "\n",
                encoding="utf-8",
            )
            actions.append("updated_.gitignore")
    else:
        gitignore.write_text(
            "# ProjectBrain cache\n" + "\n".join(ignore_entries) + "\n",
            encoding="utf-8",
        )
        actions.append("created_.gitignore")

    # 2. Write MCP configurations (.mcp.json, .cursor/mcp.json, .vscode/mcp.json)
    if write_mcp:
        stdio_cfg = {
            "command": "projectbrain-mcp",
            "args": [resolved_root],
        }
        if ide in ("all", "claude", "antigravity"):
            _write_or_merge_json(root_path / ".mcp.json", "mcpServers", "projectbrain", stdio_cfg)
            actions.append("configured_.mcp.json")
        if ide in ("all", "cursor"):
            _write_or_merge_json(root_path / ".cursor" / "mcp.json", "mcpServers", "projectbrain", stdio_cfg)
            actions.append("configured_.cursor/mcp.json")
        if ide in ("all", "vscode"):
            vscode_cfg = {
                "type": "stdio",
                "command": "projectbrain-mcp",
                "args": [resolved_root],
            }
            _write_or_merge_json(root_path / ".vscode" / "mcp.json", "servers", "projectbrain", vscode_cfg)
            actions.append("configured_.vscode/mcp.json")

    # 3. Write Agent Rule files across IDEs (Antigravity, Claude Code, Cursor, VS Code Copilot/Cline)
    if write_rules:
        agents_md = root_path / "AGENTS.md"
        agents_md.write_text(_AGENT_RULE_CONTENT, encoding="utf-8")
        actions.append("wrote_AGENTS.md")

        if ide in ("all", "antigravity"):
            rule_file = root_path / ".agents" / "rules" / "projectbrain.md"
            rule_file.parent.mkdir(parents=True, exist_ok=True)
            rule_file.write_text(_AGENT_RULE_CONTENT, encoding="utf-8")
            actions.append("wrote_.agents/rules/projectbrain.md")

        if ide in ("all", "claude"):
            _write_or_append_rule(root_path / "CLAUDE.md", _AGENT_RULE_CONTENT)
            actions.append("wrote_CLAUDE.md")

        if ide in ("all", "cursor"):
            cursor_rule = root_path / ".cursor" / "rules" / "projectbrain.mdc"
            cursor_rule.parent.mkdir(parents=True, exist_ok=True)
            cursor_rule.write_text(_CURSOR_MDC_FRONTMATTER + _AGENT_RULE_CONTENT, encoding="utf-8")
            actions.append("wrote_.cursor/rules/projectbrain.mdc")

        if ide in ("all", "vscode"):
            _write_or_append_rule(root_path / ".github" / "copilot-instructions.md", _AGENT_RULE_CONTENT)
            _write_or_append_rule(root_path / ".clinerules", _AGENT_RULE_CONTENT)
            actions.append("wrote_.github/copilot-instructions.md_and_.clinerules")

    return {
        "status": "initialized",
        "root": resolved_root,
        "ide": ide,
        "actions": actions,
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="projectbrain")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="One-command setup: index, .gitignore, MCP configs, and agent rules for Antigravity, Claude, Cursor, and VS Code")
    init_parser.add_argument("--no-mcp", action="store_true", help="Skip writing MCP configs (.mcp.json, .cursor/mcp.json, .vscode/mcp.json)")
    init_parser.add_argument("--no-rules", action="store_true", help="Skip writing agent rule files")
    init_parser.add_argument(
        "--ide",
        choices=["all", "antigravity", "claude", "cursor", "vscode"],
        default="all",
        help="Target IDE/agent environment to configure (default: all)",
    )
    sub.add_parser("index", help="Build and cache the index for the project")

    compile_parser = sub.add_parser("compile", help="Compile surgical AST slices + neighbor skeletons for a task")
    compile_parser.add_argument("prompt", help="Natural language task description")
    compile_parser.add_argument("--budget", type=int, default=3000, help="Token budget (default: 3000)")
    compile_parser.add_argument("--top-n", type=int, default=4)
    compile_group = compile_parser.add_mutually_exclusive_group()
    compile_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    compile_group.add_argument("--summary", action="store_true", help="Human-readable compiled view")

    dossier_parser = sub.add_parser(
        "dossier",
        help="Compile a self-contained multi-task Engineering Dossier to .projectbrain_dossier.md",
    )
    dossier_parser.add_argument("tasks", nargs="+", help="One or more task descriptions or engineering questions")
    dossier_parser.add_argument("--impact", nargs="*", default=None, help="Optional file paths for blast-radius analysis")
    dossier_parser.add_argument("--out", default=None, help="Output markdown path (default: <root>/.projectbrain_dossier.md)")
    dossier_parser.add_argument("--zoom", action="store_true", help="Pure 4D Surgical Zoom mode (omits globals, neighbor bodies, and repo-wide scans)")
    dossier_parser.add_argument("--stdout", action="store_true", help="Print compiled markdown directly to stdout in one call")
    dossier_parser.add_argument("--summary", action="store_true", help="Print compiled markdown directly to stdout in one call")
    patch_parser = sub.add_parser(
        "patch",
        help="Apply or dry-run a Surgical Anchor Patch spec to cut output tokens (75-90 percent reduction)",
    )
    patch_parser.add_argument("--spec", default=None, help="Anchor Patch DSL string or JSON array")
    patch_parser.add_argument("--spec-file", default=None, help="Path to a file containing the Anchor Patch DSL")
    patch_parser.add_argument("--apply", action="store_true", help="Write verified patches directly to disk (default: dry-run diff)")
    patch_parser.add_argument("--diff-out", default=None, help="Path to write unified git diff (default: <root>/.projectbrain_patch.diff)")
    patch_parser.add_argument("--include-diff", action="store_true", help="Include full unified diff string in stdout JSON")

    slice_parser = sub.add_parser("slice", help="Surgically slice specific symbols, line ranges, or file skeletons")
    slice_parser.add_argument("targets", nargs="+", help="Symbols (e.g. brain.py:verify_claim), node IDs, or files")
    slice_parser.add_argument("--max-lines", type=int, default=120, help="Max lines per symbol (default: 120)")
    slice_parser.add_argument("--summary", action="store_true", help="Human-readable code output")

    explore_parser = sub.add_parser("explore", help="Explore symbol neighborhood, trace route flow, or find path between A and B")
    explore_parser.add_argument("target", help="Symbol name, node ID, or route URL")
    explore_parser.add_argument("target_b", nargs="?", default=None, help="Optional second symbol to find shortest path to")
    explore_parser.add_argument("--depth", type=int, default=2)

    impact_parser = sub.add_parser("impact", help="Compute blast radius (direct consumers, test consumers, callers) for files")
    impact_parser.add_argument("files", nargs="+", help="File paths to analyze")

    remember_parser = sub.add_parser("remember", help="Pin an architectural note to a symbol or file")
    remember_parser.add_argument("target", help="Symbol name, file path, or node ID")
    remember_parser.add_argument("note", help="Architectural invariant or note to remember")

    query_parser = sub.add_parser("query", help="Find files relevant to a task description")
    query_parser.add_argument("prompt", help="Natural language bug report or feature task")
    query_parser.add_argument("--top-n", type=int, default=10)
    query_group = query_parser.add_mutually_exclusive_group()
    query_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    query_group.add_argument("--summary", action="store_true", help="Human-readable summary")

    context_parser = sub.add_parser("context", help="Get a file plus its 1-hop import chain")
    context_parser.add_argument("file", help="File path, relative to --path or absolute")
    context_group = context_parser.add_mutually_exclusive_group()
    context_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    context_group.add_argument("--summary", action="store_true", help="Human-readable summary")

    verify_parser = sub.add_parser(
        "verify", help="Fact-check a claim: <subject> <relation> <object>")
    verify_parser.add_argument("subject", help="Function, class, file, or route")
    verify_parser.add_argument(
        "relation",
        help="calls | reaches | inherits | has_method | defines | imports | handles")
    verify_parser.add_argument("object", help="The name the relation targets")

    sub.add_parser("health", help="Report index coverage and blind spots")

    stats_parser = sub.add_parser("stats", help="Display index topology and structural resolution telemetry")
    stats_parser.add_argument("--summary", action="store_true", help="Print human-readable structural telemetry dashboard")
    stats_parser.add_argument("--reset", action="store_true", help="Reset session working memory and resolution ledger")

    args = parser.parse_args(argv)

    if args.command == "init":
        res = _run_init(
            Path(args.path),
            write_mcp=not args.no_mcp,
            write_rules=not args.no_rules,
            ide=args.ide,
        )
        print(json.dumps(res, indent=2))
        return 0

    brain = ProjectBrain(args.path)

    if args.command == "index":
        print(json.dumps(brain.index(), indent=2))
        return 0

    if args.command == "compile":
        packet = brain.compile_context(args.prompt, budget_tokens=args.budget, top_n=args.top_n)
        if args.summary:
            _print_compiled_summary(packet)
        else:
            print(json.dumps(packet, indent=2))
        return 0

    if args.command == "dossier":
        res = brain.compile_dossier(args.tasks, impact_files=args.impact, out_path=args.out, zoom=bool(args.zoom))
        if args.stdout or args.summary:
            print(res.get("markdown", ""))
            return 0
        summary = dict(res) if args.zoom else {k: v for k, v in res.items() if k != "markdown"}
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "patch":
        spec_str = args.spec
        if not spec_str and args.spec_file:
            spec_str = Path(args.spec_file).read_text(encoding="utf-8")
        if not spec_str and not sys.stdin.isatty():
            spec_str = sys.stdin.read()
        if not spec_str:
            print(json.dumps({"error": "Provide --spec, --spec-file, or pipe patch spec via stdin"}))
            return 1
        res = brain.patch(spec_str, apply=args.apply, diff_out=args.diff_out)
        out = res if args.include_diff else {k: v for k, v in res.items() if k != "unified_diff"}
        print(json.dumps(out, indent=2))
        return 0

    if args.command == "slice":
        res = brain.slice_symbols(args.targets, max_lines_per_symbol=args.max_lines, summary_mode=bool(args.summary))
        if args.summary:
            for s in res.get("slices", []):
                if s.get("error"):
                    print(f"[ERROR] {s['error']}")
                else:
                    print(f"=== {s.get('rel_path')} :: {s.get('symbol')} ({s.get('lines', 'skeleton')}) ===")
                    print(s.get("code") or s.get("skeleton") or "")
                    print()
        else:
            print(json.dumps(res, indent=2))
        return 0

    if args.command == "explore":
        print(json.dumps(brain.explore(args.target, target_b=args.target_b, depth=args.depth), indent=2))
        return 0

    if args.command == "impact":
        print(json.dumps(brain.impact(args.files), indent=2))
        return 0

    if args.command == "remember":
        print(json.dumps(brain.remember(args.target, args.note), indent=2))
        return 0

    if args.command == "query":
        result = brain.query_with_graph(args.prompt, top_n=args.top_n)
        if args.summary:
            _print_query_summary(result)
        else:
            print(json.dumps(result, indent=2))
        return 0

    if args.command == "context":
        ctx = brain.get_context_for_file(args.file)
        if args.summary:
            _print_context_summary(ctx)
        else:
            print(json.dumps(ctx, indent=2))
        return 0

    if args.command == "verify":
        print(json.dumps(
            brain.verify_claim(args.subject, args.relation, args.object), indent=2))
        return 0

    if args.command == "health":
        print(json.dumps(brain.health(), indent=2))
        return 0

    if args.command == "stats":
        st = brain.stats(reset=args.reset)
        if args.summary and not args.reset:
            _print_stats_summary(st)
        else:
            print(json.dumps(st, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
