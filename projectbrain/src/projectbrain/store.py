"""Normalized SQLite WAL Fact Store for ProjectBrain (`.projectbrain.db`).

Provides ACID transactions, WAL concurrency, O(1) incremental file updates,
Salsa-style `interface_hash` vs `body_hash` tracking on symbols and files,
compact integer-keyed `WITHOUT ROWID` covering B-tree indexes for forward and
backward graph edges, zlib-compressed AST/4D payloads, FTS5 `detail='col'`
sub-token symbol search (`fts_symbols`), 3-hop `WITH RECURSIVE` CTE graph
queries, and a Stacked In-Memory Delta Overlay (`DELTA_DB`) for zero-disk
dry-run verification.
"""
from __future__ import annotations

import base64
import difflib
import gc
import json
import re
import sqlite3
import zlib
from collections.abc import Iterator, MutableMapping
from pathlib import Path

_PERF_INDEXED_DBS: set[str] = set()
_SUPPORTS_CONTENTLESS_DELETE_FTS = sqlite3.sqlite_version_info >= (3, 43, 0)
_SPLIT_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_SPLIT_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_IMPORT_RESOLVED_RSN_RE = re.compile(r"^'([A-Za-z0-9_$]+)' resolved via import from (.+)$")
_AMBIG_SYM_RSN_RE = re.compile(
    r"^'([A-Za-z0-9_$]+)' matches (\d+) (symbol\(s\)|class\(es\)) by name; target ambiguous without import resolution$"
)
_QUAL_RSN_RE = re.compile(
    r"^'([^']+)\(\)' (?:resolved (?:to (method on enclosing class|inherited method on base class) ([^ ]+)|via (?:static receiver type '([^']+)'|imported (class|module) '([^']+)' from (.+)))|matched uniquely to (.+))$"
)

_GN_ROW_COLS = "node_id, node_type, rel_path, name, line_start, line_end, signature, interface_hash, body_hash, calls_sig, props_json"
_GN_SELECT_COLS = "nid, " + _GN_ROW_COLS
_NODE_COLUMN_KEYS = frozenset({
    "id", "type", "rel_path", "name",
    "line_start", "line_end", "signature", "interface_hash", "ast_hash", "calls_sig",
})

_REASON_TO_CODE = {
    "direct definition in file": "@d",
    "method defined directly in class body": "@m",
    "module import resolved to repository file": "@i",
    "import statement in file": "@p",
    "named export resolved to same-file definition": "@e",
    "same-file function call": "@s",
    "nested helper/callback called in function": "@1",
    "nested helper/callback defined in function": "@2",
    "same-file base class": "@3",
    "handler function defined in same file as route decorator": "@4",
}
_CODE_TO_REASON = {v: k for v, k in ((v, k) for k, v in _REASON_TO_CODE.items())}

_EDGE_TYPE_TO_CODE = {
    "calls": "c",
    "defines": "d",
    "imports": "i",
    "has_method": "m",
    "exports": "x",
    "inherits": "h",
    "handles": "r",
    "state_flow": "s",
    "defines_nested": "n",
}
_CODE_TO_EDGE_TYPE = {v: k for k, v in _EDGE_TYPE_TO_CODE.items()}

_CONF_TO_CODE = {
    "exact": "e",
    "import_resolved": "r",
    "candidate": "c",
    "qualified": "q",
}
_CODE_TO_CONF = {v: k for k, v in _CONF_TO_CODE.items()}

_ZERO_ARITY_SPEC = {
    "min_args": 0,
    "max_args": 0,
    "has_varargs": False,
    "has_kwargs": False,
    "kwonly_required": [],
    "param_names": [],
    "pos_param_names": [],
    "required_pos_names": [],
}

_PROP_KEY_TO_SHORT = {
    "signature": "sg",
    "calls_sig": "cs",
    "interface_hash": "ih",
    "ast_hash": "ah",
    "line_start": "ls",
    "line_end": "le",
    "parent_class": "pc",
    "is_method": "im",
    "effect_provenance": "ep",
    "transitive_exits": "te",
    "transitive_z_write": "tz",
    "transitive_effects": "tf",
    "local_z_write": "lw",
    "local_z_read": "lr",
    "exit_contracts": "ec",
    "local_effects": "lf",
    "return_dict_keys": "rk",
    "temporal_mode": "tm",
    "fields": "fd",
    "bases": "bs",
}
_SHORT_TO_PROP_KEY = {v: k for k, v in _PROP_KEY_TO_SHORT.items()}


def _encode_json_blob(obj: dict, level: int = 1) -> bytes:
    """Serialize `obj` to compact JSON and compress with zlib (`level=6` on full write, `1` incremental)."""
    if not obj:
        return b""
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), level=level)


def _decode_json_blob(val: bytes | bytearray | memoryview | str | None) -> dict:
    """Decompress/deserialize a BLOB (plain UTF-8 `{...}` or zlib `0x78...`) or legacy TEXT JSON column."""
    if not val:
        return {}
    if isinstance(val, (bytes, bytearray, memoryview)):
        if val[0] == 0x7B:
            return json.loads(val)
        return json.loads(zlib.decompress(val))
    if val == "{}":
        return {}
    return json.loads(val)


def _compact_fn_entry(fn: dict) -> dict:
    out: dict = {}
    csa = fn.get("call_sites_all")
    cs = fn.get("call_sites")
    calls = fn.get("calls")
    can_derive_calls = (
        isinstance(csa, dict)
        and isinstance(cs, dict)
        and isinstance(calls, list)
        and cs == {k: v[0] for k, v in csa.items() if v}
        and calls == list(cs.keys())
    )
    for k, v in fn.items():
        if k in ("call_sites", "calls") and can_derive_calls:
            continue
        if k in ("call_sites_all", "param_types", "qualified_calls") and v == {}:
            continue
        if k == "body_tokens" and v == []:
            continue
        if k == "return_type" and v == "":
            continue
        if k == "docstring" and v is None:
            continue
        if k == "arity_spec" and isinstance(v, dict):
            if v == _ZERO_ARITY_SPEC:
                continue
            mn = v.get("min_args", 0)
            p = v.get("param_names", [])
            pp = v.get("pos_param_names", [])
            rp = v.get("required_pos_names", [])
            as_c: dict = {"mn": mn, "mx": v.get("max_args")}
            if p:
                as_c["p"] = p
            if pp != p:
                as_c["pp"] = pp
            if rp != pp[:mn]:
                as_c["rp"] = rp
            if v.get("has_varargs"):
                as_c["va"] = True
            if v.get("has_kwargs"):
                as_c["kw"] = True
            if v.get("kwonly_required"):
                as_c["kr"] = v["kwonly_required"]
            out["_as"] = as_c
            continue
        out[k] = v
    return out


def _expand_fn_entry(fn: dict) -> dict:
    out = dict(fn)
    if "_as" in out:
        as_c = out.pop("_as")
        mn = as_c.get("mn", 0)
        p = list(as_c.get("p") or ())
        pp = list(as_c["pp"]) if "pp" in as_c else list(p)
        rp = list(as_c["rp"]) if "rp" in as_c else list(pp[:mn])
        out["arity_spec"] = {
            "min_args": mn,
            "max_args": as_c.get("mx"),
            "has_varargs": bool(as_c.get("va", False)),
            "has_kwargs": bool(as_c.get("kw", False)),
            "kwonly_required": list(as_c.get("kr") or ()),
            "param_names": p,
            "pos_param_names": pp,
            "required_pos_names": rp,
        }
    elif "arity_spec" not in out:
        out["arity_spec"] = {
            "min_args": 0,
            "max_args": 0,
            "has_varargs": False,
            "has_kwargs": False,
            "kwonly_required": [],
            "param_names": [],
            "pos_param_names": [],
            "required_pos_names": [],
        }
    csa = out.setdefault("call_sites_all", {})
    if "call_sites" not in out:
        out["call_sites"] = {k: v[0] for k, v in csa.items() if v}
    if "calls" not in out:
        out["calls"] = list(out["call_sites"].keys())
    out.setdefault("return_type", "")
    out.setdefault("docstring", None)
    out.setdefault("body_tokens", [])
    out.setdefault("qualified_calls", {})
    out.setdefault("param_types", {})
    return out


def _encode_file_blob(info: dict, level: int = 1) -> bytes:
    """Losslessly compact `files.info_json` (deduping redundant call/arity/empty fields) before zlib compression."""
    if not info:
        return b""
    compact: dict = {"_c": 1}
    for k, v in info.items():
        if k in ("mtime", "interface_hash"):
            continue
        if k in ("classes", "imports", "exports", "routes", "cells_4d") and v == []:
            continue
        if k in ("import_bindings", "const_aliases", "_resolved_sym_map") and v == {}:
            continue
        if k == "parse_status" and v == "ok":
            continue
        if k == "functions":
            if v:
                compact["functions"] = [_compact_fn_entry(f) for f in v]
            continue
        compact[k] = v
    return _encode_json_blob(compact, level=level)


def _decode_file_blob(val: bytes | bytearray | memoryview | str | None) -> dict:
    """Inverse of `_encode_file_blob` — losslessly restores full `info` dict (or passes legacy blobs through)."""
    info_d = _decode_json_blob(val)
    if not info_d or info_d.pop("_c", None) != 1:
        return info_d
    fns = info_d.get("functions")
    if fns:
        info_d["functions"] = [_expand_fn_entry(f) for f in fns]
    else:
        info_d.setdefault("functions", [])
    info_d.setdefault("classes", [])
    info_d.setdefault("imports", [])
    info_d.setdefault("import_bindings", {})
    info_d.setdefault("exports", [])
    info_d.setdefault("routes", [])
    info_d.setdefault("parse_status", "ok")
    return info_d


def _encode_props_blob(extra: dict, level: int = 6) -> bytes:
    """Pack `graph_nodes.props_json` with short keys and ?Z/ΔZ dedup; leave <220B plain UTF-8 for zero-zlib load."""
    if not extra:
        return b""
    packed: dict = {}
    for k, v in extra.items():
        if (k == "is_method" and v is False) or (k == "parent_class" and v is None):
            continue
        if k in ("bases", "fields") and v == []:
            continue
        if k == "effect_provenance" and isinstance(v, dict):
            ep_compact = {
                ek: ev
                for ek, ev in v.items()
                if not (ek.startswith("?Z:") and v.get("ΔZ:" + ek[3:]) == ev)
            }
            packed["ep"] = ep_compact
        else:
            packed[_PROP_KEY_TO_SHORT.get(k, k)] = v
    if not packed:
        return b""
    raw = json.dumps(packed, separators=(",", ":")).encode("utf-8")
    if len(raw) < 220:
        return raw
    return zlib.compress(raw, level=level)


def _decode_props_blob(val: bytes | bytearray | memoryview | str | None, ntype: str = "") -> dict:
    """Inverse of `_encode_props_blob` — restores full property keys, node-type defaults, and `?Z:` aliases."""
    raw_d = _decode_json_blob(val)
    out: dict = {}
    if raw_d:
        for k, v in raw_d.items():
            full_k = _SHORT_TO_PROP_KEY.get(k, k)
            if full_k == "effect_provenance" and isinstance(v, dict):
                ep_full = dict(v)
                for ek, ev in v.items():
                    if ek.startswith("ΔZ:"):
                        ep_full.setdefault("?Z:" + ek[3:], ev)
                out["effect_provenance"] = ep_full
            else:
                out[full_k] = v
    if ntype == "fn":
        out.setdefault("is_method", False)
        out.setdefault("parent_class", None)
    elif ntype == "cls":
        out.setdefault("bases", [])
        out.setdefault("fields", [])
    return out


class LazyFileIndex(MutableMapping):
    """Order-preserving lazy file index mapping `rel_path -> info_dict`.

    Holds compressed `info_json` bytes from SQLite and materializes each file's `dict`
    only on first read (`[rel]`, `.get(rel)`, `.items()`, `.values()`), while exposing
    `get_mtime(rel)` in O(1) without decompression.
    """

    __slots__ = ("_keys", "_raw", "_decoded")

    def __init__(self, raw_entries: dict[str, tuple[float | None, str, bytes | str]] | None = None):
        self._keys: dict[str, None] = {}
        self._raw: dict[str, tuple[float | None, str, bytes | str]] = {}
        self._decoded: dict[str, dict] = {}
        if raw_entries:
            for k, tup in raw_entries.items():
                self._keys[k] = None
                self._raw[k] = tup

    def get_mtime(self, key: str) -> float | None:
        raw_t = self._raw.get(key)
        if raw_t is not None:
            return raw_t[0]
        dec = self._decoded.get(key)
        return dec.get("mtime") if dec is not None else None

    def _materialize(self, key: str) -> dict:
        mtime, if_hash, blob = self._raw.pop(key)
        info_d = _decode_file_blob(blob)
        if if_hash and "interface_hash" not in info_d:
            info_d["interface_hash"] = if_hash
        info_d["mtime"] = mtime
        self._decoded[key] = info_d
        return info_d

    def __getitem__(self, key: str) -> dict:
        if key in self._decoded:
            return self._decoded[key]
        if key in self._raw:
            return self._materialize(key)
        raise KeyError(key)

    def __setitem__(self, key: str, value: dict) -> None:
        self._keys[key] = None
        self._raw.pop(key, None)
        self._decoded[key] = value

    def __delitem__(self, key: str) -> None:
        if key not in self._keys:
            raise KeyError(key)
        del self._keys[key]
        self._raw.pop(key, None)
        self._decoded.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def get(self, key: str, default=None):  # type: ignore[override]
        if key in self._decoded:
            return self._decoded[key]
        if key in self._raw:
            return self._materialize(key)
        return default

    def pop(self, key: str, *args):  # type: ignore[override]
        if key not in self._keys:
            if args:
                return args[0]
            raise KeyError(key)
        val = self[key]
        del self[key]
        return val


def pack_symbol_imports(sym_imports: dict[str, dict[str, str]]) -> str:
    """Compress non-empty `_symbol_imports` into a compact base85 zlib string for `meta`."""
    non_empty = {k: v for k, v in sym_imports.items() if v}
    if not non_empty:
        return ""
    raw = json.dumps(non_empty, separators=(",", ":")).encode("utf-8")
    return base64.b85encode(zlib.compress(raw, 6)).decode("ascii")


def unpack_symbol_imports(b85_str: str | None) -> dict[str, dict[str, str]]:
    """Decompress `symbol_imports_b85` from `meta` in <1ms."""
    if not b85_str:
        return {}
    return json.loads(zlib.decompress(base64.b85decode(b85_str.encode("ascii"))))


def compute_stats_summary(index: MutableMapping | dict) -> dict:
    """Precompute `stats()` symbol/cell/parse_status counts so `stats()` never materializes `LazyFileIndex`."""
    p_counts = {"ok": 0, "partial": 0, "unsupported": 0}
    fn_c = cls_c = rt_c = c4_c = 0
    for v in index.values():
        fn_c += len(v.get("functions", ()))
        cls_c += len(v.get("classes", ()))
        rt_c += len(v.get("routes", ()))
        c4_c += len(v.get("cells_4d", ()))
        st = v.get("parse_status", "ok")
        p_counts[st] = p_counts.get(st, 0) + 1
    return {
        "functions": fn_c,
        "classes": cls_c,
        "routes": rt_c,
        "cells_4d": c4_c,
        "parse_status": p_counts,
    }


def _pack_reason(reason: str, dst_id: str) -> str:
    """Losslessly pack high-frequency edge reason templates into compact codes."""
    if not reason:
        return ""
    fixed = _REASON_TO_CODE.get(reason)
    if fixed is not None:
        return fixed
    if reason.startswith("'") and dst_id.startswith(("fn:", "cls:")):
        parts = dst_id.split(":", 2)
        if len(parts) == 3:
            rel, qname = parts[1], parts[2]
            base_sym = qname.rsplit(".", 1)[-1]
            cls_prefix = qname.split(".", 1)[0] if "." in qname else ""
            m_imp = _IMPORT_RESOLVED_RSN_RE.match(reason)
            if m_imp and m_imp.group(2) == rel:
                if m_imp.group(1) == qname:
                    return "@r"
                if m_imp.group(1) == base_sym:
                    return "@R"
            m_amb = _AMBIG_SYM_RSN_RE.match(reason)
            if m_amb and m_amb.group(1) == base_sym:
                return f"@a{m_amb.group(2)}" if m_amb.group(3) == "symbol(s)" else f"@b{m_amb.group(2)}"
            m_q = _QUAL_RSN_RE.match(reason)
            if m_q:
                q_expr = m_q.group(1)
                if m_q.group(2) == "method on enclosing class" and m_q.group(3) == cls_prefix:
                    return "@c" if q_expr == f"self.{base_sym}" else f"@c:{q_expr}"
                if m_q.group(2) == "inherited method on base class" and m_q.group(3) == cls_prefix:
                    return f"@h:{q_expr}"
                if m_q.group(4) and m_q.group(4) == cls_prefix:
                    return f"@t:{q_expr}"
                if m_q.group(5) == "class" and m_q.group(6) == cls_prefix and m_q.group(7) == rel:
                    return f"@k:{q_expr}"
                if m_q.group(5) == "module" and m_q.group(7) == rel and ":" not in m_q.group(6):
                    return f"@o:{m_q.group(6)}:{q_expr}"
                if m_q.group(8) and m_q.group(8) == qname:
                    return "@u" if q_expr == qname else f"@u:{q_expr}"
    if reason.startswith("@"):
        return "@" + reason
    return reason


def _unpack_reason(code: str, dst_id: str) -> str:
    """Exact inverse of `_pack_reason` — restores the byte-identical reason string."""
    if not code or not code.startswith("@"):
        return code
    if code.startswith("@@"):
        return code[1:]
    fixed = _CODE_TO_REASON.get(code)
    if fixed is not None:
        return fixed
    if dst_id.startswith(("fn:", "cls:")):
        parts = dst_id.split(":", 2)
        if len(parts) == 3:
            rel, qname = parts[1], parts[2]
            base_sym = qname.rsplit(".", 1)[-1]
            cls_prefix = qname.split(".", 1)[0] if "." in qname else ""
            if code == "@r":
                return f"'{qname}' resolved via import from {rel}"
            if code == "@R":
                return f"'{base_sym}' resolved via import from {rel}"
            if len(code) > 2 and code[1] == "a" and code[2:].isdigit():
                return f"'{base_sym}' matches {code[2:]} symbol(s) by name; target ambiguous without import resolution"
            if len(code) > 2 and code[1] == "b" and code[2:].isdigit():
                return f"'{base_sym}' matches {code[2:]} class(es) by name; target ambiguous without import resolution"
            if code == "@c":
                return f"'self.{base_sym}()' resolved to method on enclosing class {cls_prefix}"
            if code.startswith("@c:"):
                return f"'{code[3:]}()' resolved to method on enclosing class {cls_prefix}"
            if code.startswith("@h:"):
                return f"'{code[3:]}()' resolved to inherited method on base class {cls_prefix}"
            if code.startswith("@t:"):
                return f"'{code[3:]}()' resolved via static receiver type '{cls_prefix}'"
            if code.startswith("@k:"):
                return f"'{code[3:]}()' resolved via imported class '{cls_prefix}' from {rel}"
            if code.startswith("@o:"):
                sub = code[3:].split(":", 1)
                if len(sub) == 2:
                    return f"'{sub[1]}()' resolved via imported module '{sub[0]}' from {rel}"
            if code == "@u":
                return f"'{qname}()' matched uniquely to {qname}"
            if code.startswith("@u:"):
                return f"'{code[3:]}()' matched uniquely to {qname}"
    return code


def _compute_has_sql(info: dict) -> int:
    if info.get("sql_schemas") or info.get("sql_alters"):
        return 1
    for imp in info.get("imports") or ():
        if "sqlite3" in imp:
            return 1
    return 0


def _split_subtokens(text: str) -> str:
    """Split camelCase, PascalCase, and snake_case identifiers into space-separated subtokens."""
    if not text:
        return ""
    parts = _SPLIT_NON_ALNUM_RE.split(text)
    tokens: list[str] = []
    for p in parts:
        if not p:
            continue
        if p.islower() or p.isupper():
            tokens.append(p.lower())
        else:
            sub = _SPLIT_CAMEL_RE.sub(" ", p).split()
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
        try:
            conn.execute("PRAGMA mmap_size=268435456;")
            conn.execute("PRAGMA cache_size=-65536;")
        except sqlite3.Error:
            pass
        return conn

    def _ensure_perf_indexes(self, conn: sqlite3.Connection) -> None:
        """Lazily ensure case-insensitive B-tree expression indexes exist on pre-existing DBs."""
        db_key = str(self.db_path)
        if db_key in _PERF_INDEXED_DBS:
            return
        try:
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name = 'idx_graph_nodes_name_low';"
                ).fetchall()
            }
            if "idx_graph_nodes_name_low" not in existing:
                has_gn = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_nodes';"
                ).fetchone()
                if has_gn:
                    with conn:
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_name_low ON graph_nodes(LOWER(name));")
            _PERF_INDEXED_DBS.add(db_key)
        except sqlite3.Error:
            pass

    def _create_secondary_indexes(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_file ON graph_nodes(rel_path);
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_name_low ON graph_nodes(LOWER(name));
            CREATE INDEX IF NOT EXISTS idx_edges_bwd ON edges(dst_nid, edge_type, src_nid);
            CREATE INDEX IF NOT EXISTS idx_file_imports_src ON file_imports(src_rel);
            CREATE INDEX IF NOT EXISTS idx_files_has_sql ON files(has_sql) WHERE has_sql = 1;
            """
        )
        _PERF_INDEXED_DBS.add(str(self.db_path))

    def _init_schema(self, conn: sqlite3.Connection, defer_secondary_indexes: bool = False) -> None:
        # Detect and upgrade pre-v8 graph/symbols/FTS tables cleanly before DDL
        sym_obj = conn.execute("SELECT type FROM sqlite_master WHERE name='symbols';").fetchone()
        if sym_obj and sym_obj[0] == "table":
            conn.execute("DROP TABLE IF EXISTS symbols;")
        gn_cols = {row[1] for row in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()}
        ed_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)").fetchall()}
        if (gn_cols and ("nid" not in gn_cols or "body_hash" not in gn_cols)) or (ed_cols and "src_nid" not in ed_cols):
            sym_v = conn.execute("SELECT type FROM sqlite_master WHERE name='symbols';").fetchone()
            if sym_v and sym_v[0] == "view":
                conn.execute("DROP VIEW IF EXISTS symbols;")
            conn.executescript("DROP TABLE IF EXISTS edges; DROP TABLE IF EXISTS graph_nodes;")
        fts_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_symbols';"
        ).fetchone()
        if fts_row and fts_row[0]:
            fts_sql_low = fts_row[0].lower()
            if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                if "contentless_delete" not in fts_sql_low or "detail=" not in fts_sql_low:
                    conn.execute("DROP TABLE IF EXISTS fts_symbols;")
            elif "detail=" not in fts_sql_low:
                conn.execute("DROP TABLE IF EXISTS fts_symbols;")
        conn.execute("DROP INDEX IF EXISTS idx_symbols_name;")

        fts_ddl = (
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
                rel_path,
                name,
                subtokens,
                signature,
                docstring,
                content='',
                contentless_delete=1,
                detail='col'
            );
            """
            if _SUPPORTS_CONTENTLESS_DELETE_FTS
            else """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
                node_id UNINDEXED,
                rel_path,
                name,
                subtokens,
                signature,
                docstring,
                detail='col'
            );
            """
        )

        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                rel_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                interface_hash TEXT NOT NULL DEFAULT '',
                has_sql INTEGER NOT NULL DEFAULT 0,
                info_json BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_nodes (
                nid INTEGER PRIMARY KEY,
                node_id TEXT UNIQUE NOT NULL,
                node_type TEXT NOT NULL,
                rel_path TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                line_start INTEGER NOT NULL DEFAULT 0,
                line_end INTEGER NOT NULL DEFAULT 0,
                signature TEXT NOT NULL DEFAULT '',
                interface_hash TEXT NOT NULL DEFAULT '',
                body_hash TEXT NOT NULL DEFAULT '',
                calls_sig TEXT NOT NULL DEFAULT '',
                props_json BLOB NOT NULL DEFAULT x''
            );
            CREATE VIEW IF NOT EXISTS symbols AS
            SELECT
                node_id,
                rel_path,
                node_type AS kind,
                name,
                line_start,
                line_end,
                signature,
                interface_hash,
                body_hash
            FROM graph_nodes
            WHERE node_type IN ('fn', 'cls');
            CREATE TABLE IF NOT EXISTS edges (
                src_nid INTEGER NOT NULL,
                edge_type TEXT NOT NULL,
                dst_nid INTEGER NOT NULL,
                line INTEGER NOT NULL DEFAULT 0,
                confidence TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (src_nid, edge_type, dst_nid, line)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS source_blobs (
                rel_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS skipped (
                rel_path TEXT PRIMARY KEY,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_imports (
                dst_rel TEXT NOT NULL,
                src_rel TEXT NOT NULL,
                PRIMARY KEY (dst_rel, src_rel)
            ) WITHOUT ROWID;
            {fts_ddl}
            """
        )
        meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(meta)").fetchall()}
        if "value" not in meta_cols and "val" in meta_cols:
            conn.execute("ALTER TABLE meta RENAME COLUMN val TO value;")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "interface_hash" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN interface_hash TEXT NOT NULL DEFAULT '';")
        if "has_sql" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN has_sql INTEGER NOT NULL DEFAULT 0;")
        if not defer_secondary_indexes:
            self._create_secondary_indexes(conn)

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
            doc = (fn.get("docstring") or "")[:240]
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
            doc = (cls.get("docstring") or "")[:240]
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
    def _encode_node_row_fields(nid_str: str, nd: dict) -> tuple[str, str, str, str, int, int, str, str, str, str, bytes]:
        ntype = str(nd.get("type", ""))
        rpath = str(nd.get("rel_path") or "")
        nm = str(nd.get("name") or "")
        ls = int(nd.get("line_start") or 0)
        le = int(nd.get("line_end") or 0)
        sig = str(nd.get("signature") or "")
        if_h = str(nd.get("interface_hash") or nd.get("ast_hash") or "")
        b_h = str(nd.get("ast_hash") or "")
        cs = str(nd.get("calls_sig") or "")
        extra = {k: v for k, v in nd.items() if k not in _NODE_COLUMN_KEYS}
        return (str(nid_str), ntype, rpath, nm, ls, le, sig, if_h, b_h, cs, _encode_props_blob(extra))

    @staticmethod
    def _build_full_graph_rows(graph_json: str | dict | None) -> tuple[list[tuple], list[tuple]]:
        if not graph_json:
            return [], []
        try:
            g_data = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
        except json.JSONDecodeError:
            return [], []

        nid_by_id: dict[str, int] = {}
        node_rows: list[tuple] = []
        next_nid = 1
        for nid_str, nd in (g_data.get("nodes") or {}).items():
            s_id = str(nid_str)
            if s_id in nid_by_id:
                continue
            nid_by_id[s_id] = next_nid
            node_rows.append((next_nid, *SQLiteIndexStore._encode_node_row_fields(s_id, nd)))
            next_nid += 1

        edge_rows: list[tuple] = []
        seen_pk: set[tuple[int, str, int, int]] = set()
        for e in g_data.get("edges", []):
            src = str(e.get("src", ""))
            dst = str(e.get("dst", ""))
            et = str(e.get("type", ""))
            if not src or not dst or not et:
                continue
            src_nid = nid_by_id.get(src)
            dst_nid = nid_by_id.get(dst)
            if src_nid is None or dst_nid is None:
                continue
            et_c = _EDGE_TYPE_TO_CODE.get(et, et)
            ln = int(e.get("line") or 0)
            pk = (src_nid, et_c, dst_nid, ln)
            if pk not in seen_pk:
                seen_pk.add(pk)
                conf = str(e.get("confidence", "candidate"))
                conf_c = _CONF_TO_CODE.get(conf, conf)
                rsn = _pack_reason(str(e.get("reason") or ""), dst)
                edge_rows.append((src_nid, et_c, dst_nid, ln, conf_c, rsn))

        # Sort by PRIMARY KEY `(src_nid, edge_type, dst_nid, line)` for ~95% B-tree page density
        edge_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
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
        fresh_db = False
        for p_del in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                if p_del.exists():
                    p_del.unlink()
            except OSError:
                pass
        if not self.db_path.exists():
            fresh_db = True
            _PERF_INDEXED_DBS.discard(str(self.db_path))
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA page_size=4096;")
            conn.execute("PRAGMA journal_mode=MEMORY;")
            conn.execute("PRAGMA synchronous=OFF;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            try:
                conn.execute("PRAGMA cache_size=-65536;")
            except sqlite3.Error:
                pass
        else:
            conn = self._connect()
        try:
            self._init_schema(conn, defer_secondary_indexes=fresh_db)
            with conn:
                if not fresh_db:
                    conn.execute("DELETE FROM meta;")
                    conn.execute("DELETE FROM files;")
                    conn.execute("DELETE FROM graph_nodes;")
                    conn.execute("DELETE FROM edges;")
                    conn.execute("DELETE FROM source_blobs;")
                    conn.execute("DELETE FROM skipped;")
                    conn.execute("DELETE FROM file_imports;")
                    if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                        conn.execute("INSERT INTO fts_symbols(fts_symbols) VALUES('delete-all');")
                    else:
                        conn.execute("DELETE FROM fts_symbols;")

                meta_rows = [(k, json.dumps(v)) for k, v in meta.items() if k != "graph_json"]
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?);", meta_rows)

                file_rows = []
                symbol_rows = []
                fts_rows = []
                blob_rows = []
                fimp_rows = []
                store_blobs = len(index) <= 400
                for rel, info in index.items():
                    mtime = float(info.get("mtime") or 0.0)
                    size = int(sizes.get(rel) or 0)
                    if_hash = str(info.get("interface_hash") or "")
                    has_sql = _compute_has_sql(info)
                    file_rows.append((rel, mtime, size, if_hash, has_sql, _encode_file_blob(info, level=6)))
                    for dst_t in info.get("_resolved_targets") or ():
                        if dst_t and dst_t != rel:
                            fimp_rows.append((dst_t, rel))
                    s_r, f_r = self._extract_symbol_and_fts_rows(rel, info)
                    symbol_rows.extend(s_r)
                    fts_rows.extend(f_r)
                    if store_blobs:
                        b_row = self._read_source_for_blob(rel, info)
                        if b_row is not None:
                            blob_rows.append(b_row)

                file_rows.sort(key=lambda r: r[0])
                conn.executemany(
                    "INSERT INTO files (rel_path, mtime, size, interface_hash, has_sql, info_json) VALUES (?, ?, ?, ?, ?, ?);",
                    file_rows,
                )
                if fimp_rows:
                    fimp_rows.sort()
                    conn.executemany(
                        "INSERT OR IGNORE INTO file_imports (dst_rel, src_rel) VALUES (?, ?);",
                        fimp_rows,
                    )
                if blob_rows:
                    blob_rows.sort(key=lambda r: r[0])
                    conn.executemany(
                        "INSERT OR REPLACE INTO source_blobs (rel_path, content_hash, source_text) VALUES (?, ?, ?);",
                        blob_rows,
                    )
                node_rows, edge_rows = self._build_full_graph_rows(graph_json)
                if not node_rows and symbol_rows:
                    dedup_sym = sorted({r[0]: r for r in symbol_rows}.values(), key=lambda r: r[0])
                    node_rows = [
                        (idx + 1, r[0], r[2], r[1], r[3], r[4], r[5], r[6], r[7], r[8], "", b"")
                        for idx, r in enumerate(dedup_sym)
                    ]
                if node_rows:
                    conn.executemany(
                        f"INSERT INTO graph_nodes ({_GN_SELECT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        node_rows,
                    )
                if edge_rows:
                    conn.executemany(
                        """INSERT INTO edges
                           (src_nid, edge_type, dst_nid, line, confidence, reason)
                           VALUES (?, ?, ?, ?, ?, ?);""",
                        edge_rows,
                    )
                if fts_rows:
                    if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                        sym_rowid_map = {
                            r[1]: int(r[0])
                            for r in node_rows
                            if r[2] in ("fn", "cls")
                        }
                        dedup_fts = {r[0]: r for r in fts_rows}
                        fts_cd_rows = [
                            (sym_rowid_map[nid], r[1], r[2], r[3], r[4], r[5])
                            for nid, r in dedup_fts.items()
                            if nid in sym_rowid_map
                        ]
                        fts_cd_rows.sort(key=lambda r: r[0])
                        conn.executemany(
                            """INSERT INTO fts_symbols (rowid, rel_path, name, subtokens, signature, docstring)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            fts_cd_rows,
                        )
                    else:
                        conn.executemany(
                            """INSERT INTO fts_symbols (node_id, rel_path, name, subtokens, signature, docstring)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            fts_rows,
                        )
                if skipped:
                    conn.executemany(
                        "INSERT INTO skipped (rel_path, reason) VALUES (?, ?);",
                        sorted(skipped.items()),
                    )
            if fresh_db:
                with conn:
                    self._create_secondary_indexes(conn)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
        finally:
            conn.close()

    @staticmethod
    def _lookup_nids_conn(conn: sqlite3.Connection, node_ids: list[str] | set[str]) -> dict[str, int]:
        if not node_ids:
            return {}
        id_list = list(dict.fromkeys(node_ids))
        out: dict[str, int] = {}
        for i in range(0, len(id_list), 400):
            chunk = id_list[i : i + 400]
            ph = ",".join("?" for _ in chunk)
            for nid_int, nid_str in conn.execute(
                f"SELECT nid, node_id FROM graph_nodes WHERE node_id IN ({ph});",
                chunk,
            ).fetchall():
                out[nid_str] = int(nid_int)
        return out

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
                if "stats_summary" not in meta:
                    conn.execute("DELETE FROM meta WHERE key = 'stats_summary';")

                dirty_fts_rels = list(deleted_rels | set(upserted_index.keys()))
                if dirty_fts_rels:
                    for i in range(0, len(dirty_fts_rels), 400):
                        chunk_df = dirty_fts_rels[i : i + 400]
                        ph_df = ",".join("?" for _ in chunk_df)
                        if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                            conn.execute(
                                f"DELETE FROM fts_symbols WHERE rowid IN (SELECT nid FROM graph_nodes WHERE rel_path IN ({ph_df}) AND node_type IN ('fn', 'cls'));",
                                chunk_df,
                            )
                        else:
                            conn.execute(f"DELETE FROM fts_symbols WHERE rel_path IN ({ph_df});", chunk_df)

                for rel in deleted_rels:
                    conn.execute("DELETE FROM files WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM source_blobs WHERE rel_path = ?;", (rel,))
                    conn.execute("DELETE FROM file_imports WHERE src_rel = ? OR dst_rel = ?;", (rel, rel))

                existing_file_cnt = int(conn.execute("SELECT COUNT(*) FROM files;").fetchone()[0])
                store_blobs = (existing_file_cnt + len(upserted_index)) <= 400

                file_rows = []
                symbol_rows = []
                fts_rows = []
                blob_rows = []
                fimp_rows = []
                for rel, info in upserted_index.items():
                    conn.execute("DELETE FROM file_imports WHERE src_rel = ?;", (rel,))
                    mtime = float(info.get("mtime") or 0.0)
                    size = int(sizes.get(rel) or 0)
                    if_hash = str(info.get("interface_hash") or "")
                    has_sql = _compute_has_sql(info)
                    file_rows.append((rel, mtime, size, if_hash, has_sql, _encode_file_blob(info, level=1)))
                    for dst_t in info.get("_resolved_targets") or ():
                        if dst_t and dst_t != rel:
                            fimp_rows.append((dst_t, rel))
                    s_r, f_r = self._extract_symbol_and_fts_rows(rel, info)
                    symbol_rows.extend(s_r)
                    fts_rows.extend(f_r)
                    if store_blobs:
                        b_row = self._read_source_for_blob(rel, info)
                        if b_row is not None:
                            blob_rows.append(b_row)

                if file_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO files (rel_path, mtime, size, interface_hash, has_sql, info_json) VALUES (?, ?, ?, ?, ?, ?);",
                        file_rows,
                    )
                if fimp_rows:
                    conn.executemany(
                        "INSERT OR IGNORE INTO file_imports (dst_rel, src_rel) VALUES (?, ?);",
                        fimp_rows,
                    )
                if blob_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO source_blobs (rel_path, content_hash, source_text) VALUES (?, ?, ?);",
                        blob_rows,
                    )
                if graph_delta is not None:
                    rem_rels = list(graph_delta.get("removed_rels") or [])
                    rem_nids = set(graph_delta.get("removed_node_ids") or [])
                    reb_sids = list(graph_delta.get("rebuild_src_ids") or [])
                    upserted_nodes: dict[str, dict] = graph_delta.get("upserted_nodes") or {}
                    new_edges: list[dict] = graph_delta.get("new_edges") or []

                    # 1. Upsert surviving & new nodes in-place (`ON CONFLICT(node_id) DO UPDATE`)
                    #    so surviving symbols preserve their integer `nid` and incoming caller edges!
                    if upserted_nodes:
                        up_rows = [
                            self._encode_node_row_fields(nid_s, nd)
                            for nid_s, nd in upserted_nodes.items()
                        ]
                        conn.executemany(
                            f"""INSERT INTO graph_nodes ({_GN_ROW_COLS})
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(node_id) DO UPDATE SET
                                   node_type = excluded.node_type,
                                   rel_path = excluded.rel_path,
                                   name = excluded.name,
                                   line_start = excluded.line_start,
                                   line_end = excluded.line_end,
                                   signature = excluded.signature,
                                   interface_hash = excluded.interface_hash,
                                   body_hash = excluded.body_hash,
                                   calls_sig = excluded.calls_sig,
                                   props_json = excluded.props_json;""",
                            up_rows,
                        )

                    # 2. Identify truly obsolete nodes (deleted files or removed symbols in edited files)
                    dead_int_nids: set[int] = set()
                    if rem_rels:
                        for i in range(0, len(rem_rels), 400):
                            chunk_r = rem_rels[i : i + 400]
                            ph_r = ",".join("?" for _ in chunk_r)
                            for nid_i, nid_s in conn.execute(
                                f"SELECT nid, node_id FROM graph_nodes WHERE rel_path IN ({ph_r});",
                                chunk_r,
                            ).fetchall():
                                if nid_s not in upserted_nodes:
                                    dead_int_nids.add(int(nid_i))
                    if rem_nids:
                        dead_int_nids.update(self._lookup_nids_conn(conn, rem_nids).values())

                    if dead_int_nids:
                        dead_tuples = [(x,) for x in dead_int_nids]
                        conn.executemany("DELETE FROM edges WHERE src_nid = ?;", dead_tuples)
                        conn.executemany("DELETE FROM edges WHERE dst_nid = ?;", dead_tuples)
                        conn.executemany("DELETE FROM graph_nodes WHERE nid = ?;", dead_tuples)

                    # 3. Clear rebuilt outgoing edges
                    if reb_sids:
                        reb_int_nids = self._lookup_nids_conn(conn, reb_sids)
                        if reb_int_nids:
                            conn.executemany(
                                "DELETE FROM edges WHERE src_nid = ?;",
                                [(x,) for x in reb_int_nids.values()],
                            )

                    # 4. Insert new edges mapped to integer `nid`s
                    if new_edges:
                        needed_ids = {str(e.get("src", "")) for e in new_edges} | {
                            str(e.get("dst", "")) for e in new_edges
                        }
                        needed_ids.discard("")
                        nid_map = self._lookup_nids_conn(conn, needed_ids)
                        edge_rows_inc: list[tuple] = []
                        seen_pk_inc: set[tuple[int, str, int, int]] = set()
                        for e in new_edges:
                            src = str(e.get("src", ""))
                            dst = str(e.get("dst", ""))
                            et = str(e.get("type", ""))
                            src_i = nid_map.get(src)
                            dst_i = nid_map.get(dst)
                            if src_i is None or dst_i is None or not et:
                                continue
                            et_c = _EDGE_TYPE_TO_CODE.get(et, et)
                            ln = int(e.get("line") or 0)
                            pk = (src_i, et_c, dst_i, ln)
                            if pk not in seen_pk_inc:
                                seen_pk_inc.add(pk)
                                conf = str(e.get("confidence", "candidate"))
                                conf_c = _CONF_TO_CODE.get(conf, conf)
                                rsn = _pack_reason(str(e.get("reason") or ""), dst)
                                edge_rows_inc.append((src_i, et_c, dst_i, ln, conf_c, rsn))
                        if edge_rows_inc:
                            edge_rows_inc.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
                            conn.executemany(
                                """INSERT OR REPLACE INTO edges
                                   (src_nid, edge_type, dst_nid, line, confidence, reason)
                                   VALUES (?, ?, ?, ?, ?, ?);""",
                                edge_rows_inc,
                            )
                elif graph_json is not None:
                    conn.execute("DELETE FROM graph_nodes;")
                    conn.execute("DELETE FROM edges;")
                    node_rows, edge_rows = self._build_full_graph_rows(graph_json)
                    if node_rows:
                        conn.executemany(
                            f"INSERT INTO graph_nodes ({_GN_SELECT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                            node_rows,
                        )
                    if edge_rows:
                        conn.executemany(
                            """INSERT INTO edges
                               (src_nid, edge_type, dst_nid, line, confidence, reason)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            edge_rows,
                        )
                else:
                    if deleted_rels:
                        del_nids = [
                            (int(r[0]),)
                            for rel_d in deleted_rels
                            for r in conn.execute("SELECT nid FROM graph_nodes WHERE rel_path = ?;", (rel_d,)).fetchall()
                        ]
                        if del_nids:
                            conn.executemany("DELETE FROM edges WHERE src_nid = ?;", del_nids)
                            conn.executemany("DELETE FROM edges WHERE dst_nid = ?;", del_nids)
                            conn.executemany("DELETE FROM graph_nodes WHERE nid = ?;", del_nids)
                    if symbol_rows:
                        dedup_sym = list({r[0]: r for r in symbol_rows}.values())
                        conn.executemany(
                            """INSERT INTO graph_nodes
                               (node_id, node_type, rel_path, name, line_start, line_end, signature, interface_hash, body_hash)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(node_id) DO UPDATE SET
                                   line_start = excluded.line_start,
                                   line_end = excluded.line_end,
                                   signature = excluded.signature,
                                   interface_hash = excluded.interface_hash,
                                   body_hash = excluded.body_hash;""",
                            [(r[0], r[2], r[1], r[3], r[4], r[5], r[6], r[7], r[8]) for r in dedup_sym],
                        )

                if fts_rows:
                    if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                        up_rels = list(upserted_index.keys())
                        sym_rowid_map: dict[str, int] = {}
                        for i in range(0, len(up_rels), 400):
                            chunk_u = up_rels[i : i + 400]
                            ph_u = ",".join("?" for _ in chunk_u)
                            for r_id, nid in conn.execute(
                                f"SELECT nid, node_id FROM graph_nodes WHERE rel_path IN ({ph_u}) AND node_type IN ('fn', 'cls');",
                                chunk_u,
                            ).fetchall():
                                sym_rowid_map[nid] = int(r_id)
                        dedup_fts = {r[0]: r for r in fts_rows}
                        fts_cd_rows = [
                            (sym_rowid_map[nid], r[1], r[2], r[3], r[4], r[5])
                            for nid, r in dedup_fts.items()
                            if nid in sym_rowid_map
                        ]
                        fts_cd_rows.sort(key=lambda r: r[0])
                        conn.executemany(
                            """INSERT INTO fts_symbols (rowid, rel_path, name, subtokens, signature, docstring)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            fts_cd_rows,
                        )
                    else:
                        conn.executemany(
                            """INSERT INTO fts_symbols (node_id, rel_path, name, subtokens, signature, docstring)
                               VALUES (?, ?, ?, ?, ?, ?);""",
                            fts_rows,
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
            if _SUPPORTS_CONTENTLESS_DELETE_FTS:
                cur = conn.execute(
                    """
                    SELECT gn.node_id, gn.rel_path, gn.name, gn.signature,
                           bm25(fts_symbols, 2.0, 5.0, 4.0, 2.5, 1.0) AS rank_score
                    FROM fts_symbols
                    JOIN graph_nodes gn ON gn.nid = fts_symbols.rowid
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
                        "name": (r[0].split(":", 2)[2] if r[0].startswith("fn:") and r[0].count(":") >= 2 else r[2]),
                        "signature": r[3],
                        "bm25_score": -float(r[4]),
                    }
                    for r in cur.fetchall()
                ]
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
        """Execute a bounded `WITH RECURSIVE` graph traversal over the integer `WITHOUT ROWID` edges B-tree."""
        if not self.db_path.exists() or not seed_ids or max_hops < 1:
            return []
        expanded_seeds: list[str] = []
        for s in seed_ids:
            expanded_seeds.append(s)
            if "::" in s and not s.startswith("fn:"):
                expanded_seeds.append("fn:" + s.replace("::", ":"))
        seed_ids = list(dict.fromkeys(expanded_seeds))
        conn = self._connect()
        try:
            seed_nid_map = self._lookup_nids_conn(conn, seed_ids)
            if not seed_nid_map:
                return []
            seed_nids = list(seed_nid_map.values())
            placeholders = ",".join("?" for _ in seed_nids)
            et_expanded = list(dict.fromkeys([t for et in edge_types for t in (et, _EDGE_TYPE_TO_CODE.get(et, et))]))
            et_placeholders = ",".join("?" for _ in et_expanded)
            if direction == "backward":
                sql = f"""
                WITH RECURSIVE reach(nid, hop, via_edge, path_nids) AS (
                    SELECT dst_nid, 0, '', ',' || dst_nid || ','
                    FROM edges
                    WHERE dst_nid IN ({placeholders})
                    GROUP BY dst_nid
                    UNION
                    SELECT e.src_nid, r.hop + 1, e.edge_type, r.path_nids || e.src_nid || ','
                    FROM reach r
                    JOIN edges e ON e.dst_nid = r.nid
                    WHERE r.hop < ?
                      AND e.edge_type IN ({et_placeholders})
                      AND instr(r.path_nids, ',' || e.src_nid || ',') = 0
                )
                SELECT nid, MIN(hop) AS min_hop, via_edge, path_nids
                FROM reach
                WHERE hop > 0
                GROUP BY nid
                ORDER BY min_hop ASC;
                """
            else:
                sql = f"""
                WITH RECURSIVE reach(nid, hop, via_edge, path_nids) AS (
                    SELECT src_nid, 0, '', ',' || src_nid || ','
                    FROM edges
                    WHERE src_nid IN ({placeholders})
                    GROUP BY src_nid
                    UNION
                    SELECT e.dst_nid, r.hop + 1, e.edge_type, r.path_nids || e.dst_nid || ','
                    FROM reach r
                    JOIN edges e ON e.src_nid = r.nid
                    WHERE r.hop < ?
                      AND e.edge_type IN ({et_placeholders})
                      AND instr(r.path_nids, ',' || e.dst_nid || ',') = 0
                )
                SELECT nid, MIN(hop) AS min_hop, via_edge, path_nids
                FROM reach
                WHERE hop > 0
                GROUP BY nid
                ORDER BY min_hop ASC;
                """
            params = seed_nids + [max_hops] + et_expanded
            raw_rows = conn.execute(sql, params).fetchall()
            if not raw_rows:
                return []

            all_needed_nids: set[int] = set()
            for r_nid, _, _, p_str in raw_rows:
                all_needed_nids.add(int(r_nid))
                for tok in str(p_str).strip(",").split(","):
                    if tok:
                        all_needed_nids.add(int(tok))

            id_by_nid: dict[int, str] = {}
            nid_list = list(all_needed_nids)
            for i in range(0, len(nid_list), 400):
                chunk = nid_list[i : i + 400]
                ph_i = ",".join("?" for _ in chunk)
                for n_i, n_s in conn.execute(
                    f"SELECT nid, node_id FROM graph_nodes WHERE nid IN ({ph_i});",
                    chunk,
                ).fetchall():
                    id_by_nid[int(n_i)] = n_s

            sep = " <- " if direction == "backward" else " -> "
            out_reach: list[dict] = []
            for r_nid, hop_v, via_e, p_str in raw_rows:
                nid_s = id_by_nid.get(int(r_nid))
                if not nid_s:
                    continue
                path_parts = [id_by_nid.get(int(t), t) for t in str(p_str).strip(",").split(",") if t]
                out_reach.append({
                    "node_id": nid_s,
                    "hop": int(hop_v),
                    "edge_type": _CODE_TO_EDGE_TYPE.get(via_e, via_e),
                    "path": sep.join(path_parts),
                })
            return out_reach
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def open_delta_overlay(self, patched_files: dict[str, dict]) -> sqlite3.Connection:
        """Create a Stacked In-Memory Delta Overlay (`TEMP` union views over `main`) for dry-run verification."""
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
                    WHERE s.node_id = ? OR (LOWER(s.name) = LOWER(?) AND s.name = ?)
                    ORDER BY CASE WHEN s.node_id = ? THEN 0 ELSE 1 END
                    LIMIT 1;
                    """,
                    (node_id_or_name, node_id_or_name, node_id_or_name, node_id_or_name),
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
                self._ensure_perf_indexes(conn)
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
                        WHERE LOWER(s.name) = ?;
                        """,
                        (base_name,),
                    ).fetchall()
                    norm_base = re.sub(r"[^a-z0-9]+", "", base_name)
                    if not rows and len(norm_base) >= 4:
                        rows = conn.execute(
                            """
                            SELECT s.node_id, s.rel_path, s.kind, s.name, s.line_start, s.line_end, s.signature, f.mtime
                            FROM symbols s
                            LEFT JOIN files f ON f.rel_path = s.rel_path
                            WHERE REPLACE(REPLACE(LOWER(s.name), '_', ''), '-', '') = ?;
                            """,
                            (norm_base,),
                        ).fetchall()
                out: list[dict] = []
                norm_q = re.sub(r"[^a-z0-9]+", "", q_low)
                for nid, rel, kind, name, l_s, l_e, sig, mtime in rows:
                    if ff_low and ff_low not in rel.lower():
                        continue
                    qname = nid.split(":", 2)[2] if nid.count(":") >= 2 else name
                    if (
                        sym_query.startswith(("fn:", "cls:"))
                        or q_low in (name.lower(), qname.lower())
                        or (len(norm_q) >= 4 and norm_q in (re.sub(r"[^a-z0-9]+", "", name.lower()), re.sub(r"[^a-z0-9]+", "", qname.lower())))
                    ):
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

    def get_file_fast(self, rel_or_suffix: str, include_info: bool = True) -> dict | None:
        """Fast <1ms single-file lookup in SQLite `files` table."""
        if not self.db_path.exists():
            return None
        norm = rel_or_suffix.replace("\\", "/").strip("/")
        norm_low = norm.lower()
        cols_sql = "rel_path, mtime, interface_hash, info_json" if include_info else "rel_path, mtime"
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {cols_sql} FROM files WHERE rel_path = ?;",
                    (norm,),
                ).fetchone()
                if not row:
                    cands = conn.execute(
                        f"SELECT {cols_sql} FROM files WHERE LOWER(rel_path) = ? OR LOWER(rel_path) LIKE ?;",
                        (norm_low, f"%/{norm_low}"),
                    ).fetchall()
                    if cands:
                        row = min(cands, key=lambda r: (len(r[0].split("/")), len(r[0])))
                if not row:
                    return None
                if include_info:
                    info_d = _decode_file_blob(row[3])
                    if row[2] and "interface_hash" not in info_d:
                        info_d["interface_hash"] = row[2]
                else:
                    info_d = {}
                return {
                    "rel_path": row[0],
                    "mtime": row[1],
                    "info": info_d,
                }
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, zlib.error):
            return None

    def get_indexed_mtimes_fast(self) -> tuple[dict[str, float], set[str]] | None:
        """Read `(rel_path -> mtime, skipped_set)` in <2ms without deserializing `info_json` or graph tables."""
        if not self.db_path.exists():
            return None
        try:
            conn = self._connect()
            try:
                f_rows = conn.execute("SELECT rel_path, mtime FROM files;").fetchall()
                s_rows = conn.execute("SELECT rel_path FROM skipped;").fetchall()
                return ({r[0]: float(r[1]) for r in f_rows}, {r[0] for r in s_rows})
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def suggest_missing_target_fast(
        self,
        target: str,
        sym_query: str,
        file_filter: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Compute `(did_you_mean, pkg_matches)` directly from SQLite in <8ms without full graph hydration."""
        if not self.db_path.exists():
            return [], []
        ff_low = file_filter.lower().replace("\\", "/") if file_filter else None
        q_clean = re.sub(r":\d+-\d+$", "", target).replace("\\", "/").strip("/")
        q_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", q_clean.split(":")[0]).lower()
        stem_toks = [tok for tok in q_stem.split("/")[-2:] if len(tok) >= 4] if q_stem else []
        try:
            conn = self._connect()
            try:
                all_files = [r[0] for r in conn.execute("SELECT rel_path FROM files;").fetchall()]
                pkg_matches = sorted(
                    k for k in all_files
                    if q_stem and (q_stem in k.lower() or any(tok in k.lower() for tok in stem_toks))
                )[:6]

                if ff_low:
                    sym_rows = conn.execute(
                        "SELECT node_id, rel_path, kind, name FROM symbols WHERE LOWER(rel_path) LIKE ?;",
                        (f"%{ff_low}%",),
                    ).fetchall()
                else:
                    sym_rows = conn.execute("SELECT node_id, rel_path, kind, name FROM symbols;").fetchall()

                sym_candidates: list[str] = []
                sym_map_display: dict[str, str] = {}
                seen_cands: set[str] = set()
                for nid, cand_rel, kind, name in sym_rows:
                    qn = nid.split(":", 2)[2] if (kind == "fn" and nid.count(":") >= 2) else name
                    if name not in seen_cands:
                        seen_cands.add(name)
                        sym_candidates.append(name)
                    sym_map_display.setdefault(name.lower(), f"{cand_rel}:{qn}")
                    if qn != name:
                        if qn not in seen_cands:
                            seen_cands.add(qn)
                            sym_candidates.append(qn)
                        sym_map_display.setdefault(qn.lower(), f"{cand_rel}:{qn}")

                def _fast_close_matches(word: str) -> list[str]:
                    if not word:
                        return []
                    norm_w = re.sub(r"[^a-z0-9]+", "", word.lower())
                    if not norm_w:
                        return []
                    w_len = len(norm_w)
                    w_chars = set(norm_w)
                    min_shared = max(1, (len(w_chars) + 1) // 3)
                    min_l = max(1, w_len // 3)
                    max_l = w_len * 3
                    norm_to_orig: dict[str, str] = {}
                    for c in sym_candidates:
                        nc = re.sub(r"[^a-z0-9]+", "", c.lower())
                        if nc and min_l <= len(nc) <= max_l and len(w_chars & set(nc)) >= min_shared:
                            norm_to_orig.setdefault(nc, c)
                    matched_norms = difflib.get_close_matches(norm_w, list(norm_to_orig.keys()), n=5, cutoff=0.5)
                    return [norm_to_orig[mn] for mn in matched_norms]

                close_raw = _fast_close_matches(sym_query)
                if not close_raw and "." in sym_query:
                    close_raw = _fast_close_matches(sym_query.rsplit(".", 1)[-1])
                did_you_mean = list(dict.fromkeys(sym_map_display.get(c.lower(), c) for c in close_raw))[:5]
                return did_you_mean, pkg_matches
            finally:
                conn.close()
        except sqlite3.Error:
            return [], []

    def get_ast_hashes_fast(self, node_ids: list[str] | set[str]) -> dict[str, str]:
        """Point-query `body_hash` (`ast_hash`) for specific `node_ids` in <2ms."""
        if not self.db_path.exists() or not node_ids:
            return {}
        nids = list(dict.fromkeys(node_ids))
        placeholders = ",".join("?" for _ in nids)
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT node_id, body_hash FROM symbols WHERE node_id IN ({placeholders});",
                    nids,
                ).fetchall()
                return {r[0]: r[1] for r in rows if r[1]}
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    @staticmethod
    def _row_to_node_dict(row: tuple) -> dict:
        nid, ntype, rpath, nm, ls, le, sig, if_h, b_h, cs, pjson = row
        if pjson:
            nd_obj = _decode_props_blob(pjson, ntype)
        elif ntype == "fn":
            nd_obj = {"is_method": False, "parent_class": None}
        elif ntype == "cls":
            nd_obj = {"bases": [], "fields": []}
        else:
            nd_obj = {}
        nd_obj["id"] = nid
        nd_obj["type"] = ntype
        if rpath:
            nd_obj["rel_path"] = rpath
        if nm:
            nd_obj["name"] = nm
        if ntype in ("fn", "cls"):
            nd_obj["signature"] = sig
            nd_obj["line_start"] = ls or 1
            nd_obj["line_end"] = le or 1
            nd_obj["interface_hash"] = if_h
            nd_obj["ast_hash"] = b_h
            if ntype == "fn" and cs:
                nd_obj["calls_sig"] = cs
        return nd_obj

    @staticmethod
    def _row_to_edge_dict(row: tuple) -> dict:
        src_id, dst_id, et, conf, ln, rsn = row
        e_obj: dict = {
            "src": src_id,
            "dst": dst_id,
            "type": _CODE_TO_EDGE_TYPE.get(et, et),
            "confidence": _CODE_TO_CONF.get(conf, conf),
        }
        if ln:
            e_obj["line"] = int(ln)
        if rsn:
            e_obj["reason"] = _unpack_reason(rsn, dst_id)
        return e_obj

    def _find_nodes_by_name_conn(self, conn: sqlite3.Connection, name: str) -> list[str]:
        name_clean = name.strip().replace("\\", "/")
        name_low = name_clean.lower()
        norm_key = re.sub(r"[^a-z0-9]+", "", name_low)
        wants_file = "." in name_clean.rsplit("/", 1)[-1] and not name_clean.startswith(("fn:", "cls:", "route:", "mod:"))
        exact_matches: list[str] = []
        seen: set[str] = set()

        def _add(nid: str, bucket: list[str]) -> None:
            if nid not in seen:
                seen.add(nid)
                bucket.append(nid)

        for (nid,) in conn.execute("SELECT node_id FROM graph_nodes WHERE node_id = ?;", (name_clean,)).fetchall():
            _add(nid, exact_matches)
        if wants_file:
            for (nid,) in conn.execute(
                """
                SELECT node_id FROM graph_nodes
                WHERE node_type = 'file' AND (LOWER(rel_path) = ? OR LOWER(rel_path) LIKE ?);
                """,
                (name_low, f"%/{name_low}"),
            ).fetchall():
                _add(nid, exact_matches)
        for (nid,) in conn.execute(
            "SELECT node_id FROM graph_nodes WHERE LOWER(name) = ? OR LOWER(node_id) = ?;",
            (name_low, name_low),
        ).fetchall():
            _add(nid, exact_matches)
        for (nid,) in conn.execute(
            """
            SELECT node_id FROM graph_nodes
            WHERE node_type = 'file' AND (LOWER(rel_path) = ? OR LOWER(rel_path) LIKE ?);
            """,
            (name_low, f"%/{name_low}"),
        ).fetchall():
            _add(nid, exact_matches)
        for nid, pjson in conn.execute(
            "SELECT node_id, props_json FROM graph_nodes WHERE node_type = 'route';"
        ).fetchall():
            props = _decode_props_blob(pjson)
            r_path = str(props.get("path") or "").lower()
            r_full = str(props.get("full_path") or r_path).lower()
            r_lbl = f"{props.get('method', '')} {props.get('path', '')}".strip().lower()
            r_flbl = f"{props.get('method', '')} {r_full}".strip().lower()
            if name_low in (r_path, r_full) or (r_lbl and r_lbl == name_low) or (r_flbl and r_flbl == name_low):
                _add(nid, exact_matches)

        if len(norm_key) >= 4 and len(exact_matches) < 10:
            for (nid,) in conn.execute(
                """
                SELECT node_id FROM graph_nodes
                WHERE node_type IN ('fn', 'cls')
                  AND REPLACE(REPLACE(LOWER(name), '_', ''), '-', '') = ?
                LIMIT 15;
                """,
                (norm_key,),
            ).fetchall():
                _add(nid, exact_matches)

        if len(exact_matches) >= 10:
            return exact_matches

        like_pat = f"%{name_low}%"
        partial_matches: list[str] = []
        for (nid,) in conn.execute(
            """
            SELECT node_id FROM graph_nodes
            WHERE LOWER(name) LIKE ? OR LOWER(node_id) LIKE ? OR LOWER(rel_path) LIKE ?
            LIMIT 40;
            """,
            (like_pat, like_pat, like_pat),
        ).fetchall():
            _add(nid, partial_matches)
        if not exact_matches and not partial_matches and len(norm_key) >= 5:
            norm_like = f"%{norm_key}%"
            for (nid,) in conn.execute(
                """
                SELECT node_id FROM graph_nodes
                WHERE node_type IN ('fn', 'cls')
                  AND REPLACE(REPLACE(LOWER(name), '_', ''), '-', '') LIKE ?
                LIMIT 20;
                """,
                (norm_like,),
            ).fetchall():
                _add(nid, partial_matches)
        return exact_matches + partial_matches

    def explore_fast(
        self,
        target: str,
        target_b: str | None = None,
        edge_types: list[str] | None = None,
        depth: int = 2,
    ) -> dict | None:
        """Execute `explore` (1-3 hop neighbors or shortest undirected path) directly against integer SQLite B-trees."""
        if not self.db_path.exists():
            return None
        target = target.strip()
        try:
            conn = self._connect()
            try:
                self._ensure_perf_indexes(conn)
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('graph_nodes', 'edges');"
                    ).fetchall()
                }
                if "graph_nodes" not in tables or "edges" not in tables:
                    return None

                if target_b:
                    tb = target_b.strip()
                    cands_a = self._find_nodes_by_name_conn(conn, target)
                    cands_b = self._find_nodes_by_name_conn(conn, tb)
                    if not cands_a or not cands_b:
                        return {
                            "path": [],
                            "candidates_a": cands_a[:5],
                            "candidates_b": cands_b[:5],
                            "message": "No nodes matched one or both names — try a more specific name",
                        }

                    cand_nid_map = self._lookup_nids_conn(conn, cands_a[:5] + cands_b[:5])

                    def _bidir_bfs_int(a_nid: int, b_nid: int) -> list[tuple] | None:
                        if a_nid == b_nid:
                            return []
                        path_a: dict[int, list[tuple]] = {a_nid: []}
                        path_b: dict[int, list[tuple]] = {b_nid: []}
                        front_a: list[int] = [a_nid]
                        front_b: list[int] = [b_nid]
                        while front_a and front_b:
                            if len(path_a) + len(path_b) > 5000:
                                return None
                            if len(front_a) <= len(front_b):
                                ph = ",".join("?" for _ in front_a)
                                f_rows = conn.execute(
                                    f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE src_nid IN ({ph});",
                                    front_a,
                                ).fetchall()
                                b_rows = conn.execute(
                                    f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE dst_nid IN ({ph});",
                                    front_a,
                                ).fetchall()
                                nxt_front: list[int] = []
                                meet_best: list[tuple] | None = None
                                for r in f_rows:
                                    u, v = int(r[0]), int(r[1])
                                    if v not in path_a:
                                        cand_p = path_a[u] + [("fwd", u, v, r[2], r[3], r[4], r[5])]
                                        path_a[v] = cand_p
                                        nxt_front.append(v)
                                        if v in path_b:
                                            full_p = cand_p + path_b[v]
                                            if meet_best is None or len(full_p) < len(meet_best):
                                                meet_best = full_p
                                for r in b_rows:
                                    v, u, et = int(r[0]), int(r[1]), r[2]
                                    if v not in path_a:
                                        et_full = _CODE_TO_EDGE_TYPE.get(et, et)
                                        cand_p = path_a[u] + [("inv", u, v, f"inverse_{et_full}", "", 0, "")]
                                        path_a[v] = cand_p
                                        nxt_front.append(v)
                                        if v in path_b:
                                            full_p = cand_p + path_b[v]
                                            if meet_best is None or len(full_p) < len(meet_best):
                                                meet_best = full_p
                                if meet_best is not None:
                                    return meet_best
                                front_a = nxt_front
                            else:
                                ph = ",".join("?" for _ in front_b)
                                in_rows = conn.execute(
                                    f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE dst_nid IN ({ph});",
                                    front_b,
                                ).fetchall()
                                out_rows = conn.execute(
                                    f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE src_nid IN ({ph});",
                                    front_b,
                                ).fetchall()
                                nxt_front = []
                                meet_best = None
                                for r in in_rows:
                                    prev_n, curr_n = int(r[0]), int(r[1])
                                    if prev_n not in path_b:
                                        cand_p = [("fwd", prev_n, curr_n, r[2], r[3], r[4], r[5])] + path_b[curr_n]
                                        path_b[prev_n] = cand_p
                                        nxt_front.append(prev_n)
                                        if prev_n in path_a:
                                            full_p = path_a[prev_n] + cand_p
                                            if meet_best is None or len(full_p) < len(meet_best):
                                                meet_best = full_p
                                for r in out_rows:
                                    curr_n, prev_n, et = int(r[0]), int(r[1]), r[2]
                                    if prev_n not in path_b:
                                        et_full = _CODE_TO_EDGE_TYPE.get(et, et)
                                        cand_p = [("inv", prev_n, curr_n, f"inverse_{et_full}", "", 0, "")] + path_b[curr_n]
                                        path_b[prev_n] = cand_p
                                        nxt_front.append(prev_n)
                                        if prev_n in path_a:
                                            full_p = path_a[prev_n] + cand_p
                                            if meet_best is None or len(full_p) < len(meet_best):
                                                meet_best = full_p
                                if meet_best is not None:
                                    return meet_best
                                front_b = nxt_front
                        return []

                    best_raw_path: list[tuple] | None = None
                    best_a = best_b = ""
                    for a_id in cands_a[:5]:
                        a_i = cand_nid_map.get(a_id)
                        if a_i is None:
                            continue
                        for b_id in cands_b[:5]:
                            b_i = cand_nid_map.get(b_id)
                            if b_i is None:
                                continue
                            p = _bidir_bfs_int(a_i, b_i)
                            if p is None:
                                return None
                            if p and (best_raw_path is None or len(p) < len(best_raw_path)):
                                best_raw_path, best_a, best_b = p, a_id, b_id

                    if not best_raw_path:
                        return {
                            "path": [],
                            "candidates_a": cands_a[:5],
                            "candidates_b": cands_b[:5],
                            "message": "Nodes found but no path connects them in the graph",
                        }

                    path_int_nids = list({x for step in best_raw_path for x in (step[1], step[2])})
                    ph_p = ",".join("?" for _ in path_int_nids)
                    id_by_nid = {
                        int(r[0]): r[1]
                        for r in conn.execute(
                            f"SELECT nid, node_id FROM graph_nodes WHERE nid IN ({ph_p});",
                            path_int_nids,
                        ).fetchall()
                    }
                    best_path: list[dict] = []
                    for kind, u_i, v_i, et, conf, ln, rsn in best_raw_path:
                        u_s = id_by_nid.get(u_i, str(u_i))
                        v_s = id_by_nid.get(v_i, str(v_i))
                        if kind == "inv":
                            best_path.append({"src": u_s, "dst": v_s, "type": et})
                        else:
                            best_path.append(self._row_to_edge_dict((u_s, v_s, et, conf, ln, rsn)))

                    endpoints = {
                        r[0]: self._row_to_node_dict(r)
                        for r in conn.execute(
                            f"SELECT {_GN_ROW_COLS} FROM graph_nodes WHERE node_id IN (?, ?);",
                            (best_a, best_b),
                        ).fetchall()
                    }
                    from .graph import compact_explore_result
                    return compact_explore_result({
                        "path": best_path,
                        "from_node": endpoints.get(best_a, {"id": best_a}),
                        "to_node": endpoints.get(best_b, {"id": best_b}),
                    })

                # Neighbors mode (`target_b` is None)
                exact_row = conn.execute(
                    f"SELECT {_GN_SELECT_COLS} FROM graph_nodes WHERE node_id = ?;",
                    (target,),
                ).fetchone()
                if exact_row:
                    center_nid = int(exact_row[0])
                    node_id = exact_row[1]
                else:
                    matches = self._find_nodes_by_name_conn(conn, target)
                    if not matches:
                        did_you_mean, pkg_matches = self.suggest_missing_target_fast(target, target)
                        miss_out: dict = {
                            "error": f"No graph node matching '{target}'",
                            "nodes": [],
                            "edges": [],
                        }
                        if did_you_mean:
                            miss_out["did_you_mean"] = did_you_mean
                        if pkg_matches:
                            miss_out["closest_files"] = pkg_matches
                        return miss_out
                    node_id = matches[0]
                    exact_row = conn.execute(
                        f"SELECT {_GN_SELECT_COLS} FROM graph_nodes WHERE node_id = ?;",
                        (node_id,),
                    ).fetchone()
                    if not exact_row:
                        return {"error": f"Unknown node: {node_id}", "nodes": [], "edges": []}
                    center_nid = int(exact_row[0])

                depth = min(max(depth, 1), 3)
                et_set = (
                    {t for et in edge_types for t in (et, _EDGE_TYPE_TO_CODE.get(et, et))}
                    if edge_types
                    else None
                )
                visited_nids: set[int] = {center_nid}
                seen_edge_pks: set[tuple[int, str, int, int]] = set()
                raw_edges: list[tuple[int, int, str, str, int, str]] = []
                frontier: list[int] = [center_nid]

                for _ in range(depth):
                    if not frontier:
                        break
                    ph = ",".join("?" for _ in frontier)
                    f_rows = conn.execute(
                        f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE src_nid IN ({ph});",
                        frontier,
                    ).fetchall()
                    b_rows = conn.execute(
                        f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE dst_nid IN ({ph});",
                        frontier,
                    ).fetchall()
                    next_frontier: list[int] = []
                    for r in f_rows:
                        if et_set and r[2] not in et_set:
                            continue
                        s_i, d_i, ln_i = int(r[0]), int(r[1]), int(r[4] or 0)
                        pk = (s_i, r[2], d_i, ln_i)
                        if pk not in seen_edge_pks:
                            seen_edge_pks.add(pk)
                            raw_edges.append((s_i, d_i, r[2], r[3], ln_i, r[5]))
                        if d_i not in visited_nids:
                            visited_nids.add(d_i)
                            next_frontier.append(d_i)
                    for r in b_rows:
                        if et_set and r[2] not in et_set:
                            continue
                        s_i, d_i, ln_i = int(r[0]), int(r[1]), int(r[4] or 0)
                        pk = (s_i, r[2], d_i, ln_i)
                        if pk not in seen_edge_pks:
                            seen_edge_pks.add(pk)
                            raw_edges.append((s_i, d_i, r[2], r[3], ln_i, r[5]))
                        if s_i not in visited_nids:
                            visited_nids.add(s_i)
                            next_frontier.append(s_i)
                    frontier = next_frontier

                id_by_nid: dict[int, str] = {center_nid: node_id}
                visited_nids.discard(center_nid)
                neighbor_nodes: list[dict] = []
                if visited_nids:
                    vn_list = list(visited_nids)
                    for i in range(0, len(vn_list), 500):
                        chunk = vn_list[i : i + 500]
                        ph_n = ",".join("?" for _ in chunk)
                        for nr in conn.execute(
                            f"SELECT {_GN_SELECT_COLS} FROM graph_nodes WHERE nid IN ({ph_n});",
                            chunk,
                        ).fetchall():
                            id_by_nid[int(nr[0])] = nr[1]
                            neighbor_nodes.append(self._row_to_node_dict(nr[1:]))

                collected_edges = [
                    self._row_to_edge_dict((id_by_nid[s_i], id_by_nid[d_i], et, conf, ln_i, rsn))
                    for s_i, d_i, et, conf, ln_i, rsn in raw_edges
                    if s_i in id_by_nid and d_i in id_by_nid
                ]
                from .graph import compact_explore_result
                center_dict = self._row_to_node_dict(exact_row[1:])
                compacted = compact_explore_result({
                    "center": center_dict,
                    "nodes": neighbor_nodes,
                    "edges": collected_edges,
                })
                c_rel = compacted.get("center", {}).get("rel_path")
                if c_rel:
                    sz_row = conn.execute("SELECT size FROM files WHERE rel_path = ?;", (c_rel,)).fetchone()
                    if sz_row and sz_row[0] is not None and int(sz_row[0]) <= 12_000:
                        est_lines = max(1, int(sz_row[0]) // 34)
                        compacted["direct_read_hint"] = (
                            f"'{c_rel}' is small (~{est_lines} lines, {int(sz_row[0])} bytes) — "
                            f"read_file('{c_rel}') reads the full source directly in 1 turn."
                        )
                return compacted
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, zlib.error):
            return None

    def load_all(self, lazy_files: bool = False) -> dict | None:
        if not self.db_path.exists():
            return None
        _loads = json.loads
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            conn = self._connect()
            try:
                self._ensure_perf_indexes(conn)
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
                            out["graph_json"] = _loads(v)
                        else:
                            out["graph_json"] = v
                    else:
                        out[k] = _loads(v)

                tables = {
                    row[0]
                    for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
                }
                if "graph_nodes" in tables and "edges" in tables:
                    gn_cols = {row[1] for row in cur.execute("PRAGMA table_info(graph_nodes)").fetchall()}
                    if "nid" in gn_cols:
                        nodes_dict: dict[str, dict] = {}
                        id_by_nid: dict[int, str] = {}
                        if "body_hash" in gn_cols:
                            cur.execute(f"SELECT {_GN_SELECT_COLS} FROM graph_nodes;")
                            g_nodes_raw = cur.fetchall()
                            for nr in g_nodes_raw:
                                n_int, nid, ntype, rpath, nm, ls, le, sig, if_h, b_h, cs, pjson = nr
                                id_by_nid[int(n_int)] = nid
                                if pjson:
                                    nd_obj = _decode_props_blob(pjson, ntype)
                                elif ntype == "fn":
                                    nd_obj = {"is_method": False, "parent_class": None}
                                elif ntype == "cls":
                                    nd_obj = {"bases": [], "fields": []}
                                else:
                                    nd_obj = {}
                                nd_obj["id"] = nid
                                nd_obj["type"] = ntype
                                if rpath:
                                    nd_obj["rel_path"] = rpath
                                if nm:
                                    nd_obj["name"] = nm
                                if ntype in ("fn", "cls"):
                                    nd_obj["signature"] = sig
                                    nd_obj["line_start"] = ls or 1
                                    nd_obj["line_end"] = le or 1
                                    nd_obj["interface_hash"] = if_h
                                    nd_obj["ast_hash"] = b_h
                                    if ntype == "fn" and cs:
                                        nd_obj["calls_sig"] = cs
                                nodes_dict[nid] = nd_obj
                        else:
                            cur.execute("SELECT nid, node_id, node_type, rel_path, name, props_json FROM graph_nodes;")
                            g_nodes_raw = cur.fetchall()
                            for n_int, nid, ntype, rpath, nm, pjson in g_nodes_raw:
                                id_by_nid[int(n_int)] = nid
                                nd_obj = _decode_props_blob(pjson, ntype)
                                nd_obj["id"] = nid
                                nd_obj["type"] = ntype
                                if rpath:
                                    nd_obj["rel_path"] = rpath
                                if nm:
                                    nd_obj["name"] = nm
                                nodes_dict[nid] = nd_obj
                        if g_nodes_raw:
                            cur.execute("SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges;")
                            edges_list: list[dict] = []
                            _et_map = _CODE_TO_EDGE_TYPE
                            _cf_map = _CODE_TO_CONF
                            for s_i, d_i, et, conf, ln, rsn in cur.fetchall():
                                src_id = id_by_nid.get(int(s_i))
                                dst_id = id_by_nid.get(int(d_i))
                                if not src_id or not dst_id:
                                    continue
                                e_obj: dict = {
                                    "src": src_id,
                                    "dst": dst_id,
                                    "type": _et_map.get(et, et),
                                    "confidence": _cf_map.get(conf, conf),
                                }
                                if ln:
                                    e_obj["line"] = int(ln)
                                if rsn:
                                    e_obj["reason"] = _unpack_reason(rsn, dst_id)
                                edges_list.append(e_obj)
                            out["graph_json"] = {"nodes": nodes_dict, "edges": edges_list}

                cols = {row[1] for row in cur.execute("PRAGMA table_info(files)").fetchall()}
                if lazy_files:
                    raw_entries: dict[str, tuple[float | None, str, bytes | str]] = {}
                    sizes_dict: dict[str, int] = {}
                    if "interface_hash" in cols:
                        cur.execute("SELECT rel_path, mtime, size, interface_hash, info_json FROM files;")
                        for rel, mtime, size, if_hash, info_json in cur.fetchall():
                            raw_entries[rel] = (mtime, if_hash, info_json)
                            sizes_dict[rel] = size
                    else:
                        cur.execute("SELECT rel_path, mtime, size, info_json FROM files;")
                        for rel, mtime, size, info_json in cur.fetchall():
                            raw_entries[rel] = (mtime, "", info_json)
                            sizes_dict[rel] = size
                    out["lazy_index"] = LazyFileIndex(raw_entries)
                    out["sizes"] = sizes_dict
                    out["files"] = {}
                    if "file_imports" in tables:
                        out["file_imports"] = cur.execute("SELECT dst_rel, src_rel FROM file_imports;").fetchall()
                else:
                    if "interface_hash" in cols:
                        cur.execute("SELECT rel_path, mtime, size, interface_hash, info_json FROM files;")
                        files_dict: dict[str, dict] = {}
                        for rel, mtime, size, if_hash, info_json in cur.fetchall():
                            info_d = _decode_file_blob(info_json)
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
                                "info": _decode_file_blob(info_json),
                            }
                    out["files"] = files_dict

                cur.execute("SELECT rel_path, reason FROM skipped;")
                out["skipped"] = dict(cur.fetchall())
                return out
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, zlib.error, OSError):
            return None
        finally:
            if gc_was_enabled:
                gc.enable()

    def check_boundary_violations_fast(
        self,
        memory: object,
        focus_rels: set[str],
        pre_infos: dict[str, dict],
    ) -> list[dict] | None:
        """Evaluate `--forbid` boundary rules touching `focus_rels` without hydrating the full repo."""
        rules = getattr(memory, "_boundary_rules", None) or []
        if not rules or not focus_rels:
            return []
        from .memory import _compile_path_glob

        compiled = [
            (
                r,
                _compile_path_glob(r.get("src_glob", "")),
                _compile_path_glob(r.get("dst_glob", "")),
                set(r.get("edge_types") or ("imports", "calls")),
            )
            for r in rules
            if r.get("src_glob") and r.get("dst_glob")
        ]
        if not compiled:
            return []

        matching_srcs = [f for f in sorted(focus_rels) if any(src_rx.match(f) for _, src_rx, _, _ in compiled)]
        matching_dsts = [f for f in sorted(focus_rels) if any(dst_rx.match(f) for _, _, dst_rx, _ in compiled)]
        if not matching_srcs and not matching_dsts:
            return []
        if matching_dsts and not self.db_path.exists():
            return None

        violations: list[dict] = []
        seen_keys: set[tuple[str, str, str, str]] = set()

        def _record_import(src_rel: str, dst_rel: str) -> None:
            if not src_rel or not dst_rel or src_rel == dst_rel:
                return
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

        def _record_call(
            src_rel: str, src_nm: str, src_ls: int | None, src_pjson: bytes | str, dst_rel: str, dst_nm: str, e_line: int | None
        ) -> None:
            if not src_rel or not dst_rel or src_rel == dst_rel:
                return
            for rule, src_rx, dst_rx, allowed_types in compiled:
                if "calls" not in allowed_types:
                    continue
                if src_rx.match(src_rel) and dst_rx.match(dst_rel):
                    rid = rule.get("id", rule.get("rule", ""))
                    if (rid, src_rel, dst_rel, "imports") in seen_keys:
                        continue
                    vkey = (rid, src_rel, dst_rel, "calls")
                    if vkey in seen_keys:
                        continue
                    seen_keys.add(vkey)
                    line = int(e_line or src_ls or _decode_props_blob(src_pjson).get("line_start") or 1)
                    sym = f"{src_nm or '?'} -> {dst_nm or '?'}"
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

        for src_rel in matching_srcs:
            for dst_rel in (pre_infos.get(src_rel) or {}).get("_resolved_targets") or []:
                _record_import(src_rel, dst_rel)

        if not self.db_path.exists():
            return violations

        try:
            conn = self._connect()
            try:
                self._ensure_perf_indexes(conn)
                gn_cols_b = {r[1] for r in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()}
                ls_expr = "gs.line_start" if "line_start" in gn_cols_b else "0"
                if matching_dsts:
                    has_fimp = bool(conn.execute("SELECT 1 FROM file_imports LIMIT 1;").fetchone())
                    if not has_fimp and conn.execute(
                        "SELECT 1 FROM edges WHERE edge_type IN ('i', 'imports') LIMIT 1;"
                    ).fetchone() is not None:
                        return None
                    ph_d = ",".join("?" for _ in matching_dsts)
                    if has_fimp:
                        for s_rel, d_rel in conn.execute(
                            f"SELECT src_rel, dst_rel FROM file_imports WHERE dst_rel IN ({ph_d});",
                            matching_dsts,
                        ).fetchall():
                            _record_import(s_rel, d_rel)
                    for row in conn.execute(
                        f"""
                        SELECT gs.rel_path, gs.name, {ls_expr}, gs.props_json, gd.rel_path, gd.name, e.line
                        FROM graph_nodes gd
                        JOIN edges e ON e.dst_nid = gd.nid
                        JOIN graph_nodes gs ON gs.nid = e.src_nid
                        WHERE gd.rel_path IN ({ph_d})
                          AND e.edge_type IN ('c', 'calls')
                          AND e.confidence IN ('e', 'q', 'r', 'exact', 'qualified', 'import_resolved');
                        """,
                        matching_dsts,
                    ).fetchall():
                        _record_call(*row)

                if matching_srcs:
                    ph_s = ",".join("?" for _ in matching_srcs)
                    for row in conn.execute(
                        f"""
                        SELECT gs.rel_path, gs.name, {ls_expr}, gs.props_json, gd.rel_path, gd.name, e.line
                        FROM graph_nodes gs
                        JOIN edges e ON e.src_nid = gs.nid
                        JOIN graph_nodes gd ON gd.nid = e.dst_nid
                        WHERE gs.rel_path IN ({ph_s})
                          AND e.edge_type IN ('c', 'calls')
                          AND e.confidence IN ('e', 'q', 'r', 'exact', 'qualified', 'import_resolved');
                        """,
                        matching_srcs,
                    ).fetchall():
                        _record_call(*row)
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, zlib.error):
            return None
        return violations

    def load_check_neighborhood(
        self,
        focus_rels: set[str],
        extra_called_names: set[str] | None = None,
        boundary_src_globs: list | None = None,
        need_sql_schemas: bool = False,
        need_http_routes: bool = False,
        symbols_changed: bool = False,
        mutated_node_ids: set[str] | None = None,
        min_repo_files: int = 200,
    ) -> dict | None:
        """Hydrate only the 1-2 hop connected neighborhood of `focus_rels` for O(k) `check()` on large repos.

        When `not symbols_changed` and `mutated_node_ids is not None` (an edit to specific existing
        symbol(s) without adding/deleting exports), hydrates only callers of `mutated_node_ids`
        rather than every file-level importer of a hub module, while still returning full
        `direct_importers_by_focus` for accurate `_importer_counts` / `_imported_by`.
        """
        if not self.db_path.exists() or not focus_rels:
            return None
        _loads = json.loads
        try:
            conn = self._connect()
            try:
                self._ensure_perf_indexes(conn)
                cur = conn.cursor()
                tables = {
                    r[0]
                    for r in cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view');").fetchall()
                }
                if not {"meta", "files", "graph_nodes", "edges", "symbols"}.issubset(tables):
                    return None
                gn_cols_nb = {r[1] for r in cur.execute("PRAGMA table_info(graph_nodes)").fetchall()}
                if "body_hash" not in gn_cols_nb:
                    return None

                meta_cols = {row[1] for row in cur.execute("PRAGMA table_info(meta)").fetchall()}
                col_name = "value" if "value" in meta_cols else "val"
                meta_raw = dict(cur.execute(f"SELECT key, {col_name} FROM meta;").fetchall())
                if not meta_raw:
                    return None
                out: dict = {}
                for k, v in meta_raw.items():
                    if k != "graph_json":
                        out[k] = _loads(v)

                all_files = {r[0] for r in cur.execute("SELECT rel_path FROM files;").fetchall()}
                if len(all_files) <= min_repo_files:
                    return None

                has_fimp = "file_imports" in tables and bool(
                    cur.execute("SELECT 1 FROM file_imports LIMIT 1;").fetchone()
                )

                def _importers_of(target_rel: str) -> list[str]:
                    if has_fimp:
                        return [
                            r[0]
                            for r in cur.execute(
                                "SELECT src_rel FROM file_imports WHERE dst_rel = ?;",
                                (target_rel,),
                            ).fetchall()
                        ]
                    return []

                seed_rels: set[str] = set(focus_rels & all_files)
                rebuild_importer_rels: set[str] = set()
                direct_importers_by_focus: dict[str, list[str]] = {}
                targeted_symbol_mode = (not symbols_changed) and (mutated_node_ids is not None)

                # 1. Direct importers of `focus_rels`
                for fr in sorted(focus_rels):
                    imp_hits = _importers_of(fr)
                    direct_importers_by_focus[fr] = imp_hits
                    if not targeted_symbol_mode:
                        barrels = [
                            b for b in imp_hits
                            if b.endswith(("__init__.py", "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.mjs"))
                        ][:6]
                        seed_rels.update(imp_hits)
                        if symbols_changed:
                            rebuild_importer_rels.update(imp_hits[:25])
                            for br in barrels:
                                seed_rels.update(_importers_of(br))

                # 2. Existing 1-hop incoming & outgoing graph neighbors of `focus_rels`
                existing_focus = [r for r in focus_rels if r in all_files]
                f0_int_nids: list[int] = []
                mutated_int_nids: list[int] = []
                if existing_focus:
                    ph_f = ",".join("?" for _ in existing_focus)
                    for n_i, n_s in cur.execute(
                        f"SELECT nid, node_id FROM graph_nodes WHERE rel_path IN ({ph_f});",
                        existing_focus,
                    ).fetchall():
                        ni = int(n_i)
                        f0_int_nids.append(ni)
                        if not targeted_symbol_mode or (mutated_node_ids and n_s in mutated_node_ids):
                            mutated_int_nids.append(ni)

                if mutated_int_nids:
                    for i in range(0, len(mutated_int_nids), 400):
                        chunk = mutated_int_nids[i : i + 400]
                        ph_c = ",".join("?" for _ in chunk)
                        seed_rels.update(
                            r[0]
                            for r in cur.execute(
                                f"""
                                SELECT DISTINCT g.rel_path
                                FROM edges e
                                JOIN graph_nodes g ON g.nid = e.src_nid
                                WHERE e.dst_nid IN ({ph_c}) AND g.rel_path != '';
                                """,
                                chunk,
                            ).fetchall()
                        )
                if f0_int_nids:
                    for i in range(0, len(f0_int_nids), 400):
                        chunk = f0_int_nids[i : i + 400]
                        ph_c = ",".join("?" for _ in chunk)
                        seed_rels.update(
                            r[0]
                            for r in cur.execute(
                                f"""
                                SELECT DISTINCT g.rel_path
                                FROM edges e
                                JOIN graph_nodes g ON g.nid = e.dst_nid
                                WHERE e.src_nid IN ({ph_c}) AND g.rel_path != '';
                                """,
                                chunk,
                            ).fetchall()
                        )

                # 3. Candidate symbol definitions for newly called/inherited names in `focus_rels`
                if extra_called_names:
                    names_list = sorted({nm.lower() for nm in extra_called_names})[:250]
                    for i in range(0, len(names_list), 400):
                        chunk = names_list[i : i + 400]
                        ph_n = ",".join("?" for _ in chunk)
                        seed_rels.update(
                            r[0]
                            for r in cur.execute(
                                f"SELECT DISTINCT rel_path FROM symbols WHERE LOWER(name) IN ({ph_n}) LIMIT 150;",
                                chunk,
                            ).fetchall()
                        )

                # 4. Boundary rule source globs (when a focus file matches a forbidden target glob)
                if boundary_src_globs:
                    for rx in boundary_src_globs:
                        seed_rels.update(f for f in all_files if rx.match(f))

                # 5. SQL schema / SQLite adapter files (indexed partial B-tree `has_sql = 1`)
                cols = {row[1] for row in cur.execute("PRAGMA table_info(files)").fetchall()}
                if need_sql_schemas and "has_sql" in cols:
                    seed_rels.update(
                        r[0]
                        for r in cur.execute("SELECT rel_path FROM files WHERE has_sql = 1;").fetchall()
                    )
                if need_http_routes:
                    route_rels = [
                        r[0]
                        for r in cur.execute(
                            "SELECT DISTINCT rel_path FROM graph_nodes WHERE node_type = 'route' AND rel_path != '' LIMIT 80;"
                        ).fetchall()
                    ]
                    seed_rels.update(route_rels)
                    if has_fimp and route_rels:
                        ph_rt = ",".join("?" for _ in route_rels)
                        seed_rels.update(
                            r[0]
                            for r in cur.execute(
                                f"SELECT DISTINCT dst_rel FROM file_imports WHERE src_rel IN ({ph_rt}) LIMIT 120;",
                                route_rels,
                            ).fetchall()
                            if r[0] in all_files
                        )

                has_if_col = "interface_hash" in cols
                files_dict: dict[str, dict] = {}

                def _fetch_files_batch(rels_batch: list[str]) -> None:
                    for i in range(0, len(rels_batch), 400):
                        chunk = rels_batch[i : i + 400]
                        ph_b = ",".join("?" for _ in chunk)
                        if has_if_col:
                            for rel, mtime, size, if_h, info_json in cur.execute(
                                f"SELECT rel_path, mtime, size, interface_hash, info_json FROM files WHERE rel_path IN ({ph_b});",
                                chunk,
                            ).fetchall():
                                info_d = _decode_file_blob(info_json)
                                if if_h and "interface_hash" not in info_d:
                                    info_d["interface_hash"] = if_h
                                files_dict[rel] = {"mtime": mtime, "size": size, "info": info_d}
                        else:
                            for rel, mtime, size, info_json in cur.execute(
                                f"SELECT rel_path, mtime, size, info_json FROM files WHERE rel_path IN ({ph_b});",
                                chunk,
                            ).fetchall():
                                files_dict[rel] = {"mtime": mtime, "size": size, "info": _decode_file_blob(info_json)}

                _fetch_files_batch(sorted(seed_rels))

                # Follow outgoing `_resolved_targets` / `file_imports` from focus (& barrels when untargeted)
                frontier_rels = [
                    r for r in files_dict
                    if r in focus_rels
                    or (not targeted_symbol_mode and r.endswith(("__init__.py", "/index.ts", "/index.tsx", "/index.js")))
                ]
                for _ in range(1 if targeted_symbol_mode else 2):
                    if not frontier_rels:
                        break
                    extra_targets: set[str] = set()
                    if has_fimp:
                        for i in range(0, len(frontier_rels), 400):
                            chunk = frontier_rels[i : i + 400]
                            ph_fr = ",".join("?" for _ in chunk)
                            for (dst_r,) in cur.execute(
                                f"SELECT DISTINCT dst_rel FROM file_imports WHERE src_rel IN ({ph_fr});",
                                chunk,
                            ).fetchall():
                                if dst_r in all_files and dst_r not in files_dict:
                                    extra_targets.add(dst_r)
                    for r in frontier_rels:
                        inf = files_dict[r]["info"]
                        for t in inf.get("_resolved_targets") or ():
                            if t in all_files and t not in files_dict:
                                extra_targets.add(t)
                        for t in (inf.get("_resolved_sym_map") or {}).values():
                            if t in all_files and t not in files_dict:
                                extra_targets.add(t)
                    if not extra_targets:
                        break
                    batch = sorted(extra_targets)[:200]
                    _fetch_files_batch(batch)
                    frontier_rels = batch

                # Hydrate `graph_nodes` for `files_dict` and ONLY the `edges` touching `focus_rels` (+ `rebuild_importer_rels`)
                loaded_rels = sorted(files_dict.keys())
                file_imports_subset: dict[str, list[str]] = {}
                if has_fimp and loaded_rels:
                    for i in range(0, len(loaded_rels), 400):
                        chunk = loaded_rels[i : i + 400]
                        ph_fi = ",".join("?" for _ in chunk)
                        for s_r, d_r in cur.execute(
                            f"SELECT src_rel, dst_rel FROM file_imports WHERE src_rel IN ({ph_fi});",
                            chunk,
                        ).fetchall():
                            file_imports_subset.setdefault(s_r, []).append(d_r)
                nodes_dict: dict[str, dict] = {}
                id_by_nid: dict[int, str] = {}
                nid_by_id: dict[str, int] = {}
                for i in range(0, len(loaded_rels), 400):
                    chunk = loaded_rels[i : i + 400]
                    ph_r = ",".join("?" for _ in chunk)
                    for nr in cur.execute(
                        f"SELECT {_GN_SELECT_COLS} FROM graph_nodes WHERE rel_path IN ({ph_r});",
                        chunk,
                    ).fetchall():
                        ni = int(nr[0])
                        ns = nr[1]
                        id_by_nid[ni] = ns
                        nid_by_id[ns] = ni
                        nodes_dict[ns] = self._row_to_node_dict(nr[1:])

                edge_src_rels = set(focus_rels) | rebuild_importer_rels
                if boundary_src_globs:
                    edge_src_rels.update(r for r in loaded_rels if any(rx.match(r) for rx in boundary_src_globs))
                src_int_nids = [
                    nid_by_id[ns] for ns, nd in nodes_dict.items()
                    if nd.get("rel_path") in edge_src_rels and ns in nid_by_id
                ]
                seen_edge_pks: set[tuple[int, str, int, int]] = set()
                raw_edges: list[tuple[int, int, str, str, int, str]] = []

                for i in range(0, len(src_int_nids), 400):
                    chunk = src_int_nids[i : i + 400]
                    ph_e = ",".join("?" for _ in chunk)
                    for er in cur.execute(
                        f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE src_nid IN ({ph_e});",
                        chunk,
                    ).fetchall():
                        s_i, d_i, ln_i = int(er[0]), int(er[1]), int(er[4] or 0)
                        pk = (s_i, er[2], d_i, ln_i)
                        if pk not in seen_edge_pks:
                            seen_edge_pks.add(pk)
                            raw_edges.append((s_i, d_i, er[2], er[3], ln_i, er[5]))

                dst_query_nids = mutated_int_nids if targeted_symbol_mode else f0_int_nids
                if dst_query_nids:
                    for i in range(0, len(dst_query_nids), 400):
                        chunk = dst_query_nids[i : i + 400]
                        ph_f0 = ",".join("?" for _ in chunk)
                        for er in cur.execute(
                            f"SELECT src_nid, dst_nid, edge_type, confidence, line, reason FROM edges WHERE dst_nid IN ({ph_f0});",
                            chunk,
                        ).fetchall():
                            s_i, d_i, ln_i = int(er[0]), int(er[1]), int(er[4] or 0)
                            pk = (s_i, er[2], d_i, ln_i)
                            if pk not in seen_edge_pks:
                                seen_edge_pks.add(pk)
                                raw_edges.append((s_i, d_i, er[2], er[3], ln_i, er[5]))

                missing_int_nids = list(
                    ({r[0] for r in raw_edges} | {r[1] for r in raw_edges}) - set(id_by_nid.keys())
                )
                for i in range(0, len(missing_int_nids), 400):
                    chunk = missing_int_nids[i : i + 400]
                    ph_m = ",".join("?" for _ in chunk)
                    for nr in cur.execute(
                        f"SELECT {_GN_SELECT_COLS} FROM graph_nodes WHERE nid IN ({ph_m});",
                        chunk,
                    ).fetchall():
                        ni = int(nr[0])
                        ns = nr[1]
                        id_by_nid[ni] = ns
                        nodes_dict[ns] = self._row_to_node_dict(nr[1:])

                edges_list = [
                    self._row_to_edge_dict((id_by_nid[s_i], id_by_nid[d_i], et, conf, ln_i, rsn))
                    for s_i, d_i, et, conf, ln_i, rsn in raw_edges
                    if s_i in id_by_nid and d_i in id_by_nid
                ]
                out["files"] = files_dict
                out["file_imports_subset"] = file_imports_subset
                out["graph_json"] = {"nodes": nodes_dict, "edges": edges_list}
                out["skipped"] = dict(cur.execute("SELECT rel_path, reason FROM skipped;").fetchall())
                out["all_indexed_files"] = all_files
                out["direct_importers_by_focus"] = direct_importers_by_focus
                return out
            finally:
                conn.close()
        except (sqlite3.Error, json.JSONDecodeError, zlib.error, OSError):
            return None
