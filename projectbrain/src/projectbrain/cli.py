from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_AGENT_RULE_CONTENT = """\
# ProjectBrain — Code Graph & Blast-Radius Guardrail Rules

This repository is indexed by **ProjectBrain** (`projectbrain`). Use it alongside native tools:

1. **When to Use Native Tools vs. ProjectBrain (`brain_dossier` / `brain_explore`):**
   Use native `rg` and `read_file` for exact literal searches and normal file reads. Reach for `brain_dossier(tasks=["<question_or_bug>"])` (`projectbrain dossier`) or `brain_explore` (`projectbrain explore`) when tracing unfamiliar multi-file call chains, `/api/...` HTTP routes, or `co_changes`.
2. **Surgical Symbol & Variable Slicing (`brain_slice` / `projectbrain slice`):**
   When inspecting specific functions, class skeletons, or variable flows inside large files, batch targets into `brain_slice(targets=["file.py:Class.method"], focus="target_var")` (`projectbrain slice`).
3. **Edit Natively + Verify Blast Radius (`brain_check`):**
   Edit files with native editing tools (`Edit` / `edit_file`). Post-edit guardrail hooks automatically check syntax, layer boundaries, and pinned invariants. Call `brain_check` (`projectbrain check`) to inspect downstream callers or verify multi-file changes.
4. **Architectural Invariants (`brain_remember`) & Test Log Compression (`projectbrain run`):**
   Pin function invariants (`brain_remember(target="Symbol", note="...")`) or layer rules (`forbid="routes/* -> db/*"`). Use `projectbrain run "<cmd>"` when you want noisy test output folded to failing traceback symbols.
"""

_CURSOR_MDC_FRONTMATTER = """\
---
description: ProjectBrain Zero-LSP Code Graph & Blast-Radius Guardrail Rules
globs: *
alwaysApply: true
---
"""

_RULE_START_MARKER = "<!-- projectbrain:start -->"
_RULE_END_MARKER = "<!-- projectbrain:end -->"
# Exact first line of rule files written (without markers) by pre-1.0 `projectbrain init`.
_LEGACY_GENERATED_HEADER = "# ProjectBrain — Zero-LSP Code Graph & Blast-Radius Guardrail Rules"


def _load_brain_cls():
    from .brain import ProjectBrain
    return ProjectBrain


def _resolve_mcp_server_command(resolved_root: str, *, portable: bool = False) -> tuple[str, list[str]]:
    """Resolve `projectbrain-mcp` to an absolute path (or OS-portable command when `portable=True`)."""
    if portable:
        return "projectbrain-mcp", ["--lean", "."]
    import shutil
    which_mcp = shutil.which("projectbrain-mcp")
    if which_mcp:
        return str(Path(which_mcp).resolve()), ["--lean", resolved_root]
    return sys.executable, ["-m", "projectbrain.server", "--lean", resolved_root]


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
    ic = st.get("interceptions", {})
    print("===============================================================")
    print("  ProjectBrain Guardrail & Code-Graph Scorecard")
    print("===============================================================")
    print(f"  Repository                  : {st.get('repo', st.get('root'))}")
    print(f"  Indexed Topology            : {st.get('files', 0)} files | {st.get('functions', 0)} functions | {st.get('classes', 0)} classes")
    print(
        f"  Pinned Repo Governance      : {st.get('memories', 0)} AST invariants | "
        f"{st.get('boundary_rules', 0)} boundary rules | {st.get('co_change_rules', 0)} co-change rules"
    )
    print("---------------------------------------------------------------")
    print(f"  Guardrail Audits Run        : {ic.get('audits_run', 0)} ({ic.get('clean_edits_audited', 0)} clean silent edits)")
    print(f"  Total Regressions Caught    : {ic.get('total_interceptions', 0)}")
    print(f"    - Syntax Errors Caught    : {ic.get('syntax_errors_caught', 0)}")
    print(f"    - Contract Breaks Caught  : {ic.get('contract_violations_caught', 0)}")
    print(f"    - Layer Boundary Blocks   : {ic.get('boundary_violations_caught', 0)}")
    print(f"    - Signature Blast Alerts  : {ic.get('signature_blast_alerts', 0)}")
    print(f"    - Stale Invariants Flagged: {ic.get('stale_invariants_flagged', 0)}")
    print("---------------------------------------------------------------")
    print(f"  AST Slices / Dossiers Served: {res.get('ast_slices_compiled', 0)} slices across {res.get('files_resolved_total', 0)} files")
    print(f"  Closed-World Proofs         : {res.get('closed_world_scans_verified', res.get('closed_world_verifications', 0))}")
    print("===============================================================")


def format_check_report(res: dict, fmt: str = "text") -> str:
    """Format `brain.check()` output as `text`, `json`, `markdown`, `pr-comment`, `github`, or `sarif`."""
    from .reporters import format_check_report as _fmt_check_report

    return _fmt_check_report(res, fmt=fmt)


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
    """Safely insert or update the ProjectBrain rule section without overwriting user content."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    block = f"{_RULE_START_MARKER}\n{content.strip()}\n{_RULE_END_MARKER}\n"
    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8", errors="ignore")
        if _RULE_START_MARKER in existing and _RULE_END_MARKER in existing:
            s_idx = existing.index(_RULE_START_MARKER)
            e_idx = existing.index(_RULE_END_MARKER) + len(_RULE_END_MARKER)
            if e_idx < len(existing) and existing[e_idx:e_idx + 1] == "\n":
                e_idx += 1
            updated = existing[:s_idx] + block + existing[e_idx:]
            file_path.write_text(updated, encoding="utf-8")
        elif existing.lstrip().startswith(_LEGACY_GENERATED_HEADER):
            # Legacy unmarked file generated wholesale by an older `projectbrain init`: safe to replace.
            file_path.write_text(block, encoding="utf-8")
        else:
            # Any other existing content (including user headings that mention ProjectBrain) is preserved.
            suffix = "\n\n" if (existing and not existing.endswith("\n")) else ("\n" if existing else "")
            file_path.write_text(existing + suffix + block, encoding="utf-8")
    else:
        file_path.write_text(block, encoding="utf-8")


def _run_init(
    root_path: Path,
    write_mcp: bool = True,
    write_rules: bool = True,
    ide: str = "auto",
    write_hooks: bool = True,
    write_git_hook: bool = False,
    *,
    team: bool = False,
    github_action: bool = False,
    no_github_action: bool = False,
    learn: bool = False,
) -> dict:
    """One-command setup: index repo, update .gitignore, wire MCP config, rules, active hooks, CI workflow, and governance."""
    ProjectBrain = _load_brain_cls()
    brain = ProjectBrain(str(root_path))
    stats = brain.index()

    actions: list[str] = ["indexed_repository"]
    resolved_root = str(root_path.resolve())
    has_cursor = (root_path / ".cursor").exists()
    has_vscode = (root_path / ".vscode").exists()
    has_antigravity = (root_path / ".agents").exists()
    has_claude = (root_path / ".claude").exists() or (root_path / "CLAUDE.md").exists()

    # 1. Update .gitignore to ignore cache & local hooks while keeping .projectbrain_memory.json tracked
    gitignore = root_path / ".gitignore"
    ignore_entries = [
        ".projectbrain.json",
        ".projectbrain_graph.json",
        ".projectbrain.db",
        ".projectbrain.db-wal",
        ".projectbrain.db-shm",
        ".projectbrain_dossier.md",
        ".projectbrain_slice.md",
        ".projectbrain_patch.diff",
        ".projectbrain_ledger.json",
        ".claude/settings.local.json",
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

    # 2. Write MCP configurations (ENOENT-proof local paths by default; OS-portable when `team=True`)
    if write_mcp:
        mcp_cmd, mcp_args = _resolve_mcp_server_command(resolved_root, portable=team)
        stdio_cfg = {
            "command": mcp_cmd,
            "args": mcp_args,
        }
        if ide in ("auto", "all", "claude", "antigravity"):
            _write_or_merge_json(root_path / ".mcp.json", "mcpServers", "projectbrain", stdio_cfg)
            actions.append("configured_.mcp.json")
        if ide in ("all", "cursor") or (ide == "auto" and (has_cursor or team)):
            _write_or_merge_json(root_path / ".cursor" / "mcp.json", "mcpServers", "projectbrain", stdio_cfg)
            actions.append("configured_.cursor/mcp.json")
        if ide in ("all", "vscode") or (ide == "auto" and has_vscode):
            vscode_cfg = {
                "type": "stdio",
                "command": mcp_cmd,
                "args": mcp_args,
            }
            _write_or_merge_json(root_path / ".vscode" / "mcp.json", "servers", "projectbrain", vscode_cfg)
            actions.append("configured_.vscode/mcp.json")

    # 3. Write concise Agent Rule files
    if write_rules:
        _write_or_append_rule(root_path / "AGENTS.md", _AGENT_RULE_CONTENT)
        actions.append("wrote_AGENTS.md")

        if ide in ("all", "antigravity") or (ide == "auto" and has_antigravity):
            rule_file = root_path / ".agents" / "rules" / "projectbrain.md"
            _write_or_append_rule(rule_file, _AGENT_RULE_CONTENT)
            actions.append("wrote_.agents/rules/projectbrain.md")

        if ide in ("all", "claude") or (ide == "auto" and (has_claude or team)):
            _write_or_append_rule(root_path / "CLAUDE.md", _AGENT_RULE_CONTENT)
            actions.append("wrote_CLAUDE.md")

        if ide in ("all", "cursor") or (ide == "auto" and (has_cursor or team)):
            cursor_rule = root_path / ".cursor" / "rules" / "projectbrain.mdc"
            cursor_rule.parent.mkdir(parents=True, exist_ok=True)
            if cursor_rule.exists():
                _write_or_append_rule(cursor_rule, _AGENT_RULE_CONTENT)
            else:
                cursor_rule.write_text(
                    _CURSOR_MDC_FRONTMATTER
                    + f"{_RULE_START_MARKER}\n{_AGENT_RULE_CONTENT.strip()}\n{_RULE_END_MARKER}\n",
                    encoding="utf-8",
                )
            actions.append("wrote_.cursor/rules/projectbrain.mdc")

        if ide in ("all", "vscode") or (ide == "auto" and has_vscode):
            _write_or_append_rule(root_path / ".github" / "copilot-instructions.md", _AGENT_RULE_CONTENT)
            _write_or_append_rule(root_path / ".clinerules", _AGENT_RULE_CONTENT)
            actions.append("wrote_.github/copilot-instructions.md_and_.clinerules")

    # 4. Install Real-Time Active Post-Edit Guardrail Hooks (ON by default)
    if write_hooks or write_git_hook:
        from .hooks import install_git_pre_commit_hook, merge_claude_hooks, merge_cursor_hooks
        if write_hooks:
            if team:
                if ide in ("auto", "all", "claude"):
                    merge_claude_hooks(
                        root_path / ".claude" / "settings.json",
                        resolved_root,
                        portable=True,
                    )
                    actions.append("wrote_.claude/settings.json_hooks")
                if ide in ("auto", "all", "cursor"):
                    merge_cursor_hooks(
                        root_path / ".cursor" / "hooks.json",
                        resolved_root,
                        portable=True,
                    )
                    actions.append("wrote_.cursor/hooks.json")
            else:
                if ide in ("auto", "all", "claude"):
                    merge_claude_hooks(root_path / ".claude" / "settings.local.json", resolved_root)
                    actions.append("wrote_.claude/settings.local.json_hooks")
                if ide in ("all", "cursor") or (ide == "auto" and has_cursor):
                    merge_cursor_hooks(root_path / ".cursor" / "hooks.json", resolved_root)
                    actions.append("wrote_.cursor/hooks.json")

        if write_git_hook:
            if install_git_pre_commit_hook(root_path, resolved_root, portable=team):
                actions.append("installed_.git/hooks/pre-commit")

    # 5. Optional GitHub Actions PR Guardrail Workflow (`--team` or `--github-action`)
    if github_action or (team and not no_github_action):
        from .hooks import write_github_action_workflow
        write_github_action_workflow(root_path)
        actions.append("wrote_.github/workflows/projectbrain.yml")

    # 6. Optional Day-1 Governance Learning (`--learn`)
    learned_gov: dict | None = None
    if learn:
        learned_gov = brain.learn(apply=True)
        stats = brain.stats()
        actions.append("learned_governance")

    out = {
        "status": "initialized",
        "root": resolved_root,
        "ide": ide,
        "team": bool(team),
        "actions": actions,
        "stats": stats,
    }
    if learned_gov is not None:
        out["learned_governance"] = learned_gov
    return out


def _strip_rule_block(file_path: Path) -> bool:
    """Remove the `<!-- projectbrain:start -->...<!-- projectbrain:end -->` section from `file_path`."""
    if not file_path.exists():
        return False
    try:
        existing = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if _RULE_START_MARKER in existing and _RULE_END_MARKER in existing:
        s_idx = existing.index(_RULE_START_MARKER)
        e_idx = existing.index(_RULE_END_MARKER) + len(_RULE_END_MARKER)
        if e_idx < len(existing) and existing[e_idx:e_idx + 1] == "\n":
            e_idx += 1
        updated = (existing[:s_idx].rstrip() + "\n" + existing[e_idx:].lstrip()).strip()
        if not updated or updated == _CURSOR_MDC_FRONTMATTER.strip():
            file_path.unlink(missing_ok=True)
        else:
            file_path.write_text(updated + "\n", encoding="utf-8")
        return True
    # Never delete or rewrite files that do not contain ProjectBrain's own generated markers.
    return False


def _remove_mcp_entry(file_path: Path, top_key: str, server_name: str = "projectbrain") -> bool:
    if not file_path.exists():
        return False
    try:
        data = json.loads(file_path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get(top_key), dict):
        return False
    if server_name not in data[top_key]:
        return False
    data[top_key].pop(server_name, None)
    if not data[top_key] and len(data) == 1:
        file_path.unlink(missing_ok=True)
    else:
        file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _run_clean(root_path: Path, remove_memory: bool = False) -> dict:
    """One-command teardown: remove ProjectBrain caches, MCP entries, rule blocks, and hooks."""
    from .hooks import remove_projectbrain_hooks

    resolved_root = str(root_path.resolve())
    actions: list[str] = []

    cache_files = [
        ".projectbrain.db",
        ".projectbrain.db-wal",
        ".projectbrain.db-shm",
        ".projectbrain.json",
        ".projectbrain_graph.json",
        ".projectbrain_ledger.json",
        ".projectbrain_dossier.md",
        ".projectbrain_slice.md",
        ".projectbrain_patch.diff",
    ]
    if remove_memory:
        cache_files.append(".projectbrain_memory.json")

    for cf in cache_files:
        p = root_path / cf
        if p.exists():
            try:
                p.unlink(missing_ok=True)
                actions.append(f"removed_{cf}")
            except OSError:
                pass

    for mcp_rel, top_key in (
        (".mcp.json", "mcpServers"),
        (".cursor/mcp.json", "mcpServers"),
        (".vscode/mcp.json", "servers"),
    ):
        if _remove_mcp_entry(root_path / mcp_rel, top_key):
            actions.append(f"cleaned_{mcp_rel}")

    for rule_rel in (
        "AGENTS.md",
        "CLAUDE.md",
        ".agents/rules/projectbrain.md",
        ".cursor/rules/projectbrain.mdc",
        ".github/copilot-instructions.md",
        ".clinerules",
    ):
        if _strip_rule_block(root_path / rule_rel):
            actions.append(f"cleaned_{rule_rel}")

    actions.extend(remove_projectbrain_hooks(root_path))

    gitignore = root_path / ".gitignore"
    if gitignore.exists():
        try:
            gi_txt = gitignore.read_text(encoding="utf-8", errors="ignore")
            pb_entries = {
                ".projectbrain.json",
                ".projectbrain_graph.json",
                ".projectbrain.db",
                ".projectbrain.db-wal",
                ".projectbrain.db-shm",
                ".projectbrain_dossier.md",
                ".projectbrain_slice.md",
                ".projectbrain_patch.diff",
                ".projectbrain_ledger.json",
                ".claude/settings.local.json",
            }
            # Only remove the contiguous block that `init` appended under its own header,
            # so entries the user wrote elsewhere in `.gitignore` are preserved.
            gi_lines = gi_txt.splitlines()
            kept: list[str] = []
            idx = 0
            removed_any = False
            while idx < len(gi_lines):
                if gi_lines[idx].strip() == "# ProjectBrain cache":
                    removed_any = True
                    idx += 1
                    while idx < len(gi_lines) and gi_lines[idx].strip() in pb_entries:
                        idx += 1
                    continue
                kept.append(gi_lines[idx])
                idx += 1
            if removed_any:
                cleaned_gi = "\n".join(kept).strip()
                if cleaned_gi:
                    gitignore.write_text(cleaned_gi + "\n", encoding="utf-8")
                else:
                    gitignore.unlink(missing_ok=True)
                actions.append("cleaned_.gitignore")
        except OSError:
            pass

    return {
        "status": "cleaned",
        "root": resolved_root,
        "actions": actions,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if (
        len(raw_argv) >= 2
        and "hook" in raw_argv
        and not any(a in ("-h", "--help") for a in raw_argv)
    ):
        # Ultra-fast hook dispatch bypassing `argparse` subcommand construction
        h_idx = raw_argv.index("hook")
        pre_args = raw_argv[:h_idx]
        post_args = raw_argv[h_idx + 1 :]
        path_val = "."
        ok_pre = True
        idx_p = 0
        while idx_p < len(pre_args):
            tok = pre_args[idx_p]
            if tok == "--path" and idx_p + 1 < len(pre_args):
                path_val = pre_args[idx_p + 1]
                idx_p += 2
            elif tok.startswith("--path="):
                path_val = tok.split("=", 1)[1]
                idx_p += 1
            else:
                ok_pre = False
                break
        if ok_pre and post_args:
            h_act = post_args[0]
            if h_act == "post-edit":
                rem = post_args[1:]
                files_arg: list[str] | None = None
                if not rem:
                    files_arg = None
                elif rem[0] == "--files":
                    files_arg = rem[1:]
                if not rem or rem[0] == "--files":
                    from .hooks import run_post_edit_hook
                    root_p = Path(path_val)
                    raw_in = ""
                    if not files_arg and not sys.stdin.isatty():
                        try:
                            raw_in = sys.stdin.read()
                        except OSError:
                            raw_in = ""
                    default_r = root_p.resolve() if path_val != "." else None
                    rc, out_txt = run_post_edit_hook(raw_in, explicit_files=files_arg, default_root=default_r)
                    if out_txt:
                        print(out_txt)
                    return rc
            elif h_act == "pre-commit" and all(x == "--strict" for x in post_args[1:]):
                from .hooks import run_pre_commit_hook
                rc, out_txt = run_pre_commit_hook(Path(path_val).resolve(), strict=("--strict" in post_args[1:]))
                if out_txt:
                    stream = sys.stderr if rc != 0 else sys.stdout
                    print(out_txt, file=stream)
                return rc

    import argparse

    parser = argparse.ArgumentParser(prog="projectbrain")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="One-command setup: index, .gitignore, MCP configs, rules, active hooks, CI workflow, and governance")
    init_parser.add_argument("--no-mcp", action="store_true", help="Skip writing MCP configs")
    init_parser.add_argument("--no-rules", action="store_true", help="Skip writing agent rule files")
    init_parser.add_argument("--hooks", action="store_true", default=True, help="Install active post-edit guardrail hooks (default: enabled)")
    init_parser.add_argument("--no-hooks", action="store_true", help="Skip installing post-edit guardrail hooks")
    init_parser.add_argument("--git-hook", action="store_true", help="Also install .git/hooks/pre-commit guardrail")
    init_parser.add_argument("--team", action="store_true", help="Write OS-portable git-trackable team hooks (.claude/settings.json, .cursor/hooks.json) and GitHub Actions workflow")
    init_parser.add_argument("--github-action", action="store_true", help="Write .github/workflows/projectbrain.yml PR guardrail workflow")
    init_parser.add_argument("--no-github-action", action="store_true", help="Skip writing GitHub Actions workflow when using --team")
    init_parser.add_argument("--learn", action="store_true", help="Auto-discover and apply zero-violation architectural boundary & co-change rules")
    init_parser.add_argument(
        "--ide",
        choices=["auto", "all", "antigravity", "claude", "cursor", "vscode"],
        default="auto",
        help="Target IDE/agent environment to configure (default: auto)",
    )

    for clean_alias in ("clean", "uninstall"):
        clean_p = sub.add_parser(
            clean_alias,
            help="Remove ProjectBrain caches, rule blocks, MCP entries, and hooks from the repository",
        )
        clean_p.add_argument(
            "--all",
            action="store_true",
            help="Also remove git-tracked .projectbrain_memory.json",
        )

    hook_parser = sub.add_parser("hook", help="Execute or install real-time post-edit and git pre-commit guardrails")
    hook_sub = hook_parser.add_subparsers(dest="hook_action", required=True)
    hook_post = hook_sub.add_parser("post-edit", help="Audit edited files from Claude Code / Cursor hook stdin or --files")
    hook_post.add_argument("--files", nargs="*", default=None, help="Explicit modified file paths")
    hook_pre = hook_sub.add_parser("pre-commit", help="Audit staged git files before commit")
    hook_pre.add_argument("--strict", action="store_true", help="Also fail on stale AST invariants")
    hook_inst = hook_sub.add_parser("install", help="Install editor PostToolUse/afterFileEdit hooks and optional git pre-commit hook")
    hook_inst.add_argument("--git", action="store_true", help="Install .git/hooks/pre-commit")
    hook_inst.add_argument("--team", action="store_true", help="Write OS-portable git-trackable hook configs without local user paths")
    hook_inst.add_argument("--ide", choices=["auto", "all", "claude", "cursor"], default="auto")

    sub.add_parser("index", help="Build and cache the SQLite code graph index")

    compile_parser = sub.add_parser("compile", help="Compile surgical AST slices + neighbor skeletons for a task")
    compile_parser.add_argument("prompt", help="Natural language task description")
    compile_parser.add_argument("--budget", type=int, default=3000, help="Token budget (default: 3000)")
    compile_parser.add_argument("--top-n", type=int, default=4)
    compile_group = compile_parser.add_mutually_exclusive_group()
    compile_group.add_argument("--json", action="store_true", help="Raw JSON output (default)")
    compile_group.add_argument("--summary", action="store_true", help="Human-readable compiled view")

    dossier_parser = sub.add_parser(
        "dossier",
        help="Compile a self-contained Engineering Dossier (prints markdown to stdout in 1 turn)",
    )
    dossier_parser.add_argument("tasks", nargs="+", help="One or more task descriptions or engineering questions")
    dossier_parser.add_argument("--impact", nargs="*", default=None, help="Optional file paths for blast-radius analysis")
    dossier_parser.add_argument("--out", default=None, help="Output markdown path (default: <root>/.projectbrain_dossier.md)")
    dossier_parser.add_argument("--zoom", action="store_true", help="Pure Surgical Zoom mode")
    dossier_parser.add_argument("--stdout", action="store_true", help="Print compiled markdown directly to stdout (default)")
    dossier_parser.add_argument("--summary", action="store_true", help="Print compiled markdown directly to stdout")
    dossier_parser.add_argument("--json", action="store_true", help="Print compact JSON metadata instead of full markdown")

    run_parser = sub.add_parser(
        "run",
        help="Execute a test/build command (or compress piped --stdin output) with graph stack-trace resolution",
    )
    run_parser.add_argument("cmd", nargs="*", help="Command to execute (or omit when using --stdin)")
    run_parser.add_argument("--stdin", action="store_true", help="Read raw command output from stdin and compress it")
    run_parser.add_argument("--max-lines", type=int, default=80, help="Max lines to keep after folding (default: 80)")
    run_parser.add_argument("--timeout", type=int, default=120, help="Command timeout in seconds (default: 120)")
    run_parser.add_argument("--json", action="store_true", help="Output full compression telemetry as JSON")

    patch_parser = sub.add_parser(
        "patch",
        help="Legacy AST symbol/anchor patch runner (kept for backward compatibility)",
    )
    patch_parser.add_argument("--spec", default=None, help="Patch spec string or '-' for stdin")
    patch_parser.add_argument("--spec-file", default=None, help="Path to file containing patch spec")
    patch_parser.add_argument("--apply", action="store_true", help="Write verified patches to disk")
    patch_parser.add_argument("--diff-out", default=None, help="Path to write unified diff")
    patch_parser.add_argument("--include-diff", action="store_true", help="Include unified diff in JSON")

    slice_parser = sub.add_parser("slice", help="Surgically slice symbols, line ranges, file skeletons, or PDG focus variables")
    slice_parser.add_argument("targets", nargs="+", help="Symbols (e.g. service.py:Order.validate), node IDs, or files")
    slice_parser.add_argument("--focus", nargs="*", default=None, help="Variable or call identifier(s) for intra-function PDG slicing")
    slice_parser.add_argument("--max-lines", type=int, default=180, help="Max lines per symbol (default: 180)")
    slice_parser.add_argument("--summary", action="store_true", help="Human-readable code output (default)")
    slice_parser.add_argument("--json", action="store_true", help="Output raw JSON instead of code slices")

    explore_parser = sub.add_parser("explore", help="Explore symbol neighborhood, trace route flow, or find path between A and B")
    explore_parser.add_argument("target", help="Symbol name, node ID, or route URL")
    explore_parser.add_argument("target_b", nargs="?", default=None, help="Optional second symbol to find shortest path to")
    explore_parser.add_argument("--depth", type=int, default=2)
    explore_parser.add_argument("--co-changes", action="store_true", help="Include historical git co-change partners")

    impact_parser = sub.add_parser("impact", help="Compute blast radius (direct consumers, test consumers, callers) for files")
    impact_parser.add_argument("files", nargs="+", help="File paths to analyze")

    check_parser = sub.add_parser(
        "check",
        help="Run blast-radius, cross-file contract, boundary rule, governance tamper, and invariant verification gate",
    )
    check_parser.add_argument("files", nargs="*", default=None, help="Modified file paths (default: auto-detect via git merge-base / diff HEAD)")
    check_parser.add_argument("--base", default=None, help="Git base branch/commit for PR merge-base diffing (e.g. main, origin/main); auto-detected in CI PRs")
    check_parser.add_argument(
        "--allow-governance-change",
        action="store_true",
        help="Allow intentional removal or modification of rules/invariants in .projectbrain_memory.json",
    )
    check_parser.add_argument(
        "--format",
        choices=["text", "json", "markdown", "pr-comment", "github", "sarif"],
        default=None,
        help="Output format: text (default), json, markdown, pr-comment (sticky PR comment + Mermaid), github, or sarif (v2.1.0)",
    )
    check_parser.add_argument("--json", action="store_true", help="Alias for --format json")
    check_parser.add_argument("--ci", action="store_true", help="CI gate mode: emit GitHub annotations (in Actions) and exit 1 on violations")
    check_parser.add_argument(
        "--fail-on-violation",
        action="store_true",
        help="Exit with non-zero code (1) if any syntax, contract, boundary, or governance violation is detected",
    )

    learn_parser = sub.add_parser(
        "learn",
        help="Auto-discover zero-violation architectural boundary rules, git co-change contracts, and hub symbols",
    )
    learn_parser.add_argument("--apply", action="store_true", help="Save discovered rules to .projectbrain_memory.json (default: dry-run preview)")
    learn_parser.add_argument("--pin-hubs", action="store_true", help="Also pin AST invariants on top high-fan-in hub symbols when --apply is set")
    learn_parser.add_argument("--max-rules", type=int, default=12, help="Maximum boundary rules to propose (default: 12)")
    learn_parser.add_argument("--days", type=int, default=180, help="Git history window in days for co-change mining (default: 180)")
    learn_parser.add_argument("--min-cochanges", type=int, default=3, help="Minimum co-change commits required (default: 3)")
    learn_parser.add_argument("--min-confidence", type=float, default=0.70, help="Minimum co-change conditional probability (default: 0.70)")
    learn_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    remember_parser = sub.add_parser("remember", help="Pin an AST-anchored invariant, boundary rule, or co-change contract")
    remember_parser.add_argument("target", nargs="?", default="", help="Symbol name, file path, or node ID")
    remember_parser.add_argument("note", nargs="?", default="", help="Architectural invariant or rule reason")
    remember_parser.add_argument("--forbid", default=None, help="Enforceable layer rule: '<src_glob> -> <dst_glob>' (e.g. 'routes/* -> db/*')")
    remember_parser.add_argument("--co-change", default=None, help="Co-change contract: '<file_a> <-> <file_b>' or '<file_a> -> <file_b>'")
    remember_parser.add_argument("--reason", default="", help="Reason for --forbid or --co-change rule")

    memories_parser = sub.add_parser("memories", help="List, audit stale, refresh, or forget repo invariants and boundary rules")
    memories_parser.add_argument("--stale", action="store_true", help="Show only stale AST invariants")
    memories_parser.add_argument("--forget", default=None, help="Remove memory or boundary rule by ID, target, or rule string")
    memories_parser.add_argument("--refresh", nargs="?", const="*", default=None, help="Re-anchor verified stale invariants to current AST hashes")
    memories_parser.add_argument("--json", action="store_true", help="Output raw JSON")

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
        "verify", help="Fact-check a claim: <subject> <relation> <object>"
    )
    verify_parser.add_argument("subject", help="Function, class, file, or route")
    verify_parser.add_argument(
        "relation",
        help="calls | reaches | inherits | has_method | defines | imports | handles",
    )
    verify_parser.add_argument("object", help="The name the relation targets")

    sub.add_parser("health", help="Report index coverage and blind spots")

    stats_parser = sub.add_parser("stats", help="Display guardrail interception scorecard and index telemetry")
    stats_parser.add_argument("--summary", action="store_true", help="Print human-readable guardrail scorecard")
    stats_parser.add_argument("--reset", action="store_true", help="Reset session working memory and ledger")

    args = parser.parse_args(argv)

    # Fast-path dispatch for `projectbrain hook` BEFORE importing ProjectBrain / Tree-sitter!
    if args.command == "hook":
        root_p = Path(args.path)
        if args.hook_action == "post-edit":
            from .hooks import run_post_edit_hook
            raw_in = ""
            if not args.files and not sys.stdin.isatty():
                try:
                    raw_in = sys.stdin.read()
                except OSError:
                    raw_in = ""
            default_r = root_p.resolve() if args.path != "." else None
            rc, out_txt = run_post_edit_hook(raw_in, explicit_files=args.files, default_root=default_r)
            if out_txt:
                print(out_txt)
            return rc
        if args.hook_action == "pre-commit":
            from .hooks import run_pre_commit_hook
            rc, out_txt = run_pre_commit_hook(root_p.resolve(), strict=bool(args.strict))
            if out_txt:
                stream = sys.stderr if rc != 0 else sys.stdout
                print(out_txt, file=stream)
            return rc
        if args.hook_action == "install":
            from .hooks import install_git_pre_commit_hook, merge_claude_hooks, merge_cursor_hooks
            resolved_root = str(root_p.resolve())
            installed: list[str] = []
            has_cursor = (root_p / ".cursor").exists()
            portable_mode = bool(getattr(args, "team", False))
            if portable_mode:
                if args.ide in ("auto", "all", "claude"):
                    merge_claude_hooks(root_p / ".claude" / "settings.json", resolved_root, portable=True)
                    installed.append(".claude/settings.json")
                if args.ide in ("auto", "all", "cursor"):
                    merge_cursor_hooks(root_p / ".cursor" / "hooks.json", resolved_root, portable=True)
                    installed.append(".cursor/hooks.json")
            else:
                if args.ide in ("auto", "all", "claude"):
                    merge_claude_hooks(root_p / ".claude" / "settings.local.json", resolved_root)
                    installed.append(".claude/settings.local.json")
                if args.ide in ("all", "cursor") or (args.ide == "auto" and has_cursor):
                    merge_cursor_hooks(root_p / ".cursor" / "hooks.json", resolved_root)
                    installed.append(".cursor/hooks.json")
            if args.git and install_git_pre_commit_hook(root_p, resolved_root, portable=portable_mode):
                installed.append(".git/hooks/pre-commit")
            print(json.dumps({"status": "hooks_installed", "root": resolved_root, "team": portable_mode, "installed": installed}, indent=2))
            return 0

    if args.command == "init":
        enable_hooks = not bool(getattr(args, "no_hooks", False))
        res = _run_init(
            Path(args.path),
            write_mcp=not args.no_mcp,
            write_rules=not args.no_rules,
            ide=args.ide,
            write_hooks=enable_hooks,
            write_git_hook=bool(getattr(args, "git_hook", False)),
            team=bool(getattr(args, "team", False)),
            github_action=bool(getattr(args, "github_action", False)),
            no_github_action=bool(getattr(args, "no_github_action", False)),
            learn=bool(getattr(args, "learn", False)),
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.command in ("clean", "uninstall"):
        res = _run_clean(Path(args.path), remove_memory=bool(getattr(args, "all", False)))
        print(json.dumps(res, indent=2))
        return 0

    ProjectBrain = _load_brain_cls()
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
        if getattr(args, "json", False) or (args.zoom and not args.stdout and not args.summary):
            summary = dict(res) if args.zoom else {k: v for k, v in res.items() if k != "markdown"}
            print(json.dumps(summary, indent=2))
            return 0
        print(res.get("markdown", ""))
        return 0

    if args.command == "run":
        cmd_parts = list(args.cmd or [])
        if cmd_parts and cmd_parts[0] == "--":
            cmd_parts = cmd_parts[1:]
        if args.stdin or (not cmd_parts and not sys.stdin.isatty()):
            raw_in = sys.stdin.read()
            res = brain.compress_command_output(raw_in, max_lines=args.max_lines)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(res.get("compressed_output", ""))
            return 0
        if not cmd_parts:
            print(json.dumps({"error": "Provide a command to run or pipe output via --stdin"}))
            return 1
        cmd_arg = cmd_parts[0] if len(cmd_parts) == 1 else " ".join(cmd_parts)
        res = brain.run_command_compressed(cmd_arg, timeout=args.timeout, max_lines=args.max_lines)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(res.get("compressed_output", ""))
        return int(res.get("exit_code", 0))

    if args.command == "patch":
        spec_str = args.spec
        if spec_str == "-":
            spec_str = sys.stdin.read()
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
        res = brain.slice_symbols(
            args.targets,
            max_lines_per_symbol=args.max_lines,
            summary_mode=bool(args.summary),
            focus=args.focus,
        )
        out_lines: list[str] = []
        for s in res.get("slices", []):
            if s.get("error"):
                out_lines.append(f"[ERROR] {s['error']}")
            else:
                out_lines.append(f"=== {s.get('rel_path')} :: {s.get('symbol')} ({s.get('lines', 'skeleton')}) ===")
                out_lines.append(s.get("code") or s.get("skeleton") or "")
                out_lines.append("")
        full_txt = "\n".join(out_lines)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        elif len(full_txt) > 8200:
            slice_md_path = brain.root / ".projectbrain_slice.md"
            try:
                slice_md_path.write_text(full_txt, encoding="utf-8")
            except OSError:
                pass
            preview_lines = full_txt[:7400].splitlines()
            shown_n = len(preview_lines)
            total_n = len(full_txt.splitlines())
            print("\n".join(preview_lines))
            print(
                f"\n... [Batched slice is {total_n} lines ({len(full_txt):,} chars) — full output saved to {slice_md_path}. "
                f"Lines 1-{shown_n} shown above; read remaining lines {shown_n + 1}-{total_n} via read_file(Path=\"{slice_md_path}\", StartLine={shown_n + 1}, EndLine={min(total_n, shown_n + 800)})]"
            )
        else:
            print(full_txt)
        return 0

    if args.command == "explore":
        print(json.dumps(
            brain.explore(
                args.target,
                target_b=args.target_b,
                depth=args.depth,
                include_co_changes=bool(getattr(args, "co_changes", False)),
            ),
            indent=2,
        ))
        return 0

    if args.command == "impact":
        print(json.dumps(brain.impact(args.files), indent=2))
        return 0

    if args.command == "check":
        res = brain.check(
            files_modified=args.files or None,
            base_ref=getattr(args, "base", None),
            allow_governance_change=bool(getattr(args, "allow_governance_change", False)),
        )
        chosen_fmt = args.format
        if args.json:
            chosen_fmt = "json"
        elif chosen_fmt is None:
            if args.ci and os.environ.get("GITHUB_ACTIONS") == "true":
                chosen_fmt = "github"
            else:
                chosen_fmt = "text"
        print(format_check_report(res, fmt=chosen_fmt))
        fail_gate = bool(args.fail_on_violation or args.ci)
        if fail_gate and res.get("all_contracts_valid") is False:
            return 1
        return 0

    if args.command == "learn":
        res = brain.learn(
            apply=bool(args.apply),
            pin_hubs=bool(args.pin_hubs),
            max_rules=args.max_rules,
            days=args.days,
            min_cochanges=args.min_cochanges,
            min_confidence=args.min_confidence,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            from .learn import format_learn_report
            print(format_learn_report(res))
        return 0

    if args.command == "remember":
        if args.forbid:
            reason_txt = args.reason or args.note or args.target
            print(json.dumps(brain.remember(note=reason_txt, forbid=args.forbid), indent=2))
            return 0
        if args.co_change:
            reason_txt = args.reason or args.note or args.target
            print(json.dumps(brain.remember(note=reason_txt, co_change=args.co_change), indent=2))
            return 0
        print(json.dumps(brain.remember(args.target, args.note), indent=2))
        return 0

    if args.command == "memories":
        if args.forget:
            out_m = brain.remember(target=args.forget, action="forget")
            print(json.dumps(out_m, indent=2))
            return 0
        if args.refresh is not None:
            tgt = "" if args.refresh == "*" else args.refresh
            out_m = brain.remember(target=tgt, action="refresh")
            print(json.dumps(out_m, indent=2))
            return 0
        act = "stale" if args.stale else "list"
        out_m = brain.remember(action=act)
        if args.json:
            print(json.dumps(out_m, indent=2))
            return 0
        notes = out_m.get("memories", [])
        rules = out_m.get("boundary_rules", [])
        co_rules = out_m.get("co_change_rules", [])
        print(
            f"ProjectBrain Repo Governance: {len(notes)} invariant(s), "
            f"{len(rules)} boundary rule(s), {len(co_rules)} co-change rule(s)"
        )
        for r in rules:
            rsn = f" — {r['reason']}" if r.get("reason") else ""
            print(f"  [FORBID] ({r.get('id')}) {r.get('rule')}{rsn}")
        for cr in co_rules:
            rsn = f" — {cr['reason']}" if cr.get("reason") else ""
            print(f"  [CO-CHANGE] ({cr.get('id')}) {cr.get('rule')}{rsn}")
        for m in notes:
            badge = "[STALE]" if m.get("stale") else "[OK]"
            print(f"  {badge} ({m.get('id')}) {m.get('target_id')}: {m.get('note')}")
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
