"""Normalized SQLite WAL Fact Store for ProjectBrain (`.projectbrain.db`).

Provides ACID transactions, WAL concurrency, O(1) incremental file updates,
Salsa-style `interface_hash` vs `body_hash` tracking on symbols and files,
`WITHOUT ROWID` covering B-tree indexes for forward and backward graph edges,
FTS5 sub-token symbol search (`fts_symbols`), 3-hop `WITH RECURSIVE` CTE
graph queries, and a Stacked In-Memory Delta Overlay (`DELTA_DB`) for zero-disk
dry-run verification.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def _split_subtokens(text: str) -> str:
    """Split camelCase, PascalCase, and snake_case identifiers into space-separated subtokens."""
    if not text:
        return ""
    parts = re.split(r"[^a-zA-Z0-9]+", text)
    tokens: list[str] = []
    for p in parts:
        if not p:
            continue
        sub = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", p).split()
        tokens.extend(s.lower() for s in sub if s)
    return " ".join(tokens)


class SQLiteIndexStore:
    """Manages `.projectbrain.db` SQLite persistence for file index, symbols, FTS5, and code graph."""

    def __init__(self, root_or_db_path: Path):
        p = Path(root_or_db_path).resolve()
        if p.suffix == ".db":
            self.db_path = p
            self.root = p.parent
        else:
            self.root = p
            self.db_path = p / ".projectbrain.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                rel_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                interface_hash TEXT NOT NULL DEFAULT '',
                info_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                node_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                signature TEXT NOT NULL,
                interface_hash TEXT NOT NULL,
                body_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(rel_path);
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                rel_path TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                props_json TEXT NOT NULL DEFAULT '{}'
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_file ON graph_nodes(rel_path);
            CREATE TABLE IF NOT EXISTS edges (
                src_id TEXT NOT NULL,
                dst_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                confidence TEXT NOT NULL,
                line INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (src_id, edge_type, dst_id, line)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_edges_bwd ON edges(dst_id, edge_type, src_id);
            CREATE TABLE IF NOT EXISTS source_blobs (
                rel_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS skipped (
                rel_path TEXT PRIMARY KEY,
                reason TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
                node_id UNINDEXED,
                rel_path,
                name,
                subtokens,
                signature,
                docstring
            );
            """
        )
        # Ensure legacy databases with `val` on `meta` or without `interface_hash` on `files` are migrated seamlessly
        meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(meta)").fetchall()}
        if "value" not in meta_cols and "val" in meta_cols:
            conn.execute("ALTER TABLE meta RENAME COLUMN val TO value;")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "interface_hash" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN interface_hash TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _extract_symbol_and_fts_rows(rel_path: str, info: dict) -> tuple[list[tuple], list[tuple]]:
        sym_rows: list[tuple] = []
        fts_rows: list[tuple] = []
        for fn in info.get("functions", []):
            parent = fn.get("parent_class")
            name = fn.get("name", "")
            qname = f"{parent}.{name}" if parent else name
            nid = f"fn:{rel_path}:{qname}"
            sig = fn.get("signature", "")
            doc = fn.get("docstring") or ""
            if_hash = fn.get("interface_hash", fn.get("ast_hash", ""))
            b_hash = fn.get("ast_hash", "")
            sym_rows.append((
                nid,
                rel_path,
                "fn",
                name,
                int(fn.get("line_start", 1)),
                int(fn.get("line_end", 1)),
                sig,
                if_hash,
                b_hash,
            ))
            fts_rows.append((
                nid,
                rel_path,
                qname,
                _split_subtokens(f"{qname} {rel_path}"),
                sig,
                doc,
            ))
        for cls in info.get("classes", []):
            name = cls.get("name", "")
            nid = f"cls:{rel_path}:{name}"
            sig = cls.get("signature", name)
            doc = cls.get("docstring") or ""
            if_hash = cls.get("interface_hash", cls.get("ast_hash", ""))
            b_hash = cls.get("ast_hash", "")
            sym_rows.append((
                nid,
                rel_path,
                "cls",
                name,
                int(cls.get("line_start", 1)),
                int(cls.get("line_end", 1)),
                sig,
                if_hash,
                b_hash,
            ))
            fts_rows.append((
                nid,
                rel_path,
                name,
                _split_subtokens(f"{name} {rel_path}"),
                sig,
                doc,
            ))
        return sym_rows, fts_rows

    @staticmethod
    def _extract_node_and_edge_rows(graph_json: str | dict | None) -> tuple[list[tuple], list[tuple]]:
        if not graph_json:
            return [], []
        try:
            g_data = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
        except json.JSONDecodeError:
            return [], []
        node_rows: list[tuple] = []
        for nid, nd in (g_data.get("nodes") or {}).items():
            ntype = str(nd.get("type", ""))
            rpath = str(nd.get("rel_path") or "")
            nm = str(nd.get("name") or "")
            extra = {k: v for k, v in nd.items() if k not in ("id", "type", "rel_path", "name")}
            node_rows.append((str(nid), ntype, rpath, nm, json.dumps(extra, separators=(",", ":"))))

        edge_rows: list[tuple] = []
        seen_pk: set[tuple[str, str, str, int]] = set()
        for e in g_data.get("edges", []):
            src = str(e.get("src", ""))
            dst = str(e.get("dst", ""))
            et = str(e.get("type", ""))
            conf = str(e.get("confidence", "candidate"))
            ln = int(e.get("line") or 0)
            rsn = str(e.get("reason") or "")
            pk = (src, et, dst, ln)
            if src and dst and et and pk not in seen_pk:
                seen_pk.add(pk)
                edge_rows.append((src, dst, et, conf, ln, rsn))
        return node_rows, edge_rows

    def _read_source_for_blob(self, rel_path: str, info: dict) -> tuple[str, str, str] | None:
        fpath = self.root / rel_path
        if not fpath.exists():
            return None
        try:
            src_txt = fpath.read_text(encoding="utf-8", errors="ignore")
            c_hash = str(info.get("interface_hash") or "")
            return (rel_path, c_hash, src_txt)
        except OSError:
            return None

    def write_full(
        self,
        meta: dict,
        index: dict[str, dict],
        sizes: dict[str, int],
        skipped: dict[str, str],
        graph_json: str | dict | None = None,
    ) -> None:
        conn = self._connect()
        try:
            self._init_schema(conn)
            with conn:
                conn.execute("DELETE FROM meta;")
                conn.execute("DELETE FROM files;")
                conn.execute("DELETE FROM symbols;")
                conn.execute("DELETE FROM graph_nodes;")
                conn.execute("DELETE FROM edges;")
                conn.execute("DELETE FROM source_blobs;")
                conn.execute("DELETE FROM skipped;")
                conn.execute("DELETE FROM fts_symbols;")

                meta_rows = [(k, json.dumps(v)) for k, v in meta.items() if k != "graph_json"]
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?);", meta_rows)

                file_rows = []
                symbol_rows = []
                fts_rows = []
                blob_rows = []
                store_blobs = len(index) <= 400
                for rel, info in index.items():
                    mtime = float(info.get("mtime") or 0.0)
                    size = int(sizes.get(rel) or 0)
                    if_hash = str(info.get("interface_hash") or "")
                    info_copy = {k: v for k, v in info.items() if k != "mtime"}
                    file_rows.append((rel, mtime, size, if_hash, json.dumps(info_copy, separators=(",", ":"))))
                    s_r, f_r = self._extract_symbol_and_fts_rows(rel, info)
                    symbol_rows.extend(s_r)
                    fts_rows.extend(f_r)
                    if store_blobs:
                        b_row = self._read_source_for_blob(rel, info)
                        if b_row is not None:
                            blob_rows.append(b_row)

                conn.executemany(
                    "INSERT INTO files (rel_path, mtime, size, interface_hash, info_json) VALUES (?, ?, ?, ?, ?);",
                    file_rows,
                )
                if blob_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO source_blobs (rel_path, content_hash, source_text) VALUES (?, ?, ?);",
                        blob_rows,
                    )
                if symbol_rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO symbols
                           (node_id, rel_path, kind, name, line_start, line_end, signature, interface_hash, body_hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                        symbol_rows,
                    )
                if fts_rows:
                    conn.executemany(
                        """INSERT INTO fts_symbols (node_id, rel_path, name, subtokens, signature, docstring)
                           VALUES (?, ?, ?, ?, ?, ?);""",
                        fts_rows,
                    )
                node_rows, edge_rows = self._extract_node_and_edge_rows(graph_json)
                if node_rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO graph_nodes
                           (node_id, node_type, rel_path, name, props_json)
                           VALUES (?, ?, ?, ?, ?);""",
                        node_rows,
                    )
                if edge_rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO edges
                           (src_id, dst_id, edge_type, confidence, line, reason)
                           VALUES (?, ?, ?, ?, ?, ?);""",
                        edge_rows,
                    )
                if skipped:
                    conn.executemany(
                        "INSERT INTO skipped (rel_path, reason) VALUES (?, ?);",
                        list(skipped.items()),
                    )
        finally:
            conn.close()

    def apply_incremental(
        self,
        meta: dict,
        upserted_index: dict[str, dict],
        sizes: dict[str, int],
        deleted_rels: set[str],
        skipped: dict[str, str],
        graph_json: str | dict | None = None,
        graph_delta: dict | None = None,
        skipped_changed: bool = True,
    ) -> None:
        if not self.db_path.exists():
            return
        conn = self._connect()
        try:
            self._init_schema(conn)
            with conn:
                for k, v in meta.items():
                    if k != "graph_json":
                        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);", (k, json.dumps(v)))
                conn.execute("DELETE FROM meta WHERE key = 'graph_json';")

                for rel in deleted_rels:
                    conn.execute("DELETE FROM files WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM source_blobs WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM symbols WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM fts_symbols WHERE rel_path = ?;", (rel,))

                file_rows = []
                symbol_rows = []
                fts_rows = []
                blob_rows = []
                for rel, info in upserted_index.items():
                    conn.execute("DELETE FROM symbols WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM fts_symbols WHERE rel_path = ?;", (rel,))
                    mtime = float(info.get("mtime") or 0.0)
                    size = int(sizes.get(rel) or 0)
                    if_hash = str(info.get("interface_hash") or "")
                    info_copy = {k: v for k, v in info.items() if k != "mtime"}
                    file_rows.append((rel, mtime, size, if_hash, json.dumps(info_copy, separators=(",", ":"))))
                    s_r, f_r = self._extract_symbol_and_fts_rows(rel, info)
                    symbol_rows.extend(s_r)
                    fts_rows.extend(f_r)
                    b_row = self._read_source_for_blob(rel, info)
                    if b_row is not None:
                        blob_rows.append(b_row)

                if file_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO files (rel_path, mtime, size, interface_hash, info_json) VALUES (?, ?, ?, ?, ?);",
                        file_rows,
                    )
                if blob_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO source_blobs (rel_path, content_hash, source_text) VALUES (?, ?, ?);",
                        blob_rows,
                    )
                if symbol_rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO symbols
                           (node_id, rel_path, kind, name, line_start, line_end, signature, interface_hash, body_hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                        symbol_rows,
                    )
                if fts_rows:
                    conn.executemany(
                        """INSERT INTO fts_symbols (node_id, rel_path, name, subtokens, signature, docstring)
                           VALUES (?, ?, ?, ?, ?, ?);""",
                        fts_rows,
                    )
                if graph_delta is not None:
                    rem_rels = graph_delta.get("removed_rels", [])
                    rem_nids = graph_delta.get("removed_node_ids", [])
                    reb_sids = graph_delta.get("rebuild_src_ids", [])
                    if rem_rels:
                        conn.executemany("DELETE FROM graph_nodes WHERE rel_path = ?;", [(r,) for r in rem_rels])
                    if rem_nids:
                        conn.executemany("DELETE FROM graph_nodes WHERE node_id = ?;", [(n,) for n in rem_nids])
                        conn.executemany("DELETE FROM edges WHERE dst_id = ?;", [(n,) for n in rem_nids])
                    if reb_sids:
                        conn.executemany("DELETE FROM edges WHERE src_id = ?;", [(s,) for s in reb_sids])
                    node_rows, edge_rows = self._extract_node_and_edge_rows({
                        "nodes": graph_delta.get("upserted_nodes", {}),
                        "edges": graph_delta.get("new_edges", []),
                    })
                    if node_rows:
                        conn.executemany(
                            """INSERT OR REPLACE INTO graph_nodes
                               (node_id, node_type, rel_path, name, props_json)
                               VALUES (?, ?, ?, ?, ?);""",
                            node_rows,
                        )
                    if edge_rows:
                        conn.executemany(
                            """INSERT OR REPLACE INTO edges
                               (src_id, dst_id, edge_type, confidence, line, reason)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            edge_rows,
                        )
                elif graph_json is not None:
                    conn.execute("DELETE FROM graph_nodes;")
                    conn.execute("DELETE FROM edges;")
                    node_rows, edge_rows = self._extract_node_and_edge_rows(graph_json)
                    if node_rows:
                        conn.executemany(
                            """INSERT OR REPLACE INTO graph_nodes
                               (node_id, node_type, rel_path, name, props_json)
                               VALUES (?, ?, ?, ?, ?);""",
                            node_rows,
                        )
                    if edge_rows:
                        conn.executemany(
                            """INSERT OR REPLACE INTO edges
                               (src_id, dst_id, edge_type, confidence, line, reason)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            edge_rows,
                        )
                if skipped_changed:
                    conn.execute("DELETE FROM skipped;")
                    if skipped:
                        conn.executemany(
                            "INSERT INTO skipped (rel_path, reason) VALUES (?, ?);",
                            list(skipped.items()),
                        )
        finally:
            conn.close()

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        """Search symbols via FTS5 BM25 over identifiers, sub-tokens, signatures, and docstrings."""
        if not self.db_path.exists():
            return []
        terms = [t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if len(t) >= 2]
        if not terms:
            return []
        fts_expr = " OR ".join(f'"{t}"*' for t in terms[:12])
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT node_id, rel_path, name, signature,
                       bm25(fts_symbols, 0.0, 2.0, 5.0, 4.0, 2.5, 1.0) AS rank_score
                FROM fts_symbols
                WHERE fts_symbols MATCH ?
                ORDER BY rank_score
                LIMIT ?;
                """,
                (fts_expr, limit),
            )
            return [
                {
                    "node_id": r[0],
                    "rel_path": r[1],
                    "name": r[2],
                    "signature": r[3],
                    "bm25_score": -float(r[4]),
                }
                for r in cur.fetchall()
            ]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def query_reachable_cte(
        self,
        seed_ids: list[str],
        max_hops: int = 3,
        direction: str = "forward",
        edge_types: tuple[str, ...] = ("calls", "handles", "imports"),
    ) -> list[dict]:
        """Execute a bounded `WITH RECURSIVE` graph traversal over the `WITHOUT ROWID` edges B-tree."""
        if not self.db_path.exists() or not seed_ids or max_hops < 1:
            return []
        expanded_seeds: list[str] = []
        for s in seed_ids:
            expanded_seeds.append(s)
            if "::" in s and not s.startswith("fn:"):
                expanded_seeds.append("fn:" + s.replace("::", ":"))
        seed_ids = list(dict.fromkeys(expanded_seeds))
        placeholders = ",".join("?" for _ in seed_ids)
        et_placeholders = ",".join("?" for _ in edge_types)
        conn = self._connect()
        try:
            if direction == "backward":
                sql = f"""
                WITH RECURSIVE reach(node_id, hop, via_edge, path) AS (
                    SELECT dst_id, 0, '', dst_id
                    FROM edges
                    WHERE dst_id IN ({placeholders})
                    GROUP BY dst_id
                    UNION
                    SELECT e.src_id, r.hop + 1, e.edge_type, r.path || ' <- ' || e.src_id
                    FROM reach r
                    JOIN edges e ON e.dst_id = r.node_id
                    WHERE r.hop < ?
                      AND e.edge_type IN ({et_placeholders})
                      AND instr(r.path, e.src_id) = 0
                )
                SELECT node_id, MIN(hop) AS min_hop, via_edge, path
                FROM reach
                WHERE hop > 0
                GROUP BY node_id
                ORDER BY min_hop ASC;
                """
            else:
                sql = f"""
                WITH RECURSIVE reach(node_id, hop, via_edge, path) AS (
                    SELECT src_id, 0, '', src_id
                    FROM edges
                    WHERE src_id IN ({placeholders})
                    GROUP BY src_id
                    UNION
                    SELECT e.dst_id, r.hop + 1, e.edge_type, r.path || ' -> ' || e.dst_id
                    FROM reach r
                    JOIN edges e ON e.src_id = r.node_id
                    WHERE r.hop < ?
                      AND e.edge_type IN ({et_placeholders})
                      AND instr(r.path, e.dst_id) = 0
                )
                SELECT node_id, MIN(hop) AS min_hop, via_edge, path
                FROM reach
                WHERE hop > 0
                GROUP BY node_id
                ORDER BY min_hop ASC;
                """
            params = list(seed_ids) + [max_hops] + list(edge_types)
            cur = conn.execute(sql, params)
            return [
                {"node_id": r[0], "hop": int(r[1]), "edge_type": r[2], "path": r[3]}
                for r in cur.fetchall()
            ]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def open_delta_overlay(self, patched_files: dict[str, dict]) -> sqlite3.Connection:
        """Create a Stacked In-Memory Delta Overlay (`TEMP` union views over `main`) for dry-run verification.

        `patched_files` maps `rel_path -> parsed_info_dict`.
        Returns an open `sqlite3.Connection` whose `temp.unified_symbols` view merges
        the unpatched `main.symbols` with the in-memory `temp.delta_symbols` without touching disk.
        """
        conn = self._connect()
        self._init_schema(conn)
        conn.executescript(
            """
            CREATE TEMP TABLE delta_excluded_files (
                rel_path TEXT PRIMARY KEY
            );
            CREATE TEMP TABLE delta_symbols (
                node_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                signature TEXT NOT NULL,
                interface_hash TEXT NOT NULL,
                body_hash TEXT NOT NULL
            );
            CREATE TEMP VIEW unified_symbols AS
                SELECT * FROM temp.delta_symbols
                UNION ALL
                SELECT s.* FROM main.symbols s
                WHERE s.rel_path NOT IN (SELECT rel_path FROM temp.delta_excluded_files);
            """
        )
        excl_rows = [(rel,) for rel in patched_files]
        sym_rows: list[tuple] = []
        for rel, info in patched_files.items():
            s_r, _ = self._extract_symbol_and_fts_rows(rel, info)
            sym_rows.extend(s_r)
        with conn:
            if excl_rows:
                conn.executemany("INSERT OR REPLACE INTO temp.delta_excluded_files (rel_path) VALUES (?);", excl_rows)
            if sym_rows:
                conn.executemany(
                    """INSERT OR REPLACE INTO temp.delta_symbols
                       (node_id, rel_path, kind, name, line_start, line_end, signature, interface_hash, body_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    sym_rows,
                )
        return conn

    def get_source_blob(self, rel_path: str) -> str | None:
        """Retrieve content-addressed file source text directly from the SQLite Graph DB."""
        if not self.db_path.exists():
            return None
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT source_text FROM source_blobs WHERE rel_path = ?;",
                    (rel_path,),
                ).fetchone()
                return str(row[0]) if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def get_symbol_slice_from_graph(self, node_id_or_name: str) -> dict | None:
        """Pure Graph-as-Storage symbol slicer: joins `symbols` + `source_blobs` (or disk fallback)."""
        if not self.db_path.exists():
            return None
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT s.node_id, s.rel_path, s.kind, s.name, s.line_start, s.line_end,
                           s.signature, s.interface_hash, s.body_hash, b.source_text
                    FROM symbols s
                    LEFT JOIN source_blobs b ON b.rel_path = s.rel_path
                    WHERE s.node_id = ? OR s.name = ?
                    ORDER BY CASE WHEN s.node_id = ? THEN 0 ELSE 1 END
                    LIMIT 1;
                    """,
                    (node_id_or_name, node_id_or_name, node_id_or_name),
                ).fetchone()
                if not row:
                    return None
                nid, rel_path, kind, name, l_start, l_end, sig, if_h, b_h, src_txt = row
                if src_txt is None:
                    fpath = self.root / rel_path
                    if not fpath.exists():
                        return None
                    src_txt = fpath.read_text(encoding="utf-8", errors="replace")
                src_lines = str(src_txt).splitlines()
                s_idx = max(int(l_start) - 1, 0)
                e_idx = min(int(l_end), len(src_lines))
                code_str = "\n".join(f"{s_idx + idx + 1:4d} | {ln}" for idx, ln in enumerate(src_lines[s_idx:e_idx]))
                return {
                    "node_id": nid,
                    "rel_path": rel_path,
                    "kind": kind,
                    "name": name,
                    "line_start": int(l_start),
                    "line_end": int(l_end),
                    "signature": sig,
                    "interface_hash": if_h,
                    "body_hash": b_h,
                    "code": code_str,
                }
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return None

    def update_skipped_only(self, skipped: dict[str, str]) -> None:
        """Update only the `skipped` table in SQLite without touching files or graph tables."""
        if not self.db_path.exists():
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM skipped;")
                if skipped:
                    conn.executemany(
                        "INSERT INTO skipped (rel_path, reason) VALUES (?, ?);",
                        list(skipped.items()),
                    )
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def is_compatible_meta(self, schema_version: int, parser_version: str, graph_version: str) -> bool:
        """Fast <1ms check that `.projectbrain.db` matches current schema/parser/graph versions."""
        if not self.db_path.exists():
            return False
        try:
            conn = self._connect()
            try:
                rows = dict(conn.execute("SELECT key, value FROM meta;").fetchall())
                if not rows:
                    return False
                return (
                    json.loads(rows.get("schema_version", "0")) == schema_version
                    and json.loads(rows.get("parser_version", '""')) == parser_version
                    and json.loads(rows.get("graph_version", '""')) == graph_version
                )
            finally:
                conn.close()
        except Exception:
            return False

    def find_symbols_fast(self, sym_query: str, file_filter: str | None = None) -> list[dict]:
        """Fast indexed SQLite lookup for `slice_symbols` without loading the entire repo graph."""
        if not self.db_path.exists():
            return []
        q_low = sym_query.strip().lower()
        base_name = q_low.split(".")[-1]
        ff_low = file_filter.lower().replace("\\", "/") if file_filter else None
        try:
            conn = self._connect()
            try:
                if sym_query.startswith(("fn:", "cls:")):
                    rows = conn.execute(
                        """
                        SELECT s.node_id, s.rel_path, s.kind, s.name, s.line_start, s.line_end, s.signature, f.mtime
                        FROM symbols s
                        LEFT JOIN files f ON f.rel_path = s.rel_path
                        WHERE s.node_id = ?;
                        """,
                        (sym_query,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT s.node_id, s.rel_path, s.kind, s.name, s.line_start, s.line_end, s.signature, f.mtime
                        FROM symbols s
                        LEFT JOIN files f ON f.rel_path = s.rel_path
                        WHERE LOWER(s.name) = ? OR LOWER(s.node_id) LIKE ?;
                        """,
                        (base_name, f"%:{q_low}"),
                    ).fetchall()
                out: list[dict] = []
                for nid, rel, kind, name, l_s, l_e, sig, mtime in rows:
                    if ff_low and ff_low not in rel.lower():
                        continue
                    qname = nid.split(":", 2)[2] if nid.count(":") >= 2 else name
                    if sym_query.startswith(("fn:", "cls:")) or q_low in (name.lower(), qname.lower()):
                        out.append({
                            "node_id": nid,
                            "rel_path": rel,
                            "kind": kind,
                            "name": name,
                            "qname": qname,
                            "line_start": int(l_s),
                            "line_end": int(l_e),
                            "signature": sig,
                            "mtime": mtime,
                        })
                return out
            finally:
                conn.close()
        except sqlite3.Error:
            return []

    def get_file_fast(self, rel_or_suffix: str) -> dict | None:
        """Fast <1ms single-file lookup in SQLite `files` table."""
        if not self.db_path.exists():
            return None
        norm = rel_or_suffix.replace("\\", "/").strip("/")
        norm_low = norm.lower()
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT rel_path, mtime, info_json FROM files WHERE rel_path = ?;",
                    (norm,),
                ).fetchone()
                if not row:
                    cands = conn.execute(
                        "SELECT rel_path, mtime, info_json FROM files WHERE LOWER(rel_path) = ? OR LOWER(rel_path) LIKE ?;",
                        (norm_low, f"%/{norm_low}"),
                    ).fetchall()
                    if cands:
                        row = min(cands, key=lambda r: (len(r[0].split("/")), len(r[0])))
                if not row:
                    return None
                return {
                    "rel_path": row[0],
                    "mtime": row[1],
                    "info": json.loads(row[2]),
                }
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def load_all(self) -> dict | None:
        if not self.db_path.exists():
            return None
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                meta_cols = {row[1] for row in cur.execute("PRAGMA table_info(meta)").fetchall()}
                col_name = "value" if "value" in meta_cols else "val"
                cur.execute(f"SELECT key, {col_name} FROM meta;")
                meta_raw = dict(cur.fetchall())
                if not meta_raw:
                    return None

                out: dict = {}
                for k, v in meta_raw.items():
                    if k == "graph_json":
                        if isinstance(v, str) and v.startswith('"'):
                            out["graph_json"] = json.loads(v)
                        else:
                            out["graph_json"] = v
                    else:
                        out[k] = json.loads(v)

                tables = {
                    row[0]
                    for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
                }
                if "graph_nodes" in tables and "edges" in tables:
                    cur.execute("SELECT node_id, node_type, rel_path, name, props_json FROM graph_nodes;")
                    g_nodes_raw = cur.fetchall()
                    if g_nodes_raw:
                        nodes_dict: dict[str, dict] = {}
                        for nid, ntype, rpath, nm, pjson in g_nodes_raw:
                            nd_obj = json.loads(pjson) if (pjson and pjson != "{}") else {}
                            nd_obj["id"] = nid
                            nd_obj["type"] = ntype
                            if rpath:
                                nd_obj["rel_path"] = rpath
                            if nm:
                                nd_obj["name"] = nm
                            nodes_dict[nid] = nd_obj
                        cur.execute("SELECT src_id, dst_id, edge_type, confidence, line, reason FROM edges;")
                        edges_list: list[dict] = []
                        for src_id, dst_id, et, conf, ln, rsn in cur.fetchall():
                            e_obj: dict = {
                                "src": src_id,
                                "dst": dst_id,
                                "type": et,
                                "confidence": conf,
                            }
                            if ln:
                                e_obj["line"] = int(ln)
                            if rsn:
                                e_obj["reason"] = rsn
                            edges_list.append(e_obj)
                        out["graph_json"] = {"nodes": nodes_dict, "edges": edges_list}

                cols = {row[1] for row in cur.execute("PRAGMA table_info(files)").fetchall()}
                if "interface_hash" in cols:
                    cur.execute("SELECT rel_path, mtime, size, interface_hash, info_json FROM files;")
                    files_dict: dict[str, dict] = {}
                    for rel, mtime, size, if_hash, info_json in cur.fetchall():
                        info_d = json.loads(info_json)
                        if if_hash and "interface_hash" not in info_d:
                            info_d["interface_hash"] = if_hash
                        files_dict[rel] = {
                            "mtime": mtime,
                            "size": size,
                            "info": info_d,
                        }
                else:
                    cur.execute("SELECT rel_path, mtime, size, info_json FROM files;")
                    files_dict = {}
                    for rel, mtime, size, info_json in cur.fetchall():
                        files_dict[rel] = {
                            "mtime": mtime,
                            "size": size,
                            "info": json.loads(info_json),
                        }
                out["files"] = files_dict

                cur.execute("SELECT rel_path, reason FROM skipped;")
                out["skipped"] = dict(cur.fetchall())
                return out
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, OSError):
            return None
