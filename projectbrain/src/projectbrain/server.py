"""ProjectBrain MCP Server (v1.1: Zero-LSP Code Graph, PR Merge-Base & Blast-Radius Guardrail).

Exposes 5 primary non-overlapping power tools by default (`--lean`):
  1. `brain_dossier`  — One-call Architecture & Context Compiler: returns PDG/AST code
                        slices, 1-2 hop caller/callee skeletons, route/state flows,
                        covering tests, and pinned AST invariants.
  2. `brain_slice`    — `<20ms` Surgical Symbol, File Skeleton & Intra-Function PDG
                        Reader (`focus` variable slicing without full-file reads).
  3. `brain_explore`  — Unified Code Graph Navigator: traces callers/callees, shortest
                        path `A -> B`, `/api/...` frontend-to-DB flows, and git co-changes.
  4. `brain_check`    — Pre-Flight Verifier, Cross-File Contract Gate & Blast Radius:
                        validates edit plans, 3-valued claims (`SUPPORTED`/`REFUTED`/`UNKNOWN`),
                        boundary rules, caller impact, and stale invariants.
  5. `brain_remember` — Git-Tracked AST Invariants & Architectural Boundary Rules
                        (`.projectbrain_memory.json`) with automatic staleness detection.

Pass `--full` (or `PROJECTBRAIN_FULL_TOOLS=1`) to also expose auxiliary tools
(`brain_learn`, `brain_context`, `brain_stats`, `brain_run`, `brain_patch`).
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from fastmcp import FastMCP  # type: ignore[no-redef]
    except ImportError:
        from mcp.server import FastMCP  # type: ignore[attr-defined,no-redef]

if TYPE_CHECKING:
    from .brain import ProjectBrain


def parse_server_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `projectbrain-mcp` without mistaking flags for paths."""
    parser = argparse.ArgumentParser(prog="projectbrain-mcp", add_help=True)
    parser.add_argument("repo_path", nargs="?", default=None, help="Repository root path")
    parser.add_argument("--path", dest="flag_path", default=None, help="Explicit repository root path")
    parser.add_argument("--lean", action="store_true", help="Expose the 5 core MCP tools (default)")
    parser.add_argument("--full", "--all-tools", dest="full", action="store_true", help="Expose all MCP tools")
    parser.add_argument("--allow-exec", action="store_true", help="Allow direct shell execution in brain_run")
    args, _ = parser.parse_known_args(argv)
    return args


def _resolve_root(argv: list[str] | None = None) -> str:
    args = parse_server_args(argv if argv is not None else sys.argv[1:])
    chosen = args.flag_path or args.repo_path or os.environ.get("PROJECTBRAIN_ROOT") or os.getcwd()
    return str(Path(chosen).resolve())


def _is_unsafe_unindexed_root(target: Path) -> bool:
    """Prevent accidental multi-minute scans of `/` or `$HOME` when no repo_path was configured."""
    resolved = target.resolve()
    if (resolved / ".projectbrain.db").exists() or (resolved / ".git").exists() or (resolved / "pyproject.toml").exists() or (resolved / "package.json").exists():
        return False
    if resolved == resolved.anchor or str(resolved) == resolved.drive + "\\":
        return True
    try:
        if resolved == Path.home().resolve():
            return True
    except Exception:
        pass
    return False


mcp = FastMCP("projectbrain")
_brains_by_root: dict[str, "ProjectBrain"] = {}
_brains_lock = threading.RLock()


def _get_brain(repo_path: str | None = None) -> "ProjectBrain":
    from .brain import ProjectBrain

    raw_target = repo_path or _resolve_root()
    resolved = Path(raw_target).resolve()
    if _is_unsafe_unindexed_root(resolved):
        raise ValueError(
            f"Refusing to scan unindexed system/home directory '{resolved}'. "
            "Pass `repo_path='/path/to/your/repo'` or configure `projectbrain-mcp /path/to/your/repo`."
        )
    key = str(resolved)
    with _brains_lock:
        if key not in _brains_by_root:
            _brains_by_root[key] = ProjectBrain(key)
        return _brains_by_root[key]


# ── Core 5 Power Tools (Default Lean Agent Surface) ──────────────────────────

@mcp.tool()
def brain_dossier(
    tasks: list[str] | str,
    impact_files: list[str] | None = None,
    zoom: bool = False,
    include_markdown: bool = True,
    repo_path: str | None = None,
) -> dict:
    """Compile a self-contained <800-line Engineering Dossier in ONE call.

    Use when investigating an unfamiliar multi-file flow, cross-layer bug, or architecture
    question (prefer native `rg` / `read_file` when you already know the exact file or string).
    Combines PDG/AST function slices, module globals, 1-3 hop bidirectional call chains,
    route/state-flow maps, covering tests, and pinned AST invariants without writing files to disk.
    """
    task_list = [tasks] if isinstance(tasks, str) else list(tasks)
    res = _get_brain(repo_path).compile_dossier(
        task_list,
        impact_files=impact_files,
        zoom=zoom,
        write_file=False,
    )
    if include_markdown or zoom:
        return res
    return {k: v for k, v in res.items() if k != "markdown"}


@mcp.tool()
def brain_slice(
    targets: list[str] | str,
    focus: list[str] | str | None = None,
    max_lines_per_symbol: int = 120,
    repo_path: str | None = None,
) -> dict:
    """Surgically read functions, classes, file skeletons, or intra-function PDG slices in <20ms.

    Use instead of full-file reads when inspecting specific symbols or file outlines:
      - Symbol name or qualified method: `"OrderService.validate"` or `"orders.py:validate"`
      - File signature skeleton (bodies stripped): `"src/services/orders.py"`
      - Line range: `"src/services/orders.py:100-160"`
      - Intra-function PDG slice: pass `focus="discount_code"` to trace backward definitions,
        forward uses, and enclosing control guards around a target variable or call.
    """
    return _get_brain(repo_path).slice_symbols(
        targets,
        max_lines_per_symbol=max_lines_per_symbol,
        focus=focus,
    )


@mcp.tool()
def brain_explore(
    target: str,
    target_b: str | None = None,
    edge_types: list[str] | None = None,
    depth: int = 2,
    include_co_changes: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Navigate the code graph: trace callers/callees, shortest paths, or full-stack HTTP routes.

    Modes:
      1. Path between two symbols (`target` + `target_b`): shortest typed call/import/route chain.
      2. Full-stack route trace (`target` starts with `/`, e.g. `"/api/orders"`):
         traces UI fetch callers -> backend route handler -> service/database callees.
      3. Symbol/file neighborhood (`target` only): `depth`-hop callers, callees, imports,
         and optional git `co_changes` (`include_co_changes=True`).
    """
    return _get_brain(repo_path).explore(
        target,
        target_b=target_b,
        edge_types=edge_types,
        depth=depth,
        include_co_changes=include_co_changes,
    )


@mcp.tool()
def brain_check(
    files_modified: list[str] | None = None,
    claims: list[list[str]] | None = None,
    plan_files: list[str] | None = None,
    task: str = "",
    base_ref: str | None = None,
    allow_governance_change: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Verification & blast-radius gate: check claims, edit plans, PR merge-base diffs, contracts, and invariants.

    Modes:
      - Pre-edit plan gate (`plan_files=[...]`, `task="..."`): flags missing connected importers/tests.
      - Post-edit / PR merge-base gate (`files_modified=[...]`, `base_ref="main"`, or git diff): checks syntax,
        4-pillar cross-file arity/import/key/SQL contracts, deleted/renamed file consumers, architectural
        boundary rules, governance tamper lock, downstream callers, co-change drift, and stale AST invariants.
      - Fact-check claims (`claims=[["Router", "calls", "dispatch"]]`): returns `SUPPORTED`,
        `REFUTED`, or `UNKNOWN` (never guesses on dynamic dispatch or partial ASTs).
    """
    return _get_brain(repo_path).check(
        files_modified=files_modified,
        claims=claims,
        plan_files=plan_files,
        task=task,
        base_ref=base_ref,
        allow_governance_change=allow_governance_change,
    )


@mcp.tool()
def brain_remember(
    target: str = "",
    note: str = "",
    forbid: str | None = None,
    co_change: str | None = None,
    action: str = "save",
    tags: list[str] | None = None,
    repo_path: str | None = None,
) -> dict:
    """Pin AST-anchored invariants, architectural boundary rules, or co-change contracts in `.projectbrain_memory.json`.

    Usage:
      - Pin invariant: `brain_remember(target="AuthService.verify", note="Must strip Bearer prefix before HMAC")`
      - Enforce layer rule: `brain_remember(forbid="routes/* -> db/*", note="Routes must call services, not DB")`
      - Pin co-change rule: `brain_remember(co_change="schema.sql <-> models/user.py", note="Keep ORM synced")`
      - Audit invariants: `brain_remember(action="list")` or `brain_remember(action="stale")`
      - Remove/refresh: `brain_remember(target="mem_1", action="forget")` or `action="refresh"`
    """
    return _get_brain(repo_path).remember(
        target=target,
        note=note,
        tags=tags,
        forbid=forbid,
        co_change=co_change,
        action=action,
    )


# ── Auxiliary / Legacy Tools (`--full` Mode) ─────────────────────────────────

@mcp.tool()
def brain_learn(
    apply: bool = False,
    pin_hubs: bool = False,
    max_rules: int = 12,
    days: int = 180,
    min_cochanges: int = 3,
    min_confidence: float = 0.70,
    repo_path: str | None = None,
) -> dict:
    """Auto-discover zero-violation architectural `--forbid` boundaries, git co-change contracts, and hub symbols."""
    return _get_brain(repo_path).learn(
        apply=apply,
        pin_hubs=pin_hubs,
        max_rules=max_rules,
        days=days,
        min_cochanges=min_cochanges,
        min_confidence=min_confidence,
    )

@mcp.tool()
def brain_context(
    task: str,
    budget_tokens: int = 3000,
    top_n: int = 4,
    repo_path: str | None = None,
) -> dict:
    """Compile token-budgeted target slices and 1-hop neighbor signature skeletons."""
    return _get_brain(repo_path).compile_context(task, budget_tokens=budget_tokens, top_n=top_n)


@mcp.tool()
def brain_stats(
    repo_path: str | None = None,
    reset: bool = False,
) -> dict:
    """Return index topology, guardrail interception scorecard, and resolution telemetry."""
    return _get_brain(repo_path).stats(reset=reset)


@mcp.tool()
def brain_patch(
    spec: str,
    apply: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Legacy AST symbol/anchor patch tool (only exposed in `--full` mode)."""
    return _get_brain(repo_path).patch(spec, apply=apply)


@mcp.tool()
def brain_run(
    raw_output: str = "",
    command: str = "",
    max_lines: int = 80,
    timeout: int = 120,
    repo_path: str | None = None,
) -> dict:
    """Compress raw terminal/test output with Code Graph stack-trace frame resolution."""
    b = _get_brain(repo_path)
    if command.strip():
        parsed = parse_server_args(sys.argv[1:])
        allow_exec = parsed.allow_exec or (os.environ.get("PROJECTBRAIN_ALLOW_EXEC") == "1")
        if not allow_exec:
            return {
                "error": "Direct command execution over MCP is disabled by default for security. Pass `raw_output` to compress terminal/test logs, or set `PROJECTBRAIN_ALLOW_EXEC=1`.",
                "exit_code": 126,
            }
        return b.run_command_compressed(command.strip(), timeout=timeout, max_lines=max_lines)
    return b.compress_command_output(raw_output, max_lines=max_lines)


LEAN_CORE_TOOLS = {
    "brain_dossier",
    "brain_slice",
    "brain_explore",
    "brain_check",
    "brain_remember",
}


_ALL_REGISTERED_TOOLS: dict[str, object] | None = None


def configure_mcp_tools(full_mode: bool = False) -> set[str]:
    """Filter registered FastMCP tools according to `--lean` (default) vs `--full`."""
    global _ALL_REGISTERED_TOOLS
    tm = getattr(mcp, "_tool_manager", None)
    tools_dict = getattr(tm, "_tools", None)
    if isinstance(tools_dict, dict):
        if _ALL_REGISTERED_TOOLS is None:
            _ALL_REGISTERED_TOOLS = dict(tools_dict)
        tools_dict.clear()
        for t_name, t_obj in _ALL_REGISTERED_TOOLS.items():
            if full_mode or t_name in LEAN_CORE_TOOLS:
                tools_dict[t_name] = t_obj
        return set(tools_dict.keys())
    return set(LEAN_CORE_TOOLS)


def main(argv: list[str] | None = None) -> None:
    parsed = parse_server_args(argv if argv is not None else sys.argv[1:])
    full_mode = parsed.full or (os.environ.get("PROJECTBRAIN_FULL_TOOLS") == "1")
    configure_mcp_tools(full_mode=full_mode)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
