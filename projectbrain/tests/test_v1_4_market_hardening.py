"""Tests for v1.4.0 Market Hardening & Satellite Coordinate Router:
1. `.projectbrain.toml` / `[tool.projectbrain]` config, `--profile`, `--select`, `--ignore`, `exclude`.
2. Inline suppressions (`# pb:ignore PBxxx`, `// pb:ignore PBxxx`), and PB007 tamper-lock non-suppressibility.
3. Python `if TYPE_CHECKING:` & TypeScript `import type` runtime exclusion + schema resolution preservation.
4. Flat-package `learn` safety (`include_module_rules=False` default vs opt-in).
5. Zero-write `projectbrain audit` CLI (zero dotfiles created on disk).
6. Inline `# @pb-invariant:` / `// @pb-invariant:` extraction, auto-surfacing in check, and `--sync-annotations`.
7. Satellite Coordinate Router (`projectbrain route` & `brain_dossier(..., coordinates_only=True)`).
8. `PB012` tightened `AbortSignal` check & `PB011` parametric URL-template verb mismatch.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from projectbrain.brain import ProjectBrain
from projectbrain.cli import main as cli_main
from projectbrain.config import _parse_toml_fallback, load_repo_config, parse_inline_suppressions


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_git(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")


def test_toml_config_profiles_and_exclude_globs(tmp_path: Path):
    """Verify `.projectbrain.toml`, `[tool.projectbrain]`, fallback parser, profiles, and exclude globs."""
    # 1. Fallback TOML parser handles Python 3.10 cleanly
    raw_toml = """
# ProjectBrain config
profile = "fullstack"
ignore = ["PB003", "PB006"]
exclude = ["generated/*"]
"""
    parsed_fb = _parse_toml_fallback(raw_toml)
    assert parsed_fb["profile"] == "fullstack"
    assert parsed_fb["ignore"] == ["PB003", "PB006"]
    assert parsed_fb["exclude"] == ["generated/*"]

    # 2. `[tool.projectbrain]` in pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        '[tool.projectbrain]\nprofile = "governance"\nexclude = ["legacy/*"]\n',
        encoding="utf-8",
    )
    cfg1 = load_repo_config(tmp_path)
    assert cfg1["source"] == "pyproject.toml"
    assert cfg1["profile"] == "governance"
    assert cfg1["exclude"] == ["legacy/*"]

    # 3. `.projectbrain.toml` takes precedence over `pyproject.toml`
    (tmp_path / ".projectbrain.toml").write_text(
        'profile = "fullstack"\nignore = ["PB013"]\nexclude = ["generated/*"]\n',
        encoding="utf-8",
    )
    cfg2 = load_repo_config(tmp_path)
    assert cfg2["source"] == ".projectbrain.toml"
    assert cfg2["profile"] == "fullstack"
    assert cfg2["ignore"] == ["PB013"]

    # 4. Verify rule profile & exclude filtering during audit
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "broken.py").write_text(
        "from pydantic import BaseModel\n"
        "class U(BaseModel):\n"
        "    name: str | None = None\n"
        "def update_u(row, payload: U):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        setattr(row, k, v)\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from pydantic import BaseModel\n"
        "class P(BaseModel):\n"
        "    bio: str | None = None\n"
        "def update_p(row, payload: P):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        setattr(row, k, v)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    # With `.projectbrain.toml` ignoring PB013 and excluding generated/*, audit is clean!
    res_ignored = brain.check(audit=True, zero_write=True)
    assert res_ignored["all_contracts_valid"] is True
    assert res_ignored["active_profile"] == "fullstack"
    assert res_ignored["ranked_hotspots"] == []

    # Explicit `--select PB013` overrides config profile (still respecting exclude)
    (tmp_path / ".projectbrain.toml").write_text('exclude = ["generated/*"]\n', encoding="utf-8")
    res_sel = brain.check(audit=True, select_rules=["PB013"], zero_write=True)
    assert res_sel["all_contracts_valid"] is False
    flagged_files = {h["rel_path"] for h in res_sel["ranked_hotspots"]}
    assert "app.py" in flagged_files
    assert "generated/broken.py" not in flagged_files


def test_inline_suppressions_and_tamper_lock_immunity(tmp_path: Path):
    """Inline `# pb:ignore` / `// pb:ignore` suppress targeted findings, but NEVER PB007 tamper lock."""
    sup = parse_inline_suppressions(
        "x = 1  # pb:ignore PB013 -- deliberate full overwrite\n"
        "// pb:ignore PB011, PB012 -- legacy widget\n"
        "useEffect(() => {}, [id]);\n"
    )
    assert "PB013" in sup[1]
    assert "PB011" in sup[3] and "PB012" in sup[3]

    # End-to-end suppression on a real PB013 finding
    (tmp_path / "models.py").write_text(
        "from pydantic import BaseModel\n\n"
        "class SettingsPatch(BaseModel):\n"
        "    theme: str | None = None\n"
        "    locale: str | None = None\n\n"
        "def update_settings(row, payload: SettingsPatch):\n"
        "    # pb:ignore PB013 -- full reset intended\n"
        "    data = payload.model_dump()\n"
        "    for k, v in data.items():\n"
        "        setattr(row, k, v)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    assert res["all_contracts_valid"] is True
    assert res["ranked_hotspots"] == []
    assert res.get("suppressed_count", 0) >= 1

    # Verify PB007 Governance Tamper Lock CANNOT be suppressed by comments
    git_repo = tmp_path / "tamper_repo"
    git_repo.mkdir()
    _init_git(git_repo)
    (git_repo / "app.py").write_text("# pb:ignore PB007\ndef ok():\n    return 1\n", encoding="utf-8")
    (git_repo / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    b_git = ProjectBrain(str(git_repo))
    b_git.index()
    b_git.remember(forbid="routes/* -> db/*", note="Layered architecture")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "init governance")

    # Tamper with governance file by wiping boundary rules
    (git_repo / ".projectbrain_memory.json").write_text('{"version": 2, "notes": []}\n', encoding="utf-8")
    chk = b_git.check()
    assert chk["verdict"] == "UNSAFE_GOVERNANCE_TAMPER"
    assert chk["all_contracts_valid"] is False


def test_type_checking_and_ts_import_type_excluded_from_runtime_boundaries_and_learn(tmp_path: Path):
    """Python `if TYPE_CHECKING:` and TS `import type` must NOT create runtime boundary violations or learn edges."""
    for d in ("routes", "services", "db"):
        (tmp_path / d).mkdir()

    # Runtime flow is strictly routes -> services -> db; db only imports routes under TYPE_CHECKING!
    (tmp_path / "db" / "repo.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from routes.api import RouteCtx\n\n"
        "def fetch_row(ctx: 'RouteCtx | None' = None) -> dict:\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "svc.py").write_text(
        "from db.repo import fetch_row\n\n"
        "def run_svc() -> dict:\n"
        "    return fetch_row()\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "api.py").write_text(
        "from services.svc import run_svc\n\n"
        "class RouteCtx:\n"
        "    request_id: str\n\n"
        "def handler() -> dict:\n"
        "    return run_svc()\n",
        encoding="utf-8",
    )
    # TypeScript `import type` from db into routes must NOT trigger a runtime `routes/* -> db/*` violation
    (tmp_path / "db" / "types.ts").write_text(
        "export interface OrderRow { id: string; }\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "orders.ts").write_text(
        "import type { OrderRow } from '../db/types';\n"
        "export function formatOrder(o: OrderRow): string { return o.id; }\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Runtime import_targets must exclude both Python TYPE_CHECKING and TS `import type`
    assert "routes/api.py" not in brain._import_targets.get("db/repo.py", [])
    assert "db/types.ts" not in brain._import_targets.get("routes/orders.ts", [])
    # Symbol bindings MUST still resolve type-only imports for schema/type inspection!
    assert brain._symbol_imports.get("db/repo.py", {}).get("RouteCtx") == "routes/api.py"
    assert brain._symbol_imports.get("routes/orders.ts", {}).get("OrderRow") == "db/types.ts"

    # Forbidding `db/* -> routes/*` and `routes/* -> db/*` must pass with 0 violations!
    brain.remember(forbid="db/* -> routes/*", note="DB cannot depend on routes at runtime")
    brain.remember(forbid="routes/* -> db/*", note="Routes cannot depend on DB at runtime")
    chk = brain.check(files_modified=["db/repo.py", "routes/orders.ts"])
    assert chk["all_contracts_valid"] is True
    assert not chk.get("boundary_violations")


def test_flat_package_learn_skips_module_bans_by_default(tmp_path: Path):
    """Flat single-package repos must not emit noisy file-to-file bans unless `--include-module-rules` is passed."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text("def core_fn() -> int:\n    return 1\n", encoding="utf-8")
    (pkg / "cli.py").write_text("from pkg.core import core_fn\ndef run() -> int:\n    return core_fn()\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    default_learn = brain.learn(apply=False)
    assert default_learn["discovered"]["boundary_rules"] == []

    opt_in_learn = brain.learn(apply=False, include_module_rules=True)
    assert len(opt_in_learn["discovered"]["boundary_rules"]) >= 1


def test_zero_write_audit_cli_leaves_no_dotfiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`projectbrain audit` must run cleanly without creating `.projectbrain.db` or any config dotfiles."""
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n\n"
        "router = APIRouter(prefix='/api')\n\n"
        "class ItemIn(BaseModel):\n"
        "    title: str\n"
        "    price_cents: int\n\n"
        "@router.post('/items')\n"
        "def create_item(body: ItemIn):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "client.ts").write_text(
        "export async function addItem() {\n"
        "  await fetch('/api/items', {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify({ title: 'Book' }),\n"
        "  });\n"
        "}\n",
        encoding="utf-8",
    )

    rc = cli_main(["--path", str(tmp_path), "audit", "--profile", "fullstack", "--fail-on-violation"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PB011" in out
    assert "price_cents" in out
    assert "profile=fullstack" in out
    # Strictly zero dotfiles written to disk!
    assert not (tmp_path / ".projectbrain.db").exists()
    assert not (tmp_path / ".projectbrain_ledger.json").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_inline_pb_invariant_surfacing_and_sync(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Inline `# @pb-invariant:` / `// @pb-invariant:` comments surface automatically in check and sync with AST hashes."""
    (tmp_path / "billing.py").write_text(
        "def decorator(f):\n"
        "    return f\n\n"
        "# @pb-invariant: Must remain idempotent on duplicate invoice_id\n"
        "@decorator\n"
        "def settle_invoice(invoice_id: str) -> bool:\n"
        "    return bool(invoice_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "ui.ts").write_text(
        "// @pb-invariant: Never render unescaped user HTML\n"
        "export function renderBio(raw: string): string {\n"
        "  return raw.trim();\n"
        "}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Surfaces automatically during `check` WITHOUT manually calling `remember`
    chk = brain.check(files_modified=["billing.py", "ui.ts"])
    mems = chk.get("memories", [])
    notes_txt = [m["note"] for m in mems]
    assert "Must remain idempotent on duplicate invoice_id" in notes_txt
    assert "Never render unescaped user HTML" in notes_txt
    assert any(m.get("source") == "inline_annotation" for m in mems)

    # 2. `remember --sync-annotations` locks AST hashes into `.projectbrain_memory.json`
    rc_sync = cli_main(["--path", str(tmp_path), "remember", "--sync-annotations"])
    assert rc_sync == 0
    sync_out = json.loads(capsys.readouterr().out)
    assert sync_out["discovered_count"] == 2
    assert sync_out["added_count"] == 2

    # 3. Mutating the annotated function triggers STALE AST warning!
    (tmp_path / "billing.py").write_text(
        "def decorator(f):\n"
        "    return f\n\n"
        "# @pb-invariant: Must remain idempotent on duplicate invoice_id\n"
        "@decorator\n"
        "def settle_invoice(invoice_id: str) -> bool:\n"
        "    return len(invoice_id) > 2\n",
        encoding="utf-8",
    )
    chk_after = brain.check(files_modified=["billing.py"])
    stale_notes = [m["note"] for m in chk_after.get("stale_memories", [])]
    assert "Must remain idempotent on duplicate invoice_id" in stale_notes


def test_satellite_coordinate_router_cli_and_mcp(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`projectbrain route` & `brain_dossier(coordinates_only=True)` return exact coordinates with zero code dumps."""
    from projectbrain.server import brain_dossier

    (tmp_path / "db").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "db" / "ledger.py").write_text(
        "def record_settlement(tx_id: str, cents: int) -> dict:\n"
        "    return {'tx_id': tx_id, 'cents': cents}\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "billing.py").write_text(
        "from db.ledger import record_settlement\n\n"
        "# @pb-invariant: Reject negative settlement amounts\n"
        "def settle_invoice(tx_id: str, cents: int) -> dict:\n"
        "    if cents < 0:\n"
        "        raise ValueError('negative')\n"
        "    return record_settlement(tx_id, cents)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_billing.py").write_text(
        "from services.billing import settle_invoice\n\n"
        "def test_settle():\n"
        "    assert settle_invoice('t1', 50)['cents'] == 50\n",
        encoding="utf-8",
    )

    # CLI zero-write satellite route
    rc = cli_main(["--path", str(tmp_path), "route", "where is invoice settlement recorded", "--json"])
    assert rc == 0
    cli_res = json.loads(capsys.readouterr().out)
    assert cli_res["mode"] == "satellite_coordinates"
    assert "markdown" not in cli_res
    assert not (tmp_path / ".projectbrain.db").exists()  # zero-write when unindexed!

    syms = [c["symbol"] for c in cli_res["coordinates"]]
    assert "settle_invoice" in syms
    settle_coord = next(c for c in cli_res["coordinates"] if c["symbol"] == "settle_invoice")
    assert settle_coord["recommended_read"].startswith("services/billing.py:")
    assert any("record_settlement" in cl for cl in settle_coord["callees"])
    assert "tests/test_billing.py" in cli_res["related_tests"]
    assert any("Reject negative settlement amounts" in inv["note"] for inv in cli_res["invariants"])

    # MCP parity via `brain_dossier(..., coordinates_only=True)`
    mcp_res = brain_dossier(
        tasks="where is invoice settlement recorded",
        coordinates_only=True,
        repo_path=str(tmp_path),
    )
    assert mcp_res["mode"] == "satellite_coordinates"
    assert "markdown" not in mcp_res
    assert len(json.dumps(mcp_res)) < 4000


def test_pb012_signal_variable_does_not_mask_race_and_pb011_parametric_verb(tmp_path: Path):
    """Variable named `signalStrength` must not mask PB012 async race; concrete IDs must match `{user_id}` verbs."""
    (tmp_path / "backend.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "@router.patch('/users/{user_id}')\n"
        "def patch_user(user_id: str):\n"
        "    return {'id': user_id}\n",
        encoding="utf-8",
    )
    (tmp_path / "Widget.tsx").write_text(
        "import { useEffect, useState } from 'react';\n\n"
        "export function SignalWidget({ deviceId }: { deviceId: string }) {\n"
        "  const [signalStrength, setSignalStrength] = useState(0);\n"
        "  useEffect(() => {\n"
        "    fetch('/api/v1/users/u_42', { method: 'PUT', headers: { 'X-Device': deviceId } })\n"
        "      .then((r) => r.json())\n"
        "      .then((d) => setSignalStrength(Number(d.id || 0)));\n"
        "  }, [deviceId]);\n"
        "  return <div>{signalStrength}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    cats = {(h["rule_id"], h["category"]) for h in res["ranked_hotspots"]}
    assert ("PB011", "http_verb_mismatch") in cats
    assert ("PB012", "uncancelled_async_effect_race") in cats


def test_pb011_list_map_zero_fp_and_typed_res_json_and_extra_ignore_severity(tmp_path: Path):
    """PB011: list[Model].map() has 0 FP on valid fields, catches bad item fields, supports typed res.json(),
    and downgrades fuzzy optional-field extra keys under default extra='ignore' to non-blocking warning."""
    (tmp_path / "backend.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "class TaskItem(BaseModel):\n"
        "    task_id: str\n"
        "    title: str\n\n"
        "class InvoiceDto(BaseModel):\n"
        "    invoice_id: str\n"
        "    total_cents: int\n\n"
        "class ProfileUpdate(BaseModel):\n"
        "    label: str | None = None\n"
        "    bio: str | None = None\n\n"
        "@router.get('/tasks', response_model=list[TaskItem])\n"
        "def list_tasks():\n"
        "    return []\n\n"
        "@router.get('/invoice', response_model=InvoiceDto)\n"
        "def get_invoice():\n"
        "    return {'invoice_id': '1', 'total_cents': 10}\n\n"
        "@router.patch('/profile')\n"
        "def patch_profile(body: ProfileUpdate):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    # 1. Valid list.map((t) => t.title) + default extra='ignore' fuzzy optional field -> 0 blocking errors!
    (tmp_path / "SafeClient.ts").write_text(
        "interface Task { task_id: string; title: string; }\n\n"
        "export async function loadTaskTitles() {\n"
        "  const res = await fetch('/api/v1/tasks');\n"
        "  const tasks: Task[] = await res.json();\n"
        "  return tasks.map((t) => t.title);\n"
        "}\n\n"
        "export async function updateOptionalExtra() {\n"
        "  await fetch('/api/v1/profile', {\n"
        "    method: 'PATCH',\n"
        "    body: JSON.stringify({ bio: 'hi', ui_label: 'x' }),\n"
        "  });\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res_safe = brain.check(audit=True, profile="fullstack", zero_write=True)
    assert res_safe["all_contracts_valid"] is True
    assert not any(
        "title" in (h.get("summary") or "") for h in res_safe["ranked_hotspots"]
    )
    # The fuzzy optional mismatch on extra='ignore' is at most a non-blocking warning
    assert all(h["severity"] != "error" for h in res_safe["ranked_hotspots"])

    # 2. Invalid list item read + typed TS `const data: InvoiceDto = await res.json()` + `as InvoiceDto`
    (tmp_path / "BrokenClient.ts").write_text(
        "interface InvoiceDto { invoice_id: string; bogus_field: number; }\n\n"
        "export async function loadBrokenTasks() {\n"
        "  const res = await fetch('/api/v1/tasks');\n"
        "  const tasks = (await res.json()) as Array<{ missing_title: string }>;\n"
        "  return tasks.map((t) => t.missing_title);\n"
        "}\n\n"
        "export async function loadTypedInvoice() {\n"
        "  const res = await fetch('/api/v1/invoice');\n"
        "  const data: InvoiceDto = await res.json();\n"
        "  return data.bogus_field;\n"
        "}\n",
        encoding="utf-8",
    )
    brain2 = ProjectBrain(str(tmp_path))
    res_bad = brain2.check(audit=True, profile="fullstack", zero_write=True)
    assert res_bad["all_contracts_valid"] is False
    summaries = [h["summary"] for h in res_bad["ranked_hotspots"]]
    assert any("missing_title" in s for s in summaries)
    assert any("bogus_field" in s for s in summaries)


def test_config_tamper_lock_blocks_toml_weakening_and_pb007_ignore(tmp_path: Path):
    """PB007 Tamper Lock cannot be bypassed by `.projectbrain.toml` `ignore = ['PB007']` or `exclude = ['**']`."""
    from projectbrain.hooks import run_post_edit_hook, run_pre_commit_hook

    _init_git(tmp_path)
    (tmp_path / "ui").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "repo.py").write_text("def query():\n    return 1\n", encoding="utf-8")
    (tmp_path / "ui" / "view.py").write_text("def render():\n    return 'ok'\n", encoding="utf-8")
    brain = ProjectBrain(str(tmp_path))
    brain.remember(forbid="ui/* -> db/*", note="UI must not touch DB")
    _git(tmp_path, "add", "db/repo.py", "ui/view.py", ".projectbrain_memory.json")
    _git(tmp_path, "commit", "-m", "initial governance")

    # Attack: drop a `.projectbrain.toml` trying to silence PB007, PB002, and exclude everything
    (tmp_path / ".projectbrain.toml").write_text(
        'ignore = ["PB007", "PB002"]\nexclude = ["**"]\n',
        encoding="utf-8",
    )
    chk = brain.check(files_modified=[".projectbrain.toml"])
    assert chk["all_contracts_valid"] is False
    assert chk["verdict"] == "UNSAFE_GOVERNANCE_TAMPER"
    assert chk.get("governance_tamper")

    # Post-edit hook catches `.projectbrain.toml` tampering
    rc_post, out_post = run_post_edit_hook(explicit_files=[".projectbrain.toml"], default_root=tmp_path)
    assert rc_post == 0
    assert "GOVERNANCE TAMPER" in out_post

    # Pre-commit hook blocks staging `.projectbrain.toml` weakening
    _git(tmp_path, "add", ".projectbrain.toml")
    rc_pre, out_pre = run_pre_commit_hook(tmp_path)
    assert rc_pre == 1
    assert "GOVERNANCE TAMPER" in out_pre

    # Explicit `--allow-governance-change` permits authorized config updates
    chk_allowed = brain.check(files_modified=[".projectbrain.toml"], allow_governance_change=True)
    assert chk_allowed["governance_tamper_allowed"] is True
    assert chk_allowed["all_contracts_valid"] is True


def test_post_edit_hook_catches_body_only_semantic_and_inline_invariant_edits(tmp_path: Path):
    """Cold-SQLite hook fast-path must NEVER skip body-only PB011, PB012, PB013, or inline `@pb-invariant:` edits."""
    from projectbrain.hooks import run_post_edit_hook

    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "from pydantic import BaseModel\n\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "def get_db():\n"
        "    return None\n\n"
        "class OrderPatch(BaseModel):\n"
        "    customer_id: str\n"
        "    note: str | None = None\n\n"
        "# @pb-invariant: Must never overwrite unset columns\n"
        "@router.patch('/orders/{order_id}')\n"
        "def patch_order(order_id: str, payload: OrderPatch, db=Depends(get_db)):\n"
        "    row = db.get(order_id)\n"
        "    for k, v in payload.model_dump(exclude_unset=True).items():\n"
        "        setattr(row, k, v)\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "DraftBox.tsx").write_text(
        "import { useState } from 'react';\n"
        "export function DraftBox() {\n"
        "  const [text, setText] = useState('');\n"
        "  return <input value={text} onChange={(e) => setText(e.target.value)} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "Page.tsx").write_text(
        "import { useState } from 'react';\n"
        "import { DraftBox } from './DraftBox';\n\n"
        "export function OrderPage() {\n"
        "  const [saving, setSaving] = useState(false);\n"
        "  async function onSave() {\n"
        "    setSaving(true);\n"
        "    await fetch('/api/v1/orders/o_1', {\n"
        "      method: 'PATCH',\n"
        "      body: JSON.stringify({ customer_id: 'c_1' }),\n"
        "    });\n"
        "    setSaving(false);\n"
        "  }\n"
        "  return <div><button onClick={onSave}>Save</button><DraftBox /></div>;\n"
        "}\n",
        encoding="utf-8",
    )

    # Pre-index cleanly to create `.projectbrain.db`
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    assert (tmp_path / ".projectbrain.db").exists()

    # Body-only edit 1: remove `exclude_unset=True` (same signature, imports, calls) -> catches PB013 + inline invariant!
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "from pydantic import BaseModel\n\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "def get_db():\n"
        "    return None\n\n"
        "class OrderPatch(BaseModel):\n"
        "    customer_id: str\n"
        "    note: str | None = None\n\n"
        "# @pb-invariant: Must never overwrite unset columns\n"
        "@router.patch('/orders/{order_id}')\n"
        "def patch_order(order_id: str, payload: OrderPatch, db=Depends(get_db)):\n"
        "    row = db.get(order_id)\n"
        "    for k, v in payload.model_dump().items():\n"
        "        setattr(row, k, v)\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    _, out_py = run_post_edit_hook(explicit_files=["backend/api.py"], default_root=tmp_path)
    assert "pydantic_partial_update_clobber" in out_py
    assert "Must never overwrite unset columns" in out_py

    # Body-only edit 2: break PB011 payload key + PB012 unmount guard in Page.tsx (same signature/imports/calls!)
    (tmp_path / "frontend" / "Page.tsx").write_text(
        "import { useState } from 'react';\n"
        "import { DraftBox } from './DraftBox';\n\n"
        "export function OrderPage() {\n"
        "  const [saving, setSaving] = useState(false);\n"
        "  async function onSave() {\n"
        "    setSaving(true);\n"
        "    await fetch('/api/v1/orders/o_1', {\n"
        "      method: 'PATCH',\n"
        "      body: JSON.stringify({ customerId: 'c_1' }),\n"
        "    });\n"
        "    setSaving(false);\n"
        "  }\n"
        "  if (saving) return <div>Saving...</div>;\n"
        "  return <div><button onClick={onSave}>Save</button><DraftBox /></div>;\n"
        "}\n",
        encoding="utf-8",
    )
    _, out_tsx = run_post_edit_hook(explicit_files=["frontend/Page.tsx"], default_root=tmp_path)
    assert "customerId" in out_tsx
    assert "stateful_child_unmount_guard" in out_tsx


def test_pb012_terminal_view_zero_fp_and_race_recall_improvements(tmp_path: Path):
    """PB012 skips one-way terminal screens (`submitted`) while catching container ternaries, point-free `.then(setItems)`,
    unrelated `.current?.focus()`, and 1-hop local async helpers inside `useEffect`."""
    (tmp_path / "DraftBox.tsx").write_text(
        "import { useState } from 'react';\n"
        "export function DraftBox() {\n"
        "  const [val, setVal] = useState('');\n"
        "  return <input value={val} onChange={(e) => setVal(e.target.value)} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "SafeCheckout.tsx").write_text(
        "import { useState } from 'react';\n"
        "import { DraftBox } from './DraftBox';\n\n"
        "export function SafeCheckout() {\n"
        "  const [submitted, setSubmitted] = useState(false);\n"
        "  async function onConfirm() {\n"
        "    await Promise.resolve();\n"
        "    setSubmitted(true);\n"
        "  }\n"
        "  if (submitted) return <div>Order Confirmed!</div>;\n"
        "  return <div><button onClick={onConfirm}>Confirm</button><DraftBox /></div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "RaceCases.tsx").write_text(
        "import { useEffect, useRef, useState } from 'react';\n"
        "import { DraftBox } from './DraftBox';\n\n"
        "export function ContainerTernary({ id }: { id: string }) {\n"
        "  const [saving, setSaving] = useState(false);\n"
        "  async function save() { setSaving(true); await Promise.resolve(); setSaving(false); }\n"
        "  return <div><button onClick={save}>{id}</button>{saving ? <span>Wait</span> : <section><DraftBox /></section>}</div>;\n"
        "}\n\n"
        "export function PointFreeAndFocus({ orgId }: { orgId: string }) {\n"
        "  const [items, setItems] = useState<unknown[]>([]);\n"
        "  const boxRef = useRef<HTMLDivElement>(null);\n"
        "  useEffect(() => {\n"
        "    boxRef.current?.focus();\n"
        "    fetch(`/api/items/${orgId}`).then((r) => r.json()).then(setItems);\n"
        "  }, [orgId]);\n"
        "  return <div ref={boxRef}>{items.length}</div>;\n"
        "}\n\n"
        "export function LocalAsyncHelper({ orgId }: { orgId: string }) {\n"
        "  const [items, setItems] = useState<unknown[]>([]);\n"
        "  const loadItems = async () => {\n"
        "    const r = await fetch(`/api/items/${orgId}`);\n"
        "    setItems(await r.json());\n"
        "  };\n"
        "  useEffect(() => { loadItems(); }, [orgId]);\n"
        "  return <div>{items.length}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    safe_hits = [h for h in res["ranked_hotspots"] if h["rel_path"] == "SafeCheckout.tsx"]
    assert safe_hits == []
    race_hits = [h for h in res["ranked_hotspots"] if h["rel_path"] == "RaceCases.tsx"]
    cats = [h["category"] for h in race_hits]
    assert cats.count("stateful_child_unmount_guard") == 1
    assert cats.count("uncancelled_async_effect_race") == 2


def test_pb013_webhook_and_session_http_swallowed_io(tmp_path: Path):
    """PB013 catches silent `except Exception: pass` around `send_webhook(...)` and `session.post(...)`."""
    (tmp_path / "hooks_worker.py").write_text(
        "def notify_partner(webhook_client, session, payload):\n"
        "    try:\n"
        "        webhook_client.send_webhook('https://partner.example/hook', payload)\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        session.post('https://partner.example/audit', json=payload)\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    hits = [h for h in res["ranked_hotspots"] if h["rule_id"] == "PB013" and h["category"] == "swallowed_io_exception"]
    assert len(hits) == 2


def test_pb002_real_import_line_decorator_suppression_multiline_toml_and_idempotent_sync(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Verify multi-line TOML arrays, real PB002 import line numbers, decorator-top `# pb:ignore`, and idempotent sync."""
    # 1. Multi-line TOML array parsing in Python 3.10 fallback
    fb = _parse_toml_fallback(
        'profile = "fullstack"\n'
        'ignore = [\n'
        '  "PB003",\n'
        '  "PB006",\n'
        ']\n'
        'exclude = [\n'
        '  "generated/*",\n'
        ']\n'
    )
    assert fb["ignore"] == ["PB003", "PB006"]
    assert fb["exclude"] == ["generated/*"]

    # 2. PB002 reports real import line number (not hardcoded line 1) so `# pb:ignore PB002` above import works!
    (tmp_path / "ui").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (tmp_path / "ui" / "screen.py").write_text(
        '"""UI screen docstring."""\n\n'
        "import os\n\n"
        "from db.models import User\n\n"
        "def show():\n"
        "    return User\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.remember(forbid="ui/* -> db/*", note="UI must not import DB")
    chk_v = brain.check(files_modified=["ui/screen.py"])
    assert chk_v["all_contracts_valid"] is False
    bvs = chk_v["boundary_violations"]
    assert len(bvs) == 1 and bvs[0]["line"] == 5

    # Place `# pb:ignore PB002` directly above line 5 -> MUST STILL BE BLOCKED by default (pinned --forbid immunity!)
    (tmp_path / "ui" / "screen.py").write_text(
        '"""UI screen docstring."""\n\n'
        "import os\n\n"
        "# pb:ignore PB002 -- temporary migration bridge\n"
        "from db.models import User\n\n"
        "def show():\n"
        "    return User\n",
        encoding="utf-8",
    )
    chk_still_blocked = brain.check(files_modified=["ui/screen.py"])
    assert chk_still_blocked["all_contracts_valid"] is False
    assert len(chk_still_blocked["boundary_violations"]) == 1

    # Explicit opt-in via `.projectbrain.toml` `allow_inline_boundary_ignore = true` allows `# pb:ignore PB002` on line 5
    (tmp_path / ".projectbrain.toml").write_text("allow_inline_boundary_ignore = true\n", encoding="utf-8")
    chk_sup = brain.check(files_modified=["ui/screen.py"])
    assert chk_sup["all_contracts_valid"] is True
    assert chk_sup["suppressed_count"] >= 1
    (tmp_path / ".projectbrain.toml").unlink()

    # 3. `# pb:ignore PB013` directly above `@router.patch(...)` suppresses PB013 inside the decorated function!
    (tmp_path / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class ItemPatch(BaseModel):\n"
        "    title: str | None = None\n\n"
        "# pb:ignore PB013 -- full replace endpoint\n"
        "@router.patch('/items/{item_id}')\n"
        "def replace_item(item_id: str, payload: ItemPatch, row=None):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        setattr(row, k, v)\n",
        encoding="utf-8",
    )
    chk_dec = brain.check(audit=True, select_rules=["PB013"], zero_write=True)
    assert chk_dec["all_contracts_valid"] is True

    # 4. `remember --sync-annotations` reconciles deleted/edited inline invariants while keeping manual notes
    brain.remember("db/models.py", "Manually pinned DB invariant")
    (tmp_path / "service.py").write_text(
        "# @pb-invariant: Original inline rule\n"
        "def process():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert cli_main(["--path", str(tmp_path), "remember", "--sync-annotations"]) == 0
    sync1 = json.loads(capsys.readouterr().out)
    assert sync1["added_count"] == 1 and sync1["removed_count"] == 0

    # Edit the inline annotation text -> old inline note must be pruned, manual note preserved!
    (tmp_path / "service.py").write_text(
        "# @pb-invariant: Updated inline rule\n"
        "def process():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    assert cli_main(["--path", str(tmp_path), "remember", "--sync-annotations"]) == 0
    sync2 = json.loads(capsys.readouterr().out)
    assert sync2["added_count"] == 1
    assert sync2["removed_count"] == 1
    all_notes = [n["note"] for n in brain._memory.list_all()]
    assert "Updated inline rule" in all_notes
    assert "Original inline rule" not in all_notes
    assert "Manually pinned DB invariant" in all_notes


def test_reporters_audit_dedup_non_python_tests_and_clean_workflow_template(tmp_path: Path):
    """Audit text output deduplicates hotspots, formats TS tests without `pytest`, and workflow template has no author branch."""
    from projectbrain.hooks import write_github_action_workflow
    from projectbrain.reporters import format_check_report

    # 1. Audit text output prints each PB011 hotspot once (no duplicate raw markdown `error:` line)
    audit_res = {
        "verdict": "UNSAFE_CONTRACT_VIOLATION",
        "all_contracts_valid": False,
        "contract_diagnostics": [
            "- ⚠️ **[Cross-Stack HTTP Missing Required Field]** `src/App.tsx:L12` (`save`) → omits `amount`."
        ],
        "ranked_hotspots": [
            {
                "rank": 1,
                "rule_id": "PB011",
                "severity": "error",
                "rel_path": "src/App.tsx",
                "line": 12,
                "recommended_read": "src/App.tsx:4-26",
                "summary": "POST /api/invoices omits required field amount (422)",
            }
        ],
        "audit_coverage": {
            "files_scanned": 5,
            "clean_files_verified": 4,
            "flagged_files_count": 1,
            "elapsed_ms": 42.0,
        },
        "impact_analysis": {
            "details": [
                {"rel_path": "src/App.tsx", "risk": "low", "called_by": [], "direct_consumers": [], "test_consumers": ["src/App.test.tsx"]}
            ]
        },
    }
    txt = format_check_report(audit_res, fmt="text")
    assert txt.count("src/App.tsx") >= 1
    assert "  error:" not in txt
    assert "hotspot #1 [PB011/error]" in txt
    assert "npx vitest run src/App.test.tsx" in txt
    assert "pytest src/App.test.tsx" not in txt

    # 2. GitHub Actions workflow template contains no leaked author-repo local install branch
    wf_yaml = write_github_action_workflow(tmp_path).read_text(encoding="utf-8")
    assert "projectbrain/pyproject.toml" not in wf_yaml
    assert "pip install projectbrain-mcp" in wf_yaml


def test_zero_config_pb007_tamper_lock_blocks_new_toml_and_pyproject_weakening(tmp_path: Path):
    """P0-1: Even on a zero-config repo (no .projectbrain.toml or memory on main), adding a new
    config weakening rules, adding exclusions, or enabling `allow_inline_boundary_ignore` is blocked by PB007."""
    from projectbrain.hooks import run_pre_commit_hook

    _init_git(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter(prefix='/api')\n"
        "class OrderIn(BaseModel):\n"
        "    customer_id: str\n"
        "    amount: int\n"
        "@router.post('/orders')\n"
        "def create_order(body: OrderIn):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "client.ts").write_text(
        "export async function makeOrder() {\n"
        "  return fetch('/api/orders', { method: 'POST', body: JSON.stringify({ customer_id: 'c1', amount: 10 }) });\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "zero-config baseline")

    # Feature branch introduces guaranteed-422 PB011 bug AND tries to silence it with a brand-new .projectbrain.toml
    _git(tmp_path, "checkout", "-b", "feat/bypass-attempt")
    (tmp_path / "frontend" / "client.ts").write_text(
        "export async function makeOrder() {\n"
        "  return fetch('/api/orders', { method: 'POST', body: JSON.stringify({ customer_id: 'c1' }) });\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / ".projectbrain.toml").write_text(
        'ignore = ["PB011", "PB012", "PB013"]\nexclude = ["**"]\nallow_inline_boundary_ignore = true\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".projectbrain.toml", "frontend/client.ts")
    rc_pre, msg_pre = run_pre_commit_hook(tmp_path)
    assert rc_pre == 1
    assert "GOVERNANCE TAMPER" in msg_pre

    rc_ci = cli_main(["--path", str(tmp_path), "check", "--base", "main", "--profile", "fullstack", "--ci"])
    assert rc_ci == 1

    # Remove .projectbrain.toml and attempt the same bypass via `[tool.projectbrain]` in pyproject.toml
    (tmp_path / ".projectbrain.toml").unlink()
    _git(tmp_path, "reset", "HEAD", ".projectbrain.toml")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[tool.projectbrain]\nignore = ["PB011"]\nexclude = ["frontend/*"]\n',
        encoding="utf-8",
    )
    rc_ci_pyproj = cli_main(["--path", str(tmp_path), "check", "--base", "main", "--profile", "fullstack", "--ci"])
    assert rc_ci_pyproj == 1


def test_post_edit_hook_catches_body_only_pb003_kwarg_and_wrapper_var_and_precommit_blocks_pb011(tmp_path: Path):
    """P0-2 & P2-10: Post-edit hook catches body-only PB003 bad kwargs and 1-hop wrapper calls via local var
    on already-indexed repos, and default pre-commit blocks guaranteed-422 PB011 errors."""
    from projectbrain.hooks import run_post_edit_hook, run_pre_commit_hook

    _init_git(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "tax.py").write_text(
        "def compute_tax(amount: float, rate: float) -> float:\n"
        "    return amount * rate\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "service.py").write_text(
        "from backend.tax import compute_tax\n\n"
        "def calc(amount: float) -> float:\n"
        "    return compute_tax(amount, 0.1)\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter(prefix='/api')\n"
        "class OrderIn(BaseModel):\n"
        "    customer_id: str\n"
        "    amount: int\n"
        "@router.post('/orders')\n"
        "def create_order(body: OrderIn):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "client.ts").write_text(
        "export async function submitOrder(payload: Record<string, unknown>) {\n"
        "  const res = await fetch('/api/orders', { method: 'POST', body: JSON.stringify(payload) });\n"
        "  return await res.json();\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "caller.ts").write_text(
        "import { submitOrder } from './client';\n\n"
        "export async function sendOrder() {\n"
        "  const body = { customer_id: 'c1', amount: 50 };\n"
        "  return await submitOrder(body);\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "clean baseline")

    # Pre-index cleanly to create `.projectbrain.db`
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    assert (tmp_path / ".projectbrain.db").exists()

    # Case A (PB003): body-only edit introduces unknown kwarg (same signature, imports, callee name!)
    (tmp_path / "backend" / "service.py").write_text(
        "from backend.tax import compute_tax\n\n"
        "def calc(amount: float) -> float:\n"
        "    return compute_tax(amount, bad_kw=99)\n",
        encoding="utf-8",
    )
    _, out_kw = run_post_edit_hook(explicit_files=["backend/service.py"], default_root=tmp_path)
    assert "bad_kw" in out_kw

    # Case B (PB011): body-only edit on 1-hop wrapper via local var `body` (no `fetch`, `axios`, or `({`)
    (tmp_path / "frontend" / "caller.ts").write_text(
        "import { submitOrder } from './client';\n\n"
        "export async function sendOrder() {\n"
        "  const body = { customer_id: 'c1' };\n"
        "  return await submitOrder(body);\n"
        "}\n",
        encoding="utf-8",
    )
    _, out_wrap = run_post_edit_hook(explicit_files=["frontend/caller.ts"], default_root=tmp_path)
    assert "amount" in out_wrap

    # Default pre-commit hook (strict=False) MUST block guaranteed-422 PB011 contract errors
    _git(tmp_path, "add", "frontend/caller.ts")
    rc_pre, msg_pre = run_pre_commit_hook(tmp_path, strict=False)
    assert rc_pre == 1
    assert "amount" in msg_pre


def test_multiline_decorators_sync_preserves_stale_hash_and_per_symbol_forgery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """P1-5 & P1-6: Multi-line decorators preserve `# @pb-invariant:` and `# pb:ignore`, `--sync-annotations`
    never clears stale `PB008` hashes, and `git_diff` catches cross-symbol hash forgery in the same file."""
    from projectbrain.git_diff import check_governance_tamper

    _init_git(tmp_path)
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class ItemPatch(BaseModel):\n"
        "    title: str | None = None\n\n"
        "# @pb-invariant: Must audit every update\n"
        "# pb:ignore PB013 -- deliberate full overwrite\n"
        "@router.patch(\n"
        "    '/items/{item_id}',\n"
        "    tags=['items'],\n"
        ")\n"
        "def patch_item(item_id: str, payload: ItemPatch, row=None):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        setattr(row, k, v)\n"
        "    return {'ok': True}\n\n"
        "def decoy_helper():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Multi-line decorator `# pb:ignore PB013` suppresses PB013 AND `# @pb-invariant:` is discovered!
    chk_ml = brain.check(audit=True, select_rules=["PB013"], zero_write=True)
    assert chk_ml["all_contracts_valid"] is True
    assert chk_ml["suppressed_count"] >= 1

    capsys.readouterr()
    assert cli_main(["--path", str(tmp_path), "remember", "--sync-annotations"]) == 0
    sync1 = json.loads(capsys.readouterr().out)
    assert sync1["discovered_count"] == 1 and sync1["added_count"] == 1

    _git(tmp_path, "add", "api.py", ".projectbrain_memory.json", ".gitignore")
    _git(tmp_path, "commit", "-m", "baseline multiline invariant")

    # 2. Mutate `patch_item` -> running `--sync-annotations` MUST preserve stale hash (NOT silently clear PB008!)
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class ItemPatch(BaseModel):\n"
        "    title: str | None = None\n\n"
        "# @pb-invariant: Must audit every update\n"
        "# pb:ignore PB013 -- deliberate full overwrite\n"
        "@router.patch(\n"
        "    '/items/{item_id}',\n"
        "    tags=['items'],\n"
        ")\n"
        "def patch_item(item_id: str, payload: ItemPatch, row=None):\n"
        "    return {'ok': False}\n\n"
        "def decoy_helper():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    assert cli_main(["--path", str(tmp_path), "remember", "--sync-annotations"]) == 0
    sync2 = json.loads(capsys.readouterr().out)
    assert sync2["stale_preserved_count"] == 1
    chk_stale = brain.check(files_modified=["api.py"])
    assert len(chk_stale.get("stale_memories", [])) == 1

    # 3. Forging `ast_hash` to `decoy_helper`'s valid hash in the SAME file MUST trigger `invariant_hash_forged`!
    brain.index()
    decoy_hash = next(fn["ast_hash"] for fn in brain._index["api.py"]["functions"] if fn["name"] == "decoy_helper")
    mem_path = tmp_path / ".projectbrain_memory.json"
    mem_data = json.loads(mem_path.read_text(encoding="utf-8"))
    mem_data["notes"][0]["ast_hash"] = decoy_hash
    mem_path.write_text(json.dumps(mem_data, indent=2) + "\n", encoding="utf-8")
    tampers = check_governance_tamper(tmp_path, ref_commit="HEAD")
    assert any(tp["kind"] == "invariant_hash_forged" for tp in tampers)


def test_github_and_pr_comment_reporters_surface_warnings_and_exact_audit_lines_and_route_steps(tmp_path: Path):
    """P1-4, P2-9, P2-11: `--format github` & `pr-comment` surface PB012/PB013 warnings and real file count;
    audit text shows exact `file:L<line>`; `route` prints actual route flow steps."""
    from projectbrain.dossier import format_route_coordinates
    from projectbrain.reporters import format_check_report

    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "@router.post('/notify')\n"
        "def notify(webhook_client):\n"
        "    try:\n"
        "        webhook_client.send_webhook('https://example.com/h', {'ok': True})\n"
        "    except Exception:\n"
        "        pass\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "Page.tsx").write_text(
        "import { useEffect, useState } from 'react';\n"
        "export function NotifyPage({ q }: { q: string }) {\n"
        "  const [items, setItems] = useState<unknown>(null);\n"
        "  useEffect(() => {\n"
        "    fetch(`/api/v1/notify?q=${q}`, { method: 'POST' }).then((r) => r.json()).then(setItems);\n"
        "  }, [q]);\n"
        "  return <div>{String(items)}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)

    # 1. `--format github` emits ::warning for BOTH PB012 and PB013 warnings
    gh_out = format_check_report(res, fmt="github")
    assert "::warning file=backend/api.py,line=8," in gh_out
    assert "::warning file=frontend/Page.tsx,line=4," in gh_out
    assert "warnings=2" in gh_out

    # 2. `--format pr-comment` surfaces BOTH warnings and reports real audited file count (NOT 0!)
    pr_out = format_check_report(res, fmt="pr-comment")
    assert "Semantic Hazards & Async Race Advisories" in pr_out
    assert "`backend/api.py:L8`" in pr_out
    assert "`frontend/Page.tsx:L4`" in pr_out
    assert "`2`" in pr_out and "`0` file(s) audited" not in pr_out

    # 3. `--format text` on audit prints exact clickable `file:L<line>`
    txt_out = format_check_report(res, fmt="text")
    assert "backend/api.py:L8" in txt_out
    assert "frontend/Page.tsx:L4" in txt_out

    # 4. `route` prints actual route flow steps instead of just `N step(s)`
    route_res = brain.route("how does /api/v1/notify work")
    route_txt = format_route_coordinates(route_res)
    assert "backend/api.py" in route_txt and "notify" in route_txt
    assert "step(s)" not in route_txt


def test_same_file_express_router_and_untyped_fastapi_default(tmp_path: Path):
    """FN-7 & FN-8: Same-file `express.Router()` mounts resolve prefixes for verb checks,
    and FastAPI untyped default params (`row=None`) do not poison Pydantic body schema extraction."""
    (tmp_path / "server.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "const r = express.Router();\n"
        "r.post('/users', (req, res) => res.json({ ok: true }));\n"
        "app.use('/api/v2', r);\n",
        encoding="utf-8",
    )
    (tmp_path / "backend.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter(prefix='/api/v2')\n"
        "class ItemIn(BaseModel):\n"
        "    title: str\n"
        "    price_cents: int\n"
        "@router.post('/items')\n"
        "def create_item(body: ItemIn, row=None):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "client.ts").write_text(
        "export async function runAll() {\n"
        "  await fetch('/api/v2/users', { method: 'GET' });\n"
        "  await fetch('/api/v2/items', { method: 'POST', body: JSON.stringify({ title: 'Book' }) });\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    cats = {(h["rule_id"], h["category"]) for h in res["ranked_hotspots"]}
    assert ("PB011", "http_verb_mismatch") in cats
    assert ("PB011", "http_missing_required_field") in cats


def test_p0_large_repo_precommit_after_post_edit_and_git_rm_and_staged_index_blob(tmp_path: Path):
    """P0-1 & P0-3: >200-file repo pre-commit catches 1-hop PB011 wrapper right after post-edit,
    audits staged git index blobs (even if working tree is cleaned), and blocks `git rm` breaking importers."""
    from projectbrain.hooks import run_post_edit_hook, run_pre_commit_hook

    _init_git(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "filler").mkdir()
    (tmp_path / "backend" / "tax.py").write_text("def compute_tax(x: float) -> float:\n    return x * 0.1\n", encoding="utf-8")
    (tmp_path / "backend" / "service.py").write_text(
        "from backend.tax import compute_tax\n\ndef calc(x: float) -> float:\n    return compute_tax(x)\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter(prefix='/api')\n"
        "class OrderIn(BaseModel):\n"
        "    customer_id: str\n"
        "    amount: int\n"
        "@router.post('/orders')\n"
        "def create_order(body: OrderIn):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "client.ts").write_text(
        "export async function submitOrder(payload: Record<string, unknown>) {\n"
        "  const res = await fetch('/api/orders', { method: 'POST', body: JSON.stringify(payload) });\n"
        "  return await res.json();\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "caller.ts").write_text(
        "import { submitOrder } from './client';\n\n"
        "export async function sendOrder() {\n"
        "  const body = { customer_id: 'c1', amount: 50 };\n"
        "  return await submitOrder(body);\n"
        "}\n",
        encoding="utf-8",
    )
    # Create 205 filler files so repo exceeds the 200-file partial-neighborhood threshold
    for i in range(205):
        (tmp_path / "filler" / f"m_{i:03d}.py").write_text(f"def f_{i}(v: int) -> int:\n    return v + {i}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "210-file baseline")

    ProjectBrain(str(tmp_path)).index()

    # 1. Break 1-hop wrapper via local var -> post-edit catches & persists -> pre-commit MUST STILL exit 1!
    (tmp_path / "frontend" / "caller.ts").write_text(
        "import { submitOrder } from './client';\n\n"
        "export async function sendOrder() {\n"
        "  const body = { customer_id: 'c1' };\n"
        "  return await submitOrder(body);\n"
        "}\n",
        encoding="utf-8",
    )
    _, out_post = run_post_edit_hook(explicit_files=["frontend/caller.ts"], default_root=tmp_path)
    assert "amount" in out_post

    _git(tmp_path, "add", "frontend/caller.ts")
    rc_pre1, msg_pre1 = run_pre_commit_hook(tmp_path)
    assert rc_pre1 == 1 and "amount" in msg_pre1

    # 2. Clean working tree (unstaged!) while broken blob stays staged in git index -> pre-commit MUST STILL exit 1!
    (tmp_path / "frontend" / "caller.ts").write_text(
        "import { submitOrder } from './client';\n\n"
        "export async function sendOrder() {\n"
        "  const body = { customer_id: 'c1', amount: 50 };\n"
        "  return await submitOrder(body);\n"
        "}\n",
        encoding="utf-8",
    )
    rc_pre2, msg_pre2 = run_pre_commit_hook(tmp_path)
    assert rc_pre2 == 1 and "amount" in msg_pre2

    # 3. Unstage caller.ts, stage `git rm backend/tax.py` while `backend/service.py` imports it -> pre-commit MUST exit 1!
    _git(tmp_path, "reset", "HEAD", "frontend/caller.ts")
    _git(tmp_path, "rm", "backend/tax.py")
    rc_del, msg_del = run_pre_commit_hook(tmp_path)
    assert rc_del == 1 and ("tax.py" in msg_del or "service.py" in msg_del)


def test_p0_mcp_blocks_agent_self_refresh_and_find_git_context_isolation(tmp_path: Path):
    """P0-2 & P0-4: MCP `brain_remember` blocks agent `refresh`/`forget` by default,
    and `find_git_context` never leaks non-git temp directories into `$HOME/.git`."""
    import tempfile as _tf
    from projectbrain.git_diff import find_git_context
    from projectbrain.server import brain_remember

    (tmp_path / "api.py").write_text("def charge(amount: int) -> int:\n    return amount\n", encoding="utf-8")
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember("charge", "Must never charge negative amount")

    # Mutate `charge` so invariant is stale, then attempt MCP agent self-refresh and forget
    (tmp_path / "api.py").write_text("def charge(amount: int) -> int:\n    return -amount\n", encoding="utf-8")
    ref_res = brain_remember(target="charge", action="refresh", repo_path=str(tmp_path))
    assert ref_res["status"] == "blocked_by_governance_policy"
    fgt_res = brain_remember(target="charge", action="forget", repo_path=str(tmp_path))
    assert fgt_res["status"] == "blocked_by_governance_policy"

    # Invariant must remain intact and STALE!
    chk = brain.check(files_modified=["api.py"])
    assert len(chk.get("stale_memories", [])) == 1

    # Non-git directory directly inside OS TEMP (without `pytest-of-` in path) must return `(None, '')`
    with _tf.TemporaryDirectory(prefix="pb_nongit_iso_") as raw_td:
        g_root, _ = find_git_context(Path(raw_td))
        assert g_root is None


def test_p1_pb013_safe_guards_and_sqlmodel_helper_and_dict_union(tmp_path: Path):
    """P1-1 & P1-6: `if v is None: continue` and `model_fields_set` produce 0 FP;
    `save_profile` calling `.sqlmodel_update(...)` and `existing | payload.model_dump()` are caught."""
    (tmp_path / "profiles.py").write_text(
        "from pydantic import BaseModel\n\n"
        "class ProfilePatch(BaseModel):\n"
        "    bio: str | None = None\n"
        "    theme: str | None = None\n\n"
        "def safe_if_none_continue(row, payload: ProfilePatch):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        if v is None:\n"
        "            continue\n"
        "        setattr(row, k, v)\n\n"
        "def safe_model_fields_set(row, payload: ProfilePatch):\n"
        "    for k, v in payload.model_dump().items():\n"
        "        if k in payload.model_fields_set:\n"
        "            setattr(row, k, v)\n\n"
        "def save_profile_helper(row, payload: ProfilePatch):\n"
        "    row.sqlmodel_update(payload.model_dump())\n\n"
        "def merge_profile_union(existing: dict, payload: ProfilePatch):\n"
        "    return existing | payload.model_dump()\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, select_rules=["PB013"], zero_write=True)
    hots = res["ranked_hotspots"]
    assert len(hots) == 2
    lines = {h["line"] for h in hots}
    assert all(ln >= 17 for ln in lines)


def test_p1_pb011_alias_strict_jsx_in_word_express_mw_custom_axios_and_pb012_and_list_key(tmp_path: Path):
    """P1-2..P1-5: Strict Pydantic alias without populate_by_name, JSX 'in ' text, Express middleware mount,
    custom axios.create(), `{!saving && <Child/>}`, keyed list behind `if (refreshing)`, and axios body `{ signal }`."""
    (tmp_path / "backend.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel, Field\n"
        "router = APIRouter(prefix='/api/v1')\n\n"
        "class StrictAliasIn(BaseModel):\n"
        "    first_name: str = Field(alias='firstName')\n\n"
        "class UserOut(BaseModel):\n"
        "    user_id: str\n"
        "    name: str\n\n"
        "@router.post('/strict-alias', response_model=UserOut)\n"
        "def strict_alias(body: StrictAliasIn):\n"
        "    return {'user_id': 'u1', 'name': body.first_name}\n",
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n"
        "const r2 = express.Router();\n"
        "r2.post('/hooks', (req, res) => res.json({ ok: true }));\n"
        "app.use('/api/exp2', authMiddleware, r2);\n",
        encoding="utf-8",
    )
    (tmp_path / "DraftBox.tsx").write_text(
        "import { useState } from 'react';\n"
        "export function DraftBox({ id }: { id?: string }) {\n"
        "  const [val, setVal] = useState(id || '');\n"
        "  return <input value={val} onChange={(e) => setVal(e.target.value)} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Cases.tsx").write_text(
        "import axios from 'axios';\n"
        "import { useEffect, useState } from 'react';\n"
        "import { DraftBox } from './DraftBox';\n\n"
        "const backendApi = axios.create({ baseURL: '' });\n\n"
        "export async function caseStrictAliasAndCustomAxios() {\n"
        "  await backendApi.post('/api/v1/strict-alias', { first_name: 'Ada' });\n"
        "  await fetch('/api/exp2/hooks', { method: 'GET' });\n"
        "}\n\n"
        "export async function caseWordInJsx() {\n"
        "  const res = await fetch('/api/v1/strict-alias', { method: 'POST', body: JSON.stringify({ firstName: 'Ada' }) });\n"
        "  const data = await res.json();\n"
        "  return <span>Logged in as {data.bad_prop_after_in_word}</span>;\n"
        "}\n\n"
        "export function LogicalAndUnmount() {\n"
        "  const [saving, setSaving] = useState(false);\n"
        "  async function save() { setSaving(true); await Promise.resolve(); setSaving(false); }\n"
        "  return <div><button onClick={save}>Save</button>{!saving && <DraftBox />}</div>;\n"
        "}\n\n"
        "export function ListKeyUnmount({ items }: { items: { id: string }[] }) {\n"
        "  const [refreshing, setRefreshing] = useState(false);\n"
        "  async function reload() { setRefreshing(true); await Promise.resolve(); setRefreshing(false); }\n"
        "  if (refreshing) return <div>Loading...</div>;\n"
        "  return <div><button onClick={reload}>Reload</button>{items.map((it) => <DraftBox key={it.id} id={it.id} />)}</div>;\n"
        "}\n\n"
        "export function AxiosBodySignalRace({ sym, tradeSignal }: { sym: string; tradeSignal: string }) {\n"
        "  const [out, setOut] = useState<unknown>(null);\n"
        "  useEffect(() => {\n"
        "    axios.post('/api/v1/strict-alias', { firstName: sym, signal: tradeSignal }).then((r) => setOut(r.data));\n"
        "  }, [sym]);\n"
        "  return <div>{String(out)}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    hots = res["ranked_hotspots"]
    summaries = " | ".join(h["summary"] for h in hots)
    assert "first_name" in summaries and "firstName" in summaries
    assert "bad_prop_after_in_word" in summaries
    assert any(h["category"] == "http_verb_mismatch" for h in hots)
    assert sum(1 for h in hots if h["category"] == "stateful_child_unmount_guard") == 2
    assert any(h["category"] == "uncancelled_async_effect_race" for h in hots)


def test_p1_p2_react_fc_ignore_empty_route_zero_write_markdown_files_and_route_handler_name(tmp_path: Path):
    """P1-7, P2-1, P2-2, P2-3: `// pb:ignore` propagates across typed `React.FC`, empty-dir `route` writes zero dotfiles,
    `audit --format markdown` shows real file count, and `route` prints backend handler name."""
    from projectbrain.dossier import format_route_coordinates
    from projectbrain.reporters import format_check_report

    # 1. Empty directory `route` must NOT create `.projectbrain.db`
    empty_dir = tmp_path / "empty_proj"
    empty_dir.mkdir()
    assert cli_main(["--path", str(empty_dir), "route", "anything"]) == 0
    assert list(empty_dir.iterdir()) == []

    # 2. `// pb:ignore PB012` above typed `React.FC` suppresses inside component body
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "Comp.tsx").write_text(
        "import React, { useEffect, useState } from 'react';\n\n"
        "// pb:ignore PB012 -- intentional\n"
        "export const Comp: React.FC<{ q: string }> = ({ q }) => {\n"
        "  const [items, setItems] = useState<unknown>(null);\n"
        "  useEffect(() => {\n"
        "    fetch(`/api/notify?q=${q}`).then((r) => r.json()).then(setItems);\n"
        "  }, [q]);\n"
        "  return <div>{String(items)}</div>;\n"
        "};\n",
        encoding="utf-8",
    )
    (proj / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api')\n\n"
        "@router.get('/notify')\n"
        "def notify_user():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(proj))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    assert res["ranked_hotspots"] == []
    assert res["suppressed_count"] >= 1

    # 3. `audit --format markdown` displays real audited file count (2, NOT 0!)
    md_out = format_check_report(res, fmt="markdown")
    assert "**Files Analyzed:** `2`" in md_out

    # 4. `route` displays backend handler name `(notify_user)` in Route Flows
    r_res = brain.route("how does /api/notify work")
    r_txt = format_route_coordinates(r_res)
    assert "api.py" in r_txt and "(notify_user)" in r_txt


def test_v1_4_1_wild_audit_precision_hardening(tmp_path: Path):
    """Wild-audit precision fixes (v1.4.1):
    1. PB013 respects `model_dump(exclude={...})` / `include={...}` while still catching unexcluded optional fields.
    2. PB011 never overwrites ambiguous colliding unmounted routes (`/verify` in multiple routers).
    3. PB011 unwraps Axios `{ params }` shorthand & dynamic config args while still catching closed missing query params.
    4. PB011 ignores FastAPI >=0.115 `Annotated[QueryModel, Query()]` model wrappers as literal query param names.
    5. PB011 parses positional HTTP verb args in local transport helpers (`_writeCustom(url, "POST", def)`),
       ignores generic `${path}` transport helpers, and ignores `${BASE_URL}/` root collisions.
    6. PB011 skips `http_verb_mismatch` on syntax-errored partial-parse backend files (e.g. Jinja templates).
    """
    # 1. PB013 exclude={...}
    (tmp_path / "knowledge.py").write_text(
        "from typing import Optional\n"
        "from pydantic import BaseModel\n\n"
        "class KnowledgeForm(BaseModel):\n"
        "    name: str\n"
        "    access_grants: Optional[list] = None\n"
        "    meta: Optional[dict] = None\n\n"
        "class PartialForm(BaseModel):\n"
        "    name: str\n"
        "    meta: Optional[dict] = None\n"
        "    bio: Optional[str] = None\n\n"
        "def update_knowledge_safe(form_data: KnowledgeForm, table):\n"
        "    values = form_data.model_dump(exclude={'access_grants', 'meta'})\n"
        "    table.update(values)\n\n"
        "def update_partial_unsafe(form_data: PartialForm, table):\n"
        "    values = form_data.model_dump(exclude={'meta'})\n"
        "    table.update(values)\n",
        encoding="utf-8",
    )

    # 2. Colliding unmounted `/verify` routers + FastAPI Annotated[Model, Query()] + workflows POST/GET + partial Jinja route
    (tmp_path / "mfa.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class MfaVerifyForm(BaseModel):\n"
        "    challenge_token: str\n"
        "    code: str\n\n"
        "@router.post('/verify')\n"
        "def verify_mfa(form_data: MfaVerifyForm):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "openai_router.py").write_text(
        "from fastapi import APIRouter\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class OpenAIVerifyForm(BaseModel):\n"
        "    url: str\n"
        "    key: str\n\n"
        "@router.post('/verify')\n"
        "def verify_openai(form_data: OpenAIVerifyForm):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "routes_misc.py").write_text(
        "from typing import Annotated\n"
        "from fastapi import APIRouter, Query\n"
        "from pydantic import BaseModel\n"
        "router = APIRouter()\n\n"
        "class NoticeQueryParam(BaseModel):\n"
        "    title: str | None = None\n\n"
        "@router.get('/notice/list')\n"
        "def list_notices(search: Annotated[NoticeQueryParam, Query()]):\n"
        "    return {'items': []}\n\n"
        "@router.get('/items/search')\n"
        "def search_items(q: str = Query(...)):\n"
        "    return {'items': []}\n\n"
        "@router.get('/workflows/custom')\n"
        "def list_custom_workflows():\n"
        "    return {'definitions': []}\n\n"
        "@router.post('/workflows/custom')\n"
        "def create_custom_workflow():\n"
        "    return {'name': 'wf-1'}\n\n"
        "@router.get('/workflows/designer/{session_id}')\n"
        "def get_designer_session(session_id: str):\n"
        "    return {'id': session_id}\n\n"
        "@router.get('/')\n"
        "def root_health():\n"
        "    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    (tmp_path / "jinja_admin.py").write_text(
        "{%- if cookiecutter.use_jwt %}\n"
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        "@router.get('/admin/users/{user_id}')\n"
        "def get_admin_user(user_id: str):\n"
        "    return {'id': user_id}\n"
        "{%- endif %}\n",
        encoding="utf-8",
    )

    # 3. Frontend callers testing every FP guard + ensuring true-positive missing query param still fires
    (tmp_path / "client.ts").write_text(
        "import axios from 'axios';\n"
        "const apiClient = axios.create({ baseURL: '/api' });\n"
        "const OPENAI_API_BASE_URL = '/openai';\n"
        "const RAG_URL = '/rag';\n\n"
        "export async function verifyOpenAI(url: string, key: string) {\n"
        "  return fetch(`${OPENAI_API_BASE_URL}/verify`, {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify({ url, key }),\n"
        "  });\n"
        "}\n\n"
        "export async function postToRagRoot() {\n"
        "  return fetch(`${RAG_URL}/`, { method: 'POST' });\n"
        "}\n\n"
        "export async function getNotices(params?: Record<string, any>) {\n"
        "  return apiClient.get('/notice/list', params);\n"
        "}\n\n"
        "export async function searchWithShorthand(params: { q: string }) {\n"
        "  return apiClient.get('/items/search', { params });\n"
        "}\n\n"
        "export async function searchMissingRequired() {\n"
        "  return apiClient.get('/items/search', { params: { page: 1 } });\n"
        "}\n\n"
        "export async function deleteJinjaUser(userId: string) {\n"
        "  return apiClient.delete(`/admin/users/${userId}`);\n"
        "}\n\n"
        "async function _writeCustom(url: string, method: 'POST' | 'PUT', body: Record<string, any>) {\n"
        "  const res = await fetch(url, { method, body: JSON.stringify(body) });\n"
        "  return res.json();\n"
        "}\n\n"
        "export function createCustomWorkflow(def: Record<string, any>) {\n"
        "  return _writeCustom('/workflows/custom', 'POST', def);\n"
        "}\n\n"
        "async function _designerPost(path: string, body: Record<string, string>) {\n"
        "  const res = await fetch(`/workflows/designer/${path}`, {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify(body),\n"
        "  });\n"
        "  return res.json();\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "WorkflowView.ts").write_text(
        "import { createCustomWorkflow } from './client';\n\n"
        "export async function saveWorkflow(def: Record<string, any>) {\n"
        "  const saved = await createCustomWorkflow(def);\n"
        "  return saved.name;\n"
        "}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    hots = res["ranked_hotspots"]

    # PB013: safe `update_knowledge_safe` must NOT fire; unsafe `update_partial_unsafe` MUST fire for ['bio'] only
    pb013 = [h for h in hots if h["rule_id"] == "PB013"]
    assert len(pb013) == 1
    assert "PartialForm" in pb013[0]["summary"]
    assert "bio" in pb013[0]["summary"]
    assert "meta" not in pb013[0]["summary"]

    # PB011: ONLY the genuine closed missing-query-param `searchMissingRequired` must fire!
    pb011 = [h for h in hots if h["rule_id"] == "PB011"]
    assert len(pb011) == 1, f"Unexpected PB011 hotspots: {pb011}"
    assert pb011[0]["category"] == "http_missing_query_param"
    assert "/items/search" in pb011[0]["summary"]
    assert "q" in pb011[0]["summary"]
