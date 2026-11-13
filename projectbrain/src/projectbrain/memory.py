"""Persistent AST-anchored repository memory & architectural boundary rules for ProjectBrain.

Allows agents and developers to store:
  1. Architectural invariants, bug root causes, and non-obvious rules anchored to specific
     graph nodes (file:..., fn:..., cls:...). Each note stores the target node's `ast_hash`
     at creation time; when the underlying symbol's AST changes, the memory is automatically
     flagged as `stale: True` so agents and CI know to re-verify it.
  2. Deterministic structural boundary rules (`forbid`: e.g. `routes/* -> db/*`) verified
     statically against resolved cross-file import and call edges in `<20ms`.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def _compile_path_glob(pattern: str) -> re.Pattern:
    """Compile a POSIX path glob (supporting `*`, `**`, `?`, and `dir/*` subtree matching) into a regex."""
    norm = pattern.strip().replace("\\", "/").strip("/")
    if not norm or norm in ("*", "**"):
        return re.compile(r"^.*$")
    # Treat trailing `/*` as matching any file at or beneath `dir/` (intuitive architectural layer glob)
    if norm.endswith("/*") and "**" not in norm[:-2]:
        norm = norm[:-2] + "/**"
    parts: list[str] = ["^"]
    i = 0
    n = len(norm)
    while i < n:
        ch = norm[i]
        if ch == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                    continue
                parts.append(".*")
                i += 2
                continue
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        elif ch in ".+^$(){}[]|\\-":
            parts.append("\\" + ch)
        else:
            parts.append(ch)
        i += 1
    parts.append("$")
    return re.compile("".join(parts), re.IGNORECASE)


def parse_forbid_spec(spec: str) -> tuple[str, str]:
    """Parse `'src_glob -> dst_glob'` (or `'src_glob => dst_glob'`) into `(src_glob, dst_glob)`."""
    for sep in ("->", "=>", "↛"):
        if sep in spec:
            left, right = spec.split(sep, 1)
            s_glob = left.strip().replace("\\", "/")
            d_glob = right.strip().replace("\\", "/")
            if s_glob and d_glob:
                return s_glob, d_glob
    raise ValueError(
        f"Invalid boundary rule '{spec}'. Expected '<source_glob> -> <target_glob>' (e.g. 'routes/* -> db/*')."
    )


def _clean_rel_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def extract_note_rel_path(entry: dict) -> str:
    """Resolve the repository-relative file path for a memory note from `rel_path` or `target_id`."""
    rpath = _clean_rel_path(str(entry.get("rel_path") or ""))
    if rpath:
        return rpath
    tid = str(entry.get("target_id") or "").strip()
    if ":" in tid:
        parts = tid.split(":")
        if len(parts) >= 3 and parts[0] in ("fn", "cls"):
            return _clean_rel_path(parts[1])
        if len(parts) == 2 and parts[0] == "file":
            return _clean_rel_path(parts[1])
    return ""


def parse_co_change_spec(spec: str) -> tuple[str, str, bool]:
    """Parse `'file_a <-> file_b'` (bidirectional) or `'file_a -> file_b'` (directional)."""
    for sep in ("<->", "<=>", "=="):
        if sep in spec:
            left, right = spec.split(sep, 1)
            fa = _clean_rel_path(left)
            fb = _clean_rel_path(right)
            if fa and fb and fa != fb:
                return fa, fb, True
    for sep in ("->", "=>"):
        if sep in spec:
            left, right = spec.split(sep, 1)
            fa = _clean_rel_path(left)
            fb = _clean_rel_path(right)
            if fa and fb and fa != fb:
                return fa, fb, False
    raise ValueError(
        f"Invalid co-change rule '{spec}'. Expected '<file_a> <-> <file_b>' or '<file_a> -> <file_b>'."
    )


class RepoMemory:
    """Manages persistent AST-anchored notes, boundary rules, & co-change rules in `.projectbrain_memory.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / ".projectbrain_memory.json"
        self._notes: list[dict] = []
        self._boundary_rules: list[dict] = []
        self._co_change_rules: list[dict] = []
        self._loaded_sig: tuple[int, int] | None = None
        self.load()

    def _disk_sig(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _ensure_fresh(self) -> None:
        if self._disk_sig() != self._loaded_sig:
            self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._notes = []
            self._boundary_rules = []
            self._co_change_rules = []
            self._loaded_sig = None
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._notes = data.get("notes", []) if isinstance(data.get("notes"), list) else []
                self._boundary_rules = (
                    data.get("boundary_rules", [])
                    if isinstance(data.get("boundary_rules"), list)
                    else []
                )
                self._co_change_rules = (
                    data.get("co_change_rules", [])
                    if isinstance(data.get("co_change_rules"), list)
                    else []
                )
            else:
                self._notes = []
                self._boundary_rules = []
                self._co_change_rules = []
            self._loaded_sig = self._disk_sig()
        except (json.JSONDecodeError, OSError):
            self._notes = []
            self._boundary_rules = []
            self._co_change_rules = []
            self._loaded_sig = self._disk_sig()

    def save(self) -> None:
        payload: dict = {
            "version": 2,
            "updated_at": round(time.time(), 2),
            "notes": self._notes,
        }
        if self._boundary_rules:
            payload["boundary_rules"] = self._boundary_rules
        if self._co_change_rules:
            payload["co_change_rules"] = self._co_change_rules
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._loaded_sig = self._disk_sig()
        except OSError:
            pass

    def remember(
        self,
        target_id: str,
        note: str,
        *,
        rel_path: str = "",
        ast_hash: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        """Record or update an architectural note anchored to a symbol or file."""
        self._ensure_fresh()
        target_id = target_id.strip()
        note = note.strip()
        for existing in self._notes:
            if existing.get("target_id") == target_id and existing.get("note") == note:
                existing["ast_hash"] = ast_hash or existing.get("ast_hash", "")
                if tags is not None:
                    existing["tags"] = tags
                existing["updated_at"] = round(time.time(), 2)
                self.save()
                return existing

        entry = {
            "id": f"mem_{len(self._notes) + 1}_{int(time.time()) % 10000}",
            "target_id": target_id,
            "rel_path": rel_path,
            "note": note,
            "ast_hash": ast_hash,
            "tags": tags or [],
            "created_at": round(time.time(), 2),
        }
        self._notes.append(entry)
        self.save()
        return entry

    def add_boundary_rule(
        self,
        src_glob: str,
        dst_glob: str | None = None,
        *,
        reason: str = "",
        edge_types: list[str] | None = None,
    ) -> dict:
        """Add or update a deterministic forbidden dependency boundary rule (`src_glob -> dst_glob`)."""
        self._ensure_fresh()
        if dst_glob is None:
            src_g, dst_g = parse_forbid_spec(src_glob)
        else:
            src_g = src_glob.strip().replace("\\", "/")
            dst_g = dst_glob.strip().replace("\\", "/")
            if not src_g or not dst_g:
                raise ValueError("Both src_glob and dst_glob must be non-empty.")

        norm_edges = sorted(set(edge_types or ["imports", "calls"]))
        reason_clean = reason.strip()

        for existing in self._boundary_rules:
            if existing.get("src_glob") == src_g and existing.get("dst_glob") == dst_g:
                if reason_clean:
                    existing["reason"] = reason_clean
                existing["edge_types"] = norm_edges
                existing["updated_at"] = round(time.time(), 2)
                self.save()
                return existing

        rule = {
            "id": f"rule_{len(self._boundary_rules) + 1}_{int(time.time()) % 10000}",
            "src_glob": src_g,
            "dst_glob": dst_g,
            "rule": f"{src_g} -> {dst_g}",
            "reason": reason_clean,
            "edge_types": norm_edges,
            "created_at": round(time.time(), 2),
        }
        self._boundary_rules.append(rule)
        self.save()
        return rule

    def list_boundary_rules(self) -> list[dict]:
        self._ensure_fresh()
        return [dict(r) for r in self._boundary_rules]

    def add_co_change_rule(
        self,
        file_a: str,
        file_b: str | None = None,
        *,
        bidirectional: bool = True,
        confidence: float = 1.0,
        co_change_count: int = 0,
        reason: str = "",
        mode: str = "advisory",
    ) -> dict:
        """Add or update a co-change coupling rule (`file_a <-> file_b` or `file_a -> file_b`)."""
        self._ensure_fresh()
        if file_b is None:
            fa, fb, bidir = parse_co_change_spec(file_a)
        else:
            fa = _clean_rel_path(file_a)
            fb = _clean_rel_path(file_b)
            bidir = bool(bidirectional)
            if not fa or not fb or fa == fb:
                raise ValueError("Both file_a and file_b must be distinct non-empty paths.")
        if bidir and fa > fb:
            fa, fb = fb, fa
        arrow = "<->" if bidir else "->"
        rule_lbl = f"{fa} {arrow} {fb}"
        reason_clean = reason.strip()

        for existing in self._co_change_rules:
            ex_a = existing.get("file_a", "")
            ex_b = existing.get("file_b", "")
            ex_bi = bool(existing.get("bidirectional", True))
            same_pair = (ex_a == fa and ex_b == fb) or (bidir and ex_bi and ex_a == fb and ex_b == fa)
            if same_pair:
                existing["file_a"] = fa
                existing["file_b"] = fb
                existing["bidirectional"] = bidir or ex_bi
                existing["rule"] = f"{fa} {'<->' if existing['bidirectional'] else '->'} {fb}"
                existing["confidence"] = round(float(confidence), 3)
                if co_change_count:
                    existing["co_change_count"] = int(co_change_count)
                if reason_clean:
                    existing["reason"] = reason_clean
                existing["mode"] = mode
                existing["updated_at"] = round(time.time(), 2)
                self.save()
                return existing

        entry = {
            "id": f"cochange_{len(self._co_change_rules) + 1}_{int(time.time()) % 10000}",
            "file_a": fa,
            "file_b": fb,
            "rule": rule_lbl,
            "bidirectional": bidir,
            "confidence": round(float(confidence), 3),
            "co_change_count": int(co_change_count),
            "reason": reason_clean,
            "mode": mode,
            "created_at": round(time.time(), 2),
        }
        self._co_change_rules.append(entry)
        self.save()
        return entry

    def list_co_change_rules(self) -> list[dict]:
        self._ensure_fresh()
        return [dict(r) for r in self._co_change_rules]

    def check_co_change_drift(
        self,
        modified_rels: set[str],
        existing_files: set[str] | None = None,
    ) -> list[dict]:
        """Check pinned `co_change_rules` for missing partner updates in `modified_rels`."""
        self._ensure_fresh()
        if not self._co_change_rules or not modified_rels:
            return []
        warnings: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for rule in self._co_change_rules:
            fa = rule.get("file_a", "")
            fb = rule.get("file_b", "")
            if not fa or not fb:
                continue
            bidir = bool(rule.get("bidirectional", True))
            pairs = [(fa, fb), (fb, fa)] if bidir else [(fa, fb)]
            for trig, partner in pairs:
                if trig in modified_rels and partner not in modified_rels:
                    if existing_files is not None and partner not in existing_files and not (self.root / partner).exists():
                        continue
                    if (trig, partner) in seen:
                        continue
                    seen.add((trig, partner))
                    conf = rule.get("confidence", 1.0)
                    pct = round(float(conf) * 100, 1)
                    rsn = rule.get("reason", "")
                    rsn_sfx = f" ({rsn})" if rsn else ""
                    warnings.append({
                        "rule_id": rule.get("id", rule.get("rule", "")),
                        "rule": rule.get("rule", f"{fa} <-> {fb}"),
                        "trigger_file": trig,
                        "missing_partner": partner,
                        "confidence": conf,
                        "co_change_pct": pct,
                        "co_change_count": rule.get("co_change_count", 0),
                        "mode": rule.get("mode", "advisory"),
                        "source": "pinned_rule",
                        "reason": rsn,
                        "message": (
                            f"[CO-CHANGE DRIFT] `{trig}` was modified without coupled partner `{partner}`"
                            f"{rsn_sfx}."
                        ),
                    })
        return warnings

    def forget(self, target_or_id: str) -> dict:
        """Remove memory notes, boundary rules, or co-change rules matching `id`, `target_id`, `rel_path`, or `rule`."""
        self._ensure_fresh()
        q = target_or_id.strip()
        q_norm = q.replace("\\", "/")
        before_n = len(self._notes)
        before_r = len(self._boundary_rules)
        before_c = len(self._co_change_rules)
        self._notes = [
            n for n in self._notes
            if n.get("id") != q
            and n.get("target_id") != q
            and n.get("rel_path") != q_norm
            and not str(n.get("target_id", "")).endswith(f":{q}")
        ]
        self._boundary_rules = [
            r for r in self._boundary_rules
            if r.get("id") != q and r.get("rule") != q_norm
        ]
        self._co_change_rules = [
            c for c in self._co_change_rules
            if c.get("id") != q
            and c.get("rule") != q_norm
            and c.get("file_a") != q_norm
            and c.get("file_b") != q_norm
        ]
        removed_notes = before_n - len(self._notes)
        removed_rules = before_r - len(self._boundary_rules)
        removed_co_changes = before_c - len(self._co_change_rules)
        if removed_notes or removed_rules or removed_co_changes:
            self.save()
        return {
            "removed_notes": removed_notes,
            "removed_rules": removed_rules,
            "removed_co_changes": removed_co_changes,
            "remaining_notes": len(self._notes),
            "remaining_rules": len(self._boundary_rules),
            "remaining_co_changes": len(self._co_change_rules),
        }

    def refresh_hashes(self, current_hashes: dict[str, str], target_or_id: str | None = None) -> int:
        """Re-anchor stale notes to current AST hashes once verified."""
        self._ensure_fresh()
        updated = 0
        q = target_or_id.strip() if target_or_id else None
        for entry in self._notes:
            tid = entry.get("target_id", "")
            if q and entry.get("id") != q and tid != q and not tid.endswith(f":{q}"):
                continue
            curr = current_hashes.get(tid, "")
            if curr and curr != entry.get("ast_hash", ""):
                entry["ast_hash"] = curr
                entry["updated_at"] = round(time.time(), 2)
                updated += 1
        if updated:
            self.save()
        return updated

    def check_boundary_violations(
        self,
        index: dict,
        import_targets: dict[str, list[str]],
        graph: object | None = None,
        focus_rels: set[str] | None = None,
    ) -> list[dict]:
        """Evaluate all boundary rules against resolved cross-file imports and strong call edges."""
        self._ensure_fresh()
        if not self._boundary_rules:
            return []

        compiled = [
            (
                r,
                _compile_path_glob(r.get("src_glob", "")),
                _compile_path_glob(r.get("dst_glob", "")),
                set(r.get("edge_types") or ("imports", "calls")),
            )
            for r in self._boundary_rules
            if r.get("src_glob") and r.get("dst_glob")
        ]
        if not compiled:
            return []

        violations: list[dict] = []
        seen_keys: set[tuple[str, str, str, str]] = set()

        # 1. File-level resolved imports
        for src_rel, dst_list in sorted(import_targets.items()):
            if src_rel not in index:
                continue
            for dst_rel in dst_list:
                if src_rel == dst_rel:
                    continue
                if focus_rels is not None and src_rel not in focus_rels and dst_rel not in focus_rels:
                    continue
                for rule, src_rx, dst_rx, allowed_types in compiled:
                    if "imports" not in allowed_types:
                        continue
                    if src_rx.match(src_rel) and dst_rx.match(dst_rel):
                        rid = rule.get("id", rule.get("rule", ""))
                        vkey = (rid, src_rel, dst_rel, "imports")
                        if vkey in seen_keys:
                            continue
                        seen_keys.add(vkey)
                        reason = rule.get("reason", "")
                        reason_sfx = f" ({reason})" if reason else ""
                        rule_lbl = rule.get("rule", f"{rule['src_glob']} -> {rule['dst_glob']}")
                        violations.append({
                            "rule_id": rid,
                            "rule": rule_lbl,
                            "src_file": src_rel,
                            "dst_file": dst_rel,
                            "edge_type": "imports",
                            "line": 1,
                            "symbol": None,
                            "reason": reason,
                            "message": (
                                f"[BOUNDARY VIOLATION] {src_rel} imports {dst_rel}, "
                                f"violating forbidden boundary `{rule_lbl}`{reason_sfx}."
                            ),
                        })

        # 2. Cross-file resolved call edges in CodeGraph
        nodes = getattr(graph, "nodes", None)
        edges = getattr(graph, "edges", None)
        if isinstance(nodes, dict) and isinstance(edges, list):
            for edge in edges:
                if edge.get("type") != "calls":
                    continue
                if edge.get("confidence") not in ("exact", "qualified", "import_resolved"):
                    continue
                src_nd = nodes.get(edge.get("src", ""), {})
                dst_nd = nodes.get(edge.get("dst", ""), {})
                src_rel = src_nd.get("rel_path", "")
                dst_rel = dst_nd.get("rel_path", "")
                if not src_rel or not dst_rel or src_rel == dst_rel:
                    continue
                if focus_rels is not None and src_rel not in focus_rels and dst_rel not in focus_rels:
                    continue
                for rule, src_rx, dst_rx, allowed_types in compiled:
                    if "calls" not in allowed_types:
                        continue
                    if src_rx.match(src_rel) and dst_rx.match(dst_rel):
                        rid = rule.get("id", rule.get("rule", ""))
                        # Skip duplicate call violation if same rule+file pair already caught via import
                        if (rid, src_rel, dst_rel, "imports") in seen_keys:
                            continue
                        vkey = (rid, src_rel, dst_rel, "calls")
                        if vkey in seen_keys:
                            continue
                        seen_keys.add(vkey)
                        line = edge.get("line") or src_nd.get("line_start") or 1
                        sym = f"{src_nd.get('name', '?')} -> {dst_nd.get('name', '?')}"
                        reason = rule.get("reason", "")
                        reason_sfx = f" ({reason})" if reason else ""
                        rule_lbl = rule.get("rule", f"{rule['src_glob']} -> {rule['dst_glob']}")
                        violations.append({
                            "rule_id": rid,
                            "rule": rule_lbl,
                            "src_file": src_rel,
                            "dst_file": dst_rel,
                            "edge_type": "calls",
                            "line": line,
                            "symbol": sym,
                            "reason": reason,
                            "message": (
                                f"[BOUNDARY VIOLATION] {src_rel}:L{line} ({sym}) calls into {dst_rel}, "
                                f"violating forbidden boundary `{rule_lbl}`{reason_sfx}."
                            ),
                        })

        return violations

    def get_notes_for(
        self,
        rel_paths: set[str],
        node_ids: set[str],
        current_hashes: dict[str, str],
        *,
        deleted_rels: set[str] | None = None,
        indexed_files: set[str] | None = None,
    ) -> list[dict]:
        """Retrieve all memory notes matching any of the given files or node IDs.

        Checks `ast_hash` against `current_hashes` to flag stale notes when
        the underlying function/class was modified, renamed, or deleted after the note was written.
        """
        self._ensure_fresh()
        deleted_set = deleted_rels or set()
        all_target_paths = rel_paths | deleted_set
        matched: list[dict] = []
        for entry in self._notes:
            tid = entry.get("target_id", "")
            rpath = extract_note_rel_path(entry)
            if (
                tid in node_ids
                or (rpath and rpath in all_target_paths)
                or any(p and (tid.endswith(f":{p}") or f":{p}:" in tid) for p in all_target_paths)
            ):
                out = dict(entry)
                if rpath and not out.get("rel_path"):
                    out["rel_path"] = rpath
                saved_hash = entry.get("ast_hash", "")
                curr_hash = current_hashes.get(tid, "")
                if rpath and rpath in deleted_set:
                    out["stale"] = True
                    out["stale_warning"] = (
                        f"Target file '{rpath}' was deleted or renamed since this memory was recorded; "
                        "re-verify or update governance."
                    )
                elif (
                    rpath
                    and (indexed_files is None or rpath not in indexed_files)
                    and not (self.root / rpath).exists()
                ):
                    out["stale"] = True
                    out["stale_warning"] = (
                        f"Target file '{rpath}' no longer exists on disk; "
                        "re-verify or update governance."
                    )
                elif saved_hash and curr_hash and saved_hash != curr_hash:
                    out["stale"] = True
                    out["stale_warning"] = (
                        "Target symbol AST changed since this memory was recorded; "
                        "re-verify before relying on it."
                    )
                elif (
                    saved_hash
                    and not curr_hash
                    and tid.startswith(("fn:", "cls:"))
                    and indexed_files is not None
                    and rpath in indexed_files
                ):
                    out["stale"] = True
                    out["stale_warning"] = (
                        f"Target symbol '{tid}' is no longer present in '{rpath}'; "
                        "re-verify or refresh governance."
                    )
                else:
                    out["stale"] = False
                matched.append(out)
        return matched

    def list_all(
        self,
        current_hashes: dict[str, str] | None = None,
        *,
        indexed_files: set[str] | None = None,
    ) -> list[dict]:
        self._ensure_fresh()
        current_hashes = current_hashes or {}
        all_paths = {p for n in self._notes for p in (extract_note_rel_path(n),) if p}
        all_ids = {n.get("target_id", "") for n in self._notes}
        return self.get_notes_for(all_paths, all_ids, current_hashes, indexed_files=indexed_files)
