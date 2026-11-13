from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from projectbrain.brain import ProjectBrain
from projectbrain.cli import _resolve_mcp_server_command, main as cli_main
from projectbrain.hooks import (
    audit_edited_files,
    parse_hook_payload,
    run_post_edit_hook,
    run_pre_commit_hook,
)
from projectbrain.parsers import get_language, get_parser
from projectbrain.server import (
    LEAN_CORE_TOOLS,
    _is_unsafe_unindexed_root,
    _resolve_root,
    parse_server_args,
)


def test_lazy_treesitter_language_loader():
    """Verify lazy on-demand Tree-sitter C-extension grammar loading."""
    assert get_language(".py") is not None
    assert get_parser(".py") is not None
    assert get_parser(".ts") is not None
    assert get_parser(".tsx") is not None
    assert get_parser(".unknown_ext") is None


def test_hook_stdlib_fast_path_bailout_and_subprocess(tmp_path: Path):
    """Non-code files and non-edit tools must exit 0 silently without touching ProjectBrain."""
    (tmp_path / "README.md").write_text("# Docs\n", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}\n", encoding="utf-8")

    # 1. Claude PostToolUse on Markdown -> empty files, silent exit 0
    claude_md = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "README.md")},
    })
    parsed = parse_hook_payload(claude_md)
    assert parsed["protocol"] == "claude"
    assert parsed["files"] == []
    assert run_post_edit_hook(claude_md) == (0, "")

    # 2. Claude PostToolUse on Read / Bash tool -> ignored even if path is .py
    claude_read = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "app.py")},
    })
    assert parse_hook_payload(claude_read)["files"] == []
    assert run_post_edit_hook(claude_read) == (0, "")

    # 3. Cursor afterFileEdit on JSON -> silent exit 0
    cursor_json = json.dumps({
        "hook_event_name": "afterFileEdit",
        "workspace_roots": [str(tmp_path)],
        "file_path": str(tmp_path / "config.json"),
    })
    assert parse_hook_payload(cursor_json)["files"] == []
    assert run_post_edit_hook(cursor_json) == (0, "")

    # 4. End-to-end CLI subprocess fast-path on non-code file
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "projectbrain.cli", "--path", str(tmp_path), "hook", "post-edit"],
        input=claude_md,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": src_dir},
        timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (tmp_path / ".projectbrain.db").exists()


def test_silent_when_clean_body_only_edit(tmp_path: Path):
    """Clean internal body edits must emit 0 bytes to stdout while logging audit telemetry."""
    (tmp_path / "service.py").write_text(
        "def calculate_subtotal(qty: int, unit_price: int) -> int:\n"
        "    return qty * unit_price\n",
        encoding="utf-8",
    )
    (tmp_path / "handler.py").write_text(
        "from service import calculate_subtotal\n\n"
        "def checkout_endpoint(q: int) -> int:\n"
        "    return calculate_subtotal(q, 100)\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Clean body-only edit (signature, arity, syntax, boundaries all intact)
    (tmp_path / "service.py").write_text(
        "def calculate_subtotal(qty: int, unit_price: int) -> int:\n"
        "    gross = qty * unit_price\n"
        "    return max(gross, 0)\n",
        encoding="utf-8",
    )

    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "service.py")},
    })
    rc, out = run_post_edit_hook(payload)
    assert rc == 0
    assert out == ""

    stats = ProjectBrain(str(tmp_path)).stats()
    ic = stats["interceptions"]
    assert ic["audits_run"] == 1
    assert ic["clean_edits_audited"] == 1
    assert ic["total_interceptions"] == 0


def test_post_edit_guardrail_five_layers_across_cold_invocations(tmp_path: Path):
    """Verify all 5 guardrail layers (syntax, contract arity, boundary rules, signature blast, stale invariants)."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "db").mkdir()

    (tmp_path / "db" / "repo.py").write_text(
        "def fetch_invoice_row(invoice_id: str) -> dict:\n"
        "    return {'id': invoice_id, 'amount': 50}\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "billing.py").write_text(
        "from db.repo import fetch_invoice_row\n\n"
        "def settle_invoice(invoice_id: str) -> dict:\n"
        "    row = fetch_invoice_row(invoice_id)\n"
        "    return row\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "invoices.py").write_text(
        "from services.billing import settle_invoice\n\n"
        "def post_settle(inv_id: str) -> dict:\n"
        "    return settle_invoice(inv_id)\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember(
        forbid="routes/* -> db/*",
        note="HTTP routes must call services/*, never db/* directly.",
    )
    brain.remember(
        "settle_invoice",
        "Must preserve idempotency for already-settled invoices.",
    )

    # Layer 1: Pinpoint Syntax Error via Claude PostToolUse JSON protocol
    (tmp_path / "services" / "billing.py").write_text(
        "def settle_invoice(invoice_id: str) -> dict:\n"
        "    if invoice_id == :\n"
        "        return {}\n",
        encoding="utf-8",
    )
    claude_payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "services" / "billing.py")},
    })
    rc, stdout = run_post_edit_hook(claude_payload)
    assert rc == 0
    parsed_out = json.loads(stdout)
    ctx_msg = parsed_out["hookSpecificOutput"]["additionalContext"]
    assert "[SYNTAX ERROR]" in ctx_msg
    assert "services/billing.py:L2" in ctx_msg

    # Restore valid syntax so SQLite index is clean again before testing signature blast
    (tmp_path / "services" / "billing.py").write_text(
        "from db.repo import fetch_invoice_row\n\n"
        "def settle_invoice(invoice_id: str) -> dict:\n"
        "    row = fetch_invoice_row(invoice_id)\n"
        "    return row\n",
        encoding="utf-8",
    )
    ProjectBrain(str(tmp_path)).incremental_index()

    # Layer 2 + Layer 4 + Layer 5: Mutate signature (adding required arg `currency`), breaking caller arity & mutating invariant
    (tmp_path / "services" / "billing.py").write_text(
        "from db.repo import fetch_invoice_row\n\n"
        "def settle_invoice(invoice_id: str, currency: str) -> dict:\n"
        "    row = fetch_invoice_row(invoice_id)\n"
        "    row['currency'] = currency\n"
        "    return row\n",
        encoding="utf-8",
    )
    rc2, stdout2 = run_post_edit_hook(claude_payload)
    assert rc2 == 0
    ctx2 = json.loads(stdout2)["hookSpecificOutput"]["additionalContext"]
    # Contract arity break + Signature blast alert + Stale AST invariant warning!
    assert "routes/invoices.py" in ctx2
    assert "[SIGNATURE CHANGED]" in ctx2
    assert "[INVARIANT MUTATED — VERIFY STILL HOLDS]" in ctx2

    # Refresh invariant hash after verifying it still holds
    refreshed = ProjectBrain(str(tmp_path)).remember(target="settle_invoice", action="refresh")
    assert refreshed["updated_count"] == 1

    # Layer 3: Architectural Boundary Violation via Cursor `afterFileEdit` protocol
    (tmp_path / "routes" / "invoices.py").write_text(
        "from db.repo import fetch_invoice_row\n"
        "from services.billing import settle_invoice\n\n"
        "def post_settle(inv_id: str) -> dict:\n"
        "    _ = fetch_invoice_row(inv_id)\n"
        "    return settle_invoice(inv_id, 'USD')\n",
        encoding="utf-8",
    )
    cursor_payload = json.dumps({
        "hook_event_name": "afterFileEdit",
        "workspace_roots": [str(tmp_path)],
        "file_path": str(tmp_path / "routes" / "invoices.py"),
    })
    rc3, stdout3 = run_post_edit_hook(cursor_payload)
    assert rc3 == 0
    assert "[BOUNDARY VIOLATION]" in stdout3
    assert "routes/* -> db/*" in stdout3

    # Verify interception telemetry scorecard
    st = ProjectBrain(str(tmp_path)).stats()
    ic = st["interceptions"]
    assert ic["syntax_errors_caught"] >= 1
    assert ic["contract_violations_caught"] >= 1
    assert ic["boundary_violations_caught"] >= 1
    assert ic["signature_blast_alerts"] >= 1
    assert ic["stale_invariants_flagged"] >= 1
    assert ic["total_interceptions"] >= 5


def test_git_pre_commit_hook_blocks_violations_and_passes_clean(tmp_path: Path):
    """Git pre-commit guardrail must return exit code 1 on blocking violations and 0 when clean."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(tmp_path), check=True)

    (tmp_path / "ui").mkdir()
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "sql.py").write_text("def run_sql():\n    return 1\n", encoding="utf-8")
    (tmp_path / "ui" / "view.py").write_text("def render_view():\n    return 'ok'\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember(forbid="ui/* -> storage/*", note="UI cannot import storage directly")

    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    # Stage a boundary-violating edit
    (tmp_path / "ui" / "view.py").write_text(
        "from storage.sql import run_sql\n\n"
        "def render_view():\n"
        "    return str(run_sql())\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "ui/view.py"], cwd=str(tmp_path), check=True)

    rc_fail, msg_fail = run_pre_commit_hook(tmp_path)
    assert rc_fail == 1
    assert "[BOUNDARY VIOLATION]" in msg_fail

    # Fix violation and re-stage
    (tmp_path / "ui" / "view.py").write_text(
        "def render_view():\n"
        "    return 'clean'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "ui/view.py"], cwd=str(tmp_path), check=True)

    rc_ok, msg_ok = run_pre_commit_hook(tmp_path)
    assert rc_ok == 0
    assert msg_ok == ""


def test_mcp_server_lean_flag_root_parsing_and_unsafe_root_guard(tmp_path: Path):
    """Regression test: `projectbrain-mcp --lean /path/to/repo` must NEVER parse `--lean` as root path."""
    ns = parse_server_args(["--lean", str(tmp_path)])
    assert ns.lean is True
    assert ns.full is False
    assert ns.repo_path == str(tmp_path)
    assert Path(_resolve_root(["--lean", str(tmp_path)])) == tmp_path.resolve()

    ns_full = parse_server_args(["--full", "--path", str(tmp_path)])
    assert ns_full.full is True
    assert Path(_resolve_root(["--full", "--path", str(tmp_path)])) == tmp_path.resolve()

    assert LEAN_CORE_TOOLS == {
        "brain_dossier",
        "brain_slice",
        "brain_explore",
        "brain_check",
        "brain_remember",
    }

    # Unsafe root guard prevents walking `/` or `$HOME` if unindexed
    fs_root = Path(tmp_path.anchor)
    assert _is_unsafe_unindexed_root(fs_root) is True
    assert _is_unsafe_unindexed_root(tmp_path) is False


def test_enoent_proof_init_and_hook_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify ENOENT-proof MCP command resolution and active Claude/Cursor/Git hook generation."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "main.py").write_text("def start():\n    return True\n", encoding="utf-8")

    # Simulate GUI IDE where `projectbrain-mcp` is NOT on PATH -> falls back to `sys.executable -m projectbrain.server`
    monkeypatch.setattr("shutil.which", lambda name: None)
    cmd, args = _resolve_mcp_server_command(str(tmp_path.resolve()))
    assert cmd == sys.executable
    assert args == ["-m", "projectbrain.server", "--lean", str(tmp_path.resolve())]

    rc = cli_main(["--path", str(tmp_path), "init", "--ide", "all", "--git-hook"])
    assert rc == 0

    mcp_json = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["projectbrain"]["command"] == sys.executable
    assert "--lean" in mcp_json["mcpServers"]["projectbrain"]["args"]

    claude_hooks = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert "PreToolUse" not in claude_hooks["hooks"]
    post_hooks = claude_hooks["hooks"]["PostToolUse"]
    assert len(post_hooks) == 1
    assert post_hooks[0]["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    assert "hook post-edit" in post_hooks[0]["hooks"][0]["command"]

    cursor_hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert len(cursor_hooks["hooks"]["afterFileEdit"]) == 1
    assert "hook post-edit" in cursor_hooks["hooks"]["afterFileEdit"][0]["command"]

    git_hook = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "hook pre-commit" in git_hook

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".projectbrain.db" in gitignore
    assert ".projectbrain_memory.json" not in gitignore


def test_pdg_focus_slicing_cold_sqlite_and_warm_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Verify explicit `focus` intra-function PDG slicing on both cold SQLite and warm index paths."""
    lines = [
        "def compute_invoice(items: list[dict], promo_code: str, audit_enabled: bool) -> dict:",
        "    discount_total = 0",
        "    audit_entries = []",
    ]
    for i in range(1, 35):
        lines.append(f"    audit_step_{i} = f'step-{i}'")
        lines.append(f"    if audit_enabled: audit_entries.append(audit_step_{i})")
    lines.extend([
        "    if promo_code == 'SAVE20':",
        "        discount_total = sum(x.get('price', 0) for x in items) // 5",
        "    final_discount = max(discount_total, 0)",
        "    return {'discount': final_discount, 'audit_count': len(audit_entries)}",
    ])
    (tmp_path / "invoice.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ProjectBrain(str(tmp_path)).index()

    # 1. Cold SQLite fast-path (`self._index` is empty)
    cold_brain = ProjectBrain(str(tmp_path))
    assert not cold_brain._index
    cold_res = cold_brain.slice_symbols(["invoice.py:compute_invoice"], focus="discount_total")
    assert not cold_brain._index  # Served straight from SQLite!
    cold_code = cold_res["slices"][0]["code"]
    assert "discount_total" in cold_code
    assert "final_discount" in cold_code
    assert "folded" in cold_code

    # 2. Warm in-memory index path
    warm_brain = ProjectBrain(str(tmp_path))
    warm_brain.load()
    warm_res = warm_brain.slice_symbols(["invoice.py:compute_invoice"], focus=["discount_total"])
    warm_code = warm_res["slices"][0]["code"]
    assert "discount_total" in warm_code
    assert "folded" in warm_code

    # 3. CLI `--focus`
    capsys.readouterr()
    rc = cli_main(["--path", str(tmp_path), "slice", "invoice.py:compute_invoice", "--focus", "discount_total"])
    assert rc == 0
    cli_out = capsys.readouterr().out
    assert "discount_total" in cli_out


def test_cli_check_formats_ci_gate_and_memories_governance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Verify `check --format {text,markdown,github}`, `--ci`, and `memories` lifecycle commands."""
    (tmp_path / "api").mkdir()
    (tmp_path / "dao").mkdir()
    (tmp_path / "dao" / "users.py").write_text("def load_user(uid: int) -> dict:\n    return {'id': uid}\n", encoding="utf-8")
    (tmp_path / "api" / "routes.py").write_text(
        "from dao.users import load_user\n\n"
        "def get_user_route(uid: int) -> dict:\n"
        "    return load_user(uid)\n",
        encoding="utf-8",
    )

    assert cli_main(["--path", str(tmp_path), "index"]) == 0
    capsys.readouterr()

    # Pin boundary rule (immediately violated by api/routes.py -> dao/users.py) + invariant
    assert cli_main([
        "--path", str(tmp_path),
        "remember",
        "--forbid", "api/* -> dao/*",
        "--reason", "Controllers must go through service layer",
    ]) == 0
    forbid_res = json.loads(capsys.readouterr().out)
    assert forbid_res["status"] == "saved_boundary_rule"
    assert len(forbid_res["existing_violations"]) == 1

    assert cli_main([
        "--path", str(tmp_path),
        "remember",
        "load_user",
        "Must never return password hashes",
    ]) == 0
    capsys.readouterr()

    # 1. Markdown PR report
    rc_md = cli_main(["--path", str(tmp_path), "check", "api/routes.py", "--format", "markdown"])
    assert rc_md == 0
    md_out = capsys.readouterr().out
    assert "## ProjectBrain Blast-Radius & Contract Report" in md_out
    assert "ACTION REQUIRED" in md_out
    assert "api/* -> dao/*" in md_out

    # 2. GitHub Actions CI mode (`GITHUB_ACTIONS=true` + `--ci` -> exits 1 with `::error`)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc_ci = cli_main(["--path", str(tmp_path), "check", "api/routes.py", "--ci"])
    assert rc_ci == 1
    gh_out = capsys.readouterr().out
    assert "::error file=api/routes.py" in gh_out

    # 3. Memories list & forget boundary rule
    assert cli_main(["--path", str(tmp_path), "memories"]) == 0
    mem_txt = capsys.readouterr().out
    assert "[FORBID]" in mem_txt
    assert "Must never return password hashes" in mem_txt

    rule_id = forbid_res["boundary_rule"]["id"]
    assert cli_main(["--path", str(tmp_path), "memories", "--forget", rule_id]) == 0
    capsys.readouterr()

    # Now CI check passes with exit code 0!
    rc_ci_clean = cli_main(["--path", str(tmp_path), "check", "api/routes.py", "--ci"])
    assert rc_ci_clean == 0
    capsys.readouterr()

    # 4. Scorecard summary runs cleanly
    assert cli_main(["--path", str(tmp_path), "stats", "--summary"]) == 0
    scorecard = capsys.readouterr().out
    assert "ProjectBrain Guardrail & Code-Graph Scorecard" in scorecard


def test_zero_pollution_mcp_dossier_and_one_command_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Verify MCP `brain_dossier` never litters `.projectbrain_dossier.md` and `projectbrain clean` removes all footprints."""
    from projectbrain import server

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "app.py").write_text("def handle_order(order_id: int) -> int:\n    return order_id\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Team Header\nKeep existing team instructions.\n", encoding="utf-8")

    assert cli_main(["--path", str(tmp_path), "init", "--ide", "all", "--git-hook"]) == 0
    capsys.readouterr()

    # MCP `brain_dossier` returns markdown in memory without writing `.projectbrain_dossier.md` to disk
    res = server.brain_dossier("handle_order", repo_path=str(tmp_path))
    assert "handle_order" in res["markdown"]
    assert res["dossier_path"] is None
    assert not (tmp_path / ".projectbrain_dossier.md").exists()

    # `projectbrain clean` removes all caches, MCP entries, rule blocks, and hooks while keeping user AGENTS.md content
    assert cli_main(["--path", str(tmp_path), "clean"]) == 0
    clean_out = json.loads(capsys.readouterr().out)
    assert clean_out["status"] == "cleaned"
    assert not (tmp_path / ".projectbrain.db").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()
    agents_after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep existing team instructions." in agents_after
    assert "<!-- projectbrain:start -->" not in agents_after
