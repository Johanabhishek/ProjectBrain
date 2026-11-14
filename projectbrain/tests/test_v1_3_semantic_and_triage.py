"""Comprehensive verification & zero-false-positive decoy suite for ProjectBrain v1.3.0.

Covers all 4 Deep Semantic & Anomaly Triage pillars:
  1. Pillar 1 (`PB011`): Cross-stack HTTP verb, required/optional/forbid body schema,
     camelCase <-> snake_case drift, 1-hop/2-hop JS API wrapper propagation, query params,
     `GET` vs `POST` multi-verb route coexistence, `list[Model]` response unwrapping, and
     cold partial-neighborhood (`>200` files) `need_http_routes` loading.
  2. Pillar 2 (`PB012`): React post-mount conditional unmount of stateful children (direct
     `useState`/`useReducer` and transitive custom hooks), inline nested PascalCase
     components, and uncancelled `useEffect([dep])` async `setState` race warnings.
  3. Pillar 3 (`PB013`): Python partial-update `.model_dump()` / `.dict()` default clobbers
     in `setattr` loops, `.sqlmodel_update()` / `.update()`, and `{**base, **dump}` merges,
     plus silent broad `except: pass` swallows around `.commit()` / `send_email()`.
  4. Pillar 4 (`audit=True` / `--audit` / `--scope`): `<1.5s` whole-repo anomaly triage on
     both clean and dirty git working trees, SARIF v2.1.0 `PB011`-`PB013` rules, and strict
     `<= 7,800` char inline MCP serialization budget.
  5. Adversarial Decoy Suite: Proves `0` false positives (`0` errors, `0` warnings) across
     dynamic spreads, Pydantic aliases/`extra="allow"`/`Depends`, Axios `.data` envelopes,
     mount-only spinners, stable hook deps, `key={...}` resets, `AbortController`/ref guards,
     `POST` model creation, `if v is not None` guards, and teardown/logged `try/except` blocks.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from projectbrain import cli, server
from projectbrain.brain import ProjectBrain
from projectbrain.reporters import format_check_report


def _git(cwd: Path, *args: str) -> None:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"


def test_pb011_cross_stack_http_schema_and_wrapper_contracts(tmp_path: Path):
    """PB011 catches body/query/response/verb mismatches across 2-hop JS wrappers and >200-file cold loads."""
    (tmp_path / "backend" / "routers").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "pages").mkdir(parents=True)

    (tmp_path / "backend" / "schemas.py").write_text(
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "from pydantic import BaseModel, ConfigDict\n\n"
        "class InvoiceCreateRequest(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    customer_id: str\n"
        "    amount_cents: int\n"
        "    billing_memo: str | None = None\n"
        "    due_date: Optional[str] = None\n\n"
        "class InvoiceSummaryOut(BaseModel):\n"
        "    invoice_id: str\n"
        "    total_cents: int\n"
        "    status: str\n\n"
        "@dataclass\n"
        "class LedgerPreviewSpec:\n"
        "    fiscal_year: int\n"
        "    region_code: str = 'US'\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "routers" / "invoices.py").write_text(
        "from fastapi import APIRouter, Query\n"
        "from backend.schemas import InvoiceCreateRequest, InvoiceSummaryOut, LedgerPreviewSpec\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/invoices', response_model=list[InvoiceSummaryOut])\n"
        "def list_invoices(account_id: str = Query(...), limit: int = 20):\n"
        "    return []\n\n"
        "@router.post('/invoices', response_model=InvoiceSummaryOut)\n"
        "def create_invoice(payload: InvoiceCreateRequest):\n"
        "    return InvoiceSummaryOut(invoice_id='inv_1', total_cents=payload.amount_cents, status='open')\n\n"
        "@router.post('/ledger-preview')\n"
        "def build_ledger_preview(spec: LedgerPreviewSpec):\n"
        "    return {'ok': True, 'year': spec.fiscal_year}\n\n"
        "@router.delete('/invoices/{invoice_id}')\n"
        "def archive_invoice(invoice_id: str):\n"
        "    return {'archived': invoice_id}\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "from backend.routers import invoices\n\n"
        "app = FastAPI()\n"
        "app.include_router(invoices.router, prefix='/api/v1/billing')\n",
        encoding="utf-8",
    )

    # 2-hop JS transport -> client wrapper -> page component
    (tmp_path / "frontend" / "src" / "api" / "transport.js").write_text(
        "const API_BASE = '';\n"
        "export async function postJson(endpoint, payload) {\n"
        "  const res = await fetch(`${API_BASE}${endpoint}`, {\n"
        "    method: 'POST',\n"
        "    headers: { 'Content-Type': 'application/json' },\n"
        "    body: JSON.stringify(payload),\n"
        "  });\n"
        "  return await res.json();\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "frontend" / "src" / "api" / "billingClient.js").write_text(
        "import axios from 'axios';\n"
        "import { postJson } from './transport.js';\n\n"
        "export async function submitInvoiceDraft(draftBody) {\n"
        "  return await postJson('/api/v1/billing/invoices', draftBody);\n"
        "}\n\n"
        "export async function loadInvoiceNumbers() {\n"
        "  const resp = await axios.get('/api/v1/billing/invoices');\n"
        "  return resp.data.map((row) => row.invoiceNumber);\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "frontend" / "src" / "pages" / "InvoiceComposer.jsx").write_text(
        "import React from 'react';\n"
        "import { submitInvoiceDraft } from '../api/billingClient.js';\n\n"
        "export async function handleCreateAndPreview(invoiceId) {\n"
        "  const created = await submitInvoiceDraft({\n"
        "    customerId: 'cust_99',\n"
        "    rogueExtraFlag: true,\n"
        "  });\n"
        "  console.log(created.invoiceTotalCents);\n"
        "  await fetch('/api/v1/billing/ledger-preview', {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify({ region_code: 'EU' }),\n"
        "  });\n"
        "  await fetch(`/api/v1/billing/invoices/${invoiceId}`, { method: 'PATCH' });\n"
        "}\n",
        encoding="utf-8",
    )

    # Add 202 small filler modules so total indexed files > 200 (triggers partial check neighborhood)
    (tmp_path / "packages").mkdir()
    for i in range(202):
        (tmp_path / "packages" / f"mod_{i:03d}.py").write_text(
            f"def helper_val_{i}(x: int) -> int:\n    return x + {i}\n",
            encoding="utf-8",
        )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    audit_res = brain.check(audit=True)
    hots = audit_res["ranked_hotspots"]
    cats = {h["category"] for h in hots}
    all_msgs = "\n".join(audit_res.get("static_audit_findings", []))

    assert "http_missing_required_field" in cats
    assert "amount_cents" in all_msgs
    assert "fiscal_year" in all_msgs
    assert "http_payload_casing_mismatch" in cats
    assert "customer_id" in all_msgs
    assert "rogueExtraFlag" in all_msgs
    assert "http_missing_query_param" in cats
    assert "account_id" in all_msgs
    assert "http_response_key_mismatch" in cats
    assert "invoiceNumber" in all_msgs
    assert "invoiceTotalCents" in all_msgs
    assert "http_verb_mismatch" in cats
    assert "PATCH" in all_msgs and "DELETE" in all_msgs

    # Cold >200-file partial neighborhood check must load backend routes via `need_http_routes=True`
    b_cold = ProjectBrain(str(tmp_path))
    cold_chk = b_cold.check(files_modified=["frontend/src/pages/InvoiceComposer.jsx"])
    assert b_cold._is_partial_index is True
    cold_diags = "\n".join(cold_chk.get("contract_diagnostics", []))
    assert "amount_cents" in cold_diags
    assert "fiscal_year" in cold_diags
    assert "PATCH" in cold_diags


def test_pb012_react_stateful_remount_nested_component_and_async_race(tmp_path: Path):
    """PB012 catches post-mount stateful child unmounts (incl. custom hooks), nested components, and effect races."""
    (tmp_path / "src" / "hooks").mkdir(parents=True)
    (tmp_path / "src" / "components").mkdir(parents=True)

    (tmp_path / "src" / "hooks" / "useRowSelection.js").write_text(
        "import { useState } from 'react';\n"
        "export function useRowSelection(initialIds = []) {\n"
        "  const [selectedIds, setSelectedIds] = useState(initialIds);\n"
        "  return { selectedIds, setSelectedIds };\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "src" / "components" / "SelectionGrid.jsx").write_text(
        "import React from 'react';\n"
        "import { useRowSelection } from '../hooks/useRowSelection.js';\n\n"
        "export function SelectionGrid({ rows }) {\n"
        "  const { selectedIds } = useRowSelection();\n"
        "  return <div data-count={selectedIds.length}>{rows.length}</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "src" / "components" / "DraftNoteEditor.jsx").write_text(
        "import React, { useReducer } from 'react';\n\n"
        "export function DraftNoteEditor() {\n"
        "  const [state, dispatch] = useReducer((s, a) => ({ ...s, text: a }), { text: '' });\n"
        "  return <textarea value={state.text} onChange={(e) => dispatch(e.target.value)} />;\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "src" / "components" / "WorkspaceScreen.jsx").write_text(
        "import React, { useState, useEffect } from 'react';\n"
        "import { SelectionGrid } from './SelectionGrid.jsx';\n"
        "import { DraftNoteEditor } from './DraftNoteEditor.jsx';\n\n"
        "export function WorkspaceScreen({ filterTerm }) {\n"
        "  const [isReloading, setIsReloading] = useState(false);\n"
        "  const [isSaving, setIsSaving] = useState(false);\n"
        "  const [rows, setRows] = useState([]);\n\n"
        "  function InlineActionChip({ label }) {\n"
        "    return <span className='chip'>{label}</span>;\n"
        "  }\n\n"
        "  async function handleRefreshClick() {\n"
        "    setIsReloading(true);\n"
        "    const r = await fetch('/api/rows');\n"
        "    setRows(await r.json());\n"
        "    setIsReloading(false);\n"
        "  }\n\n"
        "  async function handleSaveClick() {\n"
        "    setIsSaving(true);\n"
        "    await fetch('/api/save', { method: 'POST' });\n"
        "    setIsSaving(false);\n"
        "  }\n\n"
        "  useEffect(() => {\n"
        "    fetch(`/api/search?q=${filterTerm}`)\n"
        "      .then((r) => r.json())\n"
        "      .then((d) => setRows(d.items));\n"
        "  }, [filterTerm]);\n\n"
        "  if (isReloading) {\n"
        "    return <div>Reloading...</div>;\n"
        "  }\n\n"
        "  return (\n"
        "    <section>\n"
        "      <button onClick={handleRefreshClick}>Refresh</button>\n"
        "      <button onClick={handleSaveClick}>Save</button>\n"
        "      <InlineActionChip label='Active' />\n"
        "      <SelectionGrid rows={rows} />\n"
        "      {isSaving ? <p>Saving...</p> : <DraftNoteEditor />}\n"
        "    </section>\n"
        "  );\n"
        "}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    res = brain.check(audit=True)
    hots = res["ranked_hotspots"]
    by_cat: dict[str, list[dict]] = {}
    for h in hots:
        by_cat.setdefault(h["category"], []).append(h)

    assert "stateful_child_unmount_guard" in by_cat
    unmount_summaries = " ".join(h["summary"] for h in by_cat["stateful_child_unmount_guard"])
    assert "SelectionGrid" in unmount_summaries
    assert "DraftNoteEditor" in unmount_summaries

    assert "nested_component_remount" in by_cat
    assert "InlineActionChip" in by_cat["nested_component_remount"][0]["summary"]

    assert "uncancelled_async_effect_race" in by_cat
    race_h = by_cat["uncancelled_async_effect_race"][0]
    assert race_h["severity"] == "warning"
    assert "filterTerm" in race_h["summary"]


def test_pb013_python_pydantic_partial_update_clobber_and_silent_io_swallow(tmp_path: Path):
    """PB013 catches partial-update .model_dump()/.dict() clobbers and silent broad except swallows on IO."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "schemas.py").write_text(
        "from typing import Optional\n"
        "from pydantic import BaseModel\n\n"
        "class SubscriptionPatch(BaseModel):\n"
        "    plan_code: Optional[str] = None\n"
        "    seat_limit: int = 5\n"
        "    auto_renew: bool = True\n",
        encoding="utf-8",
    )

    (tmp_path / "app" / "service.py").write_text(
        "from fastapi import APIRouter\n"
        "from app.schemas import SubscriptionPatch\n\n"
        "router = APIRouter()\n\n"
        "@router.patch('/subscriptions/{sub_id}')\n"
        "def patch_subscription_setattr(sub_id: str, payload: SubscriptionPatch, row, db):\n"
        "    raw = payload.model_dump()\n"
        "    for k, v in raw.items():\n"
        "        setattr(row, k, v)\n"
        "    try:\n"
        "        db.commit()\n"
        "    except Exception:\n"
        "        pass\n"
        "    return row\n\n"
        "def update_subscription_orm(row, payload: SubscriptionPatch):\n"
        "    row.sqlmodel_update(payload.dict())\n"
        "    return row\n\n"
        "def merge_subscription_prefs(existing_prefs: dict, payload: SubscriptionPatch, mailer):\n"
        "    merged = {**existing_prefs, **payload.model_dump()}\n"
        "    try:\n"
        "        mailer.send_email(to='ops@example.com', body=str(merged))\n"
        "    except:\n"
        "        ...\n"
        "    return merged\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    res = brain.check(audit=True)
    hots = res["ranked_hotspots"]
    clobbers = [h for h in hots if h["category"] == "pydantic_partial_update_clobber"]
    swallows = [h for h in hots if h["category"] == "swallowed_io_exception"]

    assert len(clobbers) == 3
    assert all(h["severity"] == "error" and h["rule_id"] == "PB013" for h in clobbers)
    assert len(swallows) == 2
    assert all(h["severity"] == "warning" and h["rule_id"] == "PB013" for h in swallows)


def test_zero_false_positive_adversarial_decoy_suite(tmp_path: Path):
    """Every legitimate escape hatch must produce ZERO errors and ZERO warnings."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "src").mkdir(parents=True)

    (tmp_path / "backend" / "schemas.py").write_text(
        "from typing import Annotated, Optional\n"
        "from fastapi import Depends, Header\n"
        "from pydantic import BaseModel, ConfigDict, Field\n\n"
        "def get_db():\n"
        "    return object()\n\n"
        "class Actor:\n"
        "    id: str = 'u1'\n\n"
        "def get_actor() -> Actor:\n"
        "    return Actor()\n\n"
        "CurrentActor = Annotated[Actor, Depends(get_actor)]\n\n"
        "def to_camel(s: str) -> str:\n"
        "    parts = s.split('_')\n"
        "    return parts[0] + ''.join(p.title() for p in parts[1:])\n\n"
        "class AliasedOrderCreate(BaseModel):\n"
        "    model_config = ConfigDict(populate_by_name=True)\n"
        "    org_id: str = Field(alias='orgId')\n"
        "    note_text: Optional[str]\n"
        "    tag_label: str | None\n\n"
        "class OpenMetadataPayload(BaseModel):\n"
        "    model_config = ConfigDict(extra='allow')\n"
        "    event_type: str\n\n"
        "class CamelProjectIn(BaseModel):\n"
        "    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)\n"
        "    project_title: str\n"
        "    owner_email: str\n\n"
        "class OrderPatchIn(BaseModel):\n"
        "    status: Optional[str] = None\n"
        "    priority: int = 1\n\n"
        "class OrderOut(BaseModel):\n"
        "    order_id: str\n"
        "    item_count: int\n",
        encoding="utf-8",
    )

    (tmp_path / "backend" / "routes.py").write_text(
        "import logging\n"
        "from fastapi import FastAPI, Depends, Header\n"
        "from backend.schemas import (\n"
        "    AliasedOrderCreate, CamelProjectIn, CurrentActor, OpenMetadataPayload,\n"
        "    OrderOut, OrderPatchIn, get_db,\n"
        ")\n\n"
        "logger = logging.getLogger(__name__)\n"
        "app = FastAPI()\n\n"
        "@app.post('/api/orders', response_model=OrderOut)\n"
        "def create_order(\n"
        "    payload: AliasedOrderCreate,\n"
        "    actor: CurrentActor,\n"
        "    db=Depends(get_db),\n"
        "    x_request_id: str = Header(default='req_1'),\n"
        "):\n"
        "    # Legitimate full model_dump() on POST resource creation\n"
        "    raw = payload.model_dump()\n"
        "    return OrderOut(order_id=raw['org_id'], item_count=1)\n\n"
        "@app.post('/api/events')\n"
        "def ingest_event(payload: OpenMetadataPayload):\n"
        "    return {'accepted': payload.event_type}\n\n"
        "@app.post('/api/projects')\n"
        "def create_project(payload: CamelProjectIn):\n"
        "    return {'title': payload.project_title}\n\n"
        "@app.patch('/api/orders/{order_id}')\n"
        "def patch_order_safe(order_id: str, payload: OrderPatchIn, row, db):\n"
        "    # Safe 1: exclude_unset=True\n"
        "    clean = payload.model_dump(exclude_unset=True)\n"
        "    for k, v in clean.items():\n"
        "        setattr(row, k, v)\n"
        "    # Safe 2: explicit `is not None` guard on full model_dump()\n"
        "    for k, v in payload.model_dump().items():\n"
        "        if v is not None:\n"
        "            setattr(row, k, v)\n"
        "    # Safe 3: logged exception handler\n"
        "    try:\n"
        "        db.commit()\n"
        "    except Exception as exc:\n"
        "        logger.warning('commit failed: %s', exc)\n"
        "    # Safe 4: non-side-effect PRAGMA try/except pass\n"
        "    try:\n"
        "        db.execute('PRAGMA synchronous=NORMAL')\n"
        "    except Exception:\n"
        "        pass\n"
        "    return {'order_id': order_id, 'item_count': 2}\n\n"
        "class ConnectionWrapper:\n"
        "    def close(self):\n"
        "        try:\n"
        "            self.conn.commit()\n"
        "        except Exception:\n"
        "            pass\n",
        encoding="utf-8",
    )

    (tmp_path / "frontend" / "src" / "StatefulChild.jsx").write_text(
        "import React, { useState } from 'react';\n"
        "export function StatefulChild({ docId }) {\n"
        "  const [draft, setDraft] = useState('');\n"
        "  return <input value={draft} onChange={(e) => setDraft(e.target.value)} data-doc={docId} />;\n"
        "}\n"
        "export function StatelessBadge({ text }) {\n"
        "  return <span>{text}</span>;\n"
        "}\n",
        encoding="utf-8",
    )

    (tmp_path / "frontend" / "src" / "SafeDashboard.jsx").write_text(
        "import React, { useState, useEffect, useRef } from 'react';\n"
        "import axios from 'axios';\n"
        "import { StatefulChild, StatelessBadge } from './StatefulChild.jsx';\n\n"
        "export function SafeDashboard({ docId, query, dispatch, navigate }) {\n"
        "  const [bootLoading, setBootLoading] = useState(true);\n"
        "  const [refreshingBadge, setRefreshingBadge] = useState(false);\n"
        "  const [switchingDoc, setSwitchingDoc] = useState(false);\n"
        "  const [items, setItems] = useState([]);\n"
        "  const reqSeqRef = useRef(0);\n\n"
        "  // Safe mount-only bootstrap loader calling async helper\n"
        "  useEffect(() => {\n"
        "    async function bootstrap() {\n"
        "      const baseDefaults = { orgId: 'org_1' };\n"
        "      const resp = await axios.post('/api/orders', { ...baseDefaults });\n"
        "      const count = resp.data.legacy_count || resp.data.item_count;\n"
        "      const maybeHint = resp.data?.optional_hint;\n"
        "      await fetch('/api/orders', {\n"
        "        method: 'POST',\n"
        "        body: JSON.stringify({ orgId: 'org_2' }),\n"
        "      });\n"
        "      await fetch('/api/events', {\n"
        "        method: 'POST',\n"
        "        body: JSON.stringify({ event_type: 'boot', customDynamicField: 42 }),\n"
        "      });\n"
        "      await fetch('/api/projects', {\n"
        "        method: 'POST',\n"
        "        body: JSON.stringify({ projectTitle: 'Alpha', ownerEmail: 'a@b.co' }),\n"
        "      });\n"
        "      console.log(count, maybeHint);\n"
        "      setBootLoading(false);\n"
        "    }\n"
        "    bootstrap();\n"
        "  }, []);\n\n"
        "  // Safe ESLint stable hook dependencies [dispatch, navigate]\n"
        "  useEffect(() => {\n"
        "    fetch('/api/events', {\n"
        "      method: 'POST',\n"
        "      body: JSON.stringify({ event_type: 'nav' }),\n"
        "    }).then((r) => r.json()).then((d) => setItems([d.accepted]));\n"
        "  }, [dispatch, navigate]);\n\n"
        "  // Safe async effect #1: AbortController\n"
        "  useEffect(() => {\n"
        "    const controller = new AbortController();\n"
        "    fetch(`/api/events`, {\n"
        "      method: 'POST',\n"
        "      signal: controller.signal,\n"
        "      body: JSON.stringify({ event_type: query }),\n"
        "    }).then((r) => r.json()).then((d) => setItems([d.accepted]));\n"
        "    return () => controller.abort();\n"
        "  }, [query]);\n\n"
        "  // Safe async effect #2: sequence ref guard\n"
        "  useEffect(() => {\n"
        "    const seq = ++reqSeqRef.current;\n"
        "    fetch('/api/events', {\n"
        "      method: 'POST',\n"
        "      body: JSON.stringify({ event_type: docId }),\n"
        "    }).then((r) => r.json()).then((d) => {\n"
        "      if (seq !== reqSeqRef.current) return;\n"
        "      setItems([d.accepted]);\n"
        "    });\n"
        "  }, [docId]);\n\n"
        "  async function reloadBadgeOnly() {\n"
        "    setRefreshingBadge(true);\n"
        "    setSwitchingDoc(true);\n"
        "    setRefreshingBadge(false);\n"
        "    setSwitchingDoc(false);\n"
        "  }\n\n"
        "  if (bootLoading) {\n"
        "    return <div>Initial Boot...</div>;\n"
        "  }\n\n"
        "  return (\n"
        "    <div>\n"
        "      <button onClick={reloadBadgeOnly}>Reload</button>\n"
        "      {refreshingBadge ? <span>...</span> : <StatelessBadge text='OK' />}\n"
        "      {switchingDoc ? <span>Switching</span> : <StatefulChild key={docId} docId={docId} />}\n"
        "    </div>\n"
        "  );\n"
        "}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    res = brain.check(audit=True)
    assert res.get("contract_diagnostics", []) == []
    assert res.get("semantic_warnings", []) == []
    assert res.get("ranked_hotspots", []) == []
    assert res["verdict"] == "SAFE_TO_PROCEED"
    assert res["audit_coverage"]["flagged_files_count"] == 0


def test_repo_audit_triage_dirty_git_scope_sarif_and_mcp_budget(tmp_path: Path, capsys):
    """brain_check(audit=True) audits unmodified files in dirty git repos, honors scope_glob, emits SARIF, and stays <=7,800 chars."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()

    for i in range(5):
        (tmp_path / "backend" / f"clean_mod_{i}.py").write_text(
            f"def compute_metric_{i}(val: int) -> int:\n    return val * {i + 2}\n",
            encoding="utf-8",
        )

    (tmp_path / "backend" / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "from pydantic import BaseModel\n\n"
        "app = FastAPI()\n\n"
        "class TicketCreate(BaseModel):\n"
        "    title: str\n"
        "    severity_level: int\n\n"
        "class TicketPatch(BaseModel):\n"
        "    title: str | None = None\n"
        "    priority_rank: int = 3\n\n"
        "@app.post('/api/tickets')\n"
        "def open_ticket(body: TicketCreate):\n"
        "    return {'ticket_id': 't1'}\n\n"
        "@app.patch('/api/tickets/{tid}')\n"
        "def update_ticket(tid: str, body: TicketPatch, row):\n"
        "    for k, v in body.model_dump().items():\n"
        "        setattr(row, k, v)\n"
        "    return {'ticket_id': tid}\n",
        encoding="utf-8",
    )

    (tmp_path / "frontend" / "TicketForm.jsx").write_text(
        "import React from 'react';\n"
        "export function TicketWidget() {\n"
        "  function InlineHint() { return <span>!</span>; }\n"
        "  return <InlineHint />;\n"
        "}\n"
        "export async function submitTicket() {\n"
        "  return await fetch('/api/tickets', {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify({ title: 'Broken login' }),\n"
        "  });\n"
        "}\n",
        encoding="utf-8",
    )

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "eval@example.com")
    _git(tmp_path, "config", "user.name", "Eval Bot")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial commit")

    # Dirty a CLEAN file in git working tree — unmodified buggy files must still be caught by `audit=True`!
    (tmp_path / "backend" / "clean_mod_0.py").write_text(
        "def compute_metric_0(val: int) -> int:\n    return val * 10\n",
        encoding="utf-8",
    )

    mcp_res = server.brain_check(audit=True, repo_path=str(tmp_path))
    mcp_json = json.dumps(mcp_res, indent=2, ensure_ascii=False)
    assert len(mcp_json) <= server.MCP_INLINE_CHAR_BUDGET
    assert mcp_res["verdict"] == "UNSAFE_CONTRACT_VIOLATION"
    assert mcp_res["audit_coverage"]["files_scanned"] == 7
    assert mcp_res["audit_coverage"]["clean_files_verified"] == 5
    assert mcp_res["audit_coverage"]["flagged_files_count"] == 2
    assert mcp_res["audit_coverage"]["elapsed_ms"] < 1500

    hot_rules = {h["rule_id"] for h in mcp_res["ranked_hotspots"]}
    assert "PB011" in hot_rules
    assert "PB013" in hot_rules

    # Scope filtering: `scope_glob="backend/*"` must only report backend/api.py
    scoped_res = server.brain_check(audit=True, scope_glob="backend/*", repo_path=str(tmp_path))
    assert all(h["rel_path"].startswith("backend/") for h in scoped_res["ranked_hotspots"])
    assert any(h["rule_id"] == "PB013" for h in scoped_res["ranked_hotspots"])

    # CLI `--audit --format sarif` emits valid SARIF v2.1.0 with PB011-PB013 rules
    capsys.readouterr()
    rc = cli.main(["--path", str(tmp_path), "check", "--audit", "--format", "sarif", "--ci"])
    assert rc == 1
    sarif_data = json.loads(capsys.readouterr().out)
    assert sarif_data["version"] == "2.1.0"
    sarif_rule_ids = {r["id"] for r in sarif_data["runs"][0]["tool"]["driver"]["rules"]}
    assert {"PB011", "PB012", "PB013"}.issubset(sarif_rule_ids)
    emitted_ids = {r["ruleId"] for r in sarif_data["runs"][0]["results"]}
    assert "PB011" in emitted_ids and "PB013" in emitted_ids

    # Text and Markdown reports surface ranked anomaly hotspots
    txt_report = format_check_report(mcp_res, fmt="text")
    assert "ProjectBrain Audit" in txt_report and "hotspot #1" in txt_report
    md_report = format_check_report(mcp_res, fmt="markdown")
    assert "Ranked Semantic Hotspots" in md_report

    # Stress inline MCP budget with 35 seeded PB011 call-site violations
    for idx in range(35):
        (tmp_path / "frontend" / f"BadCaller_{idx:02d}.jsx").write_text(
            "import React from 'react';\n"
            f"export async function badCall{idx}() {{\n"
            "  return await fetch('/api/tickets', {\n"
            "    method: 'POST',\n"
            f"    body: JSON.stringify({{ wrongField{idx}: {idx} }}),\n"
            "  });\n"
            "}\n",
            encoding="utf-8",
        )
    burst_res = server.brain_check(audit=True, repo_path=str(tmp_path))
    burst_json = json.dumps(burst_res, indent=2, ensure_ascii=False)
    assert len(burst_json) <= server.MCP_INLINE_CHAR_BUDGET, f"Overflowed inline cap: {len(burst_json)} chars"
    assert "audit_coverage" in burst_res
    assert len(burst_res.get("ranked_hotspots", [])) >= 5
