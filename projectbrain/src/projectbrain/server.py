"""ProjectBrain MCP Server (v1.3: Deep Semantic Triage, Cross-Stack HTTP Schemas & <8KB Inline Cap).

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
import json
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypeVar

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

_F = TypeVar("_F", bound=Callable[..., object])

# Antigravity / Claude Code / Cursor inline MCP tool cap is ~10 KB; staying strictly
# <= 7,800 serialized UTF-8 chars (with FastMCP's indent=2 formatting) guarantees ZERO
# `.system_generated/.../output.txt` spillovers and eliminates 2-turn `read_file` hops.
MCP_INLINE_CHAR_BUDGET = int(os.environ.get("PROJECTBRAIN_MCP_MAX_CHARS", "7800"))


def _unstructured_endpoint() -> Callable[[_F], _F]:
    """Register an MCP endpoint with `structured_output=False` so dicts serialize once (TextContent only)."""
    try:
        return mcp.tool(structured_output=False)  # type: ignore[return-value]
    except TypeError:
        return mcp.tool()  # type: ignore[return-value]


def _serialized_len(obj: object) -> int:
    try:
        return len(json.dumps(obj, indent=2, ensure_ascii=True))
    except Exception:
        return len(str(obj))


def _enforce_mcp_inline_budget(res: dict, max_chars: int = MCP_INLINE_CHAR_BUDGET) -> dict:
    """Guarantee every MCP tool payload serializes under `max_chars` (<8 KB inline, 0 file spillovers)."""
    if not isinstance(res, dict) or _serialized_len(res) <= max_chars:
        return res

    out = dict(res)

    # 1. Dossier payloads: strip duplicate structured arrays first, then bound markdown
    if "markdown" in out and isinstance(out["markdown"], str):
        for k_drop in ("sections", "target_slices", "neighbor_skeletons", "route_flows", "state_flows"):
            if k_drop in out and _serialized_len(out) > max_chars:
                out.pop(k_drop, None)
        if _serialized_len(out) > max_chars:
            overhead = _serialized_len({k: v for k, v in out.items() if k != "markdown"}) + 180
            md_budget = max(1200, max_chars - overhead)
            md_raw = out["markdown"]
            if len(md_raw) > md_budget:
                out["markdown"] = (
                    md_raw[:md_budget].rsplit("\n", 1)[0]
                    + "\n\n... [Trimmed to fit <8KB inline MCP cap; use `brain_slice` or `read_file` for full source]"
                )
                out["inline_budget_trimmed"] = True

    # 2. Slice payloads: trim individual `code` / `skeleton` strings proportionally
    if _serialized_len(out) > max_chars and isinstance(out.get("slices"), list) and out["slices"]:
        n_sl = max(1, len(out["slices"]))
        per_slice = max(450, (max_chars - 500) // n_sl)
        trimmed_slices: list[dict] = []
        for s in out["slices"]:
            if not isinstance(s, dict):
                continue
            sc = dict(s)
            for k_code in ("code", "skeleton"):
                val = sc.get(k_code)
                if isinstance(val, str) and len(val) > per_slice:
                    sc[k_code] = (
                        val[:per_slice].rsplit("\n", 1)[0]
                        + f"\n# ... [trimmed to fit <8KB inline cap; use read_file('{sc.get('rel_path', '')}')]"
                    )
                    if k_code == "skeleton" and sc.get("code") == val:
                        sc["code"] = sc["skeleton"]
            if sc.get("skeleton") and sc.get("code") == sc.get("skeleton"):
                sc.pop("skeleton", None)
            trimmed_slices.append(sc)
        out["slices"] = trimmed_slices
        out["inline_budget_trimmed"] = True

    # 3. Explore payloads: progressively trim `edges`, `nodes`, `steps`, `co_changes`
    if _serialized_len(out) > max_chars:
        for list_key, cap in (("edges", 18), ("nodes", 16), ("steps", 14), ("co_changes", 6), ("path", 16)):
            if isinstance(out.get(list_key), list) and len(out[list_key]) > cap:
                out[list_key] = out[list_key][:cap]
                out["inline_budget_trimmed"] = True

    # 4. Check / Impact payloads: compact verbose caller fanout & cap diagnostics
    if _serialized_len(out) > max_chars:
        imp = out.get("impact_analysis")
        if isinstance(imp, dict):
            imp_c = dict(imp)
            imp_c.pop("contract_diagnostics", None)  # already at top-level `out["contract_diagnostics"]`
            if isinstance(imp_c.get("all_impacted_files"), list) and len(imp_c["all_impacted_files"]) > 20:
                imp_c["all_impacted_files"] = imp_c["all_impacted_files"][:20]
            if isinstance(imp_c.get("details"), list):
                compact_details = []
                for det in imp_c["details"][:10]:
                    if isinstance(det, dict):
                        dc = dict(det)
                        if isinstance(dc.get("called_by"), list) and len(dc["called_by"]) > 6:
                            dc["called_by"] = dc["called_by"][:6]
                        if isinstance(dc.get("direct_consumers"), list) and len(dc["direct_consumers"]) > 8:
                            dc["direct_consumers"] = dc["direct_consumers"][:8]
                        compact_details.append(dc)
                imp_c["details"] = compact_details
            out["impact_analysis"] = imp_c
            out["inline_budget_trimmed"] = True
        if out.get("ranked_hotspots") and _serialized_len(out) > max_chars:
            out.pop("static_audit_findings", None)
            out["inline_budget_trimmed"] = True
        for diag_key, d_cap in (
            ("ranked_hotspots", 16),
            ("contract_diagnostics", 16),
            ("semantic_warnings", 10),
            ("static_audit_findings", 14),
            ("signature_blast", 6),
            ("memories", 8),
            ("stale_memories", 8),
        ):
            if isinstance(out.get(diag_key), list) and len(out[diag_key]) > d_cap:
                out[diag_key] = out[diag_key][:d_cap]
                out["inline_budget_trimmed"] = True

    # 5. Final hard safety valve: shrink largest remaining list/dict/string fields until <= max_chars
    guard_iters = 0
    while _serialized_len(out) > max_chars and guard_iters < 18:
        guard_iters += 1
        out["inline_budget_trimmed"] = True
        best_key = max(
            (k for k in out if k not in ("verdict", "all_contracts_valid", "error", "center", "audit_coverage")),
            key=lambda k: _serialized_len(out[k]),
            default=None,
        )
        if best_key is None:
            break
        val = out[best_key]
        if isinstance(val, list) and len(val) > 2:
            out[best_key] = val[: max(2, len(val) // 2)]
        elif isinstance(val, str) and len(val) > 400:
            out[best_key] = val[: max(250, len(val) // 2)] + " ... [trimmed]"
        elif isinstance(val, dict) and val:
            sub_key = max(val, key=lambda sk: _serialized_len(val[sk]), default=None)
            sub_val = val.get(sub_key) if sub_key is not None else None
            if sub_key is not None and isinstance(sub_val, list) and len(sub_val) > 2:
                out[best_key] = {**val, sub_key: sub_val[: max(2, len(sub_val) // 2)]}
            elif len(val) > 2:
                keys_keep = list(val.keys())[: max(2, len(val) // 2)]
                out[best_key] = {k: val[k] for k in keys_keep}
            else:
                out.pop(best_key, None)
        else:
            out.pop(best_key, None)

    return out


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

@_unstructured_endpoint()
def brain_dossier(
    tasks: list[str] | str,
    impact_files: list[str] | None = None,
    zoom: bool = False,
    include_markdown: bool = True,
    repo_path: str | None = None,
) -> dict:
    """Compile a self-contained inline Engineering Dossier (<8KB) in ONE call.

    Use when investigating an unfamiliar multi-file flow across a large repo (>30 files).
    Skip this tool and use native `rg` + `read_file` directly when the repo/feature spans <=6 well-named
    files or when reading small (<350-line) / monolithic single-component files.
    """
    task_list = [tasks] if isinstance(tasks, str) else list(tasks)
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.compile_dossier(
            task_list,
            impact_files=impact_files,
            zoom=zoom,
            write_file=False,
        )
    if not (include_markdown or zoom):
        res = {k: v for k, v in res.items() if k != "markdown"}
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_slice(
    targets: list[str] | str,
    focus: list[str] | str | None = None,
    max_lines_per_symbol: int = 120,
    repo_path: str | None = None,
) -> dict:
    """Surgically read functions, classes, file skeletons, or intra-function PDG slices in <20ms.

    When to use vs `read_file`:
      - If a file is <=350 lines or you are auditing a giant React JSX monolith component end-to-end,
        prefer `read_file` in 1 turn rather than slicing piecemeal.
      - Use `brain_slice` for targeted symbols (`"orders.py:validate"`), file skeletons (`"src/orders.py"`),
        monolith internal component maps, or variable PDG slices (`focus="discount_code"`).
      - Cross-language identifier matching (`"send_email"` <-> `"sendEmail"`) is automatic.
    """
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.slice_symbols(
            targets,
            max_lines_per_symbol=max_lines_per_symbol,
            focus=focus,
        )
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_explore(
    target: str,
    target_b: str | None = None,
    edge_types: list[str] | None = None,
    depth: int = 2,
    include_co_changes: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Navigate the code graph (<8KB inline): trace callers/callees, shortest paths, or full-stack HTTP routes.

    Modes:
      1. Path between two symbols (`target` + `target_b`): shortest typed call/import/route chain.
      2. Full-stack route trace (`target` starts with `/` or `"POST /api/..."`, e.g. `"/api/v1/orders"`):
         verb-agnostic trace from UI `fetch`/axios/`${baseUrl}` callers -> router mount -> handler -> callees.
      3. Symbol/file neighborhood (`target` only, `snake_case` <-> `camelCase` aware): `depth`-hop callers/callees.
    Note: For small files (<=350 lines), prefer `read_file` directly over exploring file AST edges.
    """
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.explore(
            target,
            target_b=target_b,
            edge_types=edge_types,
            depth=depth,
            include_co_changes=include_co_changes,
        )
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_check(
    files_modified: list[str] | None = None,
    claims: list[list[str]] | None = None,
    plan_files: list[str] | None = None,
    task: str = "",
    base_ref: str | None = None,
    allow_governance_change: bool = False,
    audit: bool = False,
    scope_glob: str | None = None,
    repo_path: str | None = None,
) -> dict:
    """Sub-2s verification, deep semantic repo bug triage & blast-radius gate (<8KB inline).

    Modes:
      - Whole-repo deep semantic triage (`audit=True`, optional `scope_glob="frontend/*"`): scans the entire
        repo in <1.5s (even when git is dirty) and returns `ranked_hotspots` + `recommended_read` spans across
        cross-stack HTTP/Pydantic schemas (`PB011`), React stateful remount/race bugs (`PB012`), Python
        `.model_dump()` partial-update clobbers & silent exception drops (`PB013`), and 6D/cross-file contracts.
      - Post-edit / PR merge-base gate (`files_modified=[...]`, `base_ref="main"`, or git diff): checks AST validity,
        cross-stack HTTP/Pydantic/SQL/arity/import contracts, React lifecycle bugs, boundary rules, and callers.
      - Pre-edit plan gate (`plan_files=[...]`, `task="..."`) or fact-check (`claims=[["A", "calls", "B"]]`).
    """
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.check(
            files_modified=files_modified,
            claims=claims,
            plan_files=plan_files,
            task=task,
            base_ref=base_ref,
            allow_governance_change=allow_governance_change,
            audit=audit,
            scope_glob=scope_glob,
        )
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
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
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.remember(
            target=target,
            note=note,
            tags=tags,
            forbid=forbid,
            co_change=co_change,
            action=action,
        )
    return _enforce_mcp_inline_budget(res)


# ── Auxiliary / Legacy Tools (`--full` Mode) ─────────────────────────────────

@_unstructured_endpoint()
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
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.learn(
            apply=apply,
            pin_hubs=pin_hubs,
            max_rules=max_rules,
            days=days,
            min_cochanges=min_cochanges,
            min_confidence=min_confidence,
        )
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_context(
    task: str,
    budget_tokens: int = 3000,
    top_n: int = 4,
    repo_path: str | None = None,
) -> dict:
    """Compile token-budgeted target slices and 1-hop neighbor signature skeletons."""
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.compile_context(task, budget_tokens=budget_tokens, top_n=top_n)
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_stats(
    repo_path: str | None = None,
    reset: bool = False,
) -> dict:
    """Return index topology, guardrail interception scorecard, and resolution telemetry."""
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.stats(reset=reset)
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_patch(
    spec: str,
    apply: bool = False,
    repo_path: str | None = None,
) -> dict:
    """Legacy AST symbol/anchor patch tool (only exposed in `--full` mode)."""
    b = _get_brain(repo_path)
    with b._op_lock:
        res = b.patch(spec, apply=apply)
    return _enforce_mcp_inline_budget(res)


@_unstructured_endpoint()
def brain_run(
    raw_output: str = "",
    command: str = "",
    max_lines: int = 80,
    timeout: int = 120,
    repo_path: str | None = None,
) -> dict:
    """Compress raw terminal/test output with Code Graph stack-trace frame resolution."""
    b = _get_brain(repo_path)
    with b._op_lock:
        if command.strip():
            parsed = parse_server_args(sys.argv[1:])
            allow_exec = parsed.allow_exec or (os.environ.get("PROJECTBRAIN_ALLOW_EXEC") == "1")
            if not allow_exec:
                return {
                    "error": "Direct command execution over MCP is disabled by default for security. Pass `raw_output` to compress terminal/test logs, or set `PROJECTBRAIN_ALLOW_EXEC=1`.",
                    "exit_code": 126,
                }
            res = b.run_command_compressed(command.strip(), timeout=timeout, max_lines=max_lines)
        else:
            res = b.compress_command_output(raw_output, max_lines=max_lines)
    return _enforce_mcp_inline_budget(res)


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
    os.environ.setdefault("PROJECTBRAIN_MCP_STDIO", "1")
    parsed = parse_server_args(argv if argv is not None else sys.argv[1:])
    full_mode = parsed.full or (os.environ.get("PROJECTBRAIN_FULL_TOOLS") == "1")
    configure_mcp_tools(full_mode=full_mode)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
