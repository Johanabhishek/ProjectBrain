"""Epistemic Three-Valued Claim Verification, Blast-Radius Impact, and Cross-Boundary Flow Tracer for ProjectBrain.

Provides:
  1. Three-valued epistemic claim verification (`verify_claim`, `verify_batch`) returning
     SUPPORTED / REFUTED / UNKNOWN with static edge provenance.
  2. Pre-edit architectural plan validation (`verify_plan`) and blast-radius analysis (`impact`).
  3. Cross-boundary frontend->backend request flow tracing (`trace_flow`) and git co-change analysis (`co_change_analysis`).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .graph import cls_id, file_id, fn_id, route_id

if TYPE_CHECKING:
    from .brain import ProjectBrain

_VERIFY_RELATIONS = {
    "calls": "fn",
    "reaches": "any",
    "inherits": "cls",
    "has_method": "cls",
    "defines": "file",
    "imports": "file",
    "handles": "route",
}

_STRONG = ("exact", "qualified", "import_resolved")


def _verdict(
    subject: str,
    relation: str,
    obj: str,
    verdict: str,
    explanation: str,
    confidence: str | None = None,
    evidence: list[dict] | None = None,
) -> dict:
    return {
        "verdict": verdict,
        "claim": {"subject": subject, "relation": relation, "object": obj},
        "confidence": confidence,
        "explanation": explanation,
        "evidence": evidence or [],
    }


def _name_in(obj: str, captured: set) -> bool:
    o = obj.lower()
    o_bare = o.rsplit(".", 1)[-1]
    return any(
        o == c.lower() or o_bare == c.lower() or o == c.lower().rsplit(".", 1)[-1]
        for c in captured
    )


def match_subjects(brain: "ProjectBrain", subject: str, kind: str) -> list[dict]:
    s = subject.lower()
    out: list[dict] = []
    for rel, info in brain._index.items():
        status = info.get("parse_status", "ok")
        if kind == "fn":
            for f in info.get("functions", []):
                qualified = (
                    f"{f['parent_class']}.{f['name']}"
                    if f.get("parent_class")
                    else f["name"]
                )
                if s in (f["name"].lower(), qualified.lower()):
                    out.append({
                        "rel_path": rel,
                        "status": status,
                        "kind": "fn",
                        "entity": f,
                        "label": qualified,
                        "node_id": fn_id(rel, f["name"], f.get("parent_class")),
                        "line": f.get("line_start"),
                    })
        elif kind == "cls":
            for c in info.get("classes", []):
                if c["name"].lower() == s:
                    out.append({
                        "rel_path": rel,
                        "status": status,
                        "kind": "cls",
                        "entity": c,
                        "label": c["name"],
                        "node_id": cls_id(rel, c["name"]),
                        "line": c.get("line_start"),
                    })
        elif kind == "file":
            if s in (rel.lower(), Path(rel).name.lower()):
                out.append({
                    "rel_path": rel,
                    "status": status,
                    "kind": "file",
                    "entity": info,
                    "label": rel,
                    "node_id": file_id(rel),
                    "line": 1,
                })
        elif kind == "route":
            for r in info.get("routes", []):
                forms = (
                    r["path"].lower(),
                    f"{r['method']} {r['path']}".lower(),
                    f"{r['method']}:{r['path']}".lower(),
                )
                if s in forms:
                    out.append({
                        "rel_path": rel,
                        "status": status,
                        "kind": "route",
                        "entity": r,
                        "label": f"{r['method']} {r['path']}",
                        "node_id": route_id(r["method"], r["path"]),
                        "line": r.get("line"),
                    })
    return out


def captured_set(brain: "ProjectBrain", m: dict, relation: str, info: dict) -> set:
    e = m["entity"]
    if relation == "calls":
        return set(e.get("calls", []))
    if relation == "inherits":
        return set(e.get("bases", []))
    if relation == "has_method":
        return set(e.get("methods", []))
    if relation == "handles":
        return {e["handler"]} if e.get("handler") else set()
    if relation == "defines":
        return (
            {f["name"] for f in info.get("functions", [])}
            | {c["name"] for c in info.get("classes", [])}
        )
    if relation == "imports":
        raw = set(info.get("imports", []))
        resolved = set(brain._import_targets.get(m["rel_path"], []))
        resolved_names = {Path(p).name for p in resolved} | {Path(p).stem for p in resolved}
        return raw | resolved | resolved_names
    return set()


def support_evidence(brain: "ProjectBrain", m: dict, relation: str, obj: str, info: dict) -> dict:
    rank = {"exact": 3, "qualified": 2, "import_resolved": 1, "dynamic_dispatch": 0, "candidate": 0}
    o = obj.lower()
    o_bare = o.rsplit(".", 1)[-1]
    found = None
    if brain._graph:
        for ei in brain._graph._adj.get(m["node_id"], []):
            edge = brain._graph.edges[ei]
            if edge["type"] != relation:
                continue
            tgt = brain._graph.nodes.get(edge["dst"], {})
            tname = (tgt.get("name") or tgt.get("path") or "").lower()
            tparent = (tgt.get("parent_class") or "").lower()
            tqual = f"{tparent}.{tname}" if tparent else tname
            if not (o == tqual or o == tname or o == tname.rsplit(".", 1)[-1] or o_bare == tname):
                continue
            e_conf = edge["confidence"]
            e_reason = edge.get("reason", "")
            if relation == "calls" and m["kind"] == "fn" and ("." in o or tparent):
                ent = m["entity"]
                caller_cls = (ent.get("parent_class") or "").lower()
                q_calls = {k.lower(): v for k, v in (ent.get("qualified_calls") or {}).items()}
                req_recv = o.rsplit(".", 1)[0] if "." in o else tparent
                matching_recvs = [
                    k[: -(len(o_bare) + 1)]
                    for k in q_calls
                    if k.endswith("." + o_bare)
                ]
                if matching_recvs:
                    caller_bases = {caller_cls}
                    for cls_d in info.get("classes", []):
                        if (cls_d.get("name") or "").lower() == caller_cls:
                            caller_bases.update(b.lower() for b in cls_d.get("bases", []))
                    static_recv = any(
                        r_u == req_recv
                        or (r_u in ("self", "cls", "this", "super()") and req_recv in caller_bases)
                        for r_u in matching_recvs
                    )
                    if not static_recv:
                        e_conf = "dynamic_dispatch"
                        e_reason = (
                            f"{m['label']} calls '.{o_bare}' on dynamic receiver "
                            f"'{matching_recvs[0]}' (line {edge.get('line')}; candidate: {tqual or obj}) — "
                            "receiver type cannot be resolved statically."
                        )
            if found is None or rank.get(e_conf, 0) > rank.get(found["confidence"], 0):
                found = {
                    "node_id": m["node_id"],
                    "file": m["rel_path"],
                    "target": edge["dst"],
                    "confidence": e_conf,
                    "line": edge.get("line"),
                    "reason": e_reason,
                }
    if found and found["confidence"] in _STRONG:
        return found
    if relation == "imports":
        for rp in brain._import_targets.get(m["rel_path"], []):
            if o in (rp.lower(), Path(rp).name.lower(), Path(rp).stem.lower()):
                return {
                    "node_id": m["node_id"],
                    "file": m["rel_path"],
                    "target": file_id(rp),
                    "confidence": "exact",
                    "line": m.get("line"),
                    "reason": f"'{m['label']}' imports resolved file '{rp}'.",
                }
    if relation in ("defines", "has_method", "imports", "handles"):
        return {
            "node_id": m["node_id"],
            "file": m["rel_path"],
            "target": None,
            "confidence": "exact",
            "line": m.get("line"),
            "reason": f"'{obj}' is a direct {relation} fact of {m['label']}.",
        }
    if found:
        return found
    line = m["entity"].get("call_sites", {}).get(obj) if m["kind"] == "fn" else m.get("line")
    return {
        "node_id": m["node_id"],
        "file": m["rel_path"],
        "target": None,
        "confidence": "candidate",
        "line": line,
        "reason": (
            f"{m['label']} references '{obj}', but no matching definition "
            "is import-resolved in this codebase (external or ambiguous)."
        ),
    }


def verify_reaches(brain: "ProjectBrain", subject: str, obj: str) -> dict:
    if not brain._graph:
        return _verdict(subject, "reaches", obj, "UNKNOWN", "Graph not available.")
    a_ids = brain._graph.find_nodes_by_name(subject)
    b_ids = brain._graph.find_nodes_by_name(obj)
    if not a_ids or not b_ids:
        return _verdict(
            subject,
            "reaches",
            obj,
            "UNKNOWN",
            "One or both symbols are not in the graph — cannot trace flow.",
        )
    saw_candidate = False
    for a in a_ids[:5]:
        for b in b_ids[:5]:
            path = brain._graph.find_directed_path(a, b, {"calls", "handles"})
            if not path:
                continue
            if all(e["confidence"] in _STRONG for e in path):
                return _verdict(
                    subject,
                    "reaches",
                    obj,
                    "SUPPORTED",
                    f"Control flows {subject} -> {obj} in {len(path)} hop(s) "
                    "along trusted call edges.",
                    confidence="import_resolved",
                    evidence=[{
                        "path": [
                            {
                                "src": e["src"],
                                "dst": e["dst"],
                                "type": e["type"],
                                "confidence": e["confidence"],
                                "line": e.get("line"),
                            }
                            for e in path
                        ]
                    }],
                )
            saw_candidate = True
    if saw_candidate:
        return _verdict(
            subject,
            "reaches",
            obj,
            "UNKNOWN",
            "A path exists but only through name-only (candidate) edges — "
            "the target is ambiguous, so reachability is not certain.",
        )
    return _verdict(
        subject,
        "reaches",
        obj,
        "UNKNOWN",
        "No call-flow path found. Absence of a static path is not proof of "
        "unreachability (dynamic dispatch, unresolved imports) — hence UNKNOWN, "
        "not REFUTED.",
    )


def verify_claim(brain: "ProjectBrain", subject: str, relation: str, object: str) -> dict:
    """Ground a structural claim "<subject> <relation> <object>" against the
    index, returning SUPPORTED / REFUTED / UNKNOWN with evidence.
    """
    brain._ensure_fresh()
    subject = (subject or "").strip()
    obj = (object or "").strip()
    relation = (relation or "").strip().lower()

    if relation not in _VERIFY_RELATIONS:
        return _verdict(
            subject,
            relation,
            obj,
            "UNKNOWN",
            f"Unsupported relation '{relation}'. Supported: {', '.join(sorted(_VERIFY_RELATIONS))}.",
        )
    if not subject or not obj:
        return _verdict(
            subject,
            relation,
            obj,
            "UNKNOWN",
            "Both subject and object are required to verify a claim.",
        )

    if relation == "reaches":
        return verify_reaches(brain, subject, obj)

    kind = _VERIFY_RELATIONS[relation]
    matches = match_subjects(brain, subject, kind)
    if not matches:
        return _verdict(
            subject,
            relation,
            obj,
            "UNKNOWN",
            f"No {kind} matching '{subject}' is indexed — cannot verify.",
        )

    reasons: list[str] = []
    refutable = len(matches) == 1
    if len(matches) > 1:
        reasons.append(
            f"'{subject}' is ambiguous: {len(matches)} definitions "
            f"({', '.join(m['rel_path'] for m in matches[:4])})."
        )

    for m in matches:
        info = brain._index[m["rel_path"]]
        captured = captured_set(brain, m, relation, info)
        if _name_in(obj, captured):
            ev = support_evidence(brain, m, relation, obj, info)
            if ev["confidence"] in _STRONG:
                return _verdict(
                    subject,
                    relation,
                    obj,
                    "SUPPORTED",
                    ev["reason"],
                    confidence=ev["confidence"],
                    evidence=[ev],
                )
            reasons.append(ev["reason"])
            refutable = False
        if m["status"] != "ok":
            refutable = False
            reasons.append(
                f"{m['label']} in {m['rel_path']} parsed as '{m['status']}' — cannot confirm absence."
            )

    if refutable:
        m = matches[0]
        captured = captured_set(brain, m, relation, brain._index[m["rel_path"]])
        return _verdict(
            subject,
            relation,
            obj,
            "REFUTED",
            f"No direct '{relation}' relation from {m['label']} to '{obj}'. "
            f"Checked the complete captured set ({len(captured)} item(s)) "
            "from a cleanly-parsed file.",
            confidence="exact",
            evidence=[{
                "node_id": m["node_id"],
                "file": m["rel_path"],
                "line": m.get("line"),
                "captured": sorted(captured),
            }],
        )

    return _verdict(
        subject,
        relation,
        obj,
        "UNKNOWN",
        " ".join(reasons) or "Insufficient information to confirm or refute.",
    )


def verify_batch(brain: "ProjectBrain", claims: list[tuple[str, str, str]]) -> dict:
    brain._ensure_fresh()
    verdicts = []
    counts = {"SUPPORTED": 0, "REFUTED": 0, "UNKNOWN": 0}

    for subject, relation, obj in claims:
        v = verify_claim(brain, subject, relation, obj)
        verdicts.append(v)
        counts[v.get("verdict", "UNKNOWN")] += 1

    return {
        "verdicts": verdicts,
        "summary": counts,
        "total": len(claims),
    }


def verify_plan(
    brain: "ProjectBrain",
    task: str,
    files_to_edit: list[str],
    files_to_create: list[str] | None = None,
) -> dict:
    brain._ensure_fresh()

    plan_set: set[str] = set()
    for f in files_to_edit:
        try:
            plan_set.add(str(Path(f).relative_to(brain.root)).replace("\\", "/"))
        except ValueError:
            plan_set.add(f.replace("\\", "/"))

    if files_to_create:
        for f in files_to_create:
            plan_set.add(f.replace("\\", "/"))

    warnings: list[dict] = []
    missing_files: list[dict] = []
    test_files: list[str] = []
    risks: list[dict] = []

    for rel_path in plan_set:
        info = brain._index.get(rel_path)
        if not info:
            continue

        from .reporters import _is_test_file_path

        importers = brain._imported_by.get(rel_path, [])
        for imp in importers:
            if imp in plan_set:
                continue

            if _is_test_file_path(imp):
                test_files.append(imp)
            else:
                missing_files.append({
                    "rel_path": imp,
                    "reason": f"imports '{rel_path}' which is being edited",
                    "severity": "warning",
                })

        targets = brain._import_targets.get(rel_path, [])
        for target in targets:
            if target in plan_set:
                continue
            target_info = brain._index.get(target, {})
            if target_info.get("classes"):
                cls_names = [c["name"] for c in target_info["classes"]]
                task_lower = task.lower()
                for cls_name in cls_names:
                    if cls_name.lower() in task_lower:
                        missing_files.append({
                            "rel_path": target,
                            "reason": (
                                f"defines class '{cls_name}' mentioned in the task, "
                                f"imported by planned file '{rel_path}'"
                            ),
                            "severity": "important",
                        })
                        break

        importer_count = brain._importer_counts.get(rel_path, 0)
        behaviors = brain._behaviors.get(rel_path, {})
        has_orchestrator = any(b == "orchestrator" for b in behaviors.values())
        has_handler = any(b == "handler" for b in behaviors.values())

        risk_level = "low"
        risk_reasons = []
        if importer_count >= 5:
            risk_level = "high"
            risk_reasons.append(f"imported by {importer_count} files (hub file)")
        elif importer_count >= 2:
            risk_level = "medium"
            risk_reasons.append(f"imported by {importer_count} files")
        if has_orchestrator:
            risk_level = max(risk_level, "medium", key=lambda x: ["low", "medium", "high"].index(x))
            risk_reasons.append("contains orchestrator function(s)")
        if has_handler:
            risk_reasons.append("contains route handler(s)")

        if risk_reasons:
            risks.append({
                "rel_path": rel_path,
                "risk": risk_level,
                "reasons": risk_reasons,
            })

    seen: set[str] = set()
    unique_missing = []
    for mf in missing_files:
        if mf["rel_path"] not in seen:
            seen.add(mf["rel_path"])
            unique_missing.append(mf)

    if unique_missing:
        verdict = "INCOMPLETE"
        message = (
            f"Plan covers {len(plan_set)} file(s) but may be missing "
            f"{len(unique_missing)} connected file(s)."
        )
    elif test_files:
        verdict = "NEEDS_TEST_UPDATES"
        message = (
            f"Plan looks structurally complete. "
            f"{len(set(test_files))} test file(s) may need updating."
        )
    else:
        verdict = "COMPLETE"
        message = "Plan covers all structurally connected files."

    result: dict = {
        "verdict": verdict,
        "message": message,
        "planned_files": sorted(plan_set),
    }
    if unique_missing:
        result["missing_files"] = unique_missing
    if test_files:
        result["test_files_to_update"] = sorted(set(test_files))
    if risks:
        result["risks"] = risks
    if warnings:
        result["warnings"] = warnings
    return result


def impact(brain: "ProjectBrain", files_changed: list[str] | str, *, _already_fresh: bool = False) -> dict:
    from .contracts import verify_cross_file_contracts

    if isinstance(files_changed, (str, Path)):
        files_changed = [str(files_changed)]
    if not _already_fresh:
        if hasattr(brain, "_capture_pre_edit_snapshots"):
            brain._capture_pre_edit_snapshots(files_changed)
        brain._ensure_fresh()

    results = []
    all_impacted: set[str] = set()
    valid_focus_rels: set[str] = set()
    max_risk = "low"
    _RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    for path in files_changed:
        try:
            rel_path = str(Path(path).relative_to(brain.root)).replace("\\", "/")
        except ValueError:
            rel_path = path.replace("\\", "/")

        if rel_path not in brain._index:
            results.append({
                "rel_path": rel_path,
                "status": "not_indexed",
                "risk": "unknown",
            })
            continue

        valid_focus_rels.add(rel_path)
        info = brain._index[rel_path]
        importers = brain._imported_by.get(rel_path, [])
        importer_count = len(importers)
        all_impacted.update(importers)

        from .reporters import _is_test_file_path

        defined_fns = [fn for fn in info.get("functions", [])]
        callers: list[dict] = []
        if brain._graph and defined_fns:
            for fn in defined_fns:
                fn_name = fn["name"]
                fn_node_id = fn_id(rel_path, fn_name, fn.get("parent_class"))
                if fn_node_id in brain._graph.nodes:
                    for ei in brain._graph._radj.get(fn_node_id, ()):
                        edge = brain._graph.edges[ei]
                        if edge["type"] == "calls":
                            src_node = brain._graph.nodes.get(edge["src"], {})
                            in_file = (src_node.get("rel_path") or "").replace("\\", "/")
                            if not in_file or in_file == rel_path:
                                continue
                            ln = edge.get("line")
                            if any(
                                prev["function"] == fn_name
                                and prev["in_file"] == in_file
                                and prev["line"] == ln
                                and ln is not None
                                for prev in callers
                            ):
                                continue
                            callers.append({
                                "function": fn_name,
                                "signature": fn.get("signature", ""),
                                "called_by": edge["src"],
                                "in_file": in_file,
                                "line": ln,
                                "confidence": edge.get("confidence", "candidate"),
                            })

        behaviors = brain._get_file_behaviors(rel_path)
        behavior_summary = {}
        for name, behavior in behaviors.items():
            if behavior != "unknown":
                behavior_summary.setdefault(behavior, []).append(name)

        if importer_count >= 10:
            risk = "critical"
        elif importer_count >= 5:
            risk = "high"
        elif importer_count >= 2:
            risk = "medium"
        else:
            risk = "low"

        if _RISK_ORDER.get(risk, 0) > _RISK_ORDER.get(max_risk, 0):
            max_risk = risk

        test_importers = []
        prod_importers = []
        for imp in importers:
            if _is_test_file_path(imp):
                test_importers.append(imp)
            else:
                prod_importers.append(imp)

        entry: dict = {
            "rel_path": rel_path,
            "risk": risk,
            "direct_consumers": prod_importers,
            "consumer_count": len(prod_importers),
            "test_consumers": test_importers,
            "test_consumer_count": len(test_importers),
        }
        if callers:
            entry["called_by"] = callers[:40]
        if behavior_summary:
            entry["behaviors"] = behavior_summary
        routes = info.get("routes", [])
        if routes:
            entry["routes"] = routes

        results.append(entry)

    contract_diags: list[str] = []
    sem_warnings: list[str] = []
    if valid_focus_rels:
        diag_6d, _ = brain._compile_6d_contract_diagnostics(
            "impact verification", valid_focus_rels, strict_focus=True
        )
        contract_diags.extend(diag_6d)
        for cd in verify_cross_file_contracts(brain, valid_focus_rels):
            if cd not in contract_diags:
                contract_diags.append(cd)
        sem_warnings = list(getattr(brain, "_last_semantic_warnings", None) or ())
        if contract_diags and _RISK_ORDER.get(max_risk, 0) < _RISK_ORDER["high"]:
            max_risk = "high"

    verdict = (
        "UNSAFE_CONTRACT_VIOLATION"
        if contract_diags
        else ("HIGH_IMPACT_REVIEW_CALLERS" if max_risk in ("high", "critical") else "SAFE_TO_PROCEED")
    )

    imp_out = {
        "files_analyzed": len(results),
        "total_impact_zone": len(all_impacted),
        "overall_risk": max_risk,
        "verdict": verdict,
        "all_contracts_valid": len(contract_diags) == 0,
        "contract_diagnostics": contract_diags,
        "details": results,
        "all_impacted_files": sorted(all_impacted),
    }
    if sem_warnings:
        imp_out["semantic_warnings"] = sem_warnings
    return imp_out


def url_matches(pattern: str, url: str) -> bool:
    pattern = pattern.rstrip("/") or "/"
    url = url.rstrip("/") or "/"

    if pattern == url:
        return True

    _PLACEHOLDER = "__DYN__"
    normalized = re.sub(r"(:\w+|\[\w+\]|\{\w+\})", _PLACEHOLDER, pattern)
    pattern_re = re.escape(normalized).replace(re.escape(_PLACEHOLDER), "[^/]+")
    pattern_re = pattern_re.replace(re.escape("..."), ".*")
    return bool(re.fullmatch(pattern_re, url))


def _normalize_url_query(raw_url: str, method: str | None) -> tuple[str, str | None]:
    """Extract optional leading HTTP verb (`'POST /api/foo'`) and strip query string / `${baseUrl}` prefix."""
    s = raw_url.strip()
    m_verb = re.match(r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD|ANY|\*)\s+(.+)$", s, re.IGNORECASE)
    if m_verb:
        verb = m_verb.group(1).upper()
        s = m_verb.group(2).strip()
        if method is None:
            method = None if verb in ("ANY", "*") else verb
    s = re.sub(r"^(?:\$\{[^}]+\}|https?://[^/]+)+", "", s).split("?")[0].strip()
    if not s.startswith("/"):
        s = "/" + s
    eff_method = None if (method is None or method.upper() in ("ANY", "*")) else method.upper()
    return s, eff_method


def find_url_callers(brain: "ProjectBrain", url_path: str, method: str | None = None) -> list[dict]:
    callers: list[dict] = []
    seen_locs: set[tuple[str, str]] = set()
    clean_url = url_path.rstrip("/") or "/"
    url_segs = [seg for seg in clean_url.split("/") if seg and not seg.startswith((":", "{", "["))]
    tail_2 = ("/" + "/".join(url_segs[-2:])).lower() if len(url_segs) >= 2 else ""
    last_seg = url_segs[-1].lower() if url_segs else ""
    norm_query = re.sub(r"(:\w+|\[\w+\]|\{\w+\}|\$\{[^}]+\})", "{*}", clean_url).rstrip("/") or "/"

    def _enclosing_fn_name(info: dict, line_no: int) -> str | None:
        best_fn: dict | None = None
        for fn in info.get("functions", []):
            ls, le = fn.get("line_start", 0), fn.get("line_end", 0)
            if ls <= line_no <= le:
                if best_fn is None or (le - ls) < (best_fn.get("line_end", 0) - best_fn.get("line_start", 0)):
                    best_fn = fn
        return best_fn["name"] if best_fn else None

    for rel_path, info in brain._index.items():
        routes = info.get("routes", [])
        if any(
            url_matches(r.get("full_path") or r["path"], clean_url) or url_matches(r["path"], clean_url)
            for r in routes
        ):
            continue

        matched_in_file = False
        # 1. Structured 6D `http_calls` (captures fetch/axios/template-literals/wrappers)
        for cell in info.get("cells_4d") or ():
            for hc in cell.get("http_calls") or ():
                hc_method = (hc.get("method") or "GET").upper()
                if method and hc_method not in ("ANY", "ALL", method):
                    continue
                hc_url = hc.get("url") or ""
                hc_norm = hc.get("norm_url") or ""
                is_hit = (
                    url_matches(hc_url, clean_url)
                    or hc_norm == norm_query
                    or (len(hc_norm) > 4 and hc_norm.count("/") >= 2 and norm_query.endswith(hc_norm))
                    or (len(norm_query) > 4 and norm_query.count("/") >= 2 and hc_norm.endswith(norm_query))
                )
                if is_hit:
                    ln = hc.get("line") or cell.get("line_start") or 1
                    fn_nm = _enclosing_fn_name(info, ln) or cell.get("X_scope", "").removeprefix("fn:") or Path(rel_path).stem
                    key = (rel_path, fn_nm)
                    if key not in seen_locs:
                        seen_locs.add(key)
                        callers.append({
                            "rel_path": rel_path,
                            "function": fn_nm,
                            "line": ln,
                            "detail": f"{fn_nm}() calls {hc_method} {hc_url}",
                        })
                        matched_in_file = True

        if matched_in_file or not last_seg or len(last_seg) < 3:
            continue

        # 2. Fast raw-source literal & `${baseUrl}/...` template scan (zero false negatives on custom clients)
        src_txt = brain._get_file_text(rel_path)
        if not src_txt or last_seg not in src_txt.lower():
            continue
        src_low = src_txt.lower()
        target_sub = clean_url.lower() if clean_url.lower() in src_low else (tail_2 if (tail_2 and tail_2 in src_low) else "")
        if not target_sub:
            continue
        for idx_ln, raw_ln in enumerate(src_txt.splitlines()):
            if target_sub in raw_ln.lower():
                ln_no = idx_ln + 1
                fn_nm = _enclosing_fn_name(info, ln_no) or Path(rel_path).stem
                key = (rel_path, fn_nm)
                if key not in seen_locs:
                    seen_locs.add(key)
                    verb_lbl = method or "HTTP"
                    callers.append({
                        "rel_path": rel_path,
                        "function": fn_nm,
                        "line": ln_no,
                        "detail": f"{fn_nm}() references '{target_sub}' ({verb_lbl} {clean_url})",
                    })
                break

    return callers


def find_route_handler(brain: "ProjectBrain", url_path: str, method: str | None = None) -> list[dict]:
    from .graph import CodeGraph

    CodeGraph.propagate_router_mounts(brain._index, brain._import_targets, brain._symbol_imports)
    clean_url = url_path.rstrip("/") or "/"
    exact_matches: list[dict] = []
    suffix_matches: list[dict] = []

    for rel_path, info in brain._index.items():
        for route in info.get("routes", []):
            r_method = (route.get("method") or "GET").upper()
            if method and r_method not in (method, "ANY", "ALL"):
                continue
            r_full = route.get("full_path") or route["path"]
            r_raw = route["path"]
            is_exact = url_matches(r_full, clean_url) or url_matches(r_raw, clean_url)
            is_suffix = False
            if not is_exact and r_raw.startswith("/") and len(r_raw) > 2:
                raw_norm = r_raw.rstrip("/")
                if raw_norm.count("/") >= 2 and clean_url.lower().endswith(raw_norm.lower()):
                    is_suffix = True
                elif raw_norm.count("/") == 1:
                    # Single-segment subroute `/assign-feed` inside `routers/quality.py` matching `/api/v1/quality/assign-feed`
                    parent_seg = clean_url.rstrip("/").rsplit("/", 2)
                    stem_low = Path(rel_path).stem.lower()
                    if (
                        len(parent_seg) >= 3
                        and clean_url.lower().endswith(raw_norm.lower())
                        and parent_seg[-2].lower().rstrip("s") in stem_low
                    ):
                        is_suffix = True
            if not (is_exact or is_suffix):
                continue

            handler = route.get("handler")
            if not handler and brain._graph:
                for cand_rid in (route_id(r_method, r_full), route_id(r_method, r_raw)):
                    if cand_rid in brain._graph.nodes:
                        for ei in brain._graph._adj.get(cand_rid, ()):
                            edge = brain._graph.edges[ei]
                            if edge["type"] == "handles":
                                dst_node = brain._graph.nodes.get(edge["dst"], {})
                                handler = dst_node.get("name")
                                break
                        if not handler:
                            for ei in brain._graph._radj.get(cand_rid, ()):
                                edge = brain._graph.edges[ei]
                                if edge["type"] == "handles":
                                    src_node = brain._graph.nodes.get(edge["src"], {})
                                    handler = src_node.get("name")
                                    break
                    if handler:
                        break

            item = {
                "rel_path": rel_path,
                "method": r_method,
                "route_path": r_full,
                "handler": handler,
                "line": route.get("line"),
            }
            if is_exact:
                exact_matches.append(item)
            else:
                suffix_matches.append(item)

    return exact_matches if exact_matches else suffix_matches


def trace_flow(brain: "ProjectBrain", url_path: str, method: str | None = None) -> dict:
    brain._ensure_fresh()
    clean_url, eff_method = _normalize_url_query(url_path, method)
    display_verb = eff_method or "ANY"
    steps: list[dict] = []

    route_matches = find_route_handler(brain, clean_url, eff_method)
    matched_verb = route_matches[0]["method"] if (len(route_matches) == 1 and eff_method is None) else eff_method

    ui_triggers = find_url_callers(brain, clean_url, matched_verb)
    for trigger in ui_triggers[:15]:
        steps.append({
            "step": len(steps) + 1,
            "type": "ui_trigger",
            "rel_path": trigger["rel_path"],
            "function": trigger.get("function"),
            "line": trigger.get("line"),
            "detail": trigger.get("detail", f"calls {display_verb} {clean_url}"),
        })

    for match in route_matches[:10]:
        r_verb = match.get("method") or display_verb
        steps.append({
            "step": len(steps) + 1,
            "type": "route",
            "rel_path": match["rel_path"],
            "handler": match.get("handler"),
            "line": match.get("line"),
            "detail": f"{r_verb} {match.get('route_path', clean_url)}",
        })

        handler_name = match.get("handler")
        if handler_name and brain._graph:
            handler_file = match["rel_path"]
            fn_node = fn_id(handler_file, handler_name)
            if fn_node in brain._graph.nodes:
                callees = [
                    brain._graph.edges[ei]
                    for ei in brain._graph._adj.get(fn_node, ())
                    if brain._graph.edges[ei]["type"] == "calls"
                ]
                for callee_edge in callees[:10]:
                    callee_node = brain._graph.nodes.get(callee_edge["dst"], {})
                    callee_file = callee_node.get("rel_path", "")
                    callee_name = callee_node.get("name", callee_edge["dst"])
                    behavior = ""
                    if callee_file:
                        behaviors = brain._get_file_behaviors(callee_file)
                        behavior = behaviors.get(callee_name, "")

                    step_type = "service"
                    if behavior in ("io_reader", "io_writer"):
                        step_type = "data"
                    elif behavior == "validator":
                        step_type = "validator"

                    steps.append({
                        "step": len(steps) + 1,
                        "type": step_type,
                        "rel_path": callee_file,
                        "function": callee_name,
                        "signature": callee_node.get("signature", ""),
                        "behavior": behavior,
                        "confidence": callee_edge.get("confidence", "candidate"),
                        "detail": f"{handler_name}() calls {callee_name}()",
                    })

    final_verb = matched_verb or display_verb
    if not steps:
        return {
            "url": f"{final_verb} {clean_url}",
            "steps": [],
            "message": (
                f"No flow found for {final_verb} {clean_url}. "
                "Ensure the route is indexed and URL format matches."
            ),
        }

    return {
        "url": f"{final_verb} {clean_url}",
        "steps": steps,
        "step_count": len(steps),
    }


def co_change_analysis(brain: "ProjectBrain", file_path: str, days: int = 90) -> dict:
    try:
        rel_path = str(Path(file_path).relative_to(brain.root)).replace("\\", "/")
    except ValueError:
        rel_path = file_path.replace("\\", "/")

    try:
        result = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--pretty=format:%H", "--", rel_path],
            capture_output=True,
            text=True,
            cwd=str(brain.root),
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {
                "file": rel_path,
                "co_changes": [],
                "message": f"No git history found for this file in the last {days} days.",
            }

        commit_hashes = result.stdout.strip().split("\n")
        co_change_counts: dict[str, int] = {}
        total_commits = len(commit_hashes)

        for commit in commit_hashes[:50]:
            files_result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit],
                capture_output=True,
                text=True,
                cwd=str(brain.root),
                timeout=10,
            )
            if files_result.returncode != 0:
                continue

            changed = files_result.stdout.strip().split("\n")
            for f in changed:
                f_norm = f.strip().replace("\\", "/")
                if f_norm and f_norm != rel_path:
                    co_change_counts[f_norm] = co_change_counts.get(f_norm, 0) + 1

        co_changes = []
        for f, count in sorted(co_change_counts.items(), key=lambda x: x[1], reverse=True)[:20]:
            co_changes.append({
                "rel_path": f,
                "co_change_count": count,
                "co_change_pct": round(count / total_commits * 100, 1),
                "indexed": f in brain._index,
            })

        return {
            "file": rel_path,
            "commits_analyzed": min(total_commits, 50),
            "days": days,
            "co_changes": co_changes,
        }

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {
            "file": rel_path,
            "co_changes": [],
            "message": "Git not available or timed out.",
        }


def load_neighborhood_for_check(brain: "ProjectBrain", candidate_files: list[str]) -> bool:
    """Hydrate only the connected 1-2 hop neighborhood from SQLite for O(k) `check(files_modified=...)`."""
    from . import resolve
    from .brain import PARSED_EXTS
    from .graph import CodeGraph, cls_id, fn_id
    from .parsers import parse_with_tree

    if (
        brain._index
        or not candidate_files
        or len(candidate_files) > 20
        or not brain._db_store.db_path.exists()
        or brain._cache_path.exists()
    ):
        return False

    focus_rels = {
        r
        for raw_f in candidate_files
        for r in (brain._normalize_rel(str(raw_f)),)
        if r
    }
    if not focus_rels:
        return False

    extra_called_names: set[str] = set()
    need_sql_schemas = False
    need_http_routes = False
    symbols_changed = False
    has_static_unedited_focus = False
    mutated_set: set[str] = set()

    for r in focus_rels:
        pre_inf = brain._pre_edit_index.get(r)
        pre_mtime: float | None = None
        if pre_inf is None:
            pre_hit = brain._db_store.get_file_fast(r)
            pre_inf = (pre_hit or {}).get("info") or {}
            pre_mtime = (pre_hit or {}).get("mtime")
        p = brain.root / r
        if not p.exists():
            if pre_inf:
                symbols_changed = True
            continue
        if p.suffix.lower() not in PARSED_EXTS:
            continue

        pending = brain._pending_parsed_files.get(r)
        if pending is not None:
            inf, _, src, _, _ = pending
            was_edited = True
        else:
            try:
                st = p.stat()
                if pre_inf and pre_mtime is not None and st.st_mtime == pre_mtime:
                    # Unmodified file on disk -> static clean-HEAD contract audit
                    inf = pre_inf
                    src = ""
                    was_edited = False
                else:
                    src = p.read_text(encoding="utf-8", errors="ignore")
                    inf, ts_tree = parse_with_tree(src, p.suffix.lower())
                    if pre_inf:
                        brain._pre_edit_index.setdefault(r, dict(pre_inf))
                    brain._source_cache[r] = src
                    brain._pending_parsed_files[r] = (inf, ts_tree, src, st.st_mtime, st.st_size)
                    was_edited = bool(
                        not pre_inf
                        or inf.get("interface_hash") != pre_inf.get("interface_hash")
                        or [x.get("ast_hash") for x in inf.get("functions", [])]
                        != [x.get("ast_hash") for x in pre_inf.get("functions", [])]
                        or [x.get("ast_hash") for x in inf.get("classes", [])]
                        != [x.get("ast_hash") for x in pre_inf.get("classes", [])]
                    )
            except OSError:
                continue

        if not was_edited:
            has_static_unedited_focus = True
        if src:
            src_up = src.upper()
            if "INSERT " in src_up or "SELECT " in src_up or "sqlite3" in src:
                need_sql_schemas = True
            if any(
                tok in src
                for tok in ("fetch(", "axios", "/api/", "router.", "@app.", "BaseModel", "SQLModel", "TypedDict")
            ):
                need_http_routes = True
        else:
            if pre_inf.get("sql_schemas") or "sqlite3" in (pre_inf.get("imports") or ()):
                need_sql_schemas = True
            if (
                pre_inf.get("routes")
                or any(fn.get("http_wrapper_spec") for fn in pre_inf.get("functions", []))
                or any(c.get("schema_spec") for c in pre_inf.get("classes", []))
                or any(c.get("http_calls") for c in pre_inf.get("cells_4d", []))
            ):
                need_http_routes = True

        if not pre_inf:
            symbols_changed = True
            continue

        pre_calls = {c for fn in pre_inf.get("functions", []) for c in (fn.get("calls") or ())}
        post_calls = {c for fn in inf.get("functions", []) for c in (fn.get("calls") or ())}
        pre_bases = {
            b_raw.split(".")[-1].split("[")[0].strip()
            for cls in pre_inf.get("classes", [])
            for b_raw in (cls.get("bases") or ())
            if b_raw.split(".")[-1].split("[")[0].strip()
        }
        post_bases = {
            b_raw.split(".")[-1].split("[")[0].strip()
            for cls in inf.get("classes", [])
            for b_raw in (cls.get("bases") or ())
            if b_raw.split(".")[-1].split("[")[0].strip()
        }
        extra_called_names.update((post_calls - pre_calls) | (post_bases - pre_bases))

        pre_fn_hashes = {
            (fn.get("parent_class"), fn["name"]): (fn.get("interface_hash"), fn.get("ast_hash"))
            for fn in pre_inf.get("functions", [])
        }
        post_fn_hashes = {
            (fn.get("parent_class"), fn["name"]): (fn.get("interface_hash"), fn.get("ast_hash"))
            for fn in inf.get("functions", [])
        }
        pre_cls_hashes = {
            c["name"]: (c.get("interface_hash"), c.get("ast_hash"))
            for c in pre_inf.get("classes", [])
        }
        post_cls_hashes = {
            c["name"]: (c.get("interface_hash"), c.get("ast_hash"))
            for c in inf.get("classes", [])
        }
        if (
            pre_fn_hashes.keys() != post_fn_hashes.keys()
            or pre_cls_hashes.keys() != post_cls_hashes.keys()
            or pre_bases != post_bases
            or set(pre_inf.get("exports") or ()) != set(inf.get("exports") or ())
            or set((pre_inf.get("import_bindings") or {}).keys()) != set((inf.get("import_bindings") or {}).keys())
        ):
            symbols_changed = True
        else:
            for (p_cls, fn_nm), h_pair in post_fn_hashes.items():
                if pre_fn_hashes.get((p_cls, fn_nm)) != h_pair:
                    mutated_set.add(fn_id(r, fn_nm, p_cls))
            for cls_nm, h_pair in post_cls_hashes.items():
                if pre_cls_hashes.get(cls_nm) != h_pair:
                    mutated_set.add(cls_id(r, cls_nm))

    boundary_src_globs = []
    if brain._memory._boundary_rules:
        from .memory import _compile_path_glob
        for rule in brain._memory._boundary_rules:
            if rule.get("src_glob") and rule.get("dst_glob"):
                dst_rx = _compile_path_glob(rule["dst_glob"])
                if any(dst_rx.match(f) for f in focus_rels):
                    boundary_src_globs.append(_compile_path_glob(rule["src_glob"]))

    mutated_arg = None if (symbols_changed or has_static_unedited_focus) else mutated_set
    data = brain._db_store.load_check_neighborhood(
        focus_rels,
        extra_called_names=extra_called_names,
        boundary_src_globs=boundary_src_globs,
        need_sql_schemas=need_sql_schemas,
        need_http_routes=need_http_routes,
        symbols_changed=symbols_changed,
        mutated_node_ids=mutated_arg,
    )
    if data is None or not brain._is_compatible_cache(data):
        return False

    brain._is_partial_index = True
    brain._blobs_ready = False
    brain._all_indexed_files = set(data["all_indexed_files"])
    brain._index = {
        rel: {**entry.get("info", {}), "mtime": entry.get("mtime")}
        for rel, entry in data.get("files", {}).items()
    }
    brain._sizes = {
        rel: entry.get("size") for rel, entry in data.get("files", {}).items()
    }
    brain._skipped = data.get("skipped", {})

    brain._doc_freq = {}
    brain._blobs = {}
    brain._token_sets = {}
    brain._import_token_sets = {}
    brain._body_token_sets = {}
    brain._defn_sets = {}
    brain._ast_hashes = {}
    brain._z_write_sets = {}
    brain._z_read_sets = {}
    brain._trans_effect_sets = {}
    brain._cell_4d_tokens = {}
    brain._const_aliases = {}
    brain._idf = {}
    brain._behaviors = {}
    brain._import_targets = {}
    brain._symbol_imports = {}
    brain._imported_by = {}
    brain._python_module_index = resolve.build_python_module_index(brain._all_indexed_files)

    from .store import unpack_symbol_imports

    fimp_sub: dict[str, list[str]] | None = data.get("file_imports_subset")
    sym_imps_all = unpack_symbol_imports(data["symbol_imports_b85"]) if "symbol_imports_b85" in data else None

    for rel_path, info in brain._index.items():
        for f in info.get("functions", []):
            nid = fn_id(rel_path, f["name"], f.get("parent_class"))
            if f.get("ast_hash"):
                brain._ast_hashes[nid] = f["ast_hash"]
        for c in info.get("classes", []):
            nid = cls_id(rel_path, c["name"])
            if c.get("ast_hash"):
                brain._ast_hashes[nid] = c["ast_hash"]
        if rel_path in focus_rels:
            for tok in brain._compute_file_blob(rel_path, info):
                brain._doc_freq[tok] = brain._doc_freq.get(tok, 0) + 1
        targets = info.get("_resolved_targets")
        sym_map = info.get("_resolved_sym_map")
        if (targets is None or sym_map is None) and rel_path not in focus_rels and fimp_sub is not None and sym_imps_all is not None:
            targets = list(fimp_sub.get(rel_path, ()))
            sym_map = dict(sym_imps_all.get(rel_path, {}))
            info["_resolved_targets"] = targets
            info["_resolved_sym_map"] = sym_map
        elif targets is None or sym_map is None:
            targets, sym_map = resolve.resolve_file_imports_and_symbols(
                rel_path, info, brain._all_indexed_files, brain._python_module_index, index=brain._index,
            )
            info["_resolved_targets"] = targets
            info["_resolved_sym_map"] = sym_map
        brain._import_targets[rel_path] = targets
        brain._symbol_imports[rel_path] = sym_map
        for target in targets:
            brain._imported_by.setdefault(target, []).append(rel_path)

    # Merge full direct-importer lists from `file_imports` so `impact()` consumer counts stay 100% exact
    # even when non-calling hub importers were skipped during symbol-targeted hydration.
    for fr, full_imps in (data.get("direct_importers_by_focus") or {}).items():
        cur_imps = brain._imported_by.setdefault(fr, [])
        seen_imps = set(cur_imps)
        for imp_r in full_imps:
            if imp_r not in seen_imps:
                seen_imps.add(imp_r)
                cur_imps.append(imp_r)

    brain._importer_counts = {
        rel_path: len(brain._imported_by.get(rel_path, []))
        for rel_path in brain._index
    }
    brain._graph = CodeGraph.from_json(data["graph_json"])
    return True


def run_check(
    brain: "ProjectBrain",
    files_modified: list[str] | None = None,
    claims: list[list[str]] | list[tuple[str, str, str]] | None = None,
    plan_files: list[str] | None = None,
    task: str = "",
    *,
    base_ref: str | None = None,
    allow_governance_change: bool = False,
    audit: bool = False,
    scope_glob: str | None = None,
) -> dict:
    """Unified verification gate supporting working-tree edits AND committed PR merge-base diffs."""
    from .git_diff import (
        check_governance_tamper,
        compute_git_diff,
        find_git_context,
        load_parsed_snapshots_at_ref,
        mine_ad_hoc_co_change_drift,
        resolve_git_base,
        verify_deleted_or_renamed_file_consumers,
    )
    from .hooks import _file_diag_lines, _is_auditable_code_path, compute_interface_diff
    from .memory import extract_note_rel_path

    brain._memory._ensure_fresh()
    should_resolve_base = (base_ref is not None) or (not files_modified and not claims and not plan_files)
    base_info = (
        resolve_git_base(brain.root, explicit_base=base_ref)
        if should_resolve_base
        else {
            "active": False,
            "valid": True,
            "requested_ref": None,
            "resolved_ref": None,
            "merge_base": None,
            "source": None,
            "env_key": None,
            "error": None,
        }
    )
    if base_info.get("active") and not base_info.get("valid", True):
        err_msg = base_info.get("error") or f"[GIT BASE ERROR] Invalid base ref '{base_ref}'."
        return {
            "all_contracts_valid": False,
            "verdict": "INVALID_BASE_REF",
            "contract_diagnostics": [err_msg],
            "base_ref_info": base_info,
        }

    merge_base: str | None = base_info.get("merge_base") if base_info.get("active") else None
    git_diff: dict | None = None
    deleted_or_moved_paths: list[str] = []
    renamed_from_old: dict[str, str] = {}
    pinned_note_rels = {
        p
        for m in getattr(brain._memory, "_notes", [])
        for p in (extract_note_rel_path(m),)
        if p
    }

    saved_pre_edit: dict[str, dict] | None = None
    if merge_base:
        saved_pre_edit = {k: dict(v) for k, v in brain._pre_edit_index.items()}

    try:
        if merge_base:
            git_diff = compute_git_diff(brain.root, ref_commit=merge_base, include_untracked=True)
            deleted_or_moved_paths = [
                r for r in git_diff.get("deleted_or_moved_paths", [])
                if _is_auditable_code_path(r) or r in pinned_note_rels
            ]
            renamed_from_old = dict(git_diff.get("renamed_from_old", {}))
            target_active = (
                [brain._normalize_rel(str(f)) for f in files_modified]
                if files_modified
                else [r for r in git_diff.get("active_changed_files", []) if _is_auditable_code_path(r)]
            )
            pre_base_infos, _ = load_parsed_snapshots_at_ref(
                brain.root,
                merge_base,
                target_active,
                git_diff.get("renamed_files", {}),
                deleted_or_moved_paths,
            )
            brain._pre_edit_index = {r: dict(inf) for r, inf in pre_base_infos.items()}
        else:
            brain._capture_pre_edit_snapshots(files_modified)
            if (
                not audit
                and not files_modified
                and not claims
                and not plan_files
                and not brain._pre_edit_index
                and find_git_context(brain.root)[0] is not None
            ):
                git_diff = compute_git_diff(brain.root, ref_commit="HEAD", include_untracked=True)
                deleted_or_moved_paths = [
                    r for r in git_diff.get("deleted_or_moved_paths", [])
                    if _is_auditable_code_path(r) or r in pinned_note_rels
                ]
                renamed_from_old = dict(git_diff.get("renamed_from_old", {}))
                active_head = [r for r in git_diff.get("active_changed_files", []) if _is_auditable_code_path(r)]
                if active_head or deleted_or_moved_paths:
                    saved_pre_edit = {}
                    pre_head_infos, _ = load_parsed_snapshots_at_ref(
                        brain.root,
                        "HEAD",
                        active_head,
                        git_diff.get("renamed_files", {}),
                        deleted_or_moved_paths,
                    )
                    for r, inf in pre_head_infos.items():
                        brain._pre_edit_index.setdefault(r, dict(inf))
                        brain._git_head_cache[r] = inf
                    for r_miss in active_head:
                        if r_miss not in pre_head_infos:
                            brain._git_head_cache.setdefault(r_miss, None)

        if files_modified:
            effective_modified = list(files_modified)
        elif not claims and not plan_files:
            if git_diff is not None:
                effective_modified = [
                    r for r in git_diff.get("active_changed_files", [])
                    if _is_auditable_code_path(r)
                ]
            elif brain._pre_edit_index:
                effective_modified = list(brain._pre_edit_index.keys())
            else:
                effective_modified = []
        else:
            effective_modified = []

        fast_check_scope = bool(
            not audit
            and effective_modified
            and not deleted_or_moved_paths
            and not claims
            and not plan_files
        )
        if fast_check_scope:
            if not brain._index:
                if not brain._load_neighborhood_for_check(effective_modified):
                    brain.load()
            if not brain._index:
                brain.index()
            else:
                brain.incremental_index(candidate_files=effective_modified)
                for r_cand in effective_modified:
                    brain._pending_parsed_files.pop(brain._normalize_rel(str(r_cand)), None)
        else:
            brain._ensure_fresh()

        out: dict = {}
        if base_info.get("active"):
            out["base_ref_info"] = base_info
        if git_diff is not None and (base_info.get("active") or git_diff.get("all_touched_paths")):
            filtered_diff = {
                **git_diff,
                "deleted_files": [
                    r for r in git_diff.get("deleted_files", [])
                    if _is_auditable_code_path(r) or r in pinned_note_rels
                ],
                "renamed_files": {
                    nf: of
                    for nf, of in (git_diff.get("renamed_files") or {}).items()
                    if _is_auditable_code_path(nf) or _is_auditable_code_path(of)
                },
            }
            out["git_diff"] = filtered_diff

        if claims:
            tuples = [(c[0], c[1], c[2]) for c in claims if len(c) >= 3]
            out["claim_verification"] = brain.verify_batch(tuples)

        if plan_files:
            out["plan_validation"] = brain.verify_plan(task or "planned edit", plan_files)

        all_indexed_set = (
            set(brain._all_indexed_files)
            if brain._all_indexed_files is not None
            else set(brain._index.keys())
        )
        has_diff_scope = bool(effective_modified or deleted_or_moved_paths)

        if has_diff_scope:
            norm_focus = {brain._normalize_rel(str(f)) for f in effective_modified if f}
            valid_focus = {r for r in norm_focus if r in brain._index}
            if effective_modified:
                imp_res = brain.impact(effective_modified, _already_fresh=True)
                diags = list(imp_res.get("contract_diagnostics", []))
            else:
                imp_res = {
                    "files_analyzed": 0,
                    "total_impact_zone": 0,
                    "overall_risk": "low",
                    "verdict": "SAFE_TO_PROCEED",
                    "all_contracts_valid": True,
                    "contract_diagnostics": [],
                    "details": [],
                    "all_impacted_files": [],
                }
                diags = []

            syntax_errs: list[str] = []
            files_with_syntax_err: set[str] = set()
            for r in sorted(valid_focus):
                if brain._index.get(r, {}).get("parse_status") == "partial":
                    s_lines = _file_diag_lines(brain.root, r)
                    if s_lines:
                        files_with_syntax_err.add(r)
                    for msg in s_lines:
                        syntax_errs.append(msg)
                        if msg not in diags:
                            diags.insert(0, msg)

            b_violations = (
                brain._memory.check_boundary_violations(
                    brain._index, brain._import_targets, brain._graph, focus_rels=valid_focus,
                )
                if valid_focus
                else []
            )
            for bv in b_violations:
                if bv["message"] not in diags:
                    diags.append(bv["message"])

            del_set = set(deleted_or_moved_paths)
            if del_set:
                del_diags, del_records = verify_deleted_or_renamed_file_consumers(
                    brain, del_set, renamed_from_old=renamed_from_old,
                )
                for dd in del_diags:
                    if dd not in diags:
                        diags.append(dd)
                if del_records:
                    out["deleted_file_violations"] = del_records

            from .reporters import _clean_uri, _norm_line_key

            details_by_rel = {
                d["rel_path"]: d for d in imp_res.get("details", []) if d.get("rel_path")
            }

            def _dedup_blast_callers(items: list[dict], limit: int) -> list[dict]:
                deduped: list[dict] = []
                seen_locs: set[tuple[str, int | str]] = set()
                for c in items:
                    cf = _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="")
                    if not cf:
                        continue
                    ln_key = _norm_line_key(c.get("line"))
                    loc_key = (cf, ln_key)
                    if loc_key not in seen_locs:
                        seen_locs.add(loc_key)
                        deduped.append({**c, "caller_file": cf})
                        if len(deduped) >= limit:
                            break
                return deduped

            signature_blast: list[dict] = []
            import time as _time
            blast_deadline = _time.monotonic() + 3.5
            for r in sorted(valid_focus):
                if r in files_with_syntax_err:
                    continue
                post_inf = brain._index.get(r)
                pre_inf = brain._pre_edit_index.get(r)
                if pre_inf is None and _time.monotonic() < blast_deadline:
                    pre_inf = brain._get_git_head_info(r)
                if not pre_inf or not post_inf:
                    continue
                idiff = compute_interface_diff(pre_inf, post_inf)
                mutated = idiff["changed_signatures"] + idiff["removed_symbols"]
                if not mutated:
                    continue
                det = details_by_rel.get(r, {})
                direct_mut_callers: list[dict] = []
                if brain._graph:
                    for m in mutated:
                        sym = m.get("symbol") or ""
                        if not sym or m.get("kind") != "function":
                            continue
                        p_cls, bare_nm = sym.split(".", 1) if "." in sym else (None, sym)
                        m_nid = fn_id(r, bare_nm, p_cls)
                        for ei in brain._graph._radj.get(m_nid, ()):
                            edge = brain._graph.edges[ei]
                            if edge["type"] != "calls":
                                continue
                            s_nd = brain._graph.nodes.get(edge["src"], {})
                            cf_norm = _clean_uri(s_nd.get("rel_path") or "", fallback="")
                            if cf_norm and cf_norm not in valid_focus:
                                direct_mut_callers.append({
                                    "caller_file": cf_norm,
                                    "caller": edge["src"],
                                    "callee": bare_nm,
                                    "line": edge.get("line"),
                                })
                ext_callers = [
                    {
                        "caller_file": _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback=""),
                        "caller": c.get("caller") or c.get("called_by") or "",
                        "callee": c.get("callee") or c.get("function") or "",
                        "line": c.get("line"),
                    }
                    for c in det.get("called_by", [])
                    if _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="")
                    and _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="") not in valid_focus
                ]
                ext_importers = [
                    imp_f for imp_f in det.get("direct_consumers", [])
                    if imp_f not in valid_focus
                ]
                if direct_mut_callers or ext_callers or ext_importers:
                    mutated_names = {m["symbol"].split(".")[-1] for m in mutated} | {m["symbol"] for m in mutated}
                    matched_callers = direct_mut_callers + [
                        c for c in ext_callers
                        if c.get("callee") in mutated_names or c.get("callee", "").split(".")[-1] in mutated_names
                    ]
                    relevant_callers = (
                        _dedup_blast_callers(matched_callers, limit=8)
                        if matched_callers
                        else _dedup_blast_callers(ext_callers, limit=6)
                    )
                    signature_blast.append({
                        "file": r,
                        "mutations": mutated,
                        "callers": relevant_callers,
                        "consumers": ext_importers[:8],
                        "tests": det.get("test_consumers", [])[:6],
                    })

            focus_node_ids: set[str] = set()
            if brain._graph and valid_focus:
                brain._graph._ensure_registries()
                for r in valid_focus:
                    focus_node_ids.update(brain._graph._nodes_by_file.get(r, set()))
            matched_memories = brain._memory.get_notes_for(
                valid_focus | del_set,
                focus_node_ids,
                brain._ast_hashes,
                deleted_rels=del_set,
                indexed_files=all_indexed_set,
            )
            stale_memories = [m for m in matched_memories if m.get("stale")]

            all_changed_for_cochange = {
                r for r in (
                    valid_focus
                    | (set(git_diff.get("all_touched_paths", [])) if git_diff else set())
                    | del_set
                    | norm_focus
                )
                if r and not Path(r).name.startswith(".projectbrain")
            }
            co_warnings = brain._memory.check_co_change_drift(
                all_changed_for_cochange,
                existing_files=all_indexed_set,
            )
            if merge_base and all_changed_for_cochange:
                covered_pairs = {
                    (cw["trigger_file"], cw["missing_partner"]) for cw in co_warnings
                }
                co_warnings.extend(
                    mine_ad_hoc_co_change_drift(
                        brain.root,
                        merge_base,
                        all_changed_for_cochange,
                        existing_files=all_indexed_set,
                        covered_pairs=covered_pairs,
                    )
                )

            all_valid = len(diags) == 0
            imp_res["contract_diagnostics"] = diags
            imp_res["all_contracts_valid"] = all_valid
            if not all_valid:
                imp_res["verdict"] = "UNSAFE_CONTRACT_VIOLATION"

            out["impact_analysis"] = imp_res
            out["contract_diagnostics"] = diags
            out["boundary_violations"] = b_violations
            if imp_res.get("semantic_warnings"):
                out["semantic_warnings"] = imp_res["semantic_warnings"]
            if syntax_errs:
                out["syntax_errors"] = syntax_errs
            if signature_blast:
                out["signature_blast"] = signature_blast
            if matched_memories:
                out["memories"] = matched_memories
            if stale_memories:
                out["stale_memories"] = stale_memories
            if co_warnings:
                out["co_change_warnings"] = co_warnings
            out["all_contracts_valid"] = all_valid
            out["verdict"] = imp_res.get("verdict", "SAFE_TO_PROCEED")
        elif not claims and not plan_files:
            b_violations = brain._memory.check_boundary_violations(
                brain._index, brain._import_targets, brain._graph, focus_rels=None,
            )
            if b_violations:
                out["boundary_violations"] = b_violations
                out["contract_diagnostics"] = [bv["message"] for bv in b_violations]
                out["all_contracts_valid"] = False
                out["verdict"] = "UNSAFE_CONTRACT_VIOLATION"
            else:
                out["all_contracts_valid"] = True
                out["verdict"] = "SAFE_TO_PROCEED"
            stale_all = [
                m for m in brain._memory.list_all(brain._ast_hashes, indexed_files=all_indexed_set)
                if m.get("stale")
            ]
            if stale_all:
                out["stale_memories"] = stale_all
            if git_diff and git_diff.get("all_touched_paths"):
                touched_non_code = {
                    r for r in git_diff["all_touched_paths"]
                    if r and not Path(r).name.startswith(".projectbrain")
                }
                co_w = brain._memory.check_co_change_drift(touched_non_code, existing_files=all_indexed_set)
                if co_w:
                    out["co_change_warnings"] = co_w
            # Fast whole-repo static bug audit on small/medium clean checkouts (<160 files)
            if not audit and 0 < len(brain._index) <= 160:
                from .semantic_contracts import run_semantic_repo_audit

                audit_auto = run_semantic_repo_audit(brain)
                if audit_auto["combined_diagnostics"]:
                    out["static_audit_findings"] = audit_auto["combined_diagnostics"][:25]
                if audit_auto["ranked_hotspots"]:
                    out["ranked_hotspots"] = audit_auto["ranked_hotspots"][:15]

        if audit:
            from .semantic_contracts import run_semantic_repo_audit

            audit_res = run_semantic_repo_audit(brain, scope_glob=scope_glob)
            out["audit_coverage"] = audit_res["audit_coverage"]
            out["ranked_hotspots"] = audit_res["ranked_hotspots"]
            if audit_res["combined_diagnostics"]:
                out["static_audit_findings"] = audit_res["combined_diagnostics"][:25]
            if audit_res["error_diagnostics"]:
                merged_err = list(
                    dict.fromkeys([*(out.get("contract_diagnostics") or ()), *audit_res["error_diagnostics"]])
                )
                out["contract_diagnostics"] = merged_err
                out["all_contracts_valid"] = False
                if out.get("verdict") in (None, "SAFE_TO_PROCEED", "HIGH_IMPACT_REVIEW_CALLERS"):
                    out["verdict"] = "UNSAFE_CONTRACT_VIOLATION"
            elif "verdict" not in out:
                out["all_contracts_valid"] = True
                out["verdict"] = "SAFE_TO_PROCEED"

        should_check_governance = (
            bool(merge_base)
            or (not files_modified and not claims and not plan_files)
            or any(Path(str(f)).name == ".projectbrain_memory.json" for f in (files_modified or ()))
        )
        if should_check_governance and find_git_context(brain.root)[0] is not None:
            gov_ref = merge_base or "HEAD"
            tampers = check_governance_tamper(brain.root, ref_commit=gov_ref)
            if tampers:
                out["governance_tamper"] = tampers
                if allow_governance_change:
                    out["governance_tamper_allowed"] = True
                else:
                    out["governance_tamper_blocked"] = True
                    cur_diags = list(out.get("contract_diagnostics", []))
                    for tp in tampers:
                        if tp["message"] not in cur_diags:
                            cur_diags.append(tp["message"])
                    out["contract_diagnostics"] = cur_diags
                    if "impact_analysis" in out and isinstance(out["impact_analysis"], dict):
                        out["impact_analysis"]["contract_diagnostics"] = cur_diags
                        out["impact_analysis"]["all_contracts_valid"] = False
                        out["impact_analysis"]["verdict"] = "UNSAFE_GOVERNANCE_TAMPER"
                    out["all_contracts_valid"] = False
                    out["verdict"] = "UNSAFE_GOVERNANCE_TAMPER"

        if not files_modified and not claims and not plan_files:
            out["health"] = brain.health(_already_fresh=True)

        return out
    finally:
        if saved_pre_edit is not None:
            brain._pre_edit_index = saved_pre_edit



