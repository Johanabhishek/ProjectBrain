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

        importers = brain._imported_by.get(rel_path, [])
        for imp in importers:
            if imp in plan_set:
                continue

            imp_lower = imp.lower()
            is_test = any(seg in imp_lower for seg in ("test", "spec", "__tests__", "tests/"))
            if is_test:
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
                            callers.append({
                                "function": fn_name,
                                "signature": fn.get("signature", ""),
                                "called_by": edge["src"],
                                "in_file": src_node.get("rel_path", ""),
                                "line": edge.get("line"),
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
            imp_lower = imp.lower()
            if any(seg in imp_lower for seg in ("test", "spec", "__tests__")):
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
            entry["called_by"] = callers[:20]
        if behavior_summary:
            entry["behaviors"] = behavior_summary
        routes = info.get("routes", [])
        if routes:
            entry["routes"] = routes

        results.append(entry)

    contract_diags: list[str] = []
    if valid_focus_rels:
        diag_6d, _ = brain._compile_6d_contract_diagnostics(
            "impact verification", valid_focus_rels, strict_focus=True
        )
        contract_diags.extend(diag_6d)
        for cd in verify_cross_file_contracts(brain, valid_focus_rels):
            if cd not in contract_diags:
                contract_diags.append(cd)
        if contract_diags and _RISK_ORDER.get(max_risk, 0) < _RISK_ORDER["high"]:
            max_risk = "high"

    verdict = (
        "UNSAFE_CONTRACT_VIOLATION"
        if contract_diags
        else ("HIGH_IMPACT_REVIEW_CALLERS" if max_risk in ("high", "critical") else "SAFE_TO_PROCEED")
    )

    return {
        "files_analyzed": len(results),
        "total_impact_zone": len(all_impacted),
        "overall_risk": max_risk,
        "verdict": verdict,
        "all_contracts_valid": len(contract_diags) == 0,
        "contract_diagnostics": contract_diags,
        "details": results,
        "all_impacted_files": sorted(all_impacted),
    }


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


def find_url_callers(brain: "ProjectBrain", url_path: str, method: str) -> list[dict]:
    if hasattr(brain, "_ensure_blobs"):
        brain._ensure_blobs()
    callers = []
    url_lower = url_path.lower()

    for rel_path, blob in brain._blobs.items():
        if url_lower not in blob.lower():
            continue

        info = brain._index.get(rel_path, {})
        routes = info.get("routes", [])
        is_route_definer = any(url_matches(r["path"], url_path) for r in routes)
        if is_route_definer:
            continue

        for fn in info.get("functions", []):
            calls = fn.get("calls", [])
            qualified = fn.get("qualified_calls", {})
            http_callers = {"fetch", "post", "get", "put", "delete", "patch", "request", "ajax"}
            if any(c.lower() in http_callers for c in calls):
                callers.append({
                    "rel_path": rel_path,
                    "function": fn["name"],
                    "line": fn.get("line_start"),
                    "detail": f"{fn['name']}() calls {method} {url_path}",
                })
                break
            for qc in qualified:
                qc_lower = qc.lower()
                if any(h in qc_lower for h in ("fetch", "axios", "requests", "http")):
                    callers.append({
                        "rel_path": rel_path,
                        "function": fn["name"],
                        "line": fn.get("line_start"),
                        "detail": f"{fn['name']}() calls {qc}('{url_path}')",
                    })
                    break

    return callers


def find_route_handler(brain: "ProjectBrain", url_path: str, method: str) -> list[dict]:
    matches = []
    for rel_path, info in brain._index.items():
        for route in info.get("routes", []):
            if route["method"].upper() != method:
                continue
            if not url_matches(route["path"], url_path):
                continue

            handler = route.get("handler")
            if not handler:
                route_node = route_id(method, route["path"])
                if brain._graph and route_node in brain._graph.nodes:
                    for edge in brain._graph.edges:
                        if edge["dst"] == route_node and edge["type"] == "handles":
                            src_node = brain._graph.nodes.get(edge["src"], {})
                            handler = src_node.get("name")
                            break

            matches.append({
                "rel_path": rel_path,
                "route_path": route["path"],
                "handler": handler,
                "line": route.get("line"),
            })

    return matches


def trace_flow(brain: "ProjectBrain", url_path: str, method: str = "GET") -> dict:
    brain._ensure_fresh()
    method = method.upper()
    steps: list[dict] = []

    ui_triggers = find_url_callers(brain, url_path, method)
    for trigger in ui_triggers:
        steps.append({
            "step": len(steps) + 1,
            "type": "ui_trigger",
            "rel_path": trigger["rel_path"],
            "line": trigger.get("line"),
            "detail": trigger.get("detail", f"calls {method} {url_path}"),
        })

    route_matches = find_route_handler(brain, url_path, method)
    for match in route_matches:
        steps.append({
            "step": len(steps) + 1,
            "type": "route",
            "rel_path": match["rel_path"],
            "handler": match.get("handler"),
            "line": match.get("line"),
            "detail": f"{method} {match.get('route_path', url_path)}",
        })

        handler_name = match.get("handler")
        if handler_name and brain._graph:
            handler_file = match["rel_path"]
            fn_node = fn_id(handler_file, handler_name)
            if fn_node in brain._graph.nodes:
                callees = [
                    e for e in brain._graph.edges
                    if e["src"] == fn_node and e["type"] == "calls"
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

    if not steps:
        return {
            "url": f"{method} {url_path}",
            "steps": [],
            "message": (
                f"No flow found for {method} {url_path}. "
                "Ensure the route is indexed and URL format matches."
            ),
        }

    return {
        "url": f"{method} {url_path}",
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
        elif pre_inf.get("sql_schemas") or "sqlite3" in (pre_inf.get("imports") or ()):
            need_sql_schemas = True

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


