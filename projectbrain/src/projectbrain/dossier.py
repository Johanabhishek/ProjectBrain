"""Single-Turn Engineering Dossier & Evidence Packet Compiler for ProjectBrain.

Provides:
  1. `compile_evidence_packet` (`ProjectBrain.compile_context`): Token-budgeted JSON
     evidence packet with surgical AST target slices, signature-only neighbor skeletons,
     and route flow traces.
  2. `compile_engineering_dossier` (`ProjectBrain.compile_dossier`): Self-contained,
     zero-follow-up Markdown engineering dossier with module globals, bidirectional
     call chains, 6D contract diagnostics, multiplex PPR + causal symptom chop, and
     submodular marginal information gain selection.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import (
    extract_4d_coordinate_matrix,
    query_4d_coordinate_matrix,
)
from .graph import cls_id, file_id, fn_id
from .intent import ADD_FEATURE, LOCATE_ROUTE, classify_intent
from .runner import _extract_stack_trace_frames
from .slicer import (
    build_file_skeleton,
    extract_emitted_intervals,
    extract_file_globals_slice,
    slice_lines,
)

if TYPE_CHECKING:
    from .brain import ProjectBrain

_PURE_WORD_RE = re.compile(r"^\w+$")
_WORD_EXTRACT_RE = re.compile(r"\w+")
_DOTTED_CHAIN_RE = re.compile(r"\w+(?:\.\w+)+")
_TASK_PATH_RE = re.compile(r"\b([a-zA-Z0-9_.\-]+(?:/[a-zA-Z0-9_.\-]+)+\.[a-zA-Z0-9]+)\b")
_DATA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b|`([A-Za-z_][A-Za-z0-9_]{3,})`")


def score_symbol_for_query(
    brain: "ProjectBrain",
    fn: dict,
    keywords: list[str],
    behaviors: dict[str, str],
    routes: list[dict],
    cells_4d: list[dict] | None = None,
    local_idf: dict[str, float] | None = None,
) -> float:
    from .brain import _TEST_QUERY_TERMS, _tokenize_symbol

    name_lower = fn.get("name", "").lower()
    name_tokens = set(_tokenize_symbol(fn.get("name", ""))) | {name_lower}
    parent_cls = (fn.get("parent_class") or "").lower()
    parent_tokens = set(_tokenize_symbol(parent_cls)) if parent_cls else set()
    is_lifecycle_method = bool(
        name_tokens
        & {
            "init", "initialize", "setup", "push", "pop", "open", "close",
            "dispatch", "handle", "send", "invoke", "execute", "run",
            "process", "consume", "resolve", "build", "register", "record",
            "load", "save", "convert", "validate", "parse", "merge", "extract",
        }
    )
    doc_lower = (fn.get("docstring") or "").lower()
    calls_lower = {c.lower() for c in fn.get("calls", [])}
    body_tokens = set(fn.get("body_tokens", []))
    fn_s = fn.get("line_start", 1)
    fn_e = fn.get("line_end", fn_s)
    fn_span = max(1, fn_e - fn_s)
    score = 0.0
    matched_kws = 0
    id_or_call_hits = 0
    for kw in keywords:
        idf = brain._idf.get(kw, 1.0) * (local_idf.get(kw, 1.0) if local_idf else 1.0)
        kw_stem = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
        if len(kw_stem) >= 4 and kw_stem[-1] == kw_stem[-2] and kw_stem[-1] in "pbgtdmnr":
            kw_stem = kw_stem[:-1]
        if len(kw_stem) < 3:
            kw_stem = kw
        kw_e = (kw_stem + "e") if (len(kw_stem) >= 4 and kw.endswith(("ed", "ing")) and not kw_stem.endswith("e")) else ""
        kw_root = re.sub(r"(?:ers|ors|er|or)$", "e", kw) if kw.endswith(("handler", "handlers", "serializer", "serializers", "dispatcher", "dispatchers", "loader", "loaders", "parser", "parsers", "manager", "managers", "router", "routers", "producer", "producers", "consumer", "consumers")) else re.sub(r"(?:ers|ors|er|or)$", "", kw_stem)
        if len(kw_root) < 4:
            kw_root = kw_stem
        if kw in ("handled", "handling", "work", "works", "working", "across"):
            idf *= 0.22
        is_generic_kw = kw in ("file", "files", "path", "paths", "data", "item", "items", "node", "nodes")
        name_weight = 0.8 if is_generic_kw else 2.6
        sig_tokens = set(re.findall(r"[a-z0-9]+", (fn.get("signature") or "").lower()))
        hit = False
        if kw == name_lower or kw_stem == name_lower or (kw_e and kw_e == name_lower):
            score += 4.2 * idf
            hit = True
            if not is_generic_kw:
                id_or_call_hits += 1
        elif (
            kw in name_tokens
            or kw in name_lower
            or (len(kw_stem) >= 3 and (kw_stem in name_tokens or kw_stem in name_lower))
            or (kw_e and (kw_e in name_tokens or kw_e in name_lower))
            or (len(kw_root) >= 4 and (kw_root in name_tokens or kw_root in name_lower))
            or (len(kw_stem) >= 6 and any(len(nt) >= 5 and kw_stem.startswith(nt) for nt in name_tokens))
        ):
            score += name_weight * idf
            hit = True
            if not is_generic_kw:
                id_or_call_hits += 1
            if kw in doc_lower or (len(kw_stem) >= 4 and kw_stem in doc_lower):
                score += 0.75 * idf
        elif (
            parent_tokens
            and not name_lower.startswith("_")
            and (
                kw in parent_tokens
                or kw == parent_cls
                or (len(kw_stem) >= 3 and (kw_stem in parent_tokens or kw_stem in parent_cls))
                or (len(kw_stem) >= 6 and any(len(pt) >= 4 and kw_stem.startswith(pt) for pt in parent_tokens))
            )
        ):
            score += (2.4 if is_lifecycle_method else 1.35) * idf
            hit = True
            if not is_generic_kw:
                id_or_call_hits += 1
        elif kw in sig_tokens or (len(kw_stem) >= 3 and kw_stem in sig_tokens) or (kw_e and kw_e in sig_tokens):
            score += (0.6 if is_generic_kw else 2.0) * idf
            hit = True
            if not is_generic_kw:
                id_or_call_hits += 1
        elif any(
            kw in c
            or (len(kw_stem) >= 4 and kw_stem in c)
            or (kw_e and kw_e in c)
            or (len(kw_root) >= 4 and kw_root in c)
            for c in calls_lower
        ):
            score += (0.45 if is_generic_kw else 1.45) * idf
            hit = True
            if not is_generic_kw:
                id_or_call_hits += 1
        elif kw in doc_lower or (len(kw_stem) >= 4 and kw_stem in doc_lower) or (kw_e and kw_e in doc_lower):
            score += 1.15 * idf
            hit = True
        elif kw in body_tokens or any(len(kw_stem) >= 4 and kw_stem in bt for bt in body_tokens) or (kw_e and kw_e in body_tokens):
            score += 0.75 * idf
            hit = True
        if hit:
            matched_kws += 1
    if (
        name_lower.startswith(("has_", "is_", "can_"))
        and not any(qk.startswith(("has", "is", "can", "check")) for qk in keywords)
    ):
        score *= 0.52
    if fn.get("is_nested") and name_lower not in keywords:
        score *= 0.35
    if name_lower in ("__init__", "constructor") and not any(
        qk in ("init", "__init__", "constructor", "create", "new", "instantiate", "setup", "default", "defaults")
        for qk in keywords
    ):
        score *= 0.48
    if any(qk in ("dispatch", "dispatched", "subcommand", "subcommands", "route", "routed") for qk in keywords):
        if name_tokens & {"dispatch", "invoke", "resolve", "handle"}:
            score *= 1.45
        elif "decorator" in body_tokens and name_lower in (parent_cls, "command", "group", "errorhandler", "route"):
            score *= 0.52
        elif fn_span <= 4 and len(calls_lower) <= 1:
            score *= 0.55
    if any(qk in ("parse", "parsed", "parsing", "parser") for qk in keywords):
        if name_tokens & {"parse", "consume", "process"}:
            score *= 1.38
        elif name_lower.startswith(("format_", "get_help")) and not any(qk in ("format", "help") for qk in keywords):
            score *= 0.52
    if any(qk in ("across", "between", "merge", "merged", "combine", "combined") for qk in keywords) and id_or_call_hits >= 1:
        if name_tokens & {"merge", "combine", "sync"}:
            score *= 1.65
    if name_lower.startswith("test_") and not (set(keywords) & _TEST_QUERY_TERMS):
        score *= 0.45
    if id_or_call_hits >= 2:
        score *= 1.35 + 0.10 * min(3, id_or_call_hits - 2)
    elif id_or_call_hits == 0 and len(keywords) >= 2:
        score *= 0.28
    if matched_kws >= 3 and id_or_call_hits >= 1:
        score *= 1.0 + 0.12 * (matched_kws - 2)
    if cells_4d:
        inner_cells = [
            c for c in cells_4d
            if fn_s <= c.get("line_start", 0) and c.get("line_end", 0) <= fn_e
        ]
        if inner_cells:
            cell_blob = " ".join(
                f"{c.get('X_scope', '')} {' '.join(c.get('Z_write', []))} {' '.join(c.get('Z_read', []))} {' '.join(c.get('E_exit', []))}"
                for c in inner_cells
            ).lower()
            for kw in keywords:
                idf = brain._idf.get(kw, 1.0)
                kw_stem = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 5 else kw
                if kw in cell_blob or (len(kw_stem) >= 4 and kw_stem in cell_blob):
                    score += 0.9 * idf
            kw_set = set(keywords)
            if kw_set & {"db", "database", "sql", "sqlite", "postgres", "insert", "query", "connect", "persist", "storage", "table"}:
                if any(c.get("Y_role") == "IO" or c.get("sql_inserts") for c in inner_cells):
                    score += 1.6
            if kw_set & {"bug", "bugs", "error", "errors", "fail", "crash", "cant", "cannot", "broken", "500", "409"}:
                if any(c.get("E_exit") or c.get("sql_inserts") or c.get("sql_dialects") for c in inner_cells):
                    score += 1.4
    for r in routes:
        if r.get("handler") == fn.get("name"):
            for kw in keywords:
                if kw in r.get("path", "").lower() or kw == r.get("method", "").lower():
                    score += 3.0
    return score


def score_class_for_query(brain: "ProjectBrain", cls: dict, keywords: list[str]) -> float:
    from .brain import _tokenize_symbol

    name_lower = cls.get("name", "").lower()
    name_tokens = set(_tokenize_symbol(cls.get("name", ""))) | {name_lower}
    doc_lower = (cls.get("docstring") or "").lower()
    methods_lower = {m.lower() for m in cls.get("methods", [])}
    fields_lower = " ".join(cls.get("fields", [])).lower()
    score = 0.0
    for kw in keywords:
        idf = brain._idf.get(kw, 1.0)
        if kw == name_lower:
            score += 4.0 * idf
        elif kw in name_tokens or kw in name_lower:
            score += 2.5 * idf
        elif kw in fields_lower:
            score += 2.2 * idf
        elif kw in methods_lower:
            score += 1.5 * idf
        elif kw in doc_lower:
            score += 1.0 * idf
    return score


def compile_evidence_packet(
    brain: "ProjectBrain",
    task: str,
    budget_tokens: int = 3000,
    top_n: int = 4,
    deduplicate_session: bool = True,
) -> dict:
    """Compile a surgical, token-budgeted Evidence Packet for an agent task."""
    from .brain import _extract_keywords, _tokenize_symbol

    brain._ensure_fresh()
    brain._ensure_blobs()
    intent = classify_intent(task)
    if intent.get("intent") == ADD_FEATURE or re.search(
        r"\b(add|create|implement|scaffold)\s+(?:a\s+)?(?:new\s+)?(route|endpoint|handler|controller|tool|service|model)\b",
        task.lower(),
    ):
        intent["greenfield_mode"] = True
    stack_frames = _extract_stack_trace_frames(task)
    crash_lines_by_file: dict[str, list[int]] = {}
    keywords = _extract_keywords(task)
    ranked_results = brain.query(task, top_n=top_n, min_score_ratio=0.05)
    if stack_frames:
        intent["stack_trace_frames"] = stack_frames
        existing_rels = {r["rel_path"] for r in ranked_results}
        top_sc = ranked_results[0]["score"] if ranked_results else 10.0
        for sf in reversed(stack_frames):
            fh = sf["file_hint"].lstrip("./")
            for idx_rel in brain._index:
                if idx_rel == fh or idx_rel.endswith("/" + fh) or fh.endswith("/" + idx_rel):
                    crash_lines_by_file.setdefault(idx_rel, []).append(sf["line"])
                    if idx_rel not in existing_rels:
                        ranked_results.insert(
                            0,
                            {
                                "rel_path": idx_rel,
                                "score": top_sc + 25.0,
                                "reason": f"stack trace frame L{sf['line']}",
                            },
                        )
                        existing_rels.add(idx_rel)

    char_budget = max(budget_tokens * 4, 1200)
    chars_used = 0
    actual_chars_emitted = 0

    target_slices: list[dict] = []
    neighbor_skeletons: list[dict] = []
    omitted_already_in_context: list[str] = []

    matched_files = [r["rel_path"] for r in ranked_results]
    matched_file_set = set(matched_files)
    raw_files_bytes = sum(brain._sizes.get(f, 0) for f in matched_files)

    top_file_score = ranked_results[0]["score"] if ranked_results else 0.0
    per_file_candidates: list[tuple[dict, list[tuple[float, str, dict]]]] = []
    for file_idx, r in enumerate(ranked_results):
        if file_idx > 0 and omitted_already_in_context and r.get("score", 0.0) < top_file_score * 0.45:
            continue
        rel_path = r["rel_path"]
        info = brain._index.get(rel_path, {})
        behaviors = brain._behaviors.get(rel_path, {})

        fns_list = info.get("functions", [])
        n_fns = len(fns_list)
        local_idf: dict[str, float] = {}
        if n_fns >= 6:
            for kw in keywords:
                kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                df_c = sum(
                    1
                    for f0 in fns_list
                    if kw in f0["name"].lower()
                    or kw in (f0.get("docstring") or "").lower()
                    or kw in set(f0.get("body_tokens", []))
                    or (len(kw_s) >= 3 and (kw_s in f0["name"].lower() or kw_s in (f0.get("docstring") or "").lower()))
                )
                local_idf[kw] = math.log(1.0 + n_fns / (1.0 + df_c))

        scored_fns: dict[str, tuple[float, dict]] = {}
        all_fns_by_name: dict[str, dict] = {}
        for fn in fns_list:
            q_key = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
            all_fns_by_name[q_key] = fn
            all_fns_by_name.setdefault(fn["name"], fn)
            fn_score = brain._score_symbol_for_query(
                fn, keywords, behaviors, info.get("routes", []), local_idf=local_idf
            )
            for c_ln in crash_lines_by_file.get(rel_path, ()):
                if fn.get("line_start", 1) <= c_ln <= fn.get("line_end", 1):
                    fn_score += 50.0
            if fn_score > 0:
                scored_fns[q_key] = (fn_score, fn)

        if scored_fns:
            boosts: dict[str, float] = {}
            for fn in fns_list:
                q_caller = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                for callee_nm in fn.get("calls", []):
                    callee_fn = all_fns_by_name.get(f"{fn.get('parent_class')}.{callee_nm}") or all_fns_by_name.get(
                        callee_nm
                    )
                    if not callee_fn:
                        continue
                    q_callee = (
                        f"{callee_fn.get('parent_class')}.{callee_fn['name']}"
                        if callee_fn.get("parent_class")
                        else callee_fn["name"]
                    )
                    if q_callee in scored_fns and q_callee != q_caller:
                        callee_sc = scored_fns[q_callee][0]
                        mult = 0.70 if callee_fn["name"].startswith("_") and not fn["name"].startswith("_") else 0.12
                        boosts[q_caller] = max(boosts.get(q_caller, 0.0), callee_sc * mult)
                    for callee2_nm in callee_fn.get("calls", []):
                        callee2_fn = all_fns_by_name.get(
                            f"{callee_fn.get('parent_class')}.{callee2_nm}"
                        ) or all_fns_by_name.get(callee2_nm)
                        if not callee2_fn:
                            continue
                        q_callee2 = (
                            f"{callee2_fn.get('parent_class')}.{callee2_fn['name']}"
                            if callee2_fn.get("parent_class")
                            else callee2_fn["name"]
                        )
                        if q_callee2 in scored_fns and q_callee2 != q_caller:
                            callee2_sc = scored_fns[q_callee2][0]
                            mult2 = (
                                0.55 if callee2_fn["name"].startswith("_") and not fn["name"].startswith("_") else 0.08
                            )
                            boosts[q_caller] = max(boosts.get(q_caller, 0.0), callee2_sc * mult2)
            for q_k, b_val in boosts.items():
                fn_obj = all_fns_by_name[q_k]
                prev_sc = scored_fns[q_k][0] if q_k in scored_fns else 0.0
                scored_fns[q_k] = (prev_sc + b_val, fn_obj)

        scored_symbols: list[tuple[float, str, dict]] = []
        if scored_fns:
            rem_fns = list(scored_fns.values())
            id_cov_kws: set[str] = set()
            doc_cov_kws: set[str] = set()
            selected_priv_callees: dict[str, float] = {}
            for sel_step in range(min(3, len(rem_fns))):
                best_idx = 0
                best_gain = -1.0
                for idx_f, (base_sc, fn_obj) in enumerate(rem_fns):
                    nm_l = fn_obj["name"].lower()
                    nm_toks = set(_tokenize_symbol(fn_obj["name"]))
                    sig_toks = set(re.findall(r"[a-z0-9]+", (fn_obj.get("signature") or "").lower()))
                    doc_l = (fn_obj.get("docstring") or "").lower()
                    calls_l = {c.lower() for c in fn_obj.get("calls", [])}
                    bt_l = set(fn_obj.get("body_tokens", []))
                    marginal = 0.0
                    id_hits = 0
                    fn_q = (
                        f"{fn_obj.get('parent_class')}.{fn_obj['name']}"
                        if fn_obj.get("parent_class")
                        else fn_obj["name"]
                    )
                    b_bonus = boosts.get(fn_q, 0.0) if scored_fns else 0.0
                    if selected_priv_callees and not fn_obj["name"].startswith("_"):
                        for c_raw in fn_obj.get("calls", []):
                            c_tail = c_raw.split(".")[-1]
                            if c_tail in selected_priv_callees:
                                b_bonus = max(b_bonus, selected_priv_callees[c_tail] * 0.95)
                    for kw in keywords:
                        kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                        w_idf = brain._idf.get(kw, 1.0) * local_idf.get(kw, 1.0)
                        id_mult = 1.0 if kw not in id_cov_kws else 0.12
                        doc_mult = 1.0 if (kw not in id_cov_kws and kw not in doc_cov_kws) else 0.12
                        is_gen = kw in ("file", "files", "path", "paths", "data", "node", "nodes")
                        if kw == nm_l or kw in nm_toks or (len(kw_s) >= 3 and kw_s in nm_toks):
                            marginal += (0.8 if is_gen else 5.2) * w_idf * id_mult
                            if not is_gen and kw not in id_cov_kws:
                                id_hits += 1
                        elif kw in sig_toks or (len(kw_s) >= 3 and kw_s in sig_toks):
                            marginal += (0.6 if is_gen else 3.2) * w_idf * id_mult
                            if not is_gen and kw not in id_cov_kws:
                                id_hits += 1
                        elif kw in doc_l or (len(kw_s) >= 4 and kw_s in doc_l):
                            marginal += 1.1 * w_idf * doc_mult
                        elif any(kw in c or (len(kw_s) >= 4 and kw_s in c) for c in calls_l):
                            marginal += 0.9 * w_idf * doc_mult
                        elif kw in bt_l:
                            marginal += 0.4 * w_idf * doc_mult
                    if id_hits >= 2:
                        marginal *= 1.45
                    comb_sc = (
                        (0.70 * base_sc + 0.30 * marginal)
                        if sel_step == 0
                        else (0.10 * base_sc + 0.90 * marginal + 0.85 * b_bonus)
                    )
                    if comb_sc > best_gain:
                        best_gain = comb_sc
                        best_idx = idx_f
                chosen_sc, chosen_fn = rem_fns.pop(best_idx)
                scored_symbols.append((best_gain, "fn", chosen_fn))
                if chosen_fn["name"].startswith("_"):
                    selected_priv_callees[chosen_fn["name"]] = max(
                        selected_priv_callees.get(chosen_fn["name"], 0.0), chosen_sc
                    )
                ch_nm_l = chosen_fn["name"].lower()
                ch_nm_toks = set(_tokenize_symbol(chosen_fn["name"]))
                sig_toks = set(re.findall(r"[a-z0-9]+", (chosen_fn.get("signature") or "").lower()))
                doc_l = (chosen_fn.get("docstring") or "").lower()
                for kw in keywords:
                    kw_s = re.sub(r"(?:ed|ing|es|s)$", "", kw) if len(kw) >= 4 else kw
                    if (
                        kw == ch_nm_l
                        or kw in ch_nm_toks
                        or (len(kw_s) >= 3 and kw_s in ch_nm_toks)
                        or kw in sig_toks
                        or (len(kw_s) >= 3 and kw_s in sig_toks)
                    ):
                        id_cov_kws.add(kw)
                    elif kw in doc_l or (len(kw_s) >= 4 and kw_s in doc_l):
                        doc_cov_kws.add(kw)

        methods_by_class: dict[str, int] = {}
        for _, _, fn_obj in scored_symbols:
            if fn_obj.get("parent_class"):
                methods_by_class[fn_obj["parent_class"]] = methods_by_class.get(fn_obj["parent_class"], 0) + 1

        for cls in info.get("classes", []):
            if methods_by_class.get(cls["name"], 0) >= 1:
                continue
            cls_score = brain._score_class_for_query(cls, keywords)
            if cls_score > 0:
                scored_symbols.append((cls_score, "cls", cls))

        if not scored_symbols:
            for fn in info.get("functions", [])[:2]:
                scored_symbols.append((1.0, "fn", fn))
            for cls in info.get("classes", [])[:1]:
                scored_symbols.append((1.0, "cls", cls))

        per_file_candidates.append((r, scored_symbols[:3]))

    ordered_slice_reqs: list[tuple[dict, float, str, dict]] = []
    if per_file_candidates:
        for slot_idx in (0, 1, 2):
            for r_entry, cand_list in per_file_candidates:
                if slot_idx < len(cand_list):
                    sc_v, k_v, sym_v = cand_list[slot_idx]
                    ordered_slice_reqs.append((r_entry, sc_v, k_v, sym_v))
        if brain._graph:
            seen_req_nids = {
                fn_id(re_i["rel_path"], sy_i["name"], sy_i.get("parent_class"))
                if ki_i == "fn"
                else cls_id(re_i["rel_path"], sy_i["name"])
                for re_i, _, ki_i, sy_i in ordered_slice_reqs
            }
            for re_i, sc_i, ki_i, sy_i in list(ordered_slice_reqs[:3]):
                if ki_i != "fn":
                    continue
                src_nid = fn_id(re_i["rel_path"], sy_i["name"], sy_i.get("parent_class"))
                for ei in brain._graph._adj.get(src_nid, []):
                    e_c = brain._graph.edges[ei]
                    if e_c["type"] == "calls" and e_c.get("confidence") in ("exact", "import_resolved"):
                        dst_nid = e_c["dst"]
                        if dst_nid in seen_req_nids:
                            continue
                        dst_nd = brain._graph.nodes.get(dst_nid, {})
                        dst_rel = dst_nd.get("rel_path", "")
                        if dst_rel and dst_rel != re_i["rel_path"] and dst_rel in brain._index:
                            for dfn in brain._index[dst_rel].get("functions", []):
                                if fn_id(dst_rel, dfn["name"], dfn.get("parent_class")) == dst_nid:
                                    seen_req_nids.add(dst_nid)
                                    ordered_slice_reqs.append(
                                        (
                                            {"rel_path": dst_rel, "reason": f"called by {sy_i['name']}"},
                                            sc_i * 0.5,
                                            "fn",
                                            dfn,
                                        )
                                    )
                                    break

    for r, sym_score, kind, sym in ordered_slice_reqs:
        if chars_used >= char_budget * 0.68:
            break
        rel_path = r["rel_path"]
        behaviors = brain._behaviors.get(rel_path, {})
        parent = sym.get("parent_class")
        nid = fn_id(rel_path, sym["name"], parent) if kind == "fn" else cls_id(rel_path, sym["name"])

        if deduplicate_session and nid in brain._served_slices:
            omitted_already_in_context.append(f"{nid} (L{sym.get('line_start')}-{sym.get('line_end')})")
            est_lines = min(max(sym.get("line_end", 1) - sym.get("line_start", 1) + 1, 8), 44)
            chars_used += est_lines * 48 + 90
            continue

        l_start = sym.get("line_start", 1)
        l_end = sym.get("line_end", l_start)
        max_slice_lines = (16 if (l_end - l_start) > 120 else 22) if char_budget <= 11000 else (34 if kind == "cls" else 40)
        code_text = slice_lines(
            brain.root,
            rel_path,
            l_start,
            l_end,
            max_lines=max_slice_lines,
            focus_terms=keywords,
            pin_lines=crash_lines_by_file.get(rel_path),
        )
        if not code_text:
            continue

        if deduplicate_session:
            brain._served_slices.add(nid)

        qname = f"{parent}.{sym['name']}" if parent else sym["name"]
        slice_entry = {
            "node_id": nid,
            "rel_path": rel_path,
            "symbol": qname,
            "kind": kind,
            "lines": f"L{l_start}-{l_end}",
            "signature": sym.get("signature", ""),
            "behavior": behaviors.get(qname, "unknown"),
            "reason": r.get("reason", "task match"),
            "code": code_text,
        }
        target_slices.append(slice_entry)
        chars_used += len(code_text) + 120
        actual_chars_emitted += len(code_text) + 120

    skeleton_candidates: list[tuple[str, str]] = []
    active_ranked = [
        r
        for idx, r in enumerate(ranked_results)
        if idx == 0 or not omitted_already_in_context or r.get("score", 0.0) >= top_file_score * 0.45
    ]
    for r in active_ranked:
        skeleton_candidates.append((r["rel_path"], "primary file outline"))
    for r in active_ranked[:3]:
        rp = r["rel_path"]
        if deduplicate_session and rp in brain._served_skeletons:
            continue
        for dep in brain._import_targets.get(rp, [])[:3]:
            if dep not in matched_file_set:
                skeleton_candidates.append((dep, f"imported by {rp}"))
        for imp_by in brain._imported_by.get(rp, [])[:2]:
            if imp_by not in matched_file_set:
                skeleton_candidates.append((imp_by, f"imports {rp}"))

    seen_skel: set[str] = set()
    for skel_path, skel_reason in skeleton_candidates:
        if skel_path in seen_skel or chars_used >= char_budget * 0.95:
            continue
        seen_skel.add(skel_path)
        if deduplicate_session and skel_path in brain._served_skeletons:
            omitted_already_in_context.append(f"skeleton:{skel_path}")
            chars_used += 1400
            continue
        info = brain._index.get(skel_path)
        if not info:
            continue
        skel_text = build_file_skeleton(
            skel_path, info, behaviors=brain._behaviors.get(skel_path, {}), focus_terms=keywords
        )
        if deduplicate_session:
            brain._served_skeletons.add(skel_path)
        neighbor_skeletons.append({
            "rel_path": skel_path,
            "reason": skel_reason,
            "skeleton": skel_text,
        })
        chars_used += len(skel_text) + 60
        actual_chars_emitted += len(skel_text) + 60
        if skel_path not in matched_file_set:
            raw_files_bytes += brain._sizes.get(skel_path, 0)

    flow_trace = None
    if intent["intent"] == LOCATE_ROUTE and intent.get("entities"):
        ent = intent["entities"][0]
        parts = ent.split(" ", 1)
        if len(parts) == 2:
            flow_trace = brain.trace_flow(parts[1], method=parts[0])
        else:
            flow_trace = brain.trace_flow(parts[0])

    all_touched_paths = matched_file_set | seen_skel
    all_touched_nodes = {s["node_id"] for s in target_slices} | {file_id(p) for p in all_touched_paths}
    memories = brain._memory.get_notes_for(all_touched_paths, all_touched_nodes, brain._ast_hashes)

    related_tests: set[str] = set()
    for p in matched_files:
        for imp in brain._imported_by.get(p, []):
            if any(t in imp.lower() for t in ("test", "spec", "__tests__")):
                related_tests.add(imp)

    structural_siblings: list[dict] = []
    if intent.get("greenfield_mode"):
        for r_sib in ranked_results[:3]:
            rp_s = r_sib["rel_path"]
            info_s = brain._index.get(rp_s, {})
            for rt_s in info_s.get("routes", [])[:2]:
                structural_siblings.append({
                    "rel_path": rp_s,
                    "kind": "route_exemplar",
                    "pattern": f"{rt_s.get('method')} {rt_s.get('path')} -> {rt_s.get('handler')}",
                    "line": rt_s.get("line", 1),
                })
            for cls_s in info_s.get("classes", [])[:2]:
                structural_siblings.append({
                    "rel_path": rp_s,
                    "kind": "class_exemplar",
                    "pattern": cls_s.get("signature", cls_s.get("name")),
                    "line": cls_s.get("line_start", 1),
                })

    out: dict = {
        "task": task,
        "intent": intent,
        "target_slices": target_slices,
        "neighbor_skeletons": neighbor_skeletons,
        "recommended_read_order": matched_files,
        "related_tests": sorted(related_tests),
        "resolution_telemetry": {
            "files_resolved": len(matched_files),
            "target_slices_count": len(target_slices),
            "neighbor_skeletons_count": len(neighbor_skeletons),
            "session_deduplicated_count": len(omitted_already_in_context),
        },
    }
    if memories:
        out["memories"] = memories
    if flow_trace and flow_trace.get("steps"):
        out["flow_trace"] = flow_trace
    if structural_siblings:
        out["structural_siblings"] = structural_siblings[:4]
    if omitted_already_in_context:
        out["deduplicated_in_session"] = omitted_already_in_context
    brain._ledger.record_input_op(
        op="compile",
        query=task,
        files_covered=len(matched_files),
        touched_files=all_touched_paths,
        slices_included=len(target_slices),
    )
    return out


def compile_engineering_dossier(
    brain: "ProjectBrain",
    tasks: list[str] | str,
    impact_files: list[str] | None = None,
    out_path: str | None = None,
    zoom: bool = False,
    write_file: bool = True,
) -> dict:
    """Compile a self-contained, zero-follow-up Engineering Dossier across one or more tasks."""
    from .brain import (
        DEPRIORITY_DIRS,
        STOPWORDS,
        _TEST_QUERY_TERMS,
        _extract_keywords,
        _is_test_file,
        _tokenize_symbol,
    )

    brain._ensure_fresh()
    brain._ensure_blobs()
    if isinstance(tasks, str):
        tasks = [tasks]
    impact_list = list(impact_files or [])

    seen_slices: set[str] = set()
    seen_globals: set[str] = set()
    seen_impacts: set[str] = set()
    all_keywords: list[str] = []
    for t in tasks:
        for kw in _extract_keywords(t):
            if kw not in all_keywords:
                all_keywords.append(kw)
        for rel in brain._index:
            bname = Path(rel).name
            if bname.lower() in t.lower() and any(
                w in t.lower() for w in ("import", "depend", "impact", "many files", "blast")
            ):
                if rel not in impact_list:
                    impact_list.append(rel)

    md_sections: list[str] = [
        "# ProjectBrain Complete Engineering Dossier",
        "> Self-contained AST slices, module constants/globals, bidirectional call chains, and blast radius.",
        "",
    ]

    seen_spans: list[tuple[str, int, int]] = []
    primary_files: set[str] = set()
    emitted_by_file: dict[str, list[tuple[int, int]]] = {}
    matched_4d_cells_count = 0
    monolith_4d_cells_count = 0

    def _ensure_globals(rel_path: str, buf: list[str], max_g: int = 24, kw_list: list[str] | None = None) -> None:
        if zoom or max_g <= 0 or rel_path in seen_globals or rel_path not in brain._index:
            return
        seen_globals.add(rel_path)
        f_info = brain._index[rel_path]
        glob_text = extract_file_globals_slice(brain.root, rel_path, f_info, max_lines=min(max_g, 16))
        if not glob_text:
            return
        is_ts_iface = rel_path.endswith((".ts", ".tsx")) and ("interface " in glob_text or "type " in glob_text)
        is_globals_module = len(f_info.get("functions", [])) == 0 and len(f_info.get("classes", [])) <= 1
        non_imp_lines = [
            ln.lower()
            for ln in glob_text.splitlines()
            if not ln.lstrip().startswith(("import ", "from ", "#", "//", "/*", "*", "export type "))
        ]
        non_imp_blob = "\n".join(non_imp_lines)
        has_kw_global = bool(
            kw_list
            and any(len(w) >= 4 and brain._idf.get(w, 1.0) >= 2.0 and w in non_imp_blob for w in kw_list)
        )
        if not (is_ts_iface or is_globals_module or has_kw_global):
            return
        g_ivs = extract_emitted_intervals(glob_text)
        if g_ivs:
            g_plan = brain._ledger.plan_slice_intervals(rel_path, g_ivs[0][0], g_ivs[-1][1], commit=False)
            if not g_plan["unseen_intervals"]:
                buf.append(
                    f"### [{rel_path} :: Module Constants — [Session Cache] Already delivered in this session (0 duplicate lines)]"
                )
                return
        emitted_by_file.setdefault(rel_path, []).extend(g_ivs)
        buf.append(f"### [{rel_path} :: Module Constants, State & Bootstrap]")
        buf.append(glob_text)

    def _node_display_name(nd_obj: dict) -> str:
        p_cls = nd_obj.get("parent_class")
        nm_s = nd_obj.get("name") or ""
        if p_cls and nm_s and not nm_s.startswith(f"{p_cls}."):
            return f"{p_cls}.{nm_s}"
        return nm_s

    def _add_node_slice(
        node_id: str, reason: str, kw_list: list[str], buf: list[str], max_l: int = 44, max_g: int = 24
    ) -> None:
        if not brain._graph or node_id not in brain._graph.nodes:
            return
        nd = brain._graph.nodes[node_id]
        if nd.get("type") not in ("fn", "cls"):
            return
        rel = nd.get("rel_path", "")
        if not rel or rel not in brain._index:
            return
        l_start = nd.get("line_start", 1)
        l_end = nd.get("line_end", l_start)
        is_caller_slice = any(reason.startswith(p) for p in ("calls ", "invokes "))
        m_call_line = re.search(r"@L(\d+)", reason) if is_caller_slice else None
        slice_key = f"{node_id}@L{m_call_line.group(1)}" if (m_call_line and (l_end - l_start) > 120) else node_id
        if slice_key in seen_slices:
            return
        effective_max_l = 12 if (l_end - l_start) > 400 and is_caller_slice else max_l
        slice_s, slice_e = l_start, l_end
        if m_call_line and (l_end - l_start) > 120:
            c_ln = int(m_call_line.group(1))
            slice_s = max(l_start, c_ln - 4)
            slice_e = min(l_end, c_ln + 7)
            effective_max_l = 14
        for s_rel, s_start, s_end in seen_spans:
            if s_rel == rel and s_start <= slice_s and slice_e <= s_end and (s_end - s_start) <= 130:
                seen_slices.add(slice_key)
                return
        seen_slices.add(slice_key)
        seen_spans.append((rel, slice_s, slice_e))
        _ensure_globals(rel, buf, max_g=max_g, kw_list=kw_list)
        ast_calls: list[str] = []
        if (l_end - l_start) <= 450 and not is_caller_slice:
            for f_entry in brain._index[rel].get("functions", []):
                if f_entry.get("line_start") == l_start:
                    ast_calls = [c.lower() for c in f_entry.get("calls", []) if len(c) >= 4]
                    break

        s_plan = brain._ledger.plan_slice_intervals(rel, slice_s, slice_e, commit=False)
        if not s_plan["unseen_intervals"]:
            buf.append(
                f"### [{rel} :: {_node_display_name(nd)} (L{slice_s}-L{slice_e}) — [Session Cache] Already delivered in this session (0 duplicate lines)]"
            )
            return

        node_pins: set[int] = set(brain._last_pin_lines.get(rel, set()))
        node_anns: dict[int, list[str]] = {
            k: list(v) for k, v in brain._last_line_annotations.get(rel, {}).items()
        }
        if brain._graph:
            for ei_hb in brain._graph._adj.get(node_id, []):
                ehb = brain._graph.edges[ei_hb]
                if ehb["type"] == "http_flow" and ehb.get("line"):
                    hl = int(ehb["line"])
                    dst_nd = brain._graph.nodes.get(ehb["dst"], {})
                    dst_lbl = f"{dst_nd.get('rel_path', '')}:{dst_nd.get('name', '')} (L{dst_nd.get('line_start', 1)})"
                    b_msg = f"# >>> [HTTP-BRIDGE L{hl}] {ehb.get('reason', '')} -> {dst_lbl}"
                    node_pins.add(hl)
                    if b_msg not in node_anns.setdefault(hl, []):
                        node_anns[hl].append(b_msg)
        if "Causal Symptom Chop" in reason:
            for c_cell in brain._index[rel].get("cells_4d", []):
                cs_ln = c_cell.get("line_start", 0)
                ce_ln = c_cell.get("line_end", 0)
                if slice_s <= cs_ln <= slice_e and any(
                    ex.startswith(("status:", "throw:", "err:")) for ex in c_cell.get("E_exit", [])
                ):
                    for gl in range(cs_ln, min(slice_e + 1, ce_ln + 1)):
                        node_pins.add(gl)
        in_slice_pins = sorted(p for p in node_pins if slice_s <= p <= slice_e)
        if len(in_slice_pins) >= 6:
            effective_max_l = max(effective_max_l, 64)

        matrix_header = ""
        cell_slices: list[str] = []
        cell_focus_terms: list[str] = []
        if (slice_e - slice_s) >= 10 and (not is_caller_slice or node_anns):
            pre_cells = brain._index[rel].get("cells_4d")
            if pre_cells:
                cells = [c for c in pre_cells if c["line_start"] >= slice_s and c["line_end"] <= slice_e]
            else:
                try:
                    f_lines = (brain.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    f_lines = []
                cells = extract_4d_coordinate_matrix(f_lines, slice_s, slice_e, ext=Path(rel).suffix) if f_lines else []
            if cells:
                max_rows = 22 if (slice_e - slice_s) > 200 else 1
                top_cells, matrix_header = query_4d_coordinate_matrix(
                    cells,
                    focus_terms=kw_list + ast_calls,
                    raw_query=" ".join(tasks),
                    max_table_rows=max_rows,
                )
                nonlocal matched_4d_cells_count, monolith_4d_cells_count
                if (slice_e - slice_s) > 450 and len(cells) >= 3:
                    cands_to_slice = list(top_cells[:3])
                    for c_item in cells:
                        if (
                            c_item.get("js_ast_contracts")
                            and c_item not in cands_to_slice
                            and len(cands_to_slice) < 5
                            and any(
                                brain._contract_in_query_scope(
                                    rel, c_item.get("X_scope", ""), jc0.get("summary", ""), kw_list
                                )
                                for jc0 in c_item["js_ast_contracts"]
                            )
                        ):
                            cands_to_slice.append(c_item)
                    for tc in cands_to_slice:
                        tc_s, tc_e = tc["line_start"], tc["line_end"]
                        if any(
                            sr == rel and ss <= tc_s and tc_e <= se and (se - ss) <= 130
                            for sr, ss, se in seen_spans
                        ):
                            continue
                        if tc["span"] <= 95:
                            tc_code = slice_lines(
                                brain.root,
                                rel,
                                tc_s,
                                tc_e,
                                max_lines=min(55, tc["span"] + 2),
                                focus_terms=kw_list + ast_calls,
                                pin_lines=node_pins,
                                inline_annotations=node_anns,
                            )
                            if tc_code:
                                matched_4d_cells_count += 1
                                monolith_4d_cells_count += 1
                                seen_spans.append((rel, tc_s, tc_e))
                                emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(tc_code))
                                cell_slices.append(
                                    f"# [6D Cell Match: {tc['Y_role']} | {tc['X_scope']} (L{tc_s}-L{tc_e})]\n{tc_code}"
                                )
                    for pin_ln in sorted(node_anns.keys()):
                        if not (slice_s <= pin_ln <= slice_e):
                            continue
                        if any(sr == rel and ss <= pin_ln <= se and (se - ss) <= 130 for sr, ss, se in seen_spans):
                            continue
                        p_s = max(slice_s, pin_ln - 3)
                        p_e = min(slice_e, pin_ln + 7)
                        p_code = slice_lines(
                            brain.root,
                            rel,
                            p_s,
                            p_e,
                            max_lines=14,
                            focus_terms=kw_list,
                            pin_lines=node_pins,
                            inline_annotations=node_anns,
                        )
                        if p_code:
                            seen_spans.append((rel, p_s, p_e))
                            emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(p_code))
                            cell_slices.append(f"# [6D Contract Pin Window (L{p_s}-L{p_e})]\n{p_code}")
                else:
                    matched_4d_cells_count += max(1, len(top_cells[:2]))
                    for tc in top_cells[:2]:
                        cell_focus_terms.extend(tc.get("Z_write", [])[:3])
                        cell_focus_terms.extend(tc.get("W_calls", [])[:3])

        code = (
            ""
            if cell_slices
            else slice_lines(
                brain.root,
                rel,
                slice_s,
                slice_e,
                max_lines=effective_max_l,
                focus_terms=kw_list + ast_calls + cell_focus_terms,
                pin_lines=node_pins,
                inline_annotations=node_anns,
            )
        )
        if not code and not cell_slices:
            return
        if code:
            emitted_by_file.setdefault(rel, []).extend(extract_emitted_intervals(code))
        hdr_span = f"L{slice_s}-L{slice_e}" if (m_call_line and (l_end - l_start) > 120) else f"L{l_start}-L{l_end}"
        buf.append(f"### [{rel} :: {_node_display_name(nd)} ({hdr_span}) — {reason}]")
        if matrix_header:
            buf.append(matrix_header)
        for cs in cell_slices:
            buf.append(cs)
        if code:
            buf.append(code)

    for idx, task in enumerate(tasks, start=1):
        clean_task_for_search = task.strip()
        t_kw = brain._expand_keywords_with_aliases(_extract_keywords(clean_task_for_search))
        t_low = clean_task_for_search.lower()
        sec: list[str] = [f"## Task {idx}: {task}"]
        if not zoom:
            brain._compile_6d_contract_diagnostics(task, set(brain._index.keys()))

        explicit_nodes: list[str] = []
        explicit_task_files: list[str] = []
        for m_path in _TASK_PATH_RE.findall(clean_task_for_search):
            mp_low = m_path.lower().strip("/")
            cands_mp = [
                rel_k for rel_k in brain._index if rel_k.lower() == mp_low or rel_k.lower().endswith("/" + mp_low)
            ]
            if cands_mp:
                best_mp = min(cands_mp, key=lambda k: (len(k.split("/")), len(k)))
                if best_mp not in explicit_task_files:
                    explicit_task_files.append(best_mp)
        has_explicit_route = False
        if brain._graph:
            brain._graph._ensure_registries()
            # Pre-tokenize task text once in O(|task|) to replace O(|V|) dynamic regex compilations
            task_word_tokens_exact = set(_WORD_EXTRACT_RE.findall(clean_task_for_search))
            task_dotted_pairs_low: set[str] = set()
            for d_chain in _DOTTED_CHAIN_RE.findall(t_low):
                d_parts = d_chain.split(".")
                for d_i in range(len(d_parts) - 1):
                    task_dotted_pairs_low.add(f"{d_parts[d_i]}.{d_parts[d_i + 1]}")

            for nid, nd in brain._graph.nodes.items():
                ntype = nd.get("type")
                if ntype == "route":
                    rpath = (nd.get("path") or "").lower()
                    rmeth = (nd.get("method") or "").lower()
                    if rpath and len(rpath) > 1 and rpath in t_low:
                        if not rmeth or rmeth in t_low:
                            explicit_nodes.append(nid)
                            has_explicit_route = True
                elif ntype in ("fn", "cls"):
                    raw_nm = nd.get("name") or ""
                    p_cls = nd.get("parent_class") or ""
                    if p_cls and raw_nm:
                        q_nm = f"{p_cls}.{raw_nm}"
                        q_nm_low = q_nm.lower()
                        if _PURE_WORD_RE.match(p_cls) and _PURE_WORD_RE.match(raw_nm):
                            q_matched = q_nm_low in task_dotted_pairs_low
                        else:
                            q_matched = q_nm_low in t_low and bool(
                                re.search(r"\b" + re.escape(q_nm) + r"\b", clean_task_for_search, re.IGNORECASE)
                            )
                        if q_matched:
                            explicit_nodes.append(nid)
                            continue
                    if len(raw_nm) >= 5 and (
                        raw_nm in task_word_tokens_exact or (not _PURE_WORD_RE.match(raw_nm) and raw_nm in clean_task_for_search)
                    ):
                        if (
                            any(c.isupper() for c in raw_nm[1:])
                            or "_" in raw_nm
                            or f"`{raw_nm}`" in task
                            or f"({raw_nm})" in task
                        ):
                            if _PURE_WORD_RE.match(raw_nm) or re.search(
                                r"\b" + re.escape(raw_nm) + r"\b", clean_task_for_search
                            ):
                                explicit_nodes.append(nid)

        explicit_data_keys: list[str] = []
        for m_dk_tok in _DATA_KEY_RE.findall(clean_task_for_search):
            dk_cand = (m_dk_tok[0] or m_dk_tok[1]).strip()
            if len(dk_cand) >= 5 and dk_cand.lower() not in STOPWORDS:
                if not any(
                    (brain._graph.nodes.get(en, {}).get("name") or "").lower() == dk_cand.lower()
                    for en in explicit_nodes
                ):
                    if dk_cand not in explicit_data_keys:
                        explicit_data_keys.append(dk_cand)
        explicit_state_files: set[str] = set()
        if explicit_data_keys and not explicit_nodes and brain._graph:
            q_wants_tests_dk = bool(set(t_kw) & _TEST_QUERY_TERMS)
            dk_fn_cands: list[tuple[float, str, str, str, bool]] = []
            dk_state_slices: list[tuple[float, str, int, int, str]] = []
            for dk in explicit_data_keys[:2]:
                dk_low = dk.lower()
                dk_word_re = re.compile(rf"\b{re.escape(dk_low)}\b")
                dk_attr_re = re.compile(rf"(?:self|app|config|settings|cls)\.{re.escape(dk_low)}\b")
                dk_assign_re = re.compile(rf"\b{re.escape(dk_low)}\b\s*(?::\s*[^=]+)?=")
                dk_dict_re = re.compile(rf"^['\"]{re.escape(dk)}['\"]\s*:")
                for rp_dk, info_dk in brain._index.items():
                    if not q_wants_tests_dk and (
                        _is_test_file(rp_dk)
                        or bool({p.lower() for p in Path(rp_dk).parts[:-1]} & DEPRIORITY_DIRS)
                    ):
                        continue
                    if (
                        dk_low not in brain._token_sets.get(rp_dk, set())
                        and dk_low not in brain._body_token_sets.get(rp_dk, set())
                        and dk_low not in brain._blobs.get(rp_dk, "")
                    ):
                        continue
                    raw_txt_dk = brain._get_file_text(rp_dk) or ""
                    if dk not in raw_txt_dk and dk_low not in raw_txt_dk.lower():
                        continue
                    flines_dk = raw_txt_dk.splitlines()
                    beh_dk = brain._behaviors.get(rp_dk, {})
                    rts_dk = info_dk.get("routes", [])
                    c4d_dk = info_dk.get("cells_4d", [])
                    covered_dk_lines: set[int] = set()
                    for fn_dk in info_dk.get("functions", []):
                        ls_d, le_d = fn_dk.get("line_start", 1), fn_dk.get("line_end", 1)
                        fn_txt_d = "\n".join(flines_dk[max(0, ls_d - 1) : min(len(flines_dk), le_d)])
                        fn_txt_d_low = fn_txt_d.lower()
                        if dk in fn_txt_d or (dk_low in fn_txt_d_low and dk_word_re.search(fn_txt_d_low)):
                            for cln in range(ls_d, le_d + 1):
                                covered_dk_lines.add(cln)
                            sc_d = brain._score_symbol_for_query(fn_dk, t_kw + [dk_low], beh_dk, rts_dk, c4d_dk)
                            is_code_access = bool(
                                f".{dk_low}" in fn_txt_d_low
                                or f'"{dk}"' in fn_txt_d
                                or f"'{dk}'" in fn_txt_d
                                or dk_attr_re.search(fn_txt_d_low)
                            )
                            if is_code_access:
                                sc_d += 10.0
                            nid_d = fn_id(rp_dk, fn_dk["name"], fn_dk.get("parent_class"))
                            dk_fn_cands.append((sc_d, nid_d, rp_dk, dk, is_code_access))
                    for ln_i, ln_str in enumerate(flines_dk, start=1):
                        if ln_i in covered_dk_lines:
                            continue
                        ln_low = ln_str.lower()
                        if dk in ln_str or (dk_low in ln_low and dk_assign_re.search(ln_low)):
                            st_ln = ln_str.strip()
                            if st_ln.startswith(("#", "//", "*", '"""', "'''")):
                                continue
                            is_dict_kv = bool(dk_dict_re.match(st_ln))
                            if st_ln.startswith(('"', "'")) and not is_dict_kv:
                                continue
                            is_decl = ("=" in ln_str) or is_dict_kv
                            sc_st = (11.0 if "=" in ln_str else 10.0) if is_decl else 5.0
                            w_s = max(1, ln_i - 2)
                            w_e = min(len(flines_dk), ln_i + 6)
                            dk_state_slices.append((sc_st, rp_dk, w_s, w_e, f"defines/configures `{dk}` @L{ln_i}"))
            dk_state_slices.sort(key=lambda x: x[0], reverse=True)
            for _, rp_st, ws_st, we_st, rsn_st in dk_state_slices[:3]:
                if any(sr == rp_st and ss <= ws_st and we_st <= se for sr, ss, se in seen_spans):
                    continue
                primary_files.add(rp_st)
                explicit_state_files.add(rp_st)
                seen_spans.append((rp_st, ws_st, we_st))
                st_code = slice_lines(brain.root, rp_st, ws_st, we_st, max_lines=14, focus_terms=t_kw)
                if st_code:
                    matched_4d_cells_count += 1
                    emitted_by_file.setdefault(rp_st, []).extend(extract_emitted_intervals(st_code))
                    sec.append(f"### [{rp_st} (L{ws_st}-L{we_st}) — {rsn_st}]")
                    sec.append(st_code)
            has_code_dk = any(x[4] for x in dk_fn_cands)
            if has_code_dk:
                dk_fn_cands = [x for x in dk_fn_cands if x[4]]
            dk_fn_cands.sort(key=lambda x: x[0], reverse=True)
            for _, nid_d, rp_d, dk_nm, _ in dk_fn_cands[:3]:
                primary_files.add(rp_d)
                explicit_state_files.add(rp_d)
                _add_node_slice(
                    nid_d, f"reads/uses `{dk_nm}` in {rp_d}", t_kw + [dk_nm.lower()], sec, max_l=54, max_g=6
                )
                if nid_d not in explicit_nodes:
                    explicit_nodes.append(nid_d)

        exp_by_file: dict[str, list[str]] = {rp_s: [f"state:{rp_s}"] for rp_s in explicit_state_files}
        for enid in explicit_nodes:
            if brain._graph and brain._graph.nodes.get(enid, {}).get("type") in ("fn", "cls"):
                erel = brain._graph.nodes[enid].get("rel_path", "")
                if erel:
                    exp_by_file.setdefault(erel, []).append(enid)
        dominant_exp_file = max(exp_by_file.items(), key=lambda kv: len(kv[1]))[0] if exp_by_file else ""
        filtered_explicit: list[str] = []
        for enid in explicit_nodes:
            if brain._graph and brain._graph.nodes.get(enid, {}).get("type") in ("fn", "cls"):
                e_nd = brain._graph.nodes.get(enid, {})
                erel = e_nd.get("rel_path", "")
                e_span = e_nd.get("line_end", 1) - e_nd.get("line_start", 1)
                if (
                    dominant_exp_file
                    and len(exp_by_file.get(dominant_exp_file, [])) >= 2
                    and erel != dominant_exp_file
                    and e_span > 300
                ):
                    sec.append(
                        f"### [{erel} :: {e_nd.get('name')} (L{e_nd.get('line_start', 1)}-L{e_nd.get('line_end', 1)}) — referenced component (use `slice` if internals needed)]"
                    )
                    continue
                if (
                    e_span > 1500
                    and len(exp_by_file.get(erel, [])) >= 2
                    and any(
                        200 < (brain._graph.nodes[o].get("line_end", 1) - brain._graph.nodes[o].get("line_start", 1)) < e_span
                        for o in exp_by_file.get(erel, [])
                        if o != enid
                    )
                ):
                    primary_files.add(erel)
                    continue
                if erel:
                    primary_files.add(erel)
                if e_nd.get("type") == "cls" and e_span > 90:
                    cls_nm_e = e_nd.get("name", "")
                    own_methods = [
                        fn for fn in brain._index.get(erel, {}).get("functions", [])
                        if fn.get("parent_class") == cls_nm_e
                    ]
                    if len(own_methods) >= 2:
                        e_beh = brain._behaviors.get(erel, {})
                        e_rts = brain._index.get(erel, {}).get("routes", [])
                        e_c4d = brain._index.get(erel, {}).get("cells_4d", [])
                        m_scored = sorted(
                            ((brain._score_symbol_for_query(m, t_kw, e_beh, e_rts, e_c4d), m) for m in own_methods),
                            key=lambda x: x[0],
                            reverse=True,
                        )
                        for _, m_fn in m_scored[: (1 if len(tasks) >= 3 else 2)]:
                            m_nid = fn_id(erel, m_fn["name"], cls_nm_e)
                            filtered_explicit.append(m_nid)
                            _add_node_slice(m_nid, f"top method on {cls_nm_e}", t_kw, sec, max_l=48, max_g=8)
                        continue
                filtered_explicit.append(enid)
                _add_node_slice(
                    enid, "explicitly referenced in task", t_kw, sec, max_l=58 if e_span <= 450 else 44, max_g=8
                )
        if filtered_explicit:
            explicit_nodes = filtered_explicit
        had_initial_explicit = bool(explicit_nodes)
        had_executable_explicit = any(
            brain._graph and brain._graph.nodes.get(en, {}).get("type") in ("fn", "route") for en in explicit_nodes
        )
        if brain._graph and explicit_nodes:
            cls_ref_cands: list[tuple[float, str, str, str]] = []
            q_wants_tests = bool(set(t_kw) & _TEST_QUERY_TERMS)
            for enid in list(explicit_nodes):
                e_nd = brain._graph.nodes.get(enid, {})
                if e_nd.get("type") != "cls":
                    continue
                erel = e_nd.get("rel_path", "")
                enm = e_nd.get("name", "")
                enm_low = enm.lower()
                if not erel or not enm_low:
                    continue
                own_m_cnt = sum(
                    1 for fn in brain._index.get(erel, {}).get("functions", []) if fn.get("parent_class") == enm
                )
                if own_m_cnt >= 2:
                    continue
                for imp_rel in [erel] + brain._imported_by.get(erel, []):
                    if not q_wants_tests and _is_test_file(imp_rel):
                        continue
                    f_txt = brain._get_file_text(imp_rel) or ""
                    if enm not in f_txt and enm_low not in brain._blobs.get(imp_rel, ""):
                        continue
                    imp_info = brain._index.get(imp_rel, {})
                    imp_beh = brain._behaviors.get(imp_rel, {})
                    f_lines = f_txt.splitlines() if f_txt else []
                    for ifn in imp_info.get("functions", []):
                        ls, le = ifn.get("line_start", 1), ifn.get("line_end", 1)
                        fn_span_txt = "\n".join(f_lines[max(0, ls - 1) : min(len(f_lines), le)]) if f_lines else ""
                        if (
                            enm in fn_span_txt
                            or enm_low in [bt.lower() for bt in ifn.get("body_tokens", [])]
                            or enm_low in (ifn.get("params") or "").lower()
                            or enm_low in (ifn.get("return_type") or "").lower()
                        ):
                            sc = brain._score_symbol_for_query(
                                ifn, t_kw + [enm_low], imp_beh, imp_info.get("routes", []), imp_info.get("cells_4d", [])
                            )
                            ref_nid = fn_id(imp_rel, ifn["name"], ifn.get("parent_class"))
                            cls_ref_cands.append((sc, ref_nid, imp_rel, enm))
            cls_ref_cands.sort(key=lambda x: x[0], reverse=True)
            task_cls_ref_counts: dict[str, int] = {}
            for _, ref_nid, ref_rel, cls_nm in cls_ref_cands[: (2 if len(tasks) >= 2 else 3)]:
                primary_files.add(ref_rel)
                _add_node_slice(
                    ref_nid,
                    f"references {cls_nm} in {ref_rel}",
                    t_kw,
                    sec,
                    max_l=58 if len(tasks) >= 2 else 75,
                    max_g=6,
                )
                task_cls_ref_counts[ref_rel] = task_cls_ref_counts.get(ref_rel, 0) + 1
                if ref_nid not in explicit_nodes:
                    explicit_nodes.append(ref_nid)
        else:
            task_cls_ref_counts = {}
        step2_ranked_files: set[str] = set()
        same_file_callers_to_add: list[tuple[str, str, list[str]]] = []
        top3_query_files: set[str] = set()
        if not has_explicit_route:
            ranked_all = brain.query(clean_task_for_search, top_n=8)
            top3_query_files = {r["rel_path"] for r in ranked_all[:3]} | set(explicit_task_files)
            top1_file_score = ranked_all[0]["score"] if ranked_all else 1.0

            def _is_exec_file(cand_rp: str) -> bool:
                c_inf = brain._index.get(cand_rp, {})
                c_fns = c_inf.get("functions", [])
                c_calls = sum(len(f.get("calls", [])) for f in c_fns)
                return (
                    bool(c_inf.get("routes"))
                    or c_calls >= 3
                    or (len(c_fns) >= 2 and len(c_inf.get("cells_4d", [])) >= 2)
                )

            if len(explicit_task_files) >= 2:
                ranked = [{"rel_path": ef, "score": top1_file_score} for ef in explicit_task_files]
            else:
                q_wants_t = bool(set(t_kw) & _TEST_QUERY_TERMS)
                exec_map: dict[str, float] = {}
                decl_list: list[dict] = []
                for r_item in ranked_all:
                    r_p = r_item["rel_path"]
                    if not q_wants_t and _is_test_file(r_p):
                        continue
                    if _is_exec_file(r_p):
                        exec_map[r_p] = r_item["score"]
                    elif Path(r_p).stem.lower() not in ("_types", "types", "typing"):
                        decl_list.append(r_item)
                for d_item in decl_list[:3]:
                    d_rp = d_item["rel_path"]
                    d_sc = d_item["score"]
                    for imp_rp in brain._imported_by.get(d_rp, []):
                        if not q_wants_t and _is_test_file(imp_rp):
                            continue
                        if not _is_exec_file(imp_rp):
                            continue
                        imp_inf = brain._index.get(imp_rp, {})
                        imp_beh = brain._behaviors.get(imp_rp, {})
                        imp_rts = imp_inf.get("routes", [])
                        imp_c4d = imp_inf.get("cells_4d", [])
                        best_fn = max(
                            (
                                brain._score_symbol_for_query(fn, t_kw, imp_beh, imp_rts, imp_c4d)
                                for fn in imp_inf.get("functions", [])
                            ),
                            default=0.0,
                        )
                        if best_fn > 0:
                            br_sc = d_sc * 0.78 + min(3.5, best_fn * 0.22) + (0.9 if imp_rts else 0.0)
                            if br_sc > exec_map.get(imp_rp, 0.0):
                                exec_map[imp_rp] = br_sc
                sorted_exec = sorted(exec_map.items(), key=lambda kv: kv[1], reverse=True)
                max_exec_slots = 1 if (had_executable_explicit and not explicit_task_files) else 3
                chosen_exec: list[dict] = []
                route_exec = [(p, s) for p, s in sorted_exec if brain._index.get(p, {}).get("routes")]
                logic_exec = [(p, s) for p, s in sorted_exec if not brain._index.get(p, {}).get("routes")]
                if max_exec_slots >= 2 and route_exec and logic_exec:
                    top_e_sc = sorted_exec[0][1]
                    if route_exec[0][1] >= top_e_sc * 0.55:
                        chosen_exec.append(
                            {"rel_path": route_exec[0][0], "score": max(route_exec[0][1], top1_file_score * 0.75)}
                        )
                    for l_p, l_s in logic_exec[: (max_exec_slots - len(chosen_exec))]:
                        chosen_exec.append({"rel_path": l_p, "score": max(l_s, top1_file_score * 0.75)})
                for e_p, e_s in sorted_exec:
                    if len(chosen_exec) >= max_exec_slots:
                        break
                    if all(x["rel_path"] != e_p for x in chosen_exec):
                        chosen_exec.append({"rel_path": e_p, "score": max(e_s, top1_file_score * 0.75)})
                ranked = list(chosen_exec)
                if decl_list and (
                    not chosen_exec
                    or (
                        not had_initial_explicit
                        and len(ranked) < 3
                        and decl_list[0]["score"] >= top1_file_score * 0.9
                    )
                ):
                    ranked.append(decl_list[0])
                existing_ranked_paths = {r["rel_path"] for r in ranked}
                for ef in explicit_task_files:
                    if ef not in existing_ranked_paths:
                        ranked.append({"rel_path": ef, "score": top1_file_score})
                        existing_ranked_paths.add(ef)
            rank0_top_sym_score = 0.0
            for file_rank, r in enumerate(ranked):
                rp = r["rel_path"]
                min_ratio = 0.72 if file_rank == 1 else 0.52
                if file_rank > 0 and rp not in explicit_task_files and r["score"] < top1_file_score * min_ratio:
                    continue
                if (
                    (len(filtered_explicit) >= 2 or rp in exp_by_file)
                    and rp in exp_by_file
                    and rp not in explicit_task_files
                ):
                    continue
                if task_cls_ref_counts.get(rp, 0) >= 2 or (rp in exp_by_file and len(exp_by_file[rp]) >= 1):
                    continue
                info = brain._index.get(rp, {})
                behaviors = brain._get_file_behaviors(rp)
                base_classes_in_file = {b for c in info.get("classes", []) for b in c.get("bases", [])}
                scored: list[tuple[float, str, dict]] = []
                for fn in info.get("functions", []):
                    sc = brain._score_symbol_for_query(
                        fn, t_kw, behaviors, info.get("routes", []), info.get("cells_4d", [])
                    )
                    if sc > 0:
                        if fn.get("parent_class") in base_classes_in_file:
                            sc *= 1.25
                        if brain._graph and not fn.get("name", "").startswith("_"):
                            fn_nid = fn_id(rp, fn["name"], fn.get("parent_class"))
                            cross_prod_callers = sum(
                                1
                                for ei in brain._graph._radj.get(fn_nid, [])
                                if brain._graph.edges[ei].get("type") == "calls"
                                and brain._graph.edges[ei].get("confidence") in ("exact", "import_resolved")
                                and brain._graph.nodes.get(brain._graph.edges[ei]["src"], {}).get("rel_path", rp) != rp
                                and not _is_test_file(
                                    brain._graph.nodes.get(brain._graph.edges[ei]["src"], {}).get("rel_path", "")
                                )
                            )
                            if cross_prod_callers >= 1:
                                sc *= 1.0 + 0.14 * min(4, cross_prod_callers)
                        scored.append((sc, "fn", fn))
                for cls in info.get("classes", []):
                    sc = brain._score_class_for_query(cls, t_kw)
                    if sc > 0:
                        scored.append((sc, "cls", cls))
                scored.sort(key=lambda x: x[0], reverse=True)
                dedup_scored: list[tuple[float, str, dict]] = []
                method_name_counts: dict[str, int] = {}
                for item in scored:
                    s_fn = item[2]
                    s_st, s_en = s_fn.get("line_start", 1), s_fn.get("line_end", 1)
                    if any(
                        o_fn.get("line_start", 1) <= s_st
                        and s_en <= o_fn.get("line_end", 1)
                        and (o_fn.get("line_end", 1) - o_fn.get("line_start", 1)) <= 180
                        or (
                            s_st <= o_fn.get("line_start", 1)
                            and o_fn.get("line_end", 1) <= s_en
                            and (s_en - s_st) <= 180
                        )
                        for _, _, o_fn in dedup_scored
                    ):
                        continue
                    if item[1] == "fn" and s_fn.get("parent_class"):
                        m_nm = s_fn.get("name", "")
                        if method_name_counts.get(m_nm, 0) >= 2:
                            continue
                        method_name_counts[m_nm] = method_name_counts.get(m_nm, 0) + 1
                    dedup_scored.append(item)
                if file_rank == 0 and dedup_scored:
                    rank0_top_sym_score = dedup_scored[0][0]
                elif (
                    file_rank >= 2
                    and rp not in explicit_task_files
                    and rank0_top_sym_score > 0
                    and (not dedup_scored or dedup_scored[0][0] < rank0_top_sym_score * 0.45)
                ):
                    continue
                step2_ranked_files.add(rp)
                primary_files.add(rp)
                top_k = 1 if (had_initial_explicit or file_rank > 1 or len(tasks) >= 3) else 2
                if not had_initial_explicit and len(tasks) == 1:
                    if file_rank == 0:
                        if len(dedup_scored) >= 4 and dedup_scored[3][0] >= dedup_scored[0][0] * 0.72:
                            top_k = 4
                        elif len(dedup_scored) >= 3 and dedup_scored[2][0] >= dedup_scored[0][0] * 0.48:
                            top_k = 3
                    elif file_rank == 1:
                        if len(dedup_scored) >= 3 and dedup_scored[2][0] >= dedup_scored[0][0] * 0.65:
                            top_k = max(top_k, 3)
                        elif len(dedup_scored) >= 2 and dedup_scored[1][0] >= dedup_scored[0][0] * 0.60:
                            top_k = max(top_k, 2)
                multi_file_budget = len(tasks) >= 3 and len(ranked) >= 3
                for rank_idx, (sc, kind, sym) in enumerate(dedup_scored[:top_k]):
                    nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                    sym_span = sym.get("line_end", 1) - sym.get("line_start", 1)
                    rank_max_l = (
                        (42 if multi_file_budget else (52 if sym_span <= 450 else 44))
                        if rank_idx == 0
                        else (30 if rank_idx == 1 else 24)
                    )
                    rank_max_g = 6 if multi_file_budget else (14 if file_rank == 0 else 8)
                    _add_node_slice(nid, f"top match in {rp}", t_kw, sec, max_l=rank_max_l, max_g=rank_max_g)
                    if (
                        (not had_executable_explicit or rp in explicit_task_files)
                        and nid not in explicit_nodes
                        and len(explicit_nodes) < 6
                    ):
                        explicit_nodes.append(nid)
                        added_for_sym = 0
                        for ei_c in brain._graph._radj.get(nid, []):
                            ec = brain._graph.edges[ei_c]
                            if ec["type"] == "calls" and ec.get("confidence") in ("exact", "import_resolved"):
                                c_src = ec["src"]
                                c_nd = brain._graph.nodes.get(c_src, {})
                                if c_nd.get("rel_path") == rp:
                                    c_lines = ec.get("lines") or ([ec.get("line", 0)] if ec.get("line") else [0])
                                    for call_ln in c_lines[:3]:
                                        same_file_callers_to_add.append((c_src, f"calls {sym['name']} @L{call_ln}", t_kw))
                                    added_for_sym += 1
                                    if added_for_sym >= 2:
                                        break

        if brain._graph and explicit_nodes:
            brain._graph._ensure_registries()

            def _is_reliable_call_edge(e_obj: dict) -> bool:
                conf = e_obj.get("confidence")
                if conf in ("exact", "import_resolved", "qualified"):
                    return True
                if conf == "candidate" and e_obj.get("type") == "calls" and brain._graph:
                    dst_nm = brain._graph.nodes.get(e_obj.get("dst", ""), {}).get("name") or ""
                    # Closed-world CHA: compound domain selector (>=2 subtokens) with unique definition (|defs(m)| == 1)
                    if (
                        len(dst_nm) >= 6
                        and len(_tokenize_symbol(dst_nm)) >= 2
                        and len(brain._graph._registry.get(dst_nm, ())) == 1
                    ):
                        return True
                return False

            chain_lines: list[str] = []
            expanded_nodes: list[tuple[str, str, list[str], int, int]] = []
            for seed_id in explicit_nodes[:6]:
                hop1_list: list[tuple[str, str, list[str], int, int]] = []
                hop2_cross: list[tuple[str, str, list[str], int, int]] = []
                hop2_same: list[tuple[str, str, list[str], int, int]] = []
                hop3_list: list[tuple[str, str, list[str], int, int]] = []
                bwd_expanded: list[tuple[str, str, list[str], int, int]] = []
                seed_nd = brain._graph.nodes.get(seed_id, {})
                seed_rel = seed_nd.get("rel_path", "")
                seed_s = seed_nd.get("line_start", -1)
                seed_e = seed_nd.get("line_end", -1)

                for ei in brain._graph._adj.get(seed_id, []):
                    e1 = brain._graph.edges[ei]
                    if e1["type"] == "state_flow":
                        dst1 = e1["dst"]
                        dst1_nd = brain._graph.nodes.get(dst1, {})
                        dst1_rel = dst1_nd.get("rel_path", "")
                        dst1_name = (dst1_nd.get("name") or "").lower()
                        dst1_toks = set(_tokenize_symbol(dst1_name))
                        if dst1_rel in top3_query_files and any(
                            w in dst1_name or w[:4] in dst1_name or w[:3] in dst1_toks for w in t_kw if len(w) >= 4
                        ):
                            hop1_list.append(
                                (dst1, f"reads state from {seed_id.split(':')[-1]}", t_kw + [dst1_name], 26, 0)
                            )
                        continue
                    if e1["type"] not in ("calls", "handles") or not (
                        _is_reliable_call_edge(e1)
                        if e1["type"] == "calls"
                        else e1.get("confidence") in ("exact", "import_resolved")
                    ):
                        continue
                    dst1 = e1["dst"]
                    dst1_nd = brain._graph.nodes.get(dst1, {})
                    dst1_rel = dst1_nd.get("rel_path", "")
                    if (seed_e - seed_s) > 200:
                        c_line = e1.get("line", 0)
                        dst1_low = (dst1_nd.get("name") or "").lower()
                        in_matched_cell = any(
                            sr == seed_rel and (se - ss) <= 130 and ss <= c_line <= se for sr, ss, se in seen_spans
                        )
                        kw_hit = any(w in dst1_low for w in t_kw if len(w) >= 4)
                        if not in_matched_cell and not kw_hit:
                            continue
                    dst1_s = dst1_nd.get("line_start", 0)
                    dst1_e = dst1_nd.get("line_end", 0)
                    if dst1_rel == seed_rel and seed_s <= dst1_s and dst1_e <= seed_e:
                        continue
                    chain_lines.append(f"- `{seed_id}` -[{e1['type']} (L{e1.get('line', '?')})]-> `{dst1}`")
                    dst1_name = (dst1_nd.get("name") or "").lower()
                    dst1_toks = set(_tokenize_symbol(dst1_name))
                    same_cls = bool(
                        seed_nd.get("parent_class") and dst1_nd.get("parent_class") == seed_nd.get("parent_class")
                    )
                    dst1_in_deg = sum(
                        1
                        for ei_r in brain._graph._radj.get(dst1, [])
                        if brain._graph.edges[ei_r].get("type") == "calls"
                        and brain._graph.edges[ei_r].get("confidence") in ("exact", "import_resolved")
                        and not _is_test_file(
                            brain._graph.nodes.get(brain._graph.edges[ei_r]["src"], {}).get("rel_path", "")
                        )
                    )
                    dst1_kw_overlap = any(
                        w in dst1_name or w[:4] in dst1_name or w[:3] in dst1_toks for w in t_kw if len(w) >= 4
                    )
                    if dst1_in_deg >= 6 and not dst1_kw_overlap and not same_cls:
                        continue
                    h1_max_l = 12 if (dst1_in_deg >= 4 and not dst1_kw_overlap and not same_cls) else 26
                    h1_reason = (
                        f"same-class method called by {seed_nd.get('parent_class')}.{seed_nd.get('name')}"
                        if same_cls
                        else f"called by {seed_id.split(':')[-1]}"
                    )
                    hop1_list.append((dst1, h1_reason, t_kw + [dst1_name], h1_max_l, 0))
                    for ei2 in brain._graph._adj.get(dst1, []):
                        e2 = brain._graph.edges[ei2]
                        if e2["type"] == "calls" and e2.get("confidence") in ("exact", "import_resolved"):
                            dst2 = e2["dst"]
                            dst2_nd = brain._graph.nodes.get(dst2, {})
                            dst2_rel = dst2_nd.get("rel_path", "")
                            chain_lines.append(f"  - `{dst1}` -[calls (L{e2.get('line', '?')})]-> `{dst2}`")
                            dst2_name = (dst2_nd.get("name") or "").lower()
                            dst2_toks = set(_tokenize_symbol(dst2_name))
                            dst2_in_deg = len(brain._graph._radj.get(dst2, []))
                            dst2_kw_overlap = any(
                                w in dst2_name or w[:4] in dst2_name or w[:3] in dst2_toks for w in t_kw if len(w) >= 4
                            )
                            if dst2_in_deg >= 5 and not dst2_kw_overlap:
                                continue
                            if dst2_rel != dst1_rel:
                                hop2_cross.append(
                                    (
                                        dst2,
                                        f"2-hop callee via {dst1.split(':')[-1]}",
                                        t_kw + [dst2_name],
                                        20,
                                        10 if seed_nd.get("type") == "route" else 0,
                                    )
                                )
                                for ei3 in brain._graph._adj.get(dst2, []):
                                    e3 = brain._graph.edges[ei3]
                                    if e3["type"] == "calls" and e3.get("confidence") in ("exact", "import_resolved"):
                                        dst3 = e3["dst"]
                                        dst3_nd = brain._graph.nodes.get(dst3, {})
                                        dst3_name = (dst3_nd.get("name") or "").lower()
                                        dst3_in_deg = len(brain._graph._radj.get(dst3, []))
                                        dst3_kw_overlap = any(
                                            w in dst3_name or w[:4] in dst3_name for w in t_kw if len(w) >= 4
                                        )
                                        chain_lines.append(
                                            f"    - `{dst2}` -[calls (L{e3.get('line', '?')})]-> `{dst3}`"
                                        )
                                        if dst3_in_deg < 4 or dst3_kw_overlap:
                                            hop3_list.append(
                                                (dst3, f"3-hop callee via {dst2.split(':')[-1]}", t_kw, 16, 0)
                                            )
                            elif (dst2_nd.get("line_end", 0) - dst2_nd.get("line_start", 0)) >= 5:
                                hop2_same.append(
                                    (dst2, f"2-hop callee via {dst1.split(':')[-1]}", t_kw + [dst2_name], 16, 0)
                                )

                fwd_expanded = hop1_list + hop2_cross + hop2_same + hop3_list

                seed_tokens = {
                    tok
                    for tok in _tokenize_symbol(seed_id.split(":")[-1])
                    if len(tok) >= 4 and tok not in ("mark", "handle", "save", "load", "init", "sync")
                }
                q_wants_tests_bwd = bool(set(t_kw) & _TEST_QUERY_TERMS)
                raw_callers: list[tuple[int, int, str, dict]] = []
                for ei in brain._graph._radj.get(seed_id, []):
                    e_in = brain._graph.edges[ei]
                    if e_in["type"] != "calls" or not _is_reliable_call_edge(e_in):
                        continue
                    cid = e_in["src"]
                    cnd = brain._graph.nodes.get(cid, {})
                    crel = cnd.get("rel_path", "")
                    c_is_test = 1 if (not q_wants_tests_bwd and _is_test_file(crel)) else 0
                    c_span = cnd.get("line_end", 1) - cnd.get("line_start", 1)
                    raw_callers.append((c_is_test, c_span, cid, e_in))
                raw_callers.sort(key=lambda x: (x[0], x[1]))

                caller_files_seen: set[str] = set()
                for c_is_test, c_span, caller_id, e_in in raw_callers:
                    caller_nd = brain._graph.nodes.get(caller_id, {})
                    caller_name = caller_nd.get("name", "")
                    caller_rel = caller_nd.get("rel_path", "")
                    c_lines = e_in.get("lines") or ([e_in.get("line", 0)] if e_in.get("line") else [0])
                    c_lines_str = ",".join(f"L{x}" for x in c_lines[:4]) if c_lines and c_lines[0] else "L?"
                    chain_lines.append(f"- `{caller_id}` ({c_lines_str}) -[calls]-> `{seed_id}`")
                    if c_is_test and caller_files_seen:
                        continue
                    if caller_rel in caller_files_seen and c_span > 150 and len(c_lines) <= 1:
                        continue
                    caller_files_seen.add(caller_rel)
                    for call_ln in c_lines[:3]:
                        bwd_expanded.append(
                            (
                                caller_id,
                                f"calls {seed_id.split(':')[-1]} @L{call_ln}",
                                [seed_id.split(":")[-1].lower()],
                                18,
                                0,
                            )
                        )
                        for ei_b2 in brain._graph._radj.get(caller_id, []):
                            eb2 = brain._graph.edges[ei_b2]
                            if eb2["type"] == "calls" and eb2.get("confidence") in ("exact", "import_resolved"):
                                c2_id = eb2["src"]
                                c2_nd = brain._graph.nodes.get(c2_id, {})
                                if not q_wants_tests_bwd and _is_test_file(c2_nd.get("rel_path", "")):
                                    continue
                                c2_span = c2_nd.get("line_end", 1) - c2_nd.get("line_start", 1)
                                if c2_span <= 120:
                                    bwd_expanded.append(
                                        (
                                            c2_id,
                                            f"calls {caller_name} @L{eb2.get('line', 0)}",
                                            [caller_name.lower()],
                                            16,
                                            0,
                                        )
                                    )
                    for dep_rel in brain._import_targets.get(caller_rel, []):
                        dep_tokens = {
                            tok.rstrip("s") for tok in _tokenize_symbol(Path(dep_rel).stem) if len(tok) >= 4
                        }
                        norm_seed = {tok.rstrip("s") for tok in seed_tokens}
                        if dep_tokens & norm_seed:
                            dep_info = brain._index.get(dep_rel, {})
                            cb_fns: list[dict] = []
                            for dfn in dep_info.get("functions", []):
                                dfn_tokens = {tok.rstrip("s") for tok in _tokenize_symbol(dfn["name"])}
                                span = dfn.get("line_end", 0) - dfn.get("line_start", 0)
                                if (dfn_tokens & norm_seed) and span < 40:
                                    cb_fns.append(dfn)
                            cb_names = {f["name"] for f in cb_fns}
                            caller_of_cb: list[dict] = [
                                dfn
                                for dfn in dep_info.get("functions", [])
                                if cb_names & set(dfn.get("calls", []))
                                and (dfn.get("line_end", 0) - dfn.get("line_start", 0)) < 300
                            ]
                            for dfn in (cb_fns + caller_of_cb)[:2]:
                                dnid = fn_id(dep_rel, dfn["name"], dfn.get("parent_class"))
                                bwd_expanded.append(
                                    (
                                        dnid,
                                        f"UI trigger in {dep_rel}",
                                        t_kw + list(seed_tokens) + [caller_name.lower()],
                                        22,
                                        6,
                                    )
                                )

                seed_sym = (brain._graph.nodes.get(seed_id, {}).get("name") or "").lower()
                if seed_rel and seed_sym and seed_id in explicit_nodes[:2]:
                    for imp_rel in brain._imported_by.get(seed_rel, []):
                        if not q_wants_tests_bwd and _is_test_file(imp_rel):
                            continue
                        if (
                            seed_sym in brain._blobs.get(imp_rel, "")
                            or seed_sym in brain._body_token_sets.get(imp_rel, set())
                        ):
                            imp_info = brain._index.get(imp_rel, {})
                            for ifn in imp_info.get("functions", []):
                                if seed_sym in [c.lower() for c in ifn.get("calls", [])]:
                                    inid = fn_id(imp_rel, ifn["name"], ifn.get("parent_class"))
                                    bwd_expanded.append((inid, f"invokes {seed_sym} in {imp_rel}", [seed_sym], 12, 0))

                expanded_nodes.extend(fwd_expanded[:8])
                expanded_nodes.extend(bwd_expanded[:8])

            for sc_id, sc_reason, sc_kw in same_file_callers_to_add[:3]:
                expanded_nodes.append((sc_id, sc_reason, sc_kw, 20, 0))

            for seed_id in explicit_nodes[:6]:
                s_nd = brain._graph.nodes.get(seed_id, {})
                t_zw = s_nd.get("transitive_z_write", [])
                t_eff = s_nd.get("transitive_effects", [])
                t_ex = s_nd.get("transitive_exits", [])
                prov = s_nd.get("effect_provenance", {})
                if t_zw or t_ex or "IO" in t_eff:
                    parts_eff = []
                    if t_zw:
                        parts_eff.append(f"?Z_transitive:[{', '.join(t_zw[:4])}]")
                    if t_eff:
                        parts_eff.append(f"Effects:[{', '.join(t_eff)}]")
                    if t_ex:
                        parts_eff.append(f"Exits:[{', '.join(t_ex[:3])}]")
                    sample_via = next(iter(prov.values()), "")
                    via_sfx = f" ({sample_via})" if sample_via else ""
                    chain_lines.append(f"- `{seed_id}` -[interprocedural_bubble]-> {' | '.join(parts_eff)}{via_sfx}")
                for ei_sf in brain._graph._adj.get(seed_id, []):
                    esf = brain._graph.edges[ei_sf]
                    if esf["type"] in ("state_flow", "http_flow"):
                        dst_rel = brain._graph.nodes.get(esf["dst"], {}).get("rel_path", "")
                        if not q_wants_tests_bwd and dst_rel and _is_test_file(dst_rel):
                            continue
                        chain_lines.append(f"- `{seed_id}` -[{esf['type']}: {esf.get('reason', '')}]-> `{esf['dst']}`")
                for ei_sfr in brain._graph._radj.get(seed_id, []):
                    esfr = brain._graph.edges[ei_sfr]
                    if esfr["type"] in ("state_flow", "http_flow"):
                        src_rel = brain._graph.nodes.get(esfr["src"], {}).get("rel_path", "")
                        if not q_wants_tests_bwd and src_rel and _is_test_file(src_rel):
                            continue
                        chain_lines.append(
                            f"- `{esfr['src']}` -[{esfr['type']}: {esfr.get('reason', '')}]-> `{seed_id}`"
                        )

            ppr_results = brain._graph.multiplex_ppr({sid: 1.0 for sid in explicit_nodes[:6]}, top_k=10)
            ppr_scores: dict[str, float] = {nid: prob for nid, prob, _ in ppr_results}
            existing_exp_ids = {item[0] for item in expanded_nodes}

            chop_hits = brain._graph.causal_symptom_chop(explicit_nodes[:6], clean_task_for_search)
            for chop_nid, chop_sym, chop_path in chop_hits[:2]:
                path_str = " -> ".join(f"`{p}`" for p in chop_path)
                chain_lines.append(f"- ?? **[Causal Symptom Chop]** {path_str} *(origin of `{chop_sym}`)*")
                ppr_scores[chop_nid] = max(ppr_scores.get(chop_nid, 0.0), 0.45)
                for u_hop, v_hop in zip(chop_path, chop_path[1:]):
                    u_nd = brain._graph.nodes.get(u_hop, {})
                    v_nd = brain._graph.nodes.get(v_hop, {})
                    u_rel = u_nd.get("rel_path", "")
                    v_nm = v_nd.get("name", v_hop.split(":")[-1])
                    if u_rel:
                        for ei_ch in brain._graph._adj.get(u_hop, []):
                            ech = brain._graph.edges[ei_ch]
                            if ech["dst"] == v_hop and ech.get("line") and ech["type"] == "calls":
                                ch_ln = int(ech["line"])
                                for p_ln in range(max(1, ch_ln - 1), ch_ln + 2):
                                    brain._last_pin_lines.setdefault(u_rel, set()).add(p_ln)
                                ch_msg = (
                                    f"# >>> [CAUSAL-CHOP L{ch_ln}] calls {v_nm} -> origin of {chop_sym} "
                                    f"({v_nd.get('rel_path', '')}:L{v_nd.get('line_start', 1)})"
                                )
                                ann_l = brain._last_line_annotations.setdefault(u_rel, {}).setdefault(ch_ln, [])
                                if ch_msg not in ann_l:
                                    ann_l.append(ch_msg)
                if chop_nid not in existing_exp_ids and chop_nid not in seen_slices:
                    expanded_nodes.insert(0, (chop_nid, f"Causal Symptom Chop origin ({chop_sym})", t_kw, 32, 0))
                    existing_exp_ids.add(chop_nid)
            for p_nid, p_prob, p_rsn in ppr_results[:6]:
                p_rel = brain._graph.nodes.get(p_nid, {}).get("rel_path", "")
                if not q_wants_tests_bwd and p_rel and _is_test_file(p_rel):
                    continue
                if (
                    p_nid not in existing_exp_ids
                    and p_nid not in seen_slices
                    and ("http_flow" in p_rsn or "table:" in p_rsn or p_prob >= 0.085)
                ):
                    expanded_nodes.append((p_nid, p_rsn, t_kw, 28, 0))
                    existing_exp_ids.add(p_nid)

            if chain_lines and not zoom and monolith_4d_cells_count == 0:
                sec.append("### Static AST Call, State-Channel & Interprocedural Flow Edges")
                sec.extend(list(dict.fromkeys(chain_lines))[:12])
            emitted_hops = 99 if (zoom and had_initial_explicit) else 0
            max_hops_allowed = (
                (3 if not had_initial_explicit else 1)
                if (zoom or monolith_4d_cells_count > 0 or len(filtered_explicit) >= 2)
                else 3
            )

            covered_features: set[str] = set()
            for s_id in seen_slices:
                s_obj = brain._graph.nodes.get(s_id, {})
                for zw in s_obj.get("local_z_write", []):
                    covered_features.add(f"zw:{zw.lower()}")
                for ex in s_obj.get("exit_contracts", []):
                    covered_features.add(f"ex:{ex.lower()}")

            remaining_cands: list[tuple[str, str, list[str], int, int]] = []
            seen_cand_ids: set[str] = set()
            seed_call_freq: dict[str, int] = {}
            for item in expanded_nodes:
                if item[1].startswith(("same-class method called by ", "called by ", "calls ", "2-hop callee")):
                    seed_call_freq[item[0]] = seed_call_freq.get(item[0], 0) + 1
                nd_cand = brain._graph.nodes.get(item[0], {})
                cand_span = nd_cand.get("line_end", 1) - nd_cand.get("line_start", 1)
                m_cl = re.search(r"@L(\d+)", item[1])
                cand_key = f"{item[0]}@L{m_cl.group(1)}" if (m_cl and cand_span > 120) else item[0]
                if cand_key not in seen_cand_ids and cand_key not in seen_slices and item[0] in brain._graph.nodes:
                    seen_cand_ids.add(cand_key)
                    remaining_cands.append(item)
            ordered_cands: list[tuple[str, str, list[str], int, int]] = []
            while remaining_cands:
                best_idx = 0
                best_ratio = -1e9
                for c_idx, item in enumerate(remaining_cands):
                    c_nid, c_rsn, _, c_max_l, _ = item
                    nd_obj = brain._graph.nodes.get(c_nid, {})
                    nd_nm = (nd_obj.get("name") or "").lower()
                    nd_rel = nd_obj.get("rel_path", "")
                    nd_s = nd_obj.get("line_start", 1)
                    nd_e = nd_obj.get("line_end", nd_s)
                    nd_span = max(1, nd_e - nd_s)

                    s_plan = brain._ledger.plan_slice_intervals(nd_rel, nd_s, nd_e, commit=False)
                    unseen_lines = sum(ue - us + 1 for us, ue in s_plan["unseen_intervals"])
                    cost_u = max(4.0, float(min(c_max_l, unseen_lines if unseen_lines > 0 else nd_span)))

                    gain = 0.0
                    nd_tokens = set(_tokenize_symbol(nd_nm))
                    for w in t_kw:
                        if w in ("handled", "handling", "work", "works", "working", "across"):
                            continue
                        if len(w) >= 4 and (w in nd_nm or w[:4] in nd_nm or w[:3] in nd_tokens):
                            feat_k = f"kw:{w}"
                            gain += (6.0 if feat_k not in covered_features else 1.5) * brain._idf.get(w, 1.0)
                    for zw in nd_obj.get("local_z_write", [])[:2]:
                        feat_zw = f"zw:{zw.lower()}"
                        gain += 1.4 if feat_zw not in covered_features else 0.2
                    for ex in nd_obj.get("exit_contracts", [])[:1]:
                        feat_ex = f"ex:{ex.lower()}"
                        gain += 1.2 if feat_ex not in covered_features else 0.2
                    feat_file = f"file:{nd_rel}"
                    if nd_rel in top3_query_files:
                        gain += 5.0 if feat_file not in covered_features else 1.5
                    if c_rsn.startswith("same-class method called by "):
                        gain += 22.0 + 8.0 * min(3, seed_call_freq.get(c_nid, 1) - 1)
                    elif c_rsn.startswith("called by "):
                        gain += 6.5 + 8.0 * min(3, seed_call_freq.get(c_nid, 1) - 1)
                    elif c_rsn.startswith("calls ") and nd_rel in primary_files:
                        gain += 6.0 + 6.5 * min(3, seed_call_freq.get(c_nid, 1) - 1)
                    elif not c_rsn.startswith(("2-hop", "3-hop")):
                        gain += 3.5
                    if "http_flow" in c_rsn or "state_flow" in c_rsn:
                        gain += 4.5
                    feat_ppr = f"ppr:{c_nid}"
                    if feat_ppr not in covered_features:
                        gain += 25.0 * ppr_scores.get(c_nid, 0.0)
                    if nd_span < 8 or nd_nm.startswith("_"):
                        gain *= 0.40

                    cost_exp = 0.20 if c_rsn.startswith("same-class method called by ") else 0.55
                    ratio = gain / (cost_u**cost_exp)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_idx = c_idx

                chosen = remaining_cands.pop(best_idx)
                ordered_cands.append(chosen)
                ch_obj = brain._graph.nodes.get(chosen[0], {})
                ch_nm = (ch_obj.get("name") or "").lower()
                ch_rel = ch_obj.get("rel_path", "")
                if ch_rel:
                    covered_features.add(f"file:{ch_rel}")
                covered_features.add(f"ppr:{chosen[0]}")
                for w in t_kw:
                    if len(w) >= 4 and (w in ch_nm or w[:4] in ch_nm):
                        covered_features.add(f"kw:{w}")
                for zw in ch_obj.get("local_z_write", []):
                    covered_features.add(f"zw:{zw.lower()}")
                for ex in ch_obj.get("exit_contracts", []):
                    covered_features.add(f"ex:{ex.lower()}")

            hop_pointers: list[str] = []
            monolith_call_wins = 0
            for enid, ereason, ekw, emax_l, emax_g in ordered_cands:
                if not brain._graph or enid not in brain._graph.nodes:
                    continue
                end_node = brain._graph.nodes[enid]
                full_node_span = end_node.get("line_end", 1) - end_node.get("line_start", 1)
                m_cl = re.search(r"@L(\d+)", ereason)
                cand_key = f"{enid}@L{m_cl.group(1)}" if (m_cl and full_node_span > 120) else enid
                if cand_key in seen_slices:
                    continue
                enm = (end_node.get("name") or "").lower()
                enm_tokens = set(_tokenize_symbol(enm))
                erel = end_node.get("rel_path", "")
                is_monolith_call = (
                    bool(m_cl)
                    and full_node_span > 120
                    and (erel in primary_files or erel in top3_query_files)
                    and monolith_call_wins < 2
                )
                espan = 12 if is_monolith_call else full_node_span
                sec_lines_now = sum(len(x.splitlines()) for x in sec)
                is_chop = "Causal Symptom Chop" in ereason
                is_same_cls_callee = ereason.startswith("same-class method called by ")
                is_direct_callee = is_same_cls_callee or ereason.startswith(("called by ", "calls "))
                kw_direct = (
                    is_chop
                    or is_monolith_call
                    or is_direct_callee
                    or any(w in enm or w[:4] in enm or w[:3] in enm_tokens for w in t_kw if len(w) >= 4)
                    or ("http_flow" in ereason)
                    or ("state_flow" in ereason)
                    or ("reads state from" in ereason)
                    or ("table:" in ereason)
                )
                sec_cap_direct = 185 if len(tasks) >= 3 else 310
                hop_cap = (
                    (max_hops_allowed + 1)
                    if (is_same_cls_callee and len(tasks) == 1 and not zoom)
                    else max_hops_allowed
                )
                if (
                    (emitted_hops < hop_cap or is_chop or is_monolith_call)
                    and (sec_lines_now < 140 or (kw_direct and sec_lines_now < sec_cap_direct))
                    and espan <= 120
                ):
                    sec_len_before = len(sec)
                    _add_node_slice(enid, ereason, ekw, sec, max_l=min(emax_l, 26 if is_chop else 22), max_g=0)
                    if len(sec) > sec_len_before:
                        if is_monolith_call:
                            monolith_call_wins += 1
                        elif not is_chop:
                            emitted_hops += 1
                else:
                    sig_str = f" `{end_node['signature']}`" if end_node.get("signature") else ""
                    ptr = f"- `{erel} :: {_node_display_name(end_node)}` (L{end_node.get('line_start', 1)}-L{end_node.get('line_end', 1)}){sig_str} — {ereason}"
                    if ptr not in hop_pointers:
                        hop_pointers.append(ptr)
            if hop_pointers:
                sec.append("### Connected Call-Graph Coordinate Map (1-hop/2-hop neighbors)")
                sec.extend(hop_pointers[:6])
        diag_lines, diag_nodes = brain._compile_6d_contract_diagnostics(
            task, primary_files | set(emitted_by_file.keys()) | top3_query_files
        )
        if diag_lines and not zoom:
            for d_nid in diag_nodes[:2]:
                _add_node_slice(d_nid, "6D contract violation site", t_kw, sec, max_l=58, max_g=0)
            diag_block = ["### Static 6D Contract, Schema & Dialect Diagnostics"] + diag_lines[:10]
            sec[1:1] = diag_block
        md_sections.extend(sec)

    for imp_file in impact_list:
        norm_f = brain._normalize_rel(imp_file)
        if norm_f in seen_impacts:
            continue
        seen_impacts.add(norm_f)
        imp_res = brain.impact([norm_f])
        details = imp_res.get("details", [{}])[0]
        prod_c = details.get("direct_consumers", [])
        test_c = details.get("test_consumers", [])
        all_c = imp_res.get("all_impacted_files", [])
        src_c = [f for f in all_c if f.startswith("src/")]
        md_sections.append(f"## Blast Radius / Direct Importers of `{norm_f}`")
        md_sections.append(
            f"- **Total Direct Importers in `src/`:** **{len(src_c)} files** "
            f"({len(prod_c)} production files + {len(test_c)} test files)"
        )
        md_sections.append(
            "- **Production Importers (`" + str(len(prod_c)) + "`):** " + ", ".join(f"`{p}`" for p in prod_c)
        )
        if test_c:
            md_sections.append(
                "- **Test Importers (`" + str(len(test_c)) + "`):** " + ", ".join(f"`{p}`" for p in test_c)
            )

    current_md = "\n".join(md_sections)
    current_lines = len(current_md.splitlines())
    if current_lines < 520 and not zoom:
        symptom_sec: list[str] = []
        _GENERIC_SCAN_WORDS = {
            "how", "what", "where", "when", "why", "who", "does", "is", "are", "the", "and", "for", "with",
            "from", "into", "state", "session", "usersession", "user", "app", "data", "database", "db",
            "save", "saved", "saving", "track", "tracked", "tracking", "store", "stored", "file", "files",
            "function", "code", "flow", "work", "works", "handled", "manage", "managed", "context", "sync",
            "id",
        }
        all_t_kw = {w for t in tasks for w in _extract_keywords(t)}
        specific_t_kw = {w for w in all_t_kw if w not in _GENERIC_SCAN_WORDS and len(w) >= 3}
        all_tasks_low = " ".join(tasks).lower()
        state_keys: list[str] = []
        for m_key in re.finditer(r"(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]{6,45})['\"]", current_md):
            k_val = m_key.group(1)
            if k_val in state_keys:
                continue
            k_low = k_val.lower()
            k_toks = {tok for tok in re.split(r"[_\-./]+", k_low) if len(tok) >= 3 and tok not in _GENERIC_SCAN_WORDS}
            if k_low in all_tasks_low or (specific_t_kw and (k_toks & specific_t_kw)):
                state_keys.append(k_val)
        for m_nav in re.finditer(r"Navigate\s+to=['\"](/[^'\"]{2,30})['\"]", current_md):
            n_val = m_nav.group(1)
            n_low = n_val.lower()
            n_toks = {tok for tok in re.split(r"[_\-./]+", n_low) if len(tok) >= 3 and tok not in _GENERIC_SCAN_WORDS}
            if n_val not in state_keys and (n_low in all_tasks_low or (specific_t_kw and (n_toks & specific_t_kw))):
                state_keys.append(n_val)
        state_keys.sort(
            key=lambda x: (x.lower() in all_tasks_low, any(w in x.lower() for w in specific_t_kw), "_" in x, len(x)),
            reverse=True,
        )

        wants_cross_scan = matched_4d_cells_count == 0 or any(
            w in all_tasks_low
            for w in (
                "storage",
                "localstorage",
                "persist",
                "cloud",
                "kv",
                "route",
                "redirect",
                "navigate",
                "onboarding",
                "across",
                "sync",
            )
        )
        if state_keys and wants_cross_scan:
            target_keys = state_keys[:2]
            xref_body: list[str] = []
            xref_line_count = 2
            test_hit_files: list[str] = []
            omitted_prod_hits: list[str] = []
            for sk in target_keys:
                for rp in sorted(brain._index.keys()):
                    raw_txt = brain._get_file_text(rp)
                    if raw_txt is None or sk not in raw_txt:
                        continue
                    rp_low = rp.lower()
                    if any(t_marker in rp_low for t_marker in ("test", "spec", "__tests__", "eval/")):
                        if rp not in test_hit_files:
                            test_hit_files.append(rp)
                        continue
                    flines = raw_txt.splitlines()
                    hit_lines = [ln_no for ln_no, l_txt in enumerate(flines, start=1) if sk in l_txt]
                    for h_idx, h_ln in enumerate(hit_lines):
                        if any(sr == rp and ss <= h_ln <= se and (se - ss) <= 60 for sr, ss, se in seen_spans):
                            continue
                        if h_idx >= 3 or xref_line_count >= 64 or current_lines + xref_line_count > 520:
                            omitted_prod_hits.append(f"{rp}:L{h_ln}")
                            continue
                        w_s = max(1, h_ln - 3)
                        w_e = min(len(flines), h_ln + 7)
                        seen_spans.append((rp, w_s, w_e))
                        win_code = slice_lines(brain.root, rp, w_s, w_e, max_lines=12)
                        if win_code:
                            emitted_by_file.setdefault(rp, []).extend(extract_emitted_intervals(win_code))
                            xref_body.append(f"### [{rp} (L{w_s}-L{w_e}) — references `{sk}` @L{h_ln}]")
                            xref_body.append(win_code)
                            xref_line_count += 1 + len(win_code.splitlines())
            if xref_body or omitted_prod_hits:
                if not omitted_prod_hits:
                    xref_header = [
                        "## Closed-World Exhaustive Repo Scan (100% File Coverage)",
                        f"> **EXHAUSTIVE GUARANTEE:** Scanned all `{len(brain._index)}` repository files. Every production occurrence of "
                        + ", ".join(f"`{k}`" for k in target_keys)
                        + " across the codebase is shown below. Do NOT read other files to search for additional occurrences.",
                    ]
                else:
                    xref_header = [
                        "## Cross-File Repo Scan (Truncated to Fit Dossier Budget)",
                        f"> **PARTIAL SCAN NOTE:** Scanned all `{len(brain._index)}` repository files for "
                        + ", ".join(f"`{k}`" for k in target_keys)
                        + f". Showing top slices below; {len(omitted_prod_hits)} additional production occurrence(s) omitted: "
                        + ", ".join(f"`{h}`" for h in omitted_prod_hits[:12]),
                    ]
                symptom_sec.extend(xref_header)
                symptom_sec.extend(xref_body)
                if test_hit_files:
                    symptom_sec.append(
                        f"- **Also referenced in {len(test_hit_files)} test file(s):** "
                        + ", ".join(f"`{tf}`" for tf in test_hit_files[:10])
                    )

        map_pointer_lines: list[str] = []
        for idx, task in enumerate(tasks, start=1):
            clean_t = task.strip()
            t_kw = _extract_keywords(clean_t)
            if not t_kw:
                continue
            more_ranked = brain.query(" ".join(t_kw), top_n=5)
            top1_score = more_ranked[0]["score"] if more_ranked else 1.0
            for r in more_ranked:
                rp = r["rel_path"]
                if rp in step2_ranked_files:
                    continue
                info = brain._index.get(rp, {})
                behaviors = brain._get_file_behaviors(rp)
                scored = []
                for fn in info.get("functions", []):
                    sc = brain._score_symbol_for_query(fn, t_kw, behaviors, info.get("routes", []))
                    if sc > 0:
                        scored.append((sc, "fn", fn))
                for cls in info.get("classes", []):
                    sc = brain._score_class_for_query(cls, t_kw)
                    if sc > 0:
                        scored.append((sc, "cls", cls))
                scored.sort(key=lambda x: x[0], reverse=True)
                if not scored:
                    continue
                if (
                    not had_initial_explicit
                    and matched_4d_cells_count == 0
                    and current_lines + sum(len(x.splitlines()) for x in symptom_sec) < 90
                    and r["score"] >= top1_score * 0.55
                ):
                    sc, kind, sym = scored[0]
                    nid = fn_id(rp, sym["name"], sym.get("parent_class")) if kind == "fn" else cls_id(rp, sym["name"])
                    _add_node_slice(nid, f"secondary match in {rp} (Task {idx})", t_kw, symptom_sec, max_l=24, max_g=6)
                    ptrs = [
                        f"`{_node_display_name(s)}` (L{s.get('line_start', 1)}-L{s.get('line_end', 1)})"
                        for _, _, s in scored[:4]
                    ]
                    line_ptr = f"- `{rp}` :: " + ", ".join(ptrs)
                    if line_ptr not in map_pointer_lines:
                        map_pointer_lines.append(line_ptr)
        if map_pointer_lines:
            symptom_sec.append("### Secondary Candidate Coordinate Map (Use `projectbrain slice <file>:<symbol>` if needed)")
            symptom_sec.extend(map_pointer_lines[:6])
        if symptom_sec:
            md_sections.extend(symptom_sec)
        current_lines_after = len("\n".join(md_sections).splitlines())
        if current_lines_after < 775:
            md_sections.append(
                "\n> **Tools:** `brain_slice` (`<file>:<symbol>`, `focus=\"<var>\"`) | `brain_explore` (`<symbol_or_route>`) | `brain_check` (`files_modified=[...]`) | `brain_remember` (`<target>`, `<note>`)"
            )

    full_md = "\n".join(md_sections)
    target_file: Path | None = None
    if out_path or write_file:
        target_file = Path(out_path) if out_path else (brain.root / ".projectbrain_dossier.md")
        target_file.write_text(full_md, encoding="utf-8")
    total_lines = len(full_md.splitlines())
    for f_rel, ivs in emitted_by_file.items():
        brain._ledger.commit_slice_intervals(f_rel, ivs)
    covered_files = {sr for sr, _, _ in seen_spans} | seen_globals
    credited_files = primary_files if primary_files else covered_files
    closed_world_verified = "EXHAUSTIVE GUARANTEE" in full_md
    brain._ledger.record_input_op(
        op="dossier",
        query=" | ".join(tasks),
        files_covered=len(credited_files),
        touched_files=credited_files,
        slices_included=len(seen_slices),
        cells_4d_resolved=matched_4d_cells_count,
        closed_world_verified=closed_world_verified,
    )
    return {
        "dossier_path": str(target_file) if target_file else None,
        "total_lines": total_lines,
        "total_chars": len(full_md),
        "slices_included": len(seen_slices),
        "files_globals_included": len(seen_globals),
        "cells_6d_resolved": matched_4d_cells_count,
        "cells_4d_resolved": matched_4d_cells_count,
        "files_resolved": len(credited_files),
        "closed_world_verified": closed_world_verified,
        "markdown": full_md,
    }
