"""Persistent AST-anchored repository memory for ProjectBrain.

Allows agents and developers to store architectural invariants, bug root causes,
and non-obvious rules anchored to specific graph nodes (file:..., fn:..., cls:...).
Each note stores the target node's `ast_hash` at creation time. When the underlying
symbol's AST changes, the memory is automatically flagged as `stale: True` so
agents know to re-verify it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class RepoMemory:
    """Manages persistent AST-anchored notes in `.projectbrain_memory.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / ".projectbrain_memory.json"
        self._notes: list[dict] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._notes = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._notes = data.get("notes", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, OSError):
            self._notes = []

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": round(time.time(), 2),
            "notes": self._notes,
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
        target_id = target_id.strip()
        note = note.strip()
        # Deduplicate if identical target_id + note exists
        for existing in self._notes:
            if existing.get("target_id") == target_id and existing.get("note") == note:
                existing["ast_hash"] = ast_hash or existing.get("ast_hash", "")
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

    def get_notes_for(
        self,
        rel_paths: set[str],
        node_ids: set[str],
        current_hashes: dict[str, str],
    ) -> list[dict]:
        """Retrieve all memory notes matching any of the given files or node IDs.

        Checks `ast_hash` against `current_hashes` to flag stale notes when
        the underlying function/class was modified after the note was written.
        """
        matched: list[dict] = []
        for entry in self._notes:
            tid = entry.get("target_id", "")
            rpath = entry.get("rel_path", "")
            if tid in node_ids or rpath in rel_paths or any(tid.endswith(f":{p}") for p in rel_paths):
                out = dict(entry)
                saved_hash = entry.get("ast_hash", "")
                curr_hash = current_hashes.get(tid, "")
                if saved_hash and curr_hash and saved_hash != curr_hash:
                    out["stale"] = True
                    out["stale_warning"] = (
                        "Target symbol AST changed since this memory was recorded; "
                        "re-verify before relying on it."
                    )
                else:
                    out["stale"] = False
                matched.append(out)
        return matched

    def list_all(self, current_hashes: dict[str, str] | None = None) -> list[dict]:
        current_hashes = current_hashes or {}
        all_paths = {n.get("rel_path", "") for n in self._notes}
        all_ids = {n.get("target_id", "") for n in self._notes}
        return self.get_notes_for(all_paths, all_ids, current_hashes)
