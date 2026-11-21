"""Reproducible Synthetic Full-Stack Precision & Recall Benchmark for PB011, PB012, and PB013.

Unlike backend-only OSS repos (Flask, Django, Requests, Click) where cross-stack HTTP/Pydantic
and React lifecycle verifiers rarely trigger, this synthetic planted-bug corpus measures exact
True Positives, False Positives (strictly counting every unexpected hotspot anywhere in the
corpus, verified by function line-range spans), Precision, Recall, F1, and Latency across
FastAPI/Pydantic/SQLModel + React/TypeScript files containing BOTH planted bugs AND hard negative controls.

Use `--external` to also run a zero-write audit on the real-world `fastapi/full-stack-fastapi-template` OSS repo.

Usage:
  python projectbrain/eval/bench_fullstack.py [--json] [--external]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from projectbrain.brain import ProjectBrain


def _write_files(base: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.lstrip("\n"), encoding="utf-8")


def _build_fullstack_corpus(root: Path) -> tuple[list[dict], list[dict]]:
    """Create a realistic FastAPI + Pydantic v2 + React/TSX monorepo with TP bugs and hard negatives."""
    files: dict[str, str] = {
        # ── Backend Schemas ─────────────────────────────────────────────────
        "backend/schemas.py": """
from pydantic import BaseModel, ConfigDict, Field


class InvoiceCreate(BaseModel):
    customer_id: str
    amount_cents: int
    currency: str = "USD"


class InvoiceResponse(BaseModel):
    invoice_id: str
    amount_cents: int
    status: str


class StrictWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str
    payload_id: str


class ProfilePatch(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    timezone: str | None = None


class CamelAliasInput(BaseModel):
    first_name: str = Field(alias="firstName")
    last_name: str = Field(alias="lastName")


class OpenPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str


class DictAllowPayload(BaseModel):
    model_config = {"extra": "allow"}
    kind: str


class DictStrictPayload(BaseModel):
    model_config = {"extra": "forbid"}
    event_type: str


class TaskItem(BaseModel):
    task_id: str
    title: str
""",
        # ── Backend Routes (FastAPI) ────────────────────────────────────────
        "backend/routers/billing.py": """
import httpx
from fastapi import APIRouter, Depends, Query
from backend.schemas import (
    CamelAliasInput,
    DictAllowPayload,
    DictStrictPayload,
    InvoiceCreate,
    InvoiceResponse,
    OpenPayload,
    ProfilePatch,
    StrictWebhook,
    TaskItem,
)

router = APIRouter(prefix="/api/v1")


def get_db():
    return None


@router.post("/invoices", response_model=InvoiceResponse)
def create_invoice(body: InvoiceCreate, db=Depends(get_db)):
    return {"invoice_id": "inv_1", "amount_cents": body.amount_cents, "status": "open"}


@router.post("/webhooks")
def ingest_webhook(body: StrictWebhook):
    return {"ok": True}


@router.patch("/profile/{user_id}")
def patch_profile_clobber_setattr(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    data = payload.model_dump()
    for k, v in data.items():
        setattr(row, k, v)
    try:
        db.commit()
    except Exception:
        pass
    return {"updated": True}


@router.put("/profile-sqlmodel/{user_id}")
def update_profile_clobber_sqlmodel(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    row.sqlmodel_update(payload.model_dump())
    return {"updated": True}


@router.patch("/profile-safe/{user_id}")
def patch_profile_safe(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(row, k, v)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise exc
    return {"updated": True}


@router.patch("/profile-none-guard/{user_id}")
def patch_profile_none_guard(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    for k, v in payload.model_dump().items():
        if v is not None:
            setattr(row, k, v)
    return {"updated": True}


@router.patch("/profile-filtered/{user_id}")
def patch_profile_filtered_dict(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    data = payload.model_dump()
    data = {k: v for k, v in data.items() if v is not None}
    row.sqlmodel_update(data)
    return {"updated": True}


# pb:ignore PB013 -- deliberate full overwrite across multi-line decorator
@router.patch(
    "/profile-replace/{user_id}",
    tags=["profile"],
)
def patch_profile_multiline_suppressed(user_id: str, payload: ProfilePatch, db=Depends(get_db)):
    row = db.get(user_id)
    row.sqlmodel_update(payload.model_dump())
    return {"updated": True}


def clone_profile_model_copy_clobber(existing, payload: ProfilePatch):
    return existing.model_copy(update=payload.model_dump())


@router.get("/search")
def search_invoices(q: str = Query(...)):
    return {"results": [q]}


@router.post("/alias-check")
def check_alias(body: CamelAliasInput):
    return {"ok": True}


@router.post("/open-check")
def check_open(body: OpenPayload):
    return {"ok": True}


@router.post("/dict-open-check")
def check_dict_open(body: DictAllowPayload):
    return {"ok": True}


@router.post("/dict-strict")
def check_dict_strict(body: DictStrictPayload):
    return {"ok": True}


@router.get("/tasks", response_model=list[TaskItem])
def list_tasks():
    return [{"task_id": "t1", "title": "Ship v1.4"}]


def dispatch_invoice_webhook(webhook_client, invoice_id: str):
    try:
        webhook_client.send_webhook("https://hooks.example.com", {"invoice_id": invoice_id})
    except Exception:
        pass


def post_external_with_short_httpx_var(invoice_id: str):
    with httpx.Client() as c:
        try:
            c.post("https://events.example.com/ingest", json={"invoice_id": invoice_id})
        except Exception:
            pass
""",
        # ── Frontend API Wrapper ────────────────────────────────────────────
        "frontend/src/api/billingClient.ts": """
export async function submitInvoice(payload: Record<string, unknown>) {
  const res = await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return await res.json();
}
""",
        # ── Frontend True Positives (PB011) ─────────────────────────────────
        "frontend/src/pages/BrokenBilling.tsx": """
import axios from "axios";
import { submitInvoice } from "../api/billingClient";

export async function tpMissingRequiredField() {
  const res = await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1" }),
  });
  const data = await res.json();
  return data.invoice_id;
}

export async function tpCamelCaseDriftViaWrapper() {
  return await submitInvoice({ customerId: "c_1", amount_cents: 500 });
}

export async function tpExtraForbidKey() {
  await axios.post("/api/v1/webhooks", {
    event_type: "invoice.paid",
    payload_id: "p_1",
    rogue_field: true,
  });
}

export async function tpVerbMismatch() {
  await fetch("/api/v1/profile/u_1", {
    method: "PUT",
    body: JSON.stringify({ display_name: "Ada" }),
  });
}

export async function tpMissingQueryParam() {
  await fetch("/api/v1/search");
}

export async function tpResponsePropertyMismatch() {
  const res = await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1", amount_cents: 100 }),
  });
  const data = await res.json();
  return data.nonexistent_total;
}

export async function tpListResponseReadAsObject() {
  const res = await fetch("/api/v1/tasks");
  const data = await res.json();
  return data.title;
}

export async function tpListItemFieldMismatch() {
  const res = await fetch("/api/v1/tasks");
  const tasks = await res.json();
  return tasks.map((t: { nonexistent_item_key: string }) => t.nonexistent_item_key);
}

export async function tpTypedResJsonMismatch() {
  const res = await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1", amount_cents: 100 }),
  });
  const data: { invoice_id: string; nonexistent_typed_total: number } = await res.json();
  return data.nonexistent_typed_total;
}

export async function tpAdminInSubstringResponseMismatch() {
  const res = await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1", amount_cents: 100 }),
  });
  const data = await res.json();
  const admin = data.bad_admin_field;
  return admin;
}

export async function tpMapWithNestedCallAndBadField() {
  const res = await fetch("/api/v1/tasks");
  const tasks = await res.json();
  return tasks.map((t: { task_id: string; missing_after_call: string }) => {
    const label = String(t.task_id);
    return label + t.missing_after_call;
  });
}

export async function tpDestructuredMapBadField() {
  const res = await fetch("/api/v1/tasks");
  const tasks = await res.json();
  return tasks.map(({ missing_destr }: { missing_destr: string }) => missing_destr);
}

export async function tpSpreadAuthBeforeExplicitMethod(authHeaders: Record<string, unknown>) {
  await fetch("/api/v1/invoices", {
    ...authHeaders,
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1" }),
  });
}

export function tpPromiseThenChainResponseMismatch() {
  return fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ customer_id: "c_1", amount_cents: 10 }),
  })
    .then((r) => r.json())
    .then((d) => d.missing_chain_prop);
}

export async function tpDictModelConfigExtraForbid() {
  await axios.post("/api/v1/dict-strict", {
    event_type: "order.created",
    rogue_dict_key: true,
  });
}
""",
        # ── Frontend Hard Negatives (PB011 — MUST produce 0 FPs) ────────────
        "frontend/src/pages/ValidBilling.tsx": """
import axios from "axios";

export async function hnValidAliasAndAxiosData() {
  await axios.post("/api/v1/alias-check", { firstName: "Ada", lastName: "Lovelace" });
  const resp = await axios.post("/api/v1/invoices", {
    customer_id: "c_1",
    amount_cents: 200,
  });
  return resp.data.invoice_id;
}

export async function hnValidListMap() {
  const res = await fetch("/api/v1/tasks");
  const tasks: { task_id: string; title: string }[] = await res.json();
  return tasks.map((t) => t.title);
}

export async function hnDefaultExtraIgnoreAndDictAllow() {
  await fetch("/api/v1/profile-safe/u_1", {
    method: "PATCH",
    body: JSON.stringify({ display_name: "Ada", clientTempId: "tmp_99" }),
  });
  await axios.post("/api/v1/dict-open-check", { kind: "audit", uiMeta: 42 });
}

export async function hnExtraAllowAndSpread(base: Record<string, unknown>) {
  await axios.post("/api/v1/open-check", { kind: "audit", extra_meta: 123 });
  await fetch("/api/v1/invoices", {
    method: "POST",
    body: JSON.stringify({ ...base, customer_id: "c_1" }),
  });
}

export async function hnSuppressedIntentionalMismatch() {
  // pb:ignore PB011 -- intentional legacy compatibility probe
  await fetch("/api/v1/search");
}
""",
        # ── Frontend React Lifecycle & Races (PB012 TPs + HNs) ──────────────
        "frontend/src/components/DraftForm.tsx": """
import { useState } from "react";

export function DraftForm() {
  const [draft, setDraft] = useState("");
  return <textarea value={draft} onChange={(e) => setDraft(e.target.value)} />;
}
""",
        "frontend/src/pages/BrokenReact.tsx": """
import { useEffect, useRef, useState } from "react";
import { DraftForm } from "../components/DraftForm";

export function BrokenPostMountUnmount() {
  const [isSaving, setIsSaving] = useState(false);

  async function handleSave() {
    setIsSaving(true);
    await Promise.resolve();
    setIsSaving(false);
  }

  if (isSaving) {
    return <div>Saving...</div>;
  }

  return (
    <div>
      <button onClick={handleSave}>Save</button>
      <DraftForm />
    </div>
  );
}

export function BrokenContainerTernaryUnmount() {
  const [saving, setSaving] = useState(false);

  async function handlePersist() {
    setSaving(true);
    await Promise.resolve();
    setSaving(false);
  }

  return (
    <section>
      <button onClick={handlePersist}>Persist</button>
      {saving ? <div>Saving...</div> : <div className="editor-wrap"><DraftForm /></div>}
    </section>
  );
}

export function BrokenInlineNestedComponent({ title }: { title: string }) {
  function InlineBadge() {
    const [open, setOpen] = useState(false);
    return <button onClick={() => setOpen(!open)}>{title}</button>;
  }
  return <InlineBadge />;
}

export function BrokenUncancelledAsyncRace({ deviceId }: { deviceId: string }) {
  const [signalStrength, setSignalStrength] = useState(0);

  useEffect(() => {
    async function load() {
      const res = await fetch(`/api/v1/search?q=${deviceId}`);
      const json = await res.json();
      setSignalStrength(json.results ? 1 : 0);
    }
    load();
  }, [deviceId]);

  return <div>{signalStrength}</div>;
}

export function BrokenPointFreeThenAndRefFocus({ orgId }: { orgId: string }) {
  const [items, setItems] = useState<unknown>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    boxRef.current?.focus();
    fetch(`/api/v1/search?q=${orgId}`).then((r) => r.json()).then(setItems);
  }, [orgId]);

  return <div ref={boxRef}>{String(items)}</div>;
}

export function BrokenLocalHelperAsyncEffect({ orgId }: { orgId: string }) {
  const [items, setItems] = useState<unknown>(null);

  async function loadOrgItems() {
    const res = await fetch(`/api/v1/search?q=${orgId}`);
    setItems(await res.json());
  }

  useEffect(() => {
    loadOrgItems();
  }, [orgId]);

  return <div>{String(items)}</div>;
}

export function BrokenInlineOnClickAndIfElseUnmount() {
  const [saving, setSaving] = useState(false);

  if (saving) {
    return <div>Saving...</div>;
  } else {
    return (
      <div>
        <button
          onClick={async () => {
            setSaving(true);
            await Promise.resolve();
            setSaving(false);
          }}
        >
          Save
        </button>
        <DraftForm />
      </div>
    );
  }
}

export function BrokenSiblingContainerTernaryUnmount() {
  const [reloading, setReloading] = useState(false);

  async function refresh() {
    setReloading(true);
    await Promise.resolve();
    setReloading(false);
  }

  return (
    <main>
      <button onClick={refresh}>Refresh</button>
      {reloading ? (
        <span>Loading...</span>
      ) : (
        <div>
          <h1>Editor</h1>
          <DraftForm />
        </div>
      )}
    </main>
  );
}

export function BrokenThenReturnAndIfRefFocusRace({ orgId }: { orgId: string }) {
  const [rows, setRows] = useState<unknown>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (inputRef.current) inputRef.current.focus();
    fetch(`/api/v1/search?q=${orgId}`)
      .then((res) => {
        const data = res.json();
        return data;
      })
      .then(setRows);
  }, [orgId]);

  return <input ref={inputRef} value={String(rows)} readOnly />;
}

export function BrokenPayloadSignalJsonRace({ symbol }: { symbol: string }) {
  const [quote, setQuote] = useState<unknown>(null);

  useEffect(() => {
    fetch("/api/v1/open-check", {
      method: "POST",
      body: JSON.stringify({ kind: symbol, signal: "buy" }),
    })
      .then((r) => r.json())
      .then((d) => setQuote(d));
  }, [symbol]);

  return <div>{String(quote)}</div>;
}
""",
        "frontend/src/pages/SafeReact.tsx": """
import { useEffect, useState } from "react";
import { DraftForm } from "../components/DraftForm";

export function SafeBootstrapSpinner() {
  const [bootLoading, setBootLoading] = useState(true);
  useEffect(() => {
    setBootLoading(false);
  }, []);

  if (bootLoading) {
    return <div>Booting...</div>;
  }
  return <DraftForm />;
}

export function SafeTerminalOrderConfirmed() {
  const [submitted, setSubmitted] = useState(false);

  async function handleSubmit() {
    await Promise.resolve();
    setSubmitted(true);
  }

  if (submitted) {
    return <div>Order Confirmed!</div>;
  }
  return (
    <div>
      <button onClick={handleSubmit}>Submit</button>
      <DraftForm />
    </div>
  );
}

export function SafeResettableOrderConfirmed() {
  const [submitted, setSubmitted] = useState(false);

  async function handleSubmit() {
    await Promise.resolve();
    setSubmitted(true);
  }

  function handleNewOrder() {
    setSubmitted(false);
  }

  if (submitted) {
    return (
      <div>
        <span>Order Confirmed!</span>
        <button onClick={handleNewOrder}>New Order</button>
      </div>
    );
  }
  return (
    <div>
      <button onClick={handleSubmit}>Submit</button>
      <DraftForm />
    </div>
  );
}

export function SafeEditPreviewToggle() {
  const [isPreview, setIsPreview] = useState(false);
  return (
    <div>
      <button onClick={() => setIsPreview(!isPreview)}>Toggle Preview</button>
      {isPreview ? <div>Preview</div> : <DraftForm />}
    </div>
  );
}

export function SafeAbortControllerEffect({ userId }: { userId: string }) {
  const [profile, setProfile] = useState<string>("");

  useEffect(() => {
    const ctrl = new AbortController();
    fetch(`/api/v1/search?q=${userId}`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((d) => setProfile(String(d)));
    return () => ctrl.abort();
  }, [userId]);

  return <div>{profile}</div>;
}

export function SafeSuppressedEffect({ userId }: { userId: string }) {
  const [status, setStatus] = useState("");
  // pb:ignore PB012 -- intentionally fire-and-forget telemetry
  useEffect(() => {
    fetch(`/api/v1/search?q=${userId}`).then(() => setStatus("sent"));
  }, [userId]);
  return <div>{status}</div>;
}
""",
    }
    _write_files(root, files)

    expected_tps = [
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_missing_required_field", "fn": "tpMissingRequiredField"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_payload_casing_mismatch", "fn": "tpCamelCaseDriftViaWrapper"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_unknown_payload_field", "fn": "tpExtraForbidKey"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_verb_mismatch", "fn": "tpVerbMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_missing_query_param", "fn": "tpMissingQueryParam"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpResponsePropertyMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpListResponseReadAsObject"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpListItemFieldMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpTypedResJsonMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpAdminInSubstringResponseMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpMapWithNestedCallAndBadField"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpDestructuredMapBadField"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_missing_required_field", "fn": "tpSpreadAuthBeforeExplicitMethod"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_response_key_mismatch", "fn": "tpPromiseThenChainResponseMismatch"},
        {"rule_id": "PB011", "rel_path": "frontend/src/pages/BrokenBilling.tsx", "category": "http_unknown_payload_field", "fn": "tpDictModelConfigExtraForbid"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "stateful_child_unmount_guard", "fn": "BrokenPostMountUnmount"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "stateful_child_unmount_guard", "fn": "BrokenContainerTernaryUnmount"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "nested_component_remount", "fn": "BrokenInlineNestedComponent"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "uncancelled_async_effect_race", "fn": "BrokenUncancelledAsyncRace"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "uncancelled_async_effect_race", "fn": "BrokenPointFreeThenAndRefFocus"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "uncancelled_async_effect_race", "fn": "BrokenLocalHelperAsyncEffect"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "stateful_child_unmount_guard", "fn": "BrokenInlineOnClickAndIfElseUnmount"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "stateful_child_unmount_guard", "fn": "BrokenSiblingContainerTernaryUnmount"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "uncancelled_async_effect_race", "fn": "BrokenThenReturnAndIfRefFocusRace"},
        {"rule_id": "PB012", "rel_path": "frontend/src/pages/BrokenReact.tsx", "category": "uncancelled_async_effect_race", "fn": "BrokenPayloadSignalJsonRace"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "pydantic_partial_update_clobber", "fn": "patch_profile_clobber_setattr"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "swallowed_io_exception", "fn": "patch_profile_clobber_setattr"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "pydantic_partial_update_clobber", "fn": "update_profile_clobber_sqlmodel"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "pydantic_partial_update_clobber", "fn": "clone_profile_model_copy_clobber"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "swallowed_io_exception", "fn": "dispatch_invoice_webhook"},
        {"rule_id": "PB013", "rel_path": "backend/routers/billing.py", "category": "swallowed_io_exception", "fn": "post_external_with_short_httpx_var"},
    ]
    safe_controls = [
        {"rel_path": "frontend/src/pages/ValidBilling.tsx", "fn": None},
        {"rel_path": "frontend/src/pages/SafeReact.tsx", "fn": None},
        {"rel_path": "frontend/src/components/DraftForm.tsx", "fn": None},
        {"rel_path": "backend/routers/billing.py", "fn": "patch_profile_safe"},
        {"rel_path": "backend/routers/billing.py", "fn": "patch_profile_none_guard"},
        {"rel_path": "backend/routers/billing.py", "fn": "patch_profile_filtered_dict"},
        {"rel_path": "backend/routers/billing.py", "fn": "patch_profile_multiline_suppressed"},
    ]
    return expected_tps, safe_controls


def _compute_fn_spans(root: Path, rel_path: str) -> dict[str, tuple[int, int]]:
    """Return exact 1-based `(start_line, end_line)` spans for every top-level function in `rel_path`."""
    txt = (root / rel_path).read_text(encoding="utf-8")
    spans: dict[str, tuple[int, int]] = {}
    if rel_path.endswith(".py"):
        tree = ast.parse(txt)
        for nd in tree.body:
            if isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans[nd.name] = (int(nd.lineno), int(getattr(nd, "end_lineno", nd.lineno)))
        return spans
    lines = txt.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*export\s+(?:async\s+)?function\s+([A-Za-z_]\w*)\b", ln)
        if not m:
            continue
        fn_nm = m.group(1)
        depth = 0
        started = False
        end_idx = i
        for j in range(i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                started = True
            if started and depth <= 0:
                end_idx = j
                break
        spans[fn_nm] = (i + 1, end_idx + 1)
    return spans


def run_fullstack_corpus_benchmark() -> dict:
    with tempfile.TemporaryDirectory(prefix="pb_fullstack_bench_") as tmp:
        root = Path(tmp)
        expected_tps, safe_controls = _build_fullstack_corpus(root)

        span_cache: dict[str, dict[str, tuple[int, int]]] = {}
        for item in [*expected_tps, *safe_controls]:
            rp = item["rel_path"]
            if rp not in span_cache:
                span_cache[rp] = _compute_fn_spans(root, rp)

        t0 = time.perf_counter()
        brain = ProjectBrain(str(root))
        res = brain.check(audit=True, profile="fullstack", zero_write=True)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        hotspots = res.get("ranked_hotspots", [])
        total_hotspots_emitted = int(res.get("total_hotspots", len(hotspots)))

        # Score per rule with STRICT line-span matching: ANY unexpected hotspot anywhere counts as a FP!
        per_rule: dict[str, dict] = {}
        for rid in ("PB011", "PB012", "PB013"):
            exp_for_rule = [e for e in expected_tps if e["rule_id"] == rid]
            got_for_rule = [h for h in hotspots if h.get("rule_id") == rid]
            matched_hotspot_idxs: set[int] = set()
            tp = 0
            missed: list[dict] = []

            for e in exp_for_rule:
                lo, hi = span_cache[e["rel_path"]][e["fn"]]
                hit_idx = None
                for idx_h, h in enumerate(got_for_rule):
                    if idx_h in matched_hotspot_idxs:
                        continue
                    h_ln = int(h.get("line") or 0)
                    if h.get("rel_path") == e["rel_path"] and h.get("category") == e["category"] and lo <= h_ln <= hi:
                        hit_idx = idx_h
                        break
                if hit_idx is not None:
                    matched_hotspot_idxs.add(hit_idx)
                    tp += 1
                else:
                    missed.append({**e, "expected_span": f"L{lo}-L{hi}"})

            # Every unmatched hotspot (whether in a clean file, safe function, or duplicate/spurious in a buggy file) is a FP!
            fps = [h for idx_h, h in enumerate(got_for_rule) if idx_h not in matched_hotspot_idxs]

            fn = len(missed)
            fp = len(fps)
            prec = tp / max(1, tp + fp) if (tp + fp) > 0 else 1.0
            rec = tp / max(1, tp + fn) if (tp + fn) > 0 else 1.0
            f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
            per_rule[rid] = {
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "expected_cases": len(exp_for_rule),
                "precision": round(prec, 4),
                "recall": round(rec, 4),
                "f1": round(f1, 4),
                "missed": missed,
                "false_positives": [
                    {"rel_path": h.get("rel_path"), "line": h.get("line"), "category": h.get("category"), "summary": h.get("summary")}
                    for h in fps
                ],
            }

        # Any hotspot emitted under an unexpected rule ID also counts as an overall FP
        extra_rule_fps = [h for h in hotspots if h.get("rule_id") not in ("PB011", "PB012", "PB013")]
        tot_tp = sum(v["tp"] for v in per_rule.values())
        tot_fp = sum(v["fp"] for v in per_rule.values()) + len(extra_rule_fps)
        tot_fn = sum(v["fn"] for v in per_rule.values())
        o_prec = tot_tp / max(1, tot_tp + tot_fp)
        o_rec = tot_tp / max(1, tot_tp + tot_fn)
        o_f1 = (2 * o_prec * o_rec / (o_prec + o_rec)) if (o_prec + o_rec) > 0 else 0.0

        zero_write_ok = not (root / ".projectbrain.db").exists() and not (root / ".projectbrain_ledger.json").exists()
        return {
            "corpus_kind": "synthetic_planted_bug_corpus",
            "elapsed_ms": elapsed_ms,
            "zero_write_verified": zero_write_ok,
            "suppressed_count": res.get("suppressed_count", 0),
            "total_hotspots_emitted": total_hotspots_emitted,
            "audit_coverage": res.get("audit_coverage", {}),
            "overall": {
                "tp": tot_tp,
                "fp": tot_fp,
                "fn": tot_fn,
                "precision": round(o_prec, 4),
                "recall": round(o_rec, 4),
                "f1": round(o_f1, 4),
            },
            "per_rule": per_rule,
        }


def run_external_fullstack_audit(repos_dir: Path) -> dict:
    """Optionally clone and audit `fastapi/full-stack-fastapi-template` with `--profile fullstack`."""
    dest = repos_dir / "full-stack-fastapi-template"
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/fastapi/full-stack-fastapi-template.git", str(dest)],
            check=True,
        )
    t0 = time.perf_counter()
    brain = ProjectBrain(str(dest))
    res = brain.check(audit=True, profile="fullstack", zero_write=True)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "repo": "fastapi/full-stack-fastapi-template",
        "elapsed_ms": elapsed_ms,
        "verdict": res.get("verdict"),
        "audit_coverage": res.get("audit_coverage", {}),
        "hotspots_count": len(res.get("ranked_hotspots", [])),
        "ranked_hotspots": res.get("ranked_hotspots", [])[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Full-stack PB011/PB012/PB013 precision & recall benchmark")
    parser.add_argument("--json", action="store_true", help="Print raw JSON report")
    parser.add_argument("--external", action="store_true", help="Also clone & audit fastapi/full-stack-fastapi-template")
    parser.add_argument(
        "--repos-dir",
        default=str(Path(tempfile.gettempdir()) / "pb_eval_repos"),
        help="Directory for optional external OSS full-stack clone",
    )
    args = parser.parse_args()

    report: dict = {"fullstack_corpus": run_fullstack_corpus_benchmark()}
    if args.external:
        report["external_fullstack"] = run_external_fullstack_audit(Path(args.repos_dir))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        c = report["fullstack_corpus"]
        ov = c["overall"]
        print("=" * 72)
        print("ProjectBrain Synthetic Full-Stack Corpus Benchmark (PB011 / PB012 / PB013)")
        print("=" * 72)
        print(
            f"Overall: TP={ov['tp']} FP={ov['fp']} FN={ov['fn']} | "
            f"Precision={ov['precision']:.1%} Recall={ov['recall']:.1%} F1={ov['f1']:.1%} | "
            f"Time={c['elapsed_ms']}ms | Zero-Write={c['zero_write_verified']} | "
            f"Inline Suppressed={c['suppressed_count']}"
        )
        for rid, stats in c["per_rule"].items():
            print(
                f"  {rid}: TP={stats['tp']}/{stats['expected_cases']} FP={stats['fp']} FN={stats['fn']} "
                f"(P={stats['precision']:.1%}, R={stats['recall']:.1%}, F1={stats['f1']:.1%})"
            )
            if stats["missed"]:
                print(f"    MISSED: {stats['missed']}")
            if stats["false_positives"]:
                print(f"    FALSE POSITIVES: {stats['false_positives']}")
        if "external_fullstack" in report:
            ex = report["external_fullstack"]
            print(
                f"\nExternal ({ex['repo']}): verdict={ex['verdict']} "
                f"hotspots={ex['hotspots_count']} coverage={ex['audit_coverage']} time={ex['elapsed_ms']}ms"
            )
    return 0 if report["fullstack_corpus"]["overall"]["fp"] == 0 and report["fullstack_corpus"]["overall"]["fn"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
