"""ProjectBrain MCP Server (v2: High-Leverage Context Compiler & Slicer).

Exposes 5 primary power tools designed to stop LLM coding agents from wasting
tokens on full-file reads and multi-turn grep loops:
  1. `brain_context`  — One-call Context Compiler: returns surgical AST code slices
                        for target functions/classes + signature-only skeletons for
                        1-hop neighbors + AST-anchored memories (auto-deduplicated!).
  2. `brain_slice`    — Surgical Symbol/Range Reader: reads exact function/class code
                        or file signature skeletons without opening full files.
  3. `brain_explore`  — Unified Graph Navigator: finds neighborhood callers/callees,
                        shortest path between two symbols, or frontend->backend flow.
  4. `brain_check`    — Pre-Flight Verifier & Blast Radius: fact-checks structural claims,
                        validates edit plans, and reports downstream callers/tests.
  5. `brain_remember` — Persistent Repo Memory: pins discovered architectural rules or
                        gotchas to AST nodes in `.projectbrain_memory.json`.

Legacy tools (`projectbrain_query`, `projectbrain_get_context`, etc.) are also kept
for backward compatibility.
"""
import os
import sys
import threading

from mcp.server.fastmcp import FastMCP

from .brain import ProjectBrain


def _resolve_root() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("PROJECTBRAIN_ROOT", os.getcwd())


mcp = FastMCP("projectbrain")
_default_root = _resolve_root()
_brain = ProjectBrain(_default_root)
_brains_by_root: dict[str, ProjectBrain] = {os.path.abspath(_default_root): _brain}
_brains_lock = threading.RLock()


def _get_brain(repo_path: str | None = None) -> ProjectBrain:
    if not repo_path:
        return _brain
    key = os.path.abspath(repo_path)
    with _brains_lock:
        if key not in _brains_by_root:
            _brains_by_root[key] = ProjectBrain(key)
        return _brains_by_root[key]


# ── v2 Power Tools (Primary Agent Interface) ─────────────────────────────────

@mcp.tool()
def brain_context(
    task: str,
    budget_tokens: int = 3000,
    top_n: int = 4,
    repo_path: str | None = None,
) -> dict:
    """Compile surgical code slices and neighbor signature skeletons for a task in ONE call.

    ALWAYS CALL THIS FIRST before grepping or calling view_file on unfamiliar files.
    Unlike file search tools that only return file paths (forcing you to read 1,000-line
    files afterward), `brain_context` directly returns:
      - `target_slices`: The EXACT source code (with line numbers) of the top matching
        functions, methods, or classes you need to inspect or edit.
      - `neighbor_skeletons`: Compact AST signature skeletons (parameters, return types,
        class fields, docstrings — implementation bodies stripped!) for connected files
        so you know how to call dependencies without reading their full source.
      - `memories`: Persistent architectural notes anchored to these symbols, automatically
        checked for staleness against current AST hashes.
      - `related_tests`: Exact test files that cover the matched code.
      - Automatic session deduplication: symbols already served to you in this session
        are omitted automatically so you never waste tokens re-reading the same code.

    Parameters:
      task          — Natural-language description of what you are trying to fix, build,
                      or understand (e.g. "add discount code to checkout", "fix 404 on /users").
      budget_tokens — Approximate token budget for the compiled packet (default: 3000).
      top_n         — Number of top files to compile from (default: 4).
      repo_path     — Optional path to repository root (defaults to active workspace).
    """
    return _get_brain(repo_path).compile_context(task, budget_tokens=budget_tokens, top_n=top_n)


@mcp.tool()
def brain_dossier(
    tasks: list[str],
    impact_files: list[str] | None = None,
    zoom: bool = False,
    include_markdown: bool = True,
    repo_path: str | None = None,
) -> dict:
    """Compile a self-contained <800-line Engineering Dossier (.projectbrain_dossier.md) in ONE call.

    Combines 4D AST Coordinate Matrix slices, module globals/constants, 1-3 hop bidirectional
    call chains, and Closed-World Exhaustive Repo Scan. Pass zoom=True for pure 4D Surgical Zoom.
    When zoom=True or include_markdown=True (default), returns the compiled markdown directly in
    the response so no second read_file call is needed.
    """
    res = _get_brain(repo_path).compile_dossier(tasks, impact_files=impact_files, zoom=zoom)
    if include_markdown or zoom:
        return res
    return {k: v for k, v in res.items() if k != "markdown"}


@mcp.tool()
def brain_patch(
    spec: str,
    apply: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Apply or dry-run an AST-verified Surgical Anchor Patch spec.

    Use this instead of repeating verbatim Target + Replacement blocks in `edit_file` or
    writing verbose before/after code blocks.
    Syntax:
      @file <rel_path>
      @sub L<line> `<old_expr>` -> `<new_expr>` (or `@sub ALL` / `@sub <symbol>`)
      @replace L<start>-L<end>
      <new lines only>
      @end
      @replace_body <symbol>
      <new body lines>
      @end
      @add_method <ClassName>
      <new method lines>
      @end
      @insert_after L<line>
      <new lines>
      @end
      @delete L<start>-L<end>
    """
    return _get_brain(repo_path).patch(spec, apply=apply)


@mcp.tool()
def brain_stats(
    repo_path: str | None = None,
    reset: bool = False,
) -> dict:
    """Return index topology and structural/epistemic resolution telemetry from `.projectbrain_ledger.json`."""
    return _get_brain(repo_path).stats(reset=reset)


@mcp.tool()
def brain_slice(
    targets: list[str],
    max_lines_per_symbol: int = 100,
    repo_path: str | None = None,
) -> dict:
    """Surgically read specific functions, classes, line ranges, or file skeletons.

    Use this instead of `view_file` when you only need a specific function/class body
    or when you want to see a file's signature skeleton without reading 1,000+ lines.

    Each item in `targets` can be:
      - A symbol name: `"verify_claim"` or `"ProjectBrain.verify_plan"`
      - A file:symbol pair: `"brain.py:verify_claim"`
      - A graph node ID: `"fn:src/projectbrain/brain.py:ProjectBrain.query"`
      - A line range: `"src/projectbrain/brain.py:100-160"`
      - A file path (returns its signature-only skeleton): `"src/projectbrain/brain.py"`
    """
    return _get_brain(repo_path).slice_symbols(targets, max_lines_per_symbol=max_lines_per_symbol)


@mcp.tool()
def brain_explore(
    target: str,
    target_b: str | None = None,
    edge_types: list[str] | None = None,
    depth: int = 2,
    include_co_changes: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Navigate the symbol graph: find callers/callees, trace shortest paths, or trace URL flows.

    Modes:
      1. Path between two symbols (`target` + `target_b` both provided):
         Finds the shortest typed path connecting `target` and `target_b` across imports,
         calls, routes, and inheritance (e.g. `target="checkout"`, `target_b="Coupon"`).
      2. End-to-end URL route trace (`target` starts with `/`, e.g. `"/api/checkout"`):
         Traces UI fetch callers -> backend route handler -> service/database callees.
      3. Symbol/file neighborhood (`target` only):
         Traverses `depth` hops outward from `target`, returning connected callers, callees,
         and imports (plus git `co_changes` if `include_co_changes=True`).
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
    repo_path: str | None = None,
) -> dict:
    """Pre-flight verification gate: check structural claims, validate plans, or assess edit impact.

    Call this:
      - BEFORE editing (`plan_files=["..."]`, `task="..."`): Checks if your edit plan
         misses any connected importers, models, or test files (`COMPLETE` / `INCOMPLETE`).
      - AFTER editing (`files_modified=["..."]`): Computes blast radius — all direct
         consumers, callers of functions in those files (with their signatures), and test files.
      - BEFORE asserting architectural facts (`claims=[["checkout", "reaches", "Coupon"]]`):
         Fact-checks claims against the closed-world symbol graph (`SUPPORTED` / `REFUTED` / `UNKNOWN`).
      - With no arguments: Returns index health and parse coverage.
    """
    return _get_brain(repo_path).check(
        files_modified=files_modified,
        claims=claims,
        plan_files=plan_files,
        task=task,
    )


@mcp.tool()
def brain_remember(
    target: str,
    note: str,
    tags: list[str] | None = None,
    repo_path: str | None = None,
) -> dict:
    """Save an architectural rule, invariant, or bug root-cause to `.projectbrain_memory.json`.

    The note is anchored to `target` (a function name, class name, file path, or node ID)
    along with its current AST hash. Future `brain_context` calls touching this subgraph
    will automatically surface this note — and flag it as stale if the underlying code
    is later modified.
    """
    return _get_brain(repo_path).remember(target, note, tags=tags)


# ── Legacy / Granular Tools (Preserved for Backward Compatibility) ───────────

@mcp.tool()
def projectbrain_query(prompt: str, top_n: int = 5, repo_path: str | None = None) -> dict:
    """Find which files are relevant to a coding task and return a typed symbol graph."""
    return _get_brain(repo_path).query_with_graph(prompt, top_n=top_n)


@mcp.tool()
def projectbrain_get_context(file_path: str, repo_path: str | None = None) -> dict:
    """Get one file's functions/classes/routes/imports/skeleton plus its 1-hop dependency chain."""
    return _get_brain(repo_path).get_context_for_file(file_path)


@mcp.tool()
def projectbrain_get_neighbors(
    node_id: str,
    edge_types: list[str] | None = None,
    depth: int = 1,
    repo_path: str | None = None,
) -> dict:
    """Traverse the symbol graph outward from a known node ID."""
    return _get_brain(repo_path).get_neighbors(node_id, edge_types=edge_types, depth=depth)


@mcp.tool()
def projectbrain_find_relationship(name_a: str, name_b: str, repo_path: str | None = None) -> dict:
    """Find the shortest typed path between any two named symbols or files."""
    return _get_brain(repo_path).find_relationship(name_a, name_b)


@mcp.tool()
def projectbrain_list_nodes(
    node_type: str | None = None,
    query: str | None = None,
    repo_path: str | None = None,
) -> dict:
    """Enumerate and filter nodes in the symbol graph for discovery."""
    return _get_brain(repo_path).list_nodes(node_type=node_type, query=query)


@mcp.tool()
def projectbrain_verify_claim(
    subject: str,
    relation: str,
    object: str,
    repo_path: str | None = None,
) -> dict:
    """Fact-check a structural claim about the code against the symbol graph."""
    return _get_brain(repo_path).verify_claim(subject, relation, object)


@mcp.tool()
def brain_verify_batch(claims: list[list[str]], repo_path: str | None = None) -> dict:
    """Verify multiple structural claims in a single call."""
    tuples = [(c[0], c[1], c[2]) for c in claims if len(c) >= 3]
    return _get_brain(repo_path).verify_batch(tuples)


@mcp.tool()
def brain_verify_plan(
    task: str,
    files_to_edit: list[str],
    files_to_create: list[str] | None = None,
    repo_path: str | None = None,
) -> dict:
    """Validate an agent's plan BEFORE it starts editing files."""
    return _get_brain(repo_path).verify_plan(task, files_to_edit, files_to_create)


@mcp.tool()
def brain_impact(files_changed: list[str], repo_path: str | None = None) -> dict:
    """Analyze downstream impact of changing a set of files."""
    return _get_brain(repo_path).impact(files_changed)


@mcp.tool()
def projectbrain_health(repo_path: str | None = None) -> dict:
    """Report what the index does and does NOT actually know — its blind spots."""
    return _get_brain(repo_path).health()


@mcp.tool()
def brain_start_session(task: str, repo_path: str | None = None) -> dict:
    """Start a guided exploration session for a coding task."""
    return _get_brain(repo_path).start_session(task)


@mcp.tool()
def brain_mark_read(session_id: str, file_path: str, repo_path: str | None = None) -> dict:
    """Tell Brain you've read a file — it will route you to the next one."""
    return _get_brain(repo_path).session_mark_read(session_id, file_path)


@mcp.tool()
def brain_mark_modified(
    session_id: str,
    file_path: str,
    summary: str = "",
    repo_path: str | None = None,
) -> dict:
    """Tell Brain you've modified a file — it surfaces downstream impact."""
    return _get_brain(repo_path).session_mark_modified(session_id, file_path, summary)


@mcp.tool()
def brain_next(session_id: str, count: int = 1, repo_path: str | None = None) -> dict:
    """Pop the next file(s) from the exploration frontier."""
    return _get_brain(repo_path).session_next(session_id, count)


@mcp.tool()
def brain_compress(
    file_paths: list[str],
    max_tokens: int = 4000,
    focus: str = "",
    repo_path: str | None = None,
) -> dict:
    """Get a compressed structural summary and signature skeleton of files."""
    return _get_brain(repo_path).compress_context(file_paths, max_tokens=max_tokens, focus=focus)



@mcp.tool()
def brain_run(
    raw_output: str = "",
    command: str = "",
    max_lines: int = 80,
    timeout: int = 120,
    repo_path: str | None = None,
) -> dict:
    """Compress raw terminal/test output (`raw_output`) with Code Graph traceback resolution.

    Strips passing test noise (pytest, jest/vitest, go test, cargo test), folds repetitive log/JSON/CSV
    lines, and cross-references any stack-trace `file:line` frames against the Code Graph to return
    the exact failing symbols, signatures, roles, and 1-hop callers.
    Note: Direct shell command execution over MCP is disabled by default for security unless
    `PROJECTBRAIN_ALLOW_EXEC=1` (or `--allow-exec`) is explicitly configured by the user.
    """
    b = _get_brain(repo_path)
    if command.strip():
        allow_exec = ("--allow-exec" in sys.argv) or (os.environ.get("PROJECTBRAIN_ALLOW_EXEC") == "1")
        if not allow_exec:
            return {
                "error": "Direct command execution over MCP is disabled by default for security. Pass `raw_output` to compress terminal/test logs, or set `PROJECTBRAIN_ALLOW_EXEC=1`.",
                "exit_code": 126,
            }
        return b.run_command_compressed(command.strip(), timeout=timeout, max_lines=max_lines)
    return b.compress_command_output(raw_output, max_lines=max_lines)
@mcp.tool()
def brain_status(session_id: str, repo_path: str | None = None) -> dict:
    """Check session progress: what's been read, what's left, estimated completeness."""
    return _get_brain(repo_path).session_status(session_id)


@mcp.tool()
def brain_trace_flow(url_path: str, method: str = "GET", repo_path: str | None = None) -> dict:
    """Trace a request flow across the frontend→backend boundary."""
    return _get_brain(repo_path).trace_flow(url_path, method=method)


@mcp.tool()
def brain_co_changes(file_path: str, days: int = 90, repo_path: str | None = None) -> dict:
    """Find files that historically change together with the given file."""
    return _get_brain(repo_path).co_change_analysis(file_path, days=days)


LEAN_CORE_TOOLS = {
    "brain_dossier",
    "brain_context",
    "brain_slice",
    "brain_patch",
    "brain_check",
    "brain_remember",
}


def main() -> None:
    if "--lean" in sys.argv or os.environ.get("PROJECTBRAIN_LEAN_MCP") == "1":
        tm = getattr(mcp, "_tool_manager", None)
        tools_dict = getattr(tm, "_tools", None)
        if isinstance(tools_dict, dict):
            for t_name in list(tools_dict.keys()):
                if t_name not in LEAN_CORE_TOOLS:
                    tools_dict.pop(t_name, None)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
