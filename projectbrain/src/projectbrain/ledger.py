"""Epistemic Working Memory & Structural Resolution Ledger for ProjectBrain.

Tracks session read-intervals (Zero-Waste Guard) and structural resolution
telemetry (AST slices compiled, 4D coordinate cells resolved, closed-world
verifications, and AST-verified anchor patches) across `dossier`, `compile`,
`slice`, and `patch` operations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_FILENAME = ".projectbrain_ledger.json"


class SavingsLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ledger_path = root / LEDGER_FILENAME

    def _load(self) -> dict[str, Any]:
        if self.ledger_path.exists():
            try:
                data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "events" in data:
                    return data
            except Exception:
                pass
        return {
            "repo": str(self.root.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "events": [],
        }

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.ledger_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def record_input_op(
        self,
        op: str,
        query: str,
        files_covered: int = 0,
        touched_files: list[str] | set[str] | None = None,
        slices_included: int = 0,
        cells_4d_resolved: int = 0,
        closed_world_verified: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        data = self._load()
        events = data.get("events", [])
        now_dt = datetime.now(timezone.utc)
        seen_files: set[str] = set(data.get("seen_files", []))
        if events:
            try:
                last_dt = datetime.fromisoformat(events[-1].get("timestamp", ""))
                if (now_dt - last_dt).total_seconds() > 1200:
                    seen_files.clear()
            except Exception:
                pass

        touched_list: list[str] = sorted(touched_files) if touched_files is not None else []
        for f in touched_list:
            seen_files.add(f)
        data["seen_files"] = sorted(seen_files)

        event = {
            "timestamp": now_dt.isoformat(),
            "op": op,
            "query": query[:140],
            "touched_files": touched_list,
            "files_covered": files_covered or len(touched_list),
            "slices_included": slices_included,
            "cells_4d_resolved": cells_4d_resolved,
            "closed_world_verified": closed_world_verified,
        }
        data["events"].append(event)
        self._save(data)
        return event

    def record_patch_op(
        self,
        files_patched: int,
        ops_applied: int,
        mode: str = "dry_run",
        all_syntax_valid: bool = True,
        **_: Any,
    ) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "op": f"patch ({mode})",
            "query": f"{ops_applied} surgical ops across {files_patched} files",
            "files_patched": files_patched,
            "ops_applied": ops_applied,
            "all_syntax_valid": all_syntax_valid,
        }
        data = self._load()
        data["events"].append(event)
        self._save(data)
        return event

    def get_stats(self) -> dict[str, Any]:
        data = self._load()
        events = data.get("events", [])
        seen_spans = data.get("seen_spans", {})
        tot_slices = sum(int(e.get("slices_included", 0)) for e in events)
        tot_cells_4d = sum(int(e.get("cells_4d_resolved", 0)) for e in events)
        tot_closed_world = sum(1 for e in events if e.get("closed_world_verified"))
        tot_patch_ops = sum(int(e.get("ops_applied", 0)) for e in events)
        return {
            "repo": data.get("repo", str(self.root.resolve())),
            "total_operations": len(events),
            "resolution": {
                "ast_slices_compiled": tot_slices,
                "cells_4d_resolved": tot_cells_4d,
                "closed_world_verifications": tot_closed_world,
                "patch_ops_applied": tot_patch_ops,
                "files_in_working_memory": len(seen_spans) if isinstance(seen_spans, dict) else 0,
            },
            "recent_events": events[-10:],
        }

    def plan_slice_intervals(
        self,
        rel_path: str,
        req_start: int,
        req_end: int,
        total_lines: int = 99999,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Deduplicate requested line range [req_start, req_end] against already-delivered
        session spans for `rel_path` (invalidated automatically if file mtime changed),
        and apply the Small-File Whole-Remainder Guard when appropriate.
        """
        data = self._load()
        events = data.get("events", [])
        now_dt = datetime.now(timezone.utc)
        seen_spans: dict[str, Any] = dict(data.get("seen_spans", {}))
        if events:
            try:
                last_dt = datetime.fromisoformat(events[-1].get("timestamp", ""))
                if (now_dt - last_dt).total_seconds() > 2700:
                    seen_spans.clear()
            except Exception:
                pass

        norm_path = rel_path.replace("\\", "/")
        try:
            mtime = round((self.root / norm_path).stat().st_mtime, 2)
        except OSError:
            mtime = 0.0

        entry = seen_spans.get(norm_path, {})
        if not isinstance(entry, dict) or abs(float(entry.get("mtime", -1.0)) - mtime) > 0.05:
            existing: list[tuple[int, int]] = []
        else:
            existing = [(int(iv[0]), int(iv[1])) for iv in entry.get("intervals", []) if len(iv) == 2]

        s = max(1, min(total_lines, req_start))
        e = max(s, min(total_lines, req_end))

        # Compute unseen and skipped sub-intervals within [s, e]
        unseen: list[tuple[int, int]] = [(s, e)]
        skipped: list[tuple[int, int]] = []
        for ex_s, ex_e in existing:
            next_unseen: list[tuple[int, int]] = []
            for u_s, u_e in unseen:
                if ex_e < u_s or ex_s > u_e:
                    next_unseen.append((u_s, u_e))
                else:
                    ov_s = max(u_s, ex_s)
                    ov_e = min(u_e, ex_e)
                    skipped.append((ov_s, ov_e))
                    if u_s < ov_s:
                        next_unseen.append((u_s, ov_s - 1))
                    if ov_e < u_e:
                        next_unseen.append((ov_e + 1, u_e))
            unseen = next_unseen

        # Small-File Whole-Remainder Guard: if file <= 350 lines and either:
        # (a) this is the 2nd+ slice and cumulative coverage >= 55% of the file, or
        # (b) a single slice asks for >= 72% of the file
        already_covered = sum(ex_e - ex_s + 1 for ex_s, ex_e in existing)
        new_lines = sum(u_e - u_s + 1 for u_s, u_e in unseen)
        auto_completed = False
        if (
            total_lines <= 350
            and unseen
            and (
                (existing and (already_covered + new_lines) >= int(total_lines * 0.55))
                or (new_lines >= int(total_lines * 0.72))
            )
        ):
            # Emit all remaining unread intervals of [1, total_lines] so file is 100% done in one pass
            full_unseen: list[tuple[int, int]] = [(1, total_lines)]
            for ex_s, ex_e in existing:
                nxt: list[tuple[int, int]] = []
                for u_s, u_e in full_unseen:
                    if ex_e < u_s or ex_s > u_e:
                        nxt.append((u_s, u_e))
                    else:
                        if u_s < ex_s:
                            nxt.append((u_s, ex_s - 1))
                        if ex_e < u_e:
                            nxt.append((ex_e + 1, u_e))
                full_unseen = nxt
            unseen = full_unseen
            auto_completed = True

        if commit and unseen:
            self.commit_slice_intervals(norm_path, unseen)

        return {
            "unseen_intervals": unseen,
            "skipped_intervals": skipped,
            "auto_completed_file": auto_completed,
        }

    def commit_slice_intervals(
        self,
        rel_path: str,
        new_intervals: list[tuple[int, int]],
    ) -> None:
        """Persist only the actually-emitted (unfolded) line intervals for `rel_path`."""
        if not new_intervals:
            return
        data = self._load()
        seen_spans: dict[str, Any] = dict(data.get("seen_spans", {}))
        norm_path = rel_path.replace("\\", "/")
        try:
            mtime = round((self.root / norm_path).stat().st_mtime, 2)
        except OSError:
            mtime = 0.0

        entry = seen_spans.get(norm_path, {})
        if not isinstance(entry, dict) or abs(float(entry.get("mtime", -1.0)) - mtime) > 0.05:
            existing: list[tuple[int, int]] = []
        else:
            existing = [(int(iv[0]), int(iv[1])) for iv in entry.get("intervals", []) if len(iv) == 2]

        merged = sorted(existing + [(int(s), int(e)) for s, e in new_intervals], key=lambda x: (x[0], x[1]))
        compacted: list[list[int]] = []
        for iv_s, iv_e in merged:
            if compacted and iv_s <= compacted[-1][1] + 1:
                compacted[-1][1] = max(compacted[-1][1], iv_e)
            else:
                compacted.append([iv_s, iv_e])

        seen_spans[norm_path] = {"mtime": mtime, "intervals": compacted}
        data["seen_spans"] = seen_spans
        self._save(data)
    def reset(self) -> dict[str, Any]:
        if self.ledger_path.exists():
            try:
                self.ledger_path.unlink()
            except OSError:
                pass
        return {"status": "reset", "repo": str(self.root.resolve())}
