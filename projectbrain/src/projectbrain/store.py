"""SQLite WAL-backed index storage (.projectbrain.db) for fast incremental updates and monorepo scale."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class SQLiteIndexStore:
    """Persistent SQLite storage backend with WAL mode for ProjectBrain."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    rel_path TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    info_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def write_full(
        self,
        meta: dict,
        index: dict[str, dict],
        sizes: dict[str, int],
        skipped: dict[str, str],
        graph_json: str | None = None,
    ) -> None:
        """Write full index snapshot atomically into SQLite."""
        try:
            self.init_schema()
            with self._connect() as conn:
                conn.execute("DELETE FROM meta")
                conn.execute("DELETE FROM files")
                meta_payload = {**meta, "skipped": skipped}
                if graph_json is not None:
                    meta_payload["graph_json"] = graph_json
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [(k, json.dumps(v)) for k, v in meta_payload.items()],
                )
                rows = [
                    (
                        rel,
                        info.get("mtime"),
                        sizes.get(rel),
                        json.dumps({k: v for k, v in info.items() if k != "mtime"}),
                    )
                    for rel, info in index.items()
                ]
                conn.executemany(
                    "INSERT INTO files (rel_path, mtime, size, info_json) VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def apply_incremental(
        self,
        meta: dict,
        upserted: dict[str, dict],
        sizes: dict[str, int],
        deleted: set[str],
        skipped: dict[str, str],
        graph_json: str | None = None,
    ) -> None:
        """Incrementally upsert modified/added files and remove deleted files in O(changed) time."""
        try:
            self.init_schema()
            with self._connect() as conn:
                meta_payload = {**meta, "skipped": skipped}
                if graph_json is not None:
                    meta_payload["graph_json"] = graph_json
                conn.executemany(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    [(k, json.dumps(v)) for k, v in meta_payload.items()],
                )
                if deleted:
                    conn.executemany(
                        "DELETE FROM files WHERE rel_path = ?",
                        [(rel,) for rel in deleted],
                    )
                if upserted:
                    rows = [
                        (
                            rel,
                            info.get("mtime"),
                            sizes.get(rel),
                            json.dumps({k: v for k, v in info.items() if k != "mtime"}),
                        )
                        for rel, info in upserted.items()
                    ]
                    conn.executemany(
                        "INSERT OR REPLACE INTO files (rel_path, mtime, size, info_json) VALUES (?, ?, ?, ?)",
                        rows,
                    )
                conn.commit()
        except sqlite3.Error:
            pass

    def load_all(self) -> dict | None:
        """Load the full index dictionary from SQLite if present and valid."""
        if not self.db_path.exists():
            return None
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT key, value FROM meta")
                meta = {row[0]: json.loads(row[1]) for row in cur.fetchall()}
                if not meta:
                    return None
                cur_files = conn.execute("SELECT rel_path, mtime, size, info_json FROM files")
                files_dict = {}
                for rel, mtime, size, info_json in cur_files.fetchall():
                    files_dict[rel] = {
                        "mtime": mtime,
                        "size": size,
                        "hash": None,
                        "info": json.loads(info_json),
                    }
                return {**meta, "files": files_dict}
        except (sqlite3.Error, json.JSONDecodeError):
            return None
