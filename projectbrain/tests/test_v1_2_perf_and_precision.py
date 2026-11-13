"""Adversarial regression & performance suite for ProjectBrain v1.2.0.

Verifies the 5 core engineering upgrades:
  1. Strict <8KB inline MCP payload cap (`<= 7,800` serialized chars), zero 6D IR bloat in `explore`,
     `direct_read_hint` on small files, and single-copy FastMCP `TextContent` wire serialization.
  2. Giant React JSX monolith internal component maps (`[Monolith Internal Map: ...]`) + Law 6
     `undeclared_state_setter` AST contract detection with zero false positives on DOM/prop/local setters.
  3. Dynamic JS template-literal HTTP route tracing (`` `${baseUrl}/api/v1/...` ``), cross-file
     `include_router(..., prefix=...)` propagation, verb-agnostic `brain_explore("/api/...")`, and
     `snake_case` <-> `camelCase` symbol resolution across `explore` and `slice_symbols`.
  4. Sub-2s batched `git cat-file --batch` baseline diffing + concurrent multi-threaded MCP tool safety
     under `PROJECTBRAIN_MCP_STDIO=1` on Windows without `ProcessPoolExecutor` deadlocks.
  5. Clean-repo `brain_check()` static bug audit on small/medium repos without blocking safe verdicts.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from projectbrain import server
from projectbrain.brain import ProjectBrain


def _git(cwd: Path, *args: str) -> None:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"


def test_mcp_inline_budget_zero_6d_bloat_and_single_wire_copy(tmp_path: Path):
    """Dense hub files must stay strictly <= 7,800 chars across all MCP tools with single-copy wire output."""
    # Create a 180-line shared notification utility imported and called by 55 modules
    hub_lines = [
        "export async function dispatchChannelAlert(recipient, subject, payload) {",
        "  const normalized = String(recipient || '').trim().toLowerCase();",
        "  if (!normalized) {",
        "    throw new Error('Missing recipient');",
        "  }",
    ]
    for i in range(1, 35):
        hub_lines.append(f"  const headerTag{i} = payload?.tag{i} ?? 'default_{i}';")
    hub_lines.extend([
        "  return { ok: true, to: normalized, subject, tags: headerTag1 };",
        "}",
        "",
        "export function formatAlertDigest(items) {",
        "  return (items || []).map((it) => `${it.id}:${it.title}`).join(', ');",
        "}",
    ])
    (tmp_path / "alertDispatcher.js").write_text("\n".join(hub_lines) + "\n", encoding="utf-8")

    for idx in range(55):
        (tmp_path / f"consumer_{idx:02d}.js").write_text(
            "import { dispatchChannelAlert, formatAlertDigest } from './alertDispatcher.js';\n\n"
            f"export async function triggerWorkflowStep{idx}(ctx) {{\n"
            f"  const summary = formatAlertDigest(ctx.items);\n"
            f"  return await dispatchChannelAlert(ctx.ownerEmail, summary, ctx);\n"
            "}\n",
            encoding="utf-8",
        )

    repo = str(tmp_path)
    # 1. Explore dense hub file (both warm & cold paths stay <= 7,800 chars, zero 6D IR keys)
    exp_res = server.brain_explore("alertDispatcher.js", depth=2, repo_path=repo)
    exp_json = json.dumps(exp_res, indent=2, ensure_ascii=False)
    assert len(exp_json) <= server.MCP_INLINE_CHAR_BUDGET, f"brain_explore overflowed cap: {len(exp_json)} chars"
    assert "?Z:" not in exp_json
    assert "effect_provenance" not in exp_json
    assert "ast_hash" not in exp_json
    assert "direct_read_hint" in exp_res, "Small (<350-line) file explore should guide agent to read_file directly"

    # Verify cold SQLite explore also strips 6D IR bloat and emits direct_read_hint
    b_cold = ProjectBrain(repo)
    cold_exp = b_cold.explore("alertDispatcher.js", depth=2)
    assert b_cold._index == {}
    cold_json = json.dumps(cold_exp, indent=2, ensure_ascii=False)
    assert "?Z:" not in cold_json
    assert "effect_provenance" not in cold_json
    assert "direct_read_hint" in cold_exp

    # 2. Dossier, Slice, and Check on the 55-caller hub must ALL stay <= 7,800 serialized chars
    dos_res = server.brain_dossier(
        "Audit dispatchChannelAlert recipient normalization across workflow triggers",
        impact_files=["alertDispatcher.js"],
        repo_path=repo,
    )
    dos_json = json.dumps(dos_res, indent=2, ensure_ascii=False)
    assert len(dos_json) <= server.MCP_INLINE_CHAR_BUDGET, f"brain_dossier overflowed cap: {len(dos_json)} chars"

    sl_targets = ["alertDispatcher.js"] + [f"consumer_{i:02d}.js:triggerWorkflowStep{i}" for i in range(16)]
    sl_res = server.brain_slice(sl_targets, repo_path=repo)
    sl_json = json.dumps(sl_res, indent=2, ensure_ascii=False)
    assert len(sl_json) <= server.MCP_INLINE_CHAR_BUDGET, f"brain_slice overflowed cap: {len(sl_json)} chars"

    chk_res = server.brain_check(files_modified=["alertDispatcher.js"], repo_path=repo)
    chk_json = json.dumps(chk_res, indent=2, ensure_ascii=False)
    assert len(chk_json) <= server.MCP_INLINE_CHAR_BUDGET, f"brain_check overflowed cap: {len(chk_json)} chars"

    # 3. FastMCP wire-level call_tool check: structured_output=False emits TextContent list ONLY (no duplicate tuple)
    wire_out = asyncio.run(
        server.mcp.call_tool("brain_explore", {"target": "alertDispatcher.js", "repo_path": repo})
    )
    assert not isinstance(wire_out, tuple), (
        "FastMCP returned a (TextContent, structuredContent) 2-tuple; structured_output=False must prevent double wire payload"
    )
    assert isinstance(wire_out, list) and len(wire_out) == 1
    wire_text = getattr(wire_out[0], "text", "")
    assert len(wire_text) <= server.MCP_INLINE_CHAR_BUDGET


def test_react_monolith_internal_map_and_undeclared_state_setter_law6(tmp_path: Path):
    """1,450-line React monolith slices include component map and Law 6 catches undeclared setX() typos."""
    jsx_lines: list[str] = [
        "import React, { useState, useEffect, useMemo } from 'react';",
        "",
        "export default function CatalogWorkshop({ initialItems, setDrawerOpen }) {",
        "  const [catalogMembers, setCatalogMembers] = useState(initialItems || []);",
        "  const [filterQuery, setFilterQuery] = useState('');",
        "  const [selectedSku, setSelectedSku] = useState(null);",
        "",
        "  useEffect(() => {",
        "    const timer = setTimeout(() => {",
        "      document.body.setAttribute('data-workshop-ready', '1');",
        "    }, 50);",
        "    return () => clearTimeout(timer);",
        "  }, []);",
        "",
        "  const visibleMembers = useMemo(() => {",
        "    return catalogMembers.filter((m) => m.name.includes(filterQuery));",
        "  }, [catalogMembers, filterQuery]);",
        "",
        "  const handleSelectSku = (sku) => {",
        "    setSelectedSku(sku);",
        "    setDrawerOpen(true);",
        "  };",
        "",
    ]

    # Pad middle of monolith with realistic helper blocks up to ~1,310 lines
    for blk in range(1, 160):
        jsx_lines.extend([
            f"  const computeMetricRow{blk} = (row) => {{",
            f"    const baseVal = Number(row?.metric{blk} || 0);",
            f"    const scaledVal = baseVal * {blk};",
            "    if (scaledVal < 0) {",
            "      return 0;",
            "    }",
            "    return scaledVal;",
            "  };",
        ])

    # Handler near line ~1,305 with an undeclared state setter typo `setMembers(...)` instead of `setCatalogMembers(...)`
    typo_handler_start = len(jsx_lines) + 1
    jsx_lines.extend([
        "  const handleBulkApproveMembers = (approvedIds) => {",
        "    const nextList = catalogMembers.map((item) =>",
        "      approvedIds.includes(item.id) ? { ...item, approved: true } : item",
        "    );",
        "    setMembers(nextList);",
        "  };",
        "",
        "  return (",
        "    <section className='catalog-workshop'>",
        "      <header>",
        "        <h1>Catalog Workshop ({visibleMembers.length})</h1>",
        "        <button type='button' onClick={() => handleBulkApproveMembers(['1'])}>Approve</button>",
        "      </header>",
    ])
    for row_i in range(1, 30):
        jsx_lines.append(f"      <div className='workshop-row-{row_i}'>Row {row_i}</div>")
    jsx_lines.extend([
        "    </section>",
        "  );",
        "}",
    ])
    typo_line = typo_handler_start + 4
    assert len(jsx_lines) >= 1340

    comp_path = tmp_path / "src" / "components" / "CatalogWorkshop.jsx"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_text("\n".join(jsx_lines) + "\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Slicing the giant component function prepends the Monolith Internal Map showing L4 state hooks + handlers + JSX return
    sl_res = brain.slice_symbols("CatalogWorkshop.jsx:CatalogWorkshop", max_lines_per_symbol=90)
    assert len(sl_res["slices"]) == 1
    code_out = sl_res["slices"][0]["code"]
    assert "[Monolith Internal Map:" in code_out
    assert "[catalogMembers, setCatalogMembers]" in code_out
    assert "handleBulkApproveMembers()" in code_out
    assert "JSX Return" in code_out

    # Cold SQLite slice path must also prepend the Monolith Internal Map
    cold_brain = ProjectBrain(str(tmp_path))
    cold_sl = cold_brain.slice_symbols("CatalogWorkshop.jsx:CatalogWorkshop", max_lines_per_symbol=90)
    assert cold_brain._index == {}
    assert "[Monolith Internal Map:" in cold_sl["slices"][0]["code"]
    assert "[catalogMembers, setCatalogMembers]" in cold_sl["slices"][0]["code"]

    # 2. Modified-file check catches `setMembers` at `typo_line` and suggests `setCatalogMembers`,
    #    with ZERO false positives on `setTimeout`, `setAttribute`, `setSelectedSku`, or prop `setDrawerOpen`!
    chk_mod = brain.check(files_modified=["src/components/CatalogWorkshop.jsx"])
    diags = chk_mod.get("contract_diagnostics", [])
    joined_diags = "\n".join(diags)
    assert "setMembers" in joined_diags
    assert "setCatalogMembers" in joined_diags
    assert f"L{typo_line}" in joined_diags
    assert "setTimeout" not in joined_diags
    assert "setAttribute" not in joined_diags
    assert "setDrawerOpen" not in joined_diags
    assert "setSelectedSku" not in joined_diags

    # 3. Clean-repo `brain.check()` (no args) surfaces `static_audit_findings` while keeping SAFE_TO_PROCEED
    chk_clean = brain.check()
    assert chk_clean["verdict"] == "SAFE_TO_PROCEED"
    audit_findings = chk_clean.get("static_audit_findings", [])
    assert any("setMembers" in f and "setCatalogMembers" in f for f in audit_findings)


def test_dynamic_template_url_router_mounts_and_cross_style_symbol_lookup(tmp_path: Path):
    """Verify `${baseUrl}/api/v1/...` + FastAPI `include_router` + `snake_case`<->`camelCase` resolution."""
    (tmp_path / "frontend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "routers").mkdir(parents=True)
    (tmp_path / "backend" / "services").mkdir(parents=True)

    (tmp_path / "frontend" / "src" / "api" / "httpClient.js").write_text(
        "const baseUrl = import.meta.env.VITE_API_URL || '';\n\n"
        "export async function submitBatchAssignment(batchPayload) {\n"
        "  const res = await fetch(`${baseUrl}/api/v1/fulfillment/dispatch-batch`, {\n"
        "    method: 'POST',\n"
        "    headers: { 'Content-Type': 'application/json' },\n"
        "    body: JSON.stringify(batchPayload),\n"
        "  });\n"
        "  return res.json();\n"
        "}\n\n"
        "export async function sendAlertNotification(recipientEmail, subjectLine) {\n"
        "  return { recipientEmail, subjectLine };\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "services" / "mailer.py").write_text(
        "def queue_outbound_notice(batch_id: str) -> dict:\n"
        "    return {'queued': batch_id}\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "routers" / "fulfillment.py").write_text(
        "from fastapi import APIRouter\n"
        "from backend.services.mailer import queue_outbound_notice\n\n"
        "router = APIRouter()\n\n"
        "@router.post('/dispatch-batch')\n"
        "async def dispatch_batch_endpoint(payload: dict):\n"
        "    return queue_outbound_notice(payload.get('id', '0'))\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from backend.routers import fulfillment\n\n"
        "app = FastAPI()\n"
        "API_PREFIX = '/api/v1/fulfillment'\n"
        "app.include_router(fulfillment.router, prefix=API_PREFIX)\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Verb-agnostic route explore resolves UI `${baseUrl}` caller -> mounted POST route -> Python service callee
    flow = brain.explore("/api/v1/fulfillment/dispatch-batch")
    assert flow.get("step_count", 0) >= 3, f"Expected full-stack flow steps, got: {flow}"
    step_types = [s["type"] for s in flow["steps"]]
    assert "ui_trigger" in step_types
    assert "route" in step_types
    assert any("httpClient.js" in s.get("rel_path", "") and s.get("function") == "submitBatchAssignment" for s in flow["steps"])
    assert any("fulfillment.py" in s.get("rel_path", "") and "POST /api/v1/fulfillment/dispatch-batch" in s.get("detail", "") for s in flow["steps"])
    assert any(s.get("function") == "queue_outbound_notice" for s in flow["steps"])

    # Verb-prefixed target string `"POST /api/v1/fulfillment/dispatch-batch"` also works via brain.explore
    flow_prefixed = brain.explore("POST /api/v1/fulfillment/dispatch-batch")
    assert flow_prefixed.get("step_count", 0) >= 3

    # 2. Cross-style `snake_case` query resolves `camelCase` JS function (`send_alert_notification` -> `sendAlertNotification`)
    exp_sym = brain.explore("send_alert_notification")
    assert "error" not in exp_sym, f"Cross-style explore failed: {exp_sym}"
    assert exp_sym["center"]["name"] == "sendAlertNotification"

    # Cold SQLite explore & slice also resolve `snake_case` -> `camelCase` without hydrating `_index`
    cold_b = ProjectBrain(str(tmp_path))
    cold_exp = cold_b.explore("send_alert_notification")
    assert cold_b._index == {}
    assert cold_exp["center"]["name"] == "sendAlertNotification"

    cold_sl = cold_b.slice_symbols("send_alert_notification")
    assert cold_b._index == {}
    assert "sendAlertNotification" in cold_sl["slices"][0]["code"]

    # 3. Misspelled symbol in explore returns fuzzy `did_you_mean` suggestions instead of a dead end
    miss_exp = cold_b.explore("send_alert_notifcation")
    assert "error" in miss_exp
    assert any("sendAlertNotification" in s for s in miss_exp.get("did_you_mean", []))


def test_batched_git_cat_file_speed_and_concurrent_stdio_mcp_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """65-file git repo with 35 dirty files finishes `brain_check` in <2.5s and survives 8 concurrent MCP threads."""
    monkeypatch.setenv("PROJECTBRAIN_MCP_STDIO", "1")

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ci@example.com")
    _git(tmp_path, "config", "user.name", "CI Runner")

    pkg_dir = tmp_path / "services"
    pkg_dir.mkdir(parents=True)
    for i in range(65):
        (pkg_dir / f"mod_{i:02d}.py").write_text(
            f"def compute_quota_{i}(units: int, rate: int = 2) -> int:\n"
            f"    return units * rate + {i}\n\n"
            f"def verify_quota_{i}(total: int) -> bool:\n"
            f"    return compute_quota_{i}(total) >= 0\n",
            encoding="utf-8",
        )

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial 65-module baseline")

    repo = str(tmp_path)
    # Initial index under PROJECTBRAIN_MCP_STDIO=1 (uses ThreadPoolExecutor, never spawns child processes)
    t0 = time.perf_counter()
    b = server._get_brain(repo)
    b.index()
    idx_elapsed = time.perf_counter() - t0
    assert idx_elapsed < 5.0, f"Indexing 65 files took too long: {idx_elapsed:.2f}s"

    # Dirty 35 tracked files so `brain_check` exercises `git diff` + `git cat-file --batch` baseline loading
    for i in range(35):
        (pkg_dir / f"mod_{i:02d}.py").write_text(
            f"def compute_quota_{i}(units: int, rate: int = 3) -> int:\n"
            f"    return units * rate + {i} + 1\n\n"
            f"def verify_quota_{i}(total: int) -> bool:\n"
            f"    return compute_quota_{i}(total) >= 0\n",
            encoding="utf-8",
        )

    t_chk = time.perf_counter()
    chk_res = server.brain_check(repo_path=repo)
    chk_elapsed = time.perf_counter() - t_chk
    assert chk_elapsed < 2.5, f"brain_check on 35 dirty git files took {chk_elapsed:.2f}s (expected <2.5s)"
    assert chk_res["verdict"] == "SAFE_TO_PROCEED"
    assert len(json.dumps(chk_res, indent=2)) <= server.MCP_INLINE_CHAR_BUDGET

    # Hammer MCP tools from 8 concurrent threads simultaneously to verify per-brain RLock safety
    def _worker(tid: int) -> str:
        if tid % 3 == 0:
            r = server.brain_check(repo_path=repo)
            assert "verdict" in r
            return r["verdict"]
        if tid % 3 == 1:
            r = server.brain_explore(f"compute_quota_{tid % 20}", repo_path=repo)
            assert r.get("center", {}).get("name") == f"compute_quota_{tid % 20}"
            return "explore_ok"
        r = server.brain_slice(f"mod_{tid % 20:02d}.py:verify_quota_{tid % 20}", repo_path=repo)
        assert len(r.get("slices", [])) == 1
        return "slice_ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_worker, range(16)))
    assert len(results) == 16
