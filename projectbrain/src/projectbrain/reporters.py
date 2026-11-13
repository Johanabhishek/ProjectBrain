"""Output Reporters (`text`, `json`, `markdown`, `pr-comment`, `github`, `sarif`) for `projectbrain check`.

Includes:
  1. OASIS SARIF v2.1.0 static analysis report (`--format sarif`) compatible with GitHub Code Scanning,
     GitLab SAST, and IDE SARIF viewers.
  2. Zero-Noise Sticky PR Markdown (`--format pr-comment`) with `<!-- projectbrain-pr-guardrail -->`
     marker and sanitized Mermaid `graph LR` blast-radius visualization.
  3. GitHub Actions workflow annotations (`--format github`), rich Markdown (`--format markdown`),
     and Ruff-style terminal output (`--format text`).
"""
from __future__ import annotations

import hashlib
import json
import re

from . import __version__
from .memory import extract_note_rel_path

PR_COMMENT_MARKER = "<!-- projectbrain-pr-guardrail -->"

_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_INFO_URI = "https://github.com/Johanabhishek/ProjectBrain"

_SARIF_RULE_CATALOG: dict[str, dict] = {
    "PB001": {
        "id": "PB001",
        "name": "SyntaxError",
        "shortDescription": {"text": "AST/CST syntax error in modified source file"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB002": {
        "id": "PB002",
        "name": "BoundaryViolation",
        "shortDescription": {"text": "Forbidden architectural layer dependency (--forbid)"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB003": {
        "id": "PB003",
        "name": "CallerArityViolation",
        "shortDescription": {"text": "Cross-file caller arity or keyword-argument mismatch"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB004": {
        "id": "PB004",
        "name": "ReturnShapeViolation",
        "shortDescription": {"text": "Cross-file return-key or destructuring contract mismatch"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB005": {
        "id": "PB005",
        "name": "BrokenImportOrDeletedSymbol",
        "shortDescription": {"text": "Broken import, deleted/renamed callable, or deleted/renamed file consumer"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB006": {
        "id": "PB006",
        "name": "DataOrSchemaContractViolation",
        "shortDescription": {"text": "6D SQL schema, SQL dialect, disjoint type, or HTTP route contract violation"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB007": {
        "id": "PB007",
        "name": "GovernanceTamperViolation",
        "shortDescription": {"text": "Unauthorized removal or weakening of .projectbrain_memory.json governance"},
        "defaultConfiguration": {"level": "error"},
        "helpUri": _INFO_URI,
    },
    "PB008": {
        "id": "PB008",
        "name": "StaleInvariantWarning",
        "shortDescription": {"text": "Pinned AST invariant target mutated, renamed, or deleted"},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": _INFO_URI,
    },
    "PB009": {
        "id": "PB009",
        "name": "SignatureBlastAdvisory",
        "shortDescription": {"text": "Public symbol signature changed with downstream external callers"},
        "defaultConfiguration": {"level": "note"},
        "helpUri": _INFO_URI,
    },
    "PB010": {
        "id": "PB010",
        "name": "CoChangeDriftWarning",
        "shortDescription": {"text": "Coupled co-change partner file was not updated in this diff"},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": _INFO_URI,
    },
}

_BACKTICK_LOC_RE = re.compile(r"`([^`\r\n]+?\.[A-Za-z0-9]{1,8}):L?(\d+)(?::(\d+))?`")
_FILE_LINE_COL_RE = re.compile(
    r"(?<![\w./\\+\[\]()@$%-])((?:[\w./\\+\[\]()@$%-]+)\.[A-Za-z0-9]{1,8}):L?(\d+)(?::(\d+))?"
)


_TEST_DIR_SEGMENTS = frozenset({
    "tests", "test", "testing", "__tests__", "spec", "specs", "e2e",
    "unit_test", "integration_test", "e2e_test", "smoke_test",
})
_TEST_DIR_PREFIXES = ("test_", "tests_", "spec_", "specs_", "test-", "tests-", "spec-", "specs-")
_TEST_DIR_SUFFIXES = ("_tests", "_specs", "-test", "-tests", "-spec", "-specs")


def _clean_uri(raw_path: str | None, fallback: str = ".projectbrain_memory.json") -> str:
    uri = (raw_path or "").strip().replace("\\", "/").lstrip("/")
    while uri.startswith("./"):
        uri = uri[2:]
    return uri or fallback


def _is_test_dir_segment(seg: str) -> bool:
    low = seg.lower()
    return (
        low in _TEST_DIR_SEGMENTS
        or low.startswith(_TEST_DIR_PREFIXES)
        or low.endswith(_TEST_DIR_SUFFIXES)
    )


def _is_test_file_path(raw_path: str) -> bool:
    """Return True if `raw_path` matches common test directory or filename conventions."""
    norm = _clean_uri(raw_path, fallback="")
    if not norm:
        return False
    parts = [seg for seg in norm.split("/") if seg]
    if any(_is_test_dir_segment(seg) for seg in parts[:-1]):
        return True
    fname = parts[-1]
    fname_low = fname.lower()
    if fname_low == "conftest.py" or ".test." in fname_low or ".spec." in fname_low or ".cy." in fname_low:
        return True
    dot_idx = fname_low.rfind(".")
    stem = fname[:dot_idx] if dot_idx > 0 else fname
    stem_low = stem.lower()
    if (
        stem_low in ("test", "tests", "spec", "specs")
        or stem_low.startswith(("test_", "spec_"))
        or stem_low.endswith(("_test", "_tests", "_spec", "_specs", ".test", ".spec"))
    ):
        return True
    if re.search(r"[a-z0-9](?:Test|Tests|Spec|Specs)\.(?:java|kt|cs|php|scala|swift|groovy)$", fname):
        return True
    return False


def _norm_line_key(ln_raw: object) -> int | str:
    if isinstance(ln_raw, int) and not isinstance(ln_raw, bool) and ln_raw > 0:
        return ln_raw
    if isinstance(ln_raw, str):
        s = ln_raw.strip().lstrip("Ll")
        if s.isdigit() and int(s) > 0:
            return int(s)
        return s or "?"
    return "?"


def _dedup_caller_entries(callers: list[dict], *, exclude_file: str = "") -> list[dict]:
    """Deduplicate caller records by `(file, line)` while preserving deterministic order."""
    out: list[dict] = []
    seen: set[tuple[str, int | str]] = set()
    norm_excl = _clean_uri(exclude_file, fallback="") if exclude_file else ""
    for c in callers:
        cf = _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="?")
        if norm_excl and cf == norm_excl:
            continue
        ln_key = _norm_line_key(c.get("line"))
        key = (cf, ln_key)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _match_location(msg: str) -> tuple[str, int, int | None] | None:
    for rx in (_BACKTICK_LOC_RE, _FILE_LINE_COL_RE):
        m = rx.search(msg)
        if m:
            uri = _clean_uri(m.group(1), fallback="")
            if uri:
                line = max(1, int(m.group(2)))
                col = max(1, int(m.group(3))) if m.group(3) else None
                return uri, line, col
    return None


def _extract_location(msg: str, fallback_uri: str = "REPOSITORY") -> tuple[str, int, int | None]:
    hit = _match_location(msg)
    if hit is not None:
        return hit
    return _clean_uri(fallback_uri, fallback="REPOSITORY"), 1, None


def _classify_diag_rule_id(msg: str) -> str:
    if msg.startswith("[SYNTAX ERROR]"):
        return "PB001"
    if "[BOUNDARY VIOLATION]" in msg:
        return "PB002"
    if "[GOVERNANCE TAMPER]" in msg:
        return "PB007"
    if "Caller Arity Contract" in msg or "Caller Keyword Contract" in msg:
        return "PB003"
    if "Return-Shape Key Contract" in msg:
        return "PB004"
    if (
        "Broken Import" in msg
        or "Deleted Symbol" in msg
        or "Deleted/Renamed Callable" in msg
        or "Deleted/Renamed File Import" in msg
    ):
        return "PB005"
    return "PB006"


def _fingerprint(rule_id: str, uri: str, line: int, message: str) -> str:
    digest = hashlib.sha256(f"{rule_id}|{uri}|{line}|{message}".encode("utf-8")).hexdigest()
    return digest[:32]


def format_sarif_report(res: dict) -> str:
    """Format `brain.check()` output as OASIS SARIF v2.1.0 JSON."""
    diags: list[str] = res.get("contract_diagnostics", [])
    b_violations: list[dict] = res.get("boundary_violations", [])
    tampers: list[dict] = res.get("governance_tamper", [])
    tamper_allowed = bool(res.get("governance_tamper_allowed", False))
    stale_memories: list[dict] = res.get("stale_memories", [])
    sig_blast: list[dict] = res.get("signature_blast", [])
    co_warnings: list[dict] = res.get("co_change_warnings", [])
    imp: dict = res.get("impact_analysis", {})
    details: list[dict] = imp.get("details", [])
    default_uri = details[0]["rel_path"] if details and details[0].get("rel_path") else ".projectbrain_memory.json"

    raw_results: list[tuple[str, str, str, int, int | None, str]] = []
    consumed_msgs: set[str] = set()

    # 1. Boundary violations (exact structured file + line)
    for bv in b_violations:
        msg = bv.get("message", "Boundary violation")
        consumed_msgs.add(msg)
        uri = _clean_uri(bv.get("src_file"), fallback=default_uri)
        ln = max(1, int(bv.get("line") or 1))
        raw_results.append(("PB002", "error", uri, ln, None, msg))

    # 2. Governance tamper violations
    for tp in tampers:
        msg = tp.get("message", "Governance tamper detected")
        consumed_msgs.add(msg)
        lvl = "note" if tamper_allowed else "error"
        uri = _clean_uri(tp.get("file"), fallback=".projectbrain_memory.json")
        ln = max(1, int(tp.get("line") or 1))
        raw_results.append(("PB007", lvl, uri, ln, None, msg))

    # 3. Remaining contract, syntax, and deleted/renamed file diagnostics
    for d in diags:
        if d in consumed_msgs:
            continue
        consumed_msgs.add(d)
        rid = _classify_diag_rule_id(d)
        uri, ln, col = _extract_location(d, fallback_uri=default_uri)
        raw_results.append((rid, "error", uri, ln, col, d))

    # 4. Stale AST invariants (warnings)
    for sm in stale_memories:
        uri = _clean_uri(extract_note_rel_path(sm), fallback=".projectbrain_memory.json")
        warn_txt = sm.get("stale_warning") or "Target symbol AST changed since invariant was pinned."
        msg = f"[{sm.get('target_id', '?')}] {sm.get('note', '')} ({warn_txt})"
        raw_results.append(("PB008", "warning", uri, 1, None, msg))

    # 5. Public signature mutation advisories
    for sb in sig_blast:
        uri = _clean_uri(sb.get("file"), fallback=default_uri)
        caller_cnt = len(_dedup_caller_entries(sb.get("callers") or []))
        cons_cnt = len(sb.get("consumers") or [])
        for mut in (sb.get("mutations") or [])[:6]:
            ln = max(1, int(mut.get("line") or 1))
            if "new_signature" in mut:
                if mut.get("old_signature") == mut.get("new_signature"):
                    m_txt = (
                        f"Return/interface contract of `{mut.get('symbol')}` updated "
                        f"(`{mut.get('new_signature')}`); affects {caller_cnt} caller(s), {cons_cnt} importer(s)."
                    )
                else:
                    m_txt = (
                        f"Signature of `{mut.get('symbol')}` changed (`{mut.get('old_signature')}` -> "
                        f"`{mut.get('new_signature')}`); affects {caller_cnt} caller(s), {cons_cnt} importer(s)."
                    )
            else:
                m_txt = (
                    f"Public symbol `{mut.get('symbol')}` (`{mut.get('old_signature')}`) removed; "
                    f"affects {caller_cnt} caller(s), {cons_cnt} importer(s)."
                )
            raw_results.append(("PB009", "note", uri, ln, None, m_txt))

    # 6. Co-change drift warnings
    for cw in co_warnings:
        uri = _clean_uri(cw.get("trigger_file"), fallback=default_uri)
        msg = cw.get("message") or f"Modified without coupled partner `{cw.get('missing_partner')}`."
        raw_results.append(("PB010", "warning", uri, 1, None, msg))

    used_rule_ids: list[str] = []
    for rid, *_ in raw_results:
        if rid not in used_rule_ids and rid in _SARIF_RULE_CATALOG:
            used_rule_ids.append(rid)

    driver_rules = [_SARIF_RULE_CATALOG[rid] for rid in used_rule_ids]
    rule_index_map = {rid: idx for idx, rid in enumerate(used_rule_ids)}

    sarif_results: list[dict] = []
    for rid, level, uri, ln, col, msg in raw_results:
        region: dict = {"startLine": max(1, int(ln))}
        if col is not None and col >= 1:
            region["startColumn"] = int(col)
        sarif_results.append({
            "ruleId": rid,
            "ruleIndex": rule_index_map[rid],
            "level": level,
            "message": {"text": msg},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": uri},
                        "region": region,
                    }
                }
            ],
            "partialFingerprints": {
                "primaryLocationLineHash": _fingerprint(rid, uri, ln, msg),
            },
        })

    sarif_doc = {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ProjectBrain",
                        "version": __version__,
                        "informationUri": _INFO_URI,
                        "rules": driver_rules,
                    }
                },
                "results": sarif_results,
            }
        ],
    }
    return json.dumps(sarif_doc, indent=2)


def _mermaid_label(text: str) -> str:
    cleaned = (
        str(text)
        .replace("\\", "/")
        .replace('"', "'")
        .replace("`", "'")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace(";", ",")
        .strip()
    )
    return cleaned[:64]


def build_mermaid_blast_diagram(res: dict, *, max_nodes: int = 16, max_edges: int = 24) -> str:
    """Build a sanitized Mermaid `graph LR` blast-radius diagram for PR comments."""
    imp: dict = res.get("impact_analysis", {})
    details: list[dict] = imp.get("details", [])
    b_violations: list[dict] = res.get("boundary_violations", [])
    del_records: list[dict] = res.get("deleted_file_violations", [])
    git_diff: dict = res.get("git_diff", {})
    deleted_files: list[str] = git_diff.get("deleted_files", [])
    renamed_files: dict[str, str] = git_diff.get("renamed_files", {})
    diags: list[str] = res.get("contract_diagnostics", [])

    node_ids: dict[str, str] = {}
    node_classes: dict[str, str] = {}
    edges: list[tuple[str, str, str, str]] = []  # (src, dst, arrow, label)
    overflow_seen: set[str] = set()

    CLASS_RANK = {"consumer": 1, "test": 2, "modified": 3, "deleted": 4, "violation": 5}
    violating_files: set[str] = set()
    for bv in b_violations:
        if bv.get("src_file"):
            violating_files.add(_clean_uri(bv["src_file"], fallback=""))
    for d in diags:
        hit = _match_location(d)
        if hit is not None:
            violating_files.add(hit[0])

    deleted_set: set[str] = {
        _clean_uri(old_f, fallback="")
        for old_f in deleted_files
        if _clean_uri(old_f, fallback="")
    } | {
        _clean_uri(dr.get("missing_file", ""), fallback="")
        for dr in del_records
        if _clean_uri(dr.get("missing_file", ""), fallback="")
    }
    known_test_files: set[str] = {
        _clean_uri(tf, fallback="")
        for det in details
        for tf in det.get("test_consumers", [])
        if _clean_uri(tf, fallback="")
    }

    def _external_role(path_str: str) -> str:
        if path_str in violating_files:
            return "violation"
        if path_str in deleted_set:
            return "deleted"
        if path_str in known_test_files or _is_test_file_path(path_str):
            return "test"
        return "consumer"

    def _register_node(label: str, cls_name: str) -> str | None:
        if label in node_ids:
            cur_cls = node_classes.get(label, "consumer")
            if CLASS_RANK.get(cls_name, 0) > CLASS_RANK.get(cur_cls, 0):
                node_classes[label] = cls_name
            return node_ids[label]
        if len(node_ids) >= max_nodes:
            overflow_seen.add(label)
            return None
        nid = f"N{len(node_ids)}"
        node_ids[label] = nid
        node_classes[label] = cls_name
        return nid

    def _has_edge_between(src_lbl: str, dst_lbl: str) -> bool:
        s_id = node_ids.get(src_lbl)
        d_id = node_ids.get(dst_lbl)
        if not s_id or not d_id:
            return False
        return any(e_src == s_id and e_dst == d_id for e_src, e_dst, _, _ in edges)

    def _add_edge(src_lbl: str, src_cls: str, dst_lbl: str, dst_cls: str, arrow: str, edge_lbl: str) -> None:
        if src_lbl == dst_lbl or len(edges) >= max_edges:
            return
        s_id = _register_node(src_lbl, src_cls)
        d_id = _register_node(dst_lbl, dst_cls)
        if not s_id or not d_id:
            return
        key = (s_id, d_id, arrow, edge_lbl)
        if key not in edges:
            edges.append(key)

    # Register modified files first so they always get slots
    for det in details:
        rp = _clean_uri(det.get("rel_path", ""), fallback="")
        if not rp or det.get("status") == "not_indexed":
            continue
        cls = "violation" if rp in violating_files else "modified"
        _register_node(rp, cls)

    for old_f in deleted_files[:4]:
        norm_old = _clean_uri(old_f, fallback="")
        if norm_old:
            _register_node(norm_old, "deleted")

    for new_f, old_f in list(renamed_files.items())[:4]:
        norm_new = _clean_uri(new_f, fallback="")
        norm_old = _clean_uri(old_f, fallback="")
        if norm_old and norm_new:
            _add_edge(norm_old, "deleted", norm_new, "modified", "-.->", "renamed")

    for bv in b_violations[:8]:
        sf = _clean_uri(bv.get("src_file", ""), fallback="")
        df = _clean_uri(bv.get("dst_file", ""), fallback="")
        if sf and df:
            _add_edge(sf, "violation", df, _external_role(df), "==>", "FORBIDDEN")

    for dr in del_records[:6]:
        imp_f = _clean_uri(dr.get("importer_file", ""), fallback="")
        miss_f = _clean_uri(dr.get("missing_file", ""), fallback="")
        if imp_f and miss_f:
            _add_edge(imp_f, "violation", miss_f, "deleted", "==>", "broken import")

    for det in details:
        rp = _clean_uri(det.get("rel_path", ""), fallback="")
        if not rp or det.get("status") == "not_indexed":
            continue
        rp_cls = "violation" if rp in violating_files else "modified"
        seen_caller_files: set[str] = set()
        added_callers = 0
        for c in det.get("called_by", []):
            if added_callers >= 6:
                break
            cf = _clean_uri(c.get("caller_file") or c.get("in_file") or "", fallback="")
            if cf and cf != rp and cf not in seen_caller_files:
                seen_caller_files.add(cf)
                added_callers += 1
                fn_lbl = c.get("callee") or c.get("function") or ""
                edge_txt = f"calls {fn_lbl}()" if fn_lbl else "calls"
                _add_edge(cf, _external_role(cf), rp, rp_cls, "-->", edge_txt)
        added_consumers = 0
        for raw_cons in det.get("direct_consumers", []):
            if added_consumers >= 5:
                break
            cons = _clean_uri(raw_cons, fallback="")
            if cons and cons != rp and cons not in seen_caller_files and not _has_edge_between(cons, rp):
                seen_caller_files.add(cons)
                added_consumers += 1
                _add_edge(cons, _external_role(cons), rp, rp_cls, "-->", "imports")
        added_tests = 0
        for raw_tf in det.get("test_consumers", []):
            if added_tests >= 3:
                break
            tf = _clean_uri(raw_tf, fallback="")
            if tf and tf != rp and tf not in seen_caller_files and not _has_edge_between(tf, rp):
                seen_caller_files.add(tf)
                added_tests += 1
                _add_edge(tf, _external_role(tf), rp, rp_cls, "-.->", "tests")

    if not node_ids:
        return ""

    lines = ["```mermaid", "graph LR"]
    for label, nid in node_ids.items():
        lines.append(f'  {nid}["{_mermaid_label(label)}"]')
    if overflow_seen:
        lines.append(f'  N_MORE["+{len(overflow_seen)} more connected file(s)"]')
    for s_id, d_id, arrow, elbl in edges:
        if elbl:
            safe_e = _mermaid_label(elbl).replace("|", "/")
            lines.append(f'  {s_id} {arrow}|"{safe_e}"| {d_id}')
        else:
            lines.append(f"  {s_id} {arrow} {d_id}")

    lines.append("  classDef modified fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px;")
    lines.append("  classDef violation fill:#fee2e2,stroke:#dc2626,color:#7f1d1d,stroke-width:2px;")
    lines.append("  classDef deleted fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-dasharray: 4 4;")
    lines.append("  classDef consumer fill:#f3f4f6,stroke:#6b7280,color:#111827;")
    lines.append("  classDef test fill:#dcfce7,stroke:#16a34a,color:#14532d;")
    for label, nid in node_ids.items():
        cls = node_classes.get(label, "consumer")
        lines.append(f"  class {nid} {cls};")
    if overflow_seen:
        lines.append("  class N_MORE consumer;")
    lines.append("```")
    return "\n".join(lines)


def format_pr_comment_report(res: dict) -> str:
    """Format `brain.check()` as Zero-Noise Sticky PR Markdown with Mermaid blast-radius graph."""
    verdict = res.get("verdict", "SAFE_TO_PROCEED")
    all_valid = res.get("all_contracts_valid", True)
    diags: list[str] = res.get("contract_diagnostics", [])
    tampers: list[dict] = res.get("governance_tamper", [])
    tamper_allowed = bool(res.get("governance_tamper_allowed", False))
    imp: dict = res.get("impact_analysis", {})
    details: list[dict] = imp.get("details", [])
    memories: list[dict] = res.get("memories", [])
    stale_memories: list[dict] = res.get("stale_memories", [])
    sig_blast: list[dict] = res.get("signature_blast", [])
    co_warnings: list[dict] = res.get("co_change_warnings", [])
    git_diff: dict = res.get("git_diff", {})
    base_info: dict = res.get("base_ref_info", {})

    deleted_files: list[str] = git_diff.get("deleted_files", [])
    renamed_files: dict[str, str] = git_diff.get("renamed_files", {})
    files_audited = imp.get("files_analyzed", len(details)) + len(deleted_files)
    impact_zone = imp.get("total_impact_zone", 0)
    has_external_impact = any(
        _dedup_caller_entries(d.get("called_by", []), exclude_file=d.get("rel_path", ""))
        or d.get("direct_consumers")
        for d in details
    )

    # Zero-Noise Sticky PR Guarantee: when clean with no external blast radius, signature changes,
    # stale invariants, governance changes, deleted/renamed files, or co-change drift, emit a
    # single-line green pass status under the sticky marker.
    if (
        all_valid
        and not diags
        and not tampers
        and not stale_memories
        and not sig_blast
        and not co_warnings
        and not deleted_files
        and not renamed_files
        and not has_external_impact
    ):
        base_lbl = f" vs `{base_info['resolved_ref']}`" if base_info.get("resolved_ref") else ""
        return (
            f"{PR_COMMENT_MARKER}\n"
            f"✅ **ProjectBrain Guardrail — PASS** · `{files_audited}` file(s) audited{base_lbl} · "
            f"`0` contract/boundary violations · low blast radius."
        )

    badge = "✅ PASS" if all_valid else "❌ ACTION REQUIRED"
    lines: list[str] = [
        PR_COMMENT_MARKER,
        f"## ProjectBrain PR Guardrail — {badge}",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Verdict** | `{verdict}` |",
    ]
    if base_info.get("resolved_ref"):
        mb_short = (base_info.get("merge_base") or "")[:8]
        mb_sfx = f" (`{mb_short}`)" if mb_short else ""
        lines.append(f"| **Base Reference** | `{base_info['resolved_ref']}`{mb_sfx} |")
    lines.append(f"| **Files Audited** | `{files_audited}` (`{impact_zone}` downstream consumer file(s)) |")
    lines.append(f"| **Blocking Violations** | `{len(diags)}` |")
    lines.append(
        f"| **Advisories** | `{len(stale_memories) + len(co_warnings) + len(sig_blast)}` |"
    )

    if deleted_files or renamed_files:
        change_notes: list[str] = []
        for nf, of in list(renamed_files.items())[:6]:
            change_notes.append(f"renamed `{of}` → `{nf}`")
        for df in deleted_files[:6]:
            change_notes.append(f"deleted `{df}`")
        if change_notes:
            lines.append(f"| **Structural File Ops** | {', '.join(change_notes)} |")

    if diags:
        lines.append("\n### ❌ Blocking Contract, Boundary & Governance Violations")
        for d in diags[:15]:
            clean_d = d[2:].strip() if d.startswith("- ") else d
            lines.append(f"- {clean_d}")

    if tampers and tamper_allowed:
        lines.append("\n### 🔓 Governance Changes Explicitly Allowed (`--allow-governance-change`)")
        for tp in tampers[:8]:
            lines.append(f"- {tp.get('message')}")

    mermaid = build_mermaid_blast_diagram(res)
    if mermaid:
        lines.append("\n### 🕸️ Blast-Radius & Dependency Graph")
        lines.append(mermaid)

    if sig_blast:
        lines.append("\n### 🔔 Public Signature Mutations")
        for sb in sig_blast[:8]:
            for mut in sb.get("mutations", [])[:4]:
                if "new_signature" in mut:
                    if mut.get("old_signature") == mut.get("new_signature"):
                        lines.append(
                            f"- **`{sb['file']}`** — return/interface contract updated for `{mut['new_signature']}`"
                        )
                    else:
                        lines.append(
                            f"- **`{sb['file']}`** — `{mut['old_signature']}` → `{mut['new_signature']}`"
                        )
                else:
                    lines.append(
                        f"- **`{sb['file']}`** — removed `{mut['old_signature']}`"
                    )
            if sb.get("callers"):
                dedup_sb_callers = _dedup_caller_entries(sb["callers"], exclude_file=sb.get("file", ""))
                if dedup_sb_callers:
                    c_locs = ", ".join(
                        f"`{_clean_uri(c.get('caller_file') or c.get('in_file'), fallback='?')}:L{_norm_line_key(c.get('line'))}`"
                        for c in dedup_sb_callers[:5]
                    )
                    lines.append(f"  - Downstream callers to verify: {c_locs}")

    if stale_memories or memories:
        lines.append("\n### 📌 AST-Anchored Architectural Invariants")
        seen_m: set[str] = set()
        for m in [*stale_memories, *memories]:
            mid = m.get("id", m.get("target_id", ""))
            if mid in seen_m:
                continue
            seen_m.add(mid)
            icon = "⚠️ **[STALE — RE-VERIFY]**" if m.get("stale") else "📌 **[PINNED]**"
            lines.append(f"- {icon} `{m.get('target_id')}`: {m.get('note')}")

    if co_warnings:
        lines.append("\n### 🔗 Co-Change Drift Advisories")
        for cw in co_warnings[:8]:
            lines.append(f"- ⚠️ {cw.get('message')}")

    tests = sorted({t for d in details for t in d.get("test_consumers", [])})
    if tests:
        lines.extend([
            "",
            f"<details><summary>🧪 <b>Covering Test Suite ({len(tests)} file(s))</b></summary>",
            "",
            "```bash",
            f"pytest {' '.join(tests[:12])}",
            "```",
            "</details>",
        ])

    return "\n".join(lines)


def format_check_report(res: dict, fmt: str = "text") -> str:
    """Format `brain.check()` output as `text`, `json`, `markdown`, `pr-comment`, `github`, or `sarif`."""
    if fmt == "json":
        return json.dumps(res, indent=2)
    if fmt == "sarif":
        return format_sarif_report(res)
    if fmt == "pr-comment":
        return format_pr_comment_report(res)

    verdict = res.get("verdict", "SAFE_TO_PROCEED")
    all_valid = res.get("all_contracts_valid", True)
    diags: list[str] = res.get("contract_diagnostics", [])
    b_violations: list[dict] = res.get("boundary_violations", [])
    tampers: list[dict] = res.get("governance_tamper", [])
    tamper_allowed = bool(res.get("governance_tamper_allowed", False))
    imp: dict = res.get("impact_analysis", {})
    details: list[dict] = imp.get("details", [])
    memories: list[dict] = res.get("memories", [])
    stale_memories: list[dict] = res.get("stale_memories", [])
    sig_blast: list[dict] = res.get("signature_blast", [])
    co_warnings: list[dict] = res.get("co_change_warnings", [])
    health: dict = res.get("health", {})

    if fmt == "github":
        lines: list[str] = []
        consumed: set[str] = set()
        for bv in b_violations:
            sf = bv.get("src_file", "")
            ln = bv.get("line", 1)
            msg = bv.get("message", "")
            consumed.add(msg)
            lines.append(f"::error file={sf},line={ln},title=ProjectBrain Boundary Violation::{msg}")
        if not tamper_allowed:
            for tp in tampers:
                msg = tp.get("message", "")
                consumed.add(msg)
                lines.append(
                    f"::error file={tp.get('file', '.projectbrain_memory.json')},line=1,"
                    f"title=ProjectBrain Governance Tamper::{msg}"
                )
        for d in diags:
            if d in consumed:
                continue
            hit = _match_location(d)
            if hit:
                lines.append(f"::error file={hit[0]},line={hit[1]},title=ProjectBrain Contract Violation::{d}")
            else:
                lines.append(f"::error title=ProjectBrain Contract Violation::{d}")
        for sm in stale_memories:
            rf = extract_note_rel_path(sm)
            loc_part = f" file={rf}," if rf else " "
            lines.append(
                f"::warning{loc_part}title=Stale AST Invariant::({sm.get('target_id')}) {sm.get('note')}"
            )
        for cw in co_warnings:
            tf = cw.get("trigger_file") or ""
            loc_part = f" file={tf}," if tf else " "
            lines.append(
                f"::warning{loc_part}title=Co-Change Drift::{cw.get('message')}"
            )
        status_txt = "PASS" if all_valid else "FAILED"
        lines.append(
            f"ProjectBrain CI Gate: {status_txt} (verdict={verdict}, diagnostics={len(diags)}, stale_invariants={len(stale_memories)})"
        )
        return "\n".join(lines)

    if fmt == "markdown":
        badge = "PASS" if all_valid else "ACTION REQUIRED"
        md: list[str] = [
            f"## ProjectBrain Blast-Radius & Contract Report — `{badge}`",
            "",
            f"- **Verdict:** `{verdict}`",
            f"- **Files Analyzed:** `{imp.get('files_analyzed', len(details))}` | **Impact Zone:** `{imp.get('total_impact_zone', 0)}` file(s)",
        ]
        if diags:
            md.append("\n### Contract, Syntax & Boundary Violations")
            for d in diags:
                md.append(f"- ❌ {d}")
        if details:
            high_callers = [
                d for d in details
                if _dedup_caller_entries(d.get("called_by", []), exclude_file=d.get("rel_path", ""))
                or d.get("direct_consumers")
            ]
            if high_callers:
                md.append("\n### Downstream Blast Radius")
                for d in high_callers:
                    dedup_c = _dedup_caller_entries(d.get("called_by", []), exclude_file=d.get("rel_path", ""))
                    callers_str = ", ".join(
                        f"`{_clean_uri(c.get('caller_file') or c.get('in_file'), fallback='?')}:L{_norm_line_key(c.get('line'))}` (`{c.get('caller') or c.get('called_by') or '?'}`)"
                        for c in dedup_c[:6]
                    ) or ", ".join(f"`{_clean_uri(c, fallback='?')}`" for c in d.get("direct_consumers", [])[:6])
                    md.append(f"- **`{d['rel_path']}`** (`{d.get('risk', 'low')}` risk) → {callers_str}")
        if sig_blast:
            md.append("\n### Public Signature Mutations")
            for sb in sig_blast[:8]:
                for mut in sb.get("mutations", [])[:4]:
                    if "new_signature" in mut:
                        if mut.get("old_signature") == mut.get("new_signature"):
                            md.append(f"- 🔔 `{sb['file']}`: return/interface contract updated for `{mut['new_signature']}`")
                        else:
                            md.append(f"- 🔔 `{sb['file']}`: `{mut['old_signature']}` → `{mut['new_signature']}`")
                    else:
                        md.append(f"- 🔔 `{sb['file']}`: removed `{mut['old_signature']}`")
        if memories or stale_memories:
            md.append("\n### Architectural Invariants")
            seen_m: set[str] = set()
            for m in [*stale_memories, *memories]:
                mid = m.get("id", m.get("target_id", ""))
                if mid in seen_m:
                    continue
                seen_m.add(mid)
                icon = "⚠️ **[STALE — RE-VERIFY]**" if m.get("stale") else "📌 **[PINNED]**"
                md.append(f"- {icon} `{m.get('target_id')}`: {m.get('note')}")
        if co_warnings:
            md.append("\n### Co-Change Drift Advisories")
            for cw in co_warnings[:8]:
                md.append(f"- ⚠️ {cw.get('message')}")
        tests = sorted({t for d in details for t in d.get("test_consumers", [])})
        if tests:
            md.append(f"\n### Affected Tests\n```bash\npytest {' '.join(tests[:10])}\n```")
        return "\n".join(md)

    # Default `text` (Ruff-style scannable terminal output)
    out: list[str] = []
    status_lbl = "PASS" if all_valid else "VIOLATION"
    if details or diags or co_warnings or sig_blast:
        out.append(f"ProjectBrain Check [{status_lbl}] — Verdict: {verdict}")
    elif health:
        out.append(
            f"ProjectBrain Check [{status_lbl}] — {health.get('files_indexed', 0)} files indexed "
            f"({health.get('graph_nodes', 0)} nodes, {health.get('graph_edges', 0)} edges, "
            f"{health.get('memories_count', 0)} invariants)"
        )
    else:
        out.append(f"ProjectBrain Check [{status_lbl}]")

    for d in diags:
        out.append(f"  error: {d}")

    for sb in sig_blast:
        for mut in sb.get("mutations", [])[:4]:
            if "new_signature" in mut:
                if mut.get("old_signature") == mut.get("new_signature"):
                    out.append(
                        f"  signature-changed: {sb['file']} :: return/interface contract updated for `{mut['new_signature']}`"
                    )
                else:
                    out.append(f"  signature-changed: {sb['file']} :: `{mut['old_signature']}` -> `{mut['new_signature']}`")
            else:
                out.append(f"  symbol-removed: {sb['file']} :: `{mut['old_signature']}`")

    for d in details:
        callers = _dedup_caller_entries(d.get("called_by", []), exclude_file=d.get("rel_path", ""))
        consumers = d.get("direct_consumers", [])
        if callers:
            c_fmt = ", ".join(
                f"{_clean_uri(c.get('caller_file') or c.get('in_file'), fallback='?')}:L{_norm_line_key(c.get('line'))}"
                for c in callers[:5]
            )
            out.append(f"  impact: {d['rel_path']} [{d.get('risk', 'low')}] -> called by {c_fmt}")
        elif consumers:
            out.append(f"  impact: {d['rel_path']} [{d.get('risk', 'low')}] -> imported by {', '.join(consumers[:5])}")

    seen_ids: set[str] = set()
    for m in [*stale_memories, *memories]:
        mid = m.get("id", m.get("target_id", ""))
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        tag = "stale-invariant" if m.get("stale") else "invariant"
        out.append(f"  {tag}: [{m.get('target_id')}] {m.get('note')}")

    for cw in co_warnings[:6]:
        out.append(f"  warning: {cw.get('message')}")

    tests = sorted({t for d in details for t in d.get("test_consumers", [])})
    if tests:
        out.append(f"  tests: pytest {' '.join(tests[:8])}")

    if health and health.get("partial_files"):
        out.append(f"  partial-parse: {', '.join(health['partial_files'][:5])}")

    return "\n".join(out)
