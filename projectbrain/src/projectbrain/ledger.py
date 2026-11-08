"""Resolution & Coverage Telemetry Ledger for ProjectBrain (`.projectbrain_ledger.json`).

Tracks verifiable static analysis metrics (files resolved, AST slices compiled,
4D coordinate cells matched, closed-world scan guarantees, and surgical patch
operations) plus an instance-scoped Session Zero-Waste Guard (`seen_spans`) that
prevents re-emitting identical source lines within the same `ProjectBrain` session
without leaking stale suppression across separate CLI processes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent 1-based inclusive [start, end] line intervals."""
    if not intervals:
        return []
    sorted_ivs = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: list[tuple[int, int]] = [sorted_ivs[0]]
    for s, e in sorted_ivs[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e + 1:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


def _subtract_intervals(
    req_start: int, req_end: int, seen: list[tuple[int, int]]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Subtract `seen` intervals from `[req_start, req_end]`.
    Returns `(unseen_intervals, skipped_overlap_intervals)`.
    """
    s, e = min(req_start, req_end), max(req_start, req_end)
    if not seen:
        return [(s, e)], []

    unseen: list[tuple[int, int]] = []
    skipped: list[tuple[int, int]] = []
    cur = s
    for seen_s, seen_e in _merge_intervals(seen):
        if seen_e < cur:
            continue
        if seen_s > e:
            break
        if seen_s > cur:
            unseen.append((cur, min(e, seen_s - 1)))
        ov_s = max(cur, seen_s)
        ov_e = min(e, seen_e)
        if ov_s <= ov_e:
            skipped.append((ov_s, ov_e))
        cur = max(cur, seen_e + 1)
        if cur > e:
            break
    if cur <= e:
        unseen.append((cur, e))
    return unseen, skipped


class SavingsLedger:
    """Persists cumulative resolution telemetry to `<repo>/.projectbrain_ledger.json`
    while scoping `seen_spans` deduplication to the live `ProjectBrain` session.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / ".projectbrain_ledger.json"
        self._session_seen: dict[str, dict] = {}

    def _default_state(self) -> dict:
        return {
            "version": 2,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_ops": 0,
            "ops_by_type": {
                "dossier": 0,
                "compile": 0,
                "slice": 0,
                "patch": 0,
            },
            "resolution": {
                "files_resolved_total": 0,
                "ast_slices_compiled": 0,
                "cells_4d_resolved": 0,
                "closed_world_scans_verified": 0,
                "patch_ops_applied": 0,
                "files_patched": 0,
                "syntax_verified_patches": 0,
            },
            "session_seen": {},
            "recent_events": [],
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._default_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 2 or "resolution" not in data:
                return self._default_state()
            return data
        except (OSError, json.JSONDecodeError):
            return self._default_state()

    def _save(self, data: dict) -> None:
        try:
            data["session_seen"] = self._session_seen
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def invalidate_file(self, rel_path: str | None = None) -> None:
        """Clear cached seen_spans for `rel_path` (or all files if None)."""
        if rel_path is None:
            self._session_seen.clear()
        else:
            norm = rel_path.replace("\\", "/")
            self._session_seen.pop(norm, None)

    def plan_slice_intervals(
        self,
        rel_path: str,
        req_start: int,
        req_end: int,
        total_file_lines: int = 0,
        commit: bool = True,
    ) -> dict:
        """Session Zero-Waste Guard & Small-File Remainder Guard.

        1. Checks file `mtime` on disk against `self._session_seen[rel_path]`.
           If the file was modified since last read, resets its seen intervals.
        2. Subtracts already-seen intervals from `[req_start, req_end]` so overlapping
           `slice` or `dossier` calls within the SAME `ProjectBrain` session never emit duplicate lines.
        3. Small-File Remainder Guard: If `total_file_lines <= 220` and a prior slice
           was already read on this file, automatically expands to cover all remaining
           unread lines of the file in one pass.
        """
        norm_rel = rel_path.replace("\\", "/")
        abs_path = self.root / norm_rel
        try:
            cur_mtime = abs_path.stat().st_mtime if abs_path.exists() else 0.0
        except OSError:
            cur_mtime = 0.0

        entry = self._session_seen.get(norm_rel)
        if not entry or abs(entry.get("mtime", 0.0) - cur_mtime) > 1e-6:
            entry = {"mtime": cur_mtime, "intervals": []}

        seen_ivs: list[tuple[int, int]] = [tuple(iv) for iv in entry.get("intervals", [])]  # type: ignore[misc]
        s, e = max(1, min(req_start, req_end)), max(req_start, req_end)

        auto_completed_file = False
        if seen_ivs and 0 < total_file_lines <= 220 and (e - s + 1) < total_file_lines:
            s, e = 1, total_file_lines
            auto_completed_file = True

        unseen, skipped = _subtract_intervals(s, e, seen_ivs)
        if commit and unseen:
            new_seen = _merge_intervals(seen_ivs + unseen)
            self._session_seen[norm_rel] = {
                "mtime": cur_mtime,
                "intervals": [[a, b] for a, b in new_seen],
            }

        return {
            "unseen_intervals": unseen,
            "skipped_intervals": skipped,
            "auto_completed_file": auto_completed_file,
        }

    def commit_slice_intervals(self, rel_path: str, emitted_intervals: list[tuple[int, int]]) -> None:
        """Record actually emitted (unfolded) line intervals for `rel_path` in the session cache."""
        if not emitted_intervals:
            return
        norm_rel = rel_path.replace("\\", "/")
        abs_path = self.root / norm_rel
        try:
            cur_mtime = abs_path.stat().st_mtime if abs_path.exists() else 0.0
        except OSError:
            cur_mtime = 0.0

        entry = self._session_seen.get(norm_rel)
        if not entry or abs(entry.get("mtime", 0.0) - cur_mtime) > 1e-6:
            seen_ivs: list[tuple[int, int]] = []
        else:
            seen_ivs = [tuple(iv) for iv in entry.get("intervals", [])]  # type: ignore[misc]

        merged = _merge_intervals(seen_ivs + emitted_intervals)
        self._session_seen[norm_rel] = {
            "mtime": cur_mtime,
            "intervals": [[a, b] for a, b in merged],
        }

    def record_input_op(
        self,
        op: str,
        query: str,
        files_covered: int = 0,
        touched_files: set[str] | list[str] | None = None,
        slices_included: int = 0,
        cells_4d_resolved: int = 0,
        closed_world_verified: bool = False,
        naive_bytes: int = 0,
        emitted_chars: int = 0,
    ) -> dict:
        """Record a verifiable context compilation / dossier / slice event."""
        data = self._load()
        data["total_ops"] = data.get("total_ops", 0) + 1
        ops_map = data.setdefault("ops_by_type", {})
        ops_map[op] = ops_map.get(op, 0) + 1

        res = data.setdefault("resolution", {})
        res["files_resolved_total"] = res.get("files_resolved_total", 0) + int(files_covered)
        res["ast_slices_compiled"] = res.get("ast_slices_compiled", 0) + int(slices_included)
        res["cells_4d_resolved"] = res.get("cells_4d_resolved", 0) + int(cells_4d_resolved)
        if closed_world_verified:
            res["closed_world_scans_verified"] = res.get("closed_world_scans_verified", 0) + 1

        event = {
            "ts": time.strftime("%H:%M:%S", time.gmtime()),
            "op": op,
            "query": query[:90],
            "files_resolved": int(files_covered),
            "slices_included": int(slices_included),
            "cells_4d_resolved": int(cells_4d_resolved),
            "closed_world_verified": bool(closed_world_verified),
        }
        recent = data.setdefault("recent_events", [])
        recent.append(event)
        if len(recent) > 25:
            data["recent_events"] = recent[-25:]

        self._save(data)
        return event

    def record_patch_op(
        self,
        files_patched: int,
        ops_applied: int,
        mode: str,
        all_syntax_valid: bool,
        orig_total_bytes: int = 0,
        spec_chars: int = 0,
        diff_chars: int = 0,
    ) -> dict:
        """Record a Surgical Anchor Patch (`projectbrain patch`) execution."""
        data = self._load()
        data["total_ops"] = data.get("total_ops", 0) + 1
        ops_map = data.setdefault("ops_by_type", {})
        ops_map["patch"] = ops_map.get("patch", 0) + 1

        res = data.setdefault("resolution", {})
        res["patch_ops_applied"] = res.get("patch_ops_applied", 0) + int(ops_applied)
        res["files_patched"] = res.get("files_patched", 0) + int(files_patched)
        if all_syntax_valid:
            res["syntax_verified_patches"] = res.get("syntax_verified_patches", 0) + 1

        event = {
            "ts": time.strftime("%H:%M:%S", time.gmtime()),
            "op": f"patch:{mode}",
            "files_patched": int(files_patched),
            "ops_applied": int(ops_applied),
            "syntax_valid": bool(all_syntax_valid),
        }
        recent = data.setdefault("recent_events", [])
        recent.append(event)
        if len(recent) > 25:
            data["recent_events"] = recent[-25:]

        self._save(data)
        return event

    def get_stats(self) -> dict:
        """Return formatted resolution and structural coverage statistics."""
        data = self._load()
        res = data.get("resolution", {})
        return {
            "ledger_path": str(self.path),
            "started_at": data.get("started_at"),
            "total_operations": data.get("total_ops", 0),
            "operations_by_type": data.get("ops_by_type", {}),
            "resolution": {
                "files_resolved_total": res.get("files_resolved_total", 0),
                "ast_slices_compiled": res.get("ast_slices_compiled", 0),
                "cells_4d_resolved": res.get("cells_4d_resolved", 0),
                "closed_world_scans_verified": res.get("closed_world_scans_verified", 0),
                "patch_ops_applied": res.get("patch_ops_applied", 0),
                "files_patched": res.get("files_patched", 0),
                "syntax_verified_patches": res.get("syntax_verified_patches", 0),
                "files_in_working_memory": len(self._session_seen),
            },
            "recent_events": data.get("recent_events", [])[-10:],
        }

    def reset(self) -> dict:
        self._session_seen.clear()
        fresh = self._default_state()
        self._save(fresh)
        return self.get_stats()
