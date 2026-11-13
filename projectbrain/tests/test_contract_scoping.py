"""Regression tests for call-site contract scoping (no overfit suppressions).

Breakages that MUST still be reported, and statically-unresolvable call sites that MUST be
reported as UNKNOWN (silently skipped) instead of being guessed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectbrain.brain import ProjectBrain
from projectbrain.cli import main as cli_main
from projectbrain.hooks import run_post_edit_hook


def _hook_output(root: Path, rel: str) -> str:
    return run_post_edit_hook(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": str(root),
        "tool_input": {"file_path": str(root / rel)},
    }))[1]


def _run(tmp_path: Path, files: dict[str, str], edit_rel: str, new_src: str) -> str:
    for rel, src in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(src, encoding="utf-8")
    ProjectBrain(str(tmp_path)).index()
    (tmp_path / edit_rel).write_text(new_src, encoding="utf-8")
    return _hook_output(tmp_path, edit_rel)


def test_name_mentioned_before_call_is_not_treated_as_shadowing(tmp_path: Path):
    out = _run(
        tmp_path,
        {
            "svc.py": "def charge(amount):\n    return amount\n",
            "routes.py": "from svc import charge\n\ndef pay(x):\n    print(charge, x)\n    return charge(x)\n",
        },
        "svc.py",
        "def charge(amount, currency):\n    return amount\n",
    )
    assert "[CONTRACT]" in out and "routes.py" in out


def test_resolved_receiver_class_is_checked_not_widened_to_other_classes(tmp_path: Path):
    out = _run(
        tmp_path,
        {
            "models.py": "class Order:\n    def save(self):\n        return 1\n\nclass Draft:\n    def save(self, force=False):\n        return 2\n",
            "app.py": "from models import Order\n\ndef finish():\n    o = Order()\n    return o.save()\n",
        },
        "models.py",
        "class Order:\n    def save(self, user):\n        return 1\n\nclass Draft:\n    def save(self, force=False):\n        return 2\n",
    )
    assert "[CONTRACT]" in out and "app.py" in out


def test_parameter_shadowing_imported_name_is_unknown(tmp_path: Path):
    out = _run(
        tmp_path,
        {
            "svc.py": "def func():\n    return 1\n",
            "routes.py": "from svc import func\n\ndef prompt(func, text):\n    return func(text)\n",
        },
        "svc.py",
        "def func():\n    return 2\n\ndef extra(a):\n    return a\n",
    )
    assert "[CONTRACT]" not in out


def test_factory_built_receiver_is_unknown(tmp_path: Path):
    out = _run(
        tmp_path,
        {
            "models.py": "class Request:\n    def prepare(self):\n        return 1\n\ndef build():\n    return Request()\n",
            "app.py": "from models import build\n\ndef go():\n    p = build()\n    return p.prepare(url=1)\n",
        },
        "models.py",
        "class Request:\n    def prepare(self):\n        return 3\n\ndef build():\n    return Request()\n",
    )
    assert "[CONTRACT]" not in out


def test_clean_never_deletes_user_files_without_markers(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# ProjectBrain team notes\nOur own rules.\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\nnode_modules/\n", encoding="utf-8")
    assert cli_main(["--path", str(tmp_path), "init", "--ide", "all"]) == 0
    assert cli_main(["--path", str(tmp_path), "clean"]) == 0
    capsys.readouterr()
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Our own rules." in agents and "<!-- projectbrain:start -->" not in agents
    assert not (tmp_path / ".cursor" / "rules" / "projectbrain.mdc").exists()
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gi  # user's own line survives
    assert ".projectbrain.db" not in gi


def test_barrel_reexport_caller_preserved_across_signature_mutation(tmp_path: Path):
    """Regression test: mutating a symbol's signature must NOT drop incoming call edges from barrel re-exports."""
    out = _run(
        tmp_path,
        {
            "pkg/__init__.py": "from .impl import compute\n",
            "pkg/impl.py": "def compute(x: int) -> int:\n    return x + 1\n",
            "app.py": "from pkg import compute\n\ndef handler() -> int:\n    return compute(10)\n",
        },
        "pkg/impl.py",
        "def compute(x: int, factor: int) -> int:\n    return x * factor\n",
    )
    assert "[CONTRACT]" in out and "app.py" in out


def test_body_only_edit_with_boundary_rule_stays_fast_and_accurate(tmp_path: Path):
    for rel, src in {
        "routes/api.py": "from services.logic import run\n\ndef endpoint():\n    return run()\n",
        "services/logic.py": "def run():\n    return 42\n",
        "db/sql.py": "def query():\n    return []\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(src, encoding="utf-8")

    b = ProjectBrain(str(tmp_path))
    b.index()
    b.remember(forbid="routes/* -> db/*", note="Layered architecture")

    (tmp_path / "services/logic.py").write_text("def run():\n    val = 42\n    return val\n", encoding="utf-8")
    assert _hook_output(tmp_path, "services/logic.py") == ""


def test_large_repo_neighborhood_check_and_partial_reset(tmp_path: Path):
    """Repos with >200 files must check modified files via O(k) neighborhood hydration and reset cleanly on stats()."""
    for i in range(205):
        (tmp_path / f"pkg/mod_{i}.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"pkg/mod_{i}.py").write_text(f"def f_{i}():\n    return {i}\n", encoding="utf-8")
    (tmp_path / "core.py").write_text("def verify_token(tok: str) -> bool:\n    return bool(tok)\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        "from core import verify_token\n\ndef handle(t: str) -> bool:\n    return verify_token(t)\n",
        encoding="utf-8",
    )
    ProjectBrain(str(tmp_path)).index()

    (tmp_path / "core.py").write_text(
        "def verify_token(tok: str, *, audience: str) -> bool:\n    return bool(tok and audience)\n",
        encoding="utf-8",
    )
    cold = ProjectBrain(str(tmp_path))
    res = cold.check(files_modified=["core.py"])
    assert cold._is_partial_index is True
    assert len(cold._index) < 25
    assert res["all_contracts_valid"] is False
    assert any("consumer.py" in d for d in res["contract_diagnostics"])

    # Full-repo methods on the same instance must transparently hydrate the complete 207-file index
    st = cold.stats()
    assert cold._is_partial_index is False
    assert st["files"] == 207


def test_jsx_keyword_attrs_and_sfc_generic_script_parse_cleanly():
    from projectbrain.parsers import parse

    jsx_info = parse(
        'export function App() {\n  return <>\n    <div class="hero" for="x">ok</div>\n  </>\n}\n',
        ".jsx",
    )
    assert jsx_info["parse_status"] == "ok"

    sfc_info = parse(
        '<script setup lang="ts" generic="TItem extends Array<string>, TVal">\n'
        'export function pick(xs: TItem): TVal | undefined { return undefined }\n'
        '</script>\n<template><div /></template>\n',
        ".vue",
    )
    assert sfc_info["parse_status"] == "ok"
    assert any(f["name"] == "pick" for f in sfc_info["functions"])


def test_differential_cst_syntax_suppression_on_committed_ts_grammar_gaps(tmp_path: Path):
    import subprocess
    from projectbrain.hooks import audit_edited_files, run_pre_commit_hook

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True, capture_output=True)

    barrel = tmp_path / "index.ts"
    barrel.write_text(
        "export function greet(name: string): string {\n  return name\n}\n"
        "export type * as Ns from './other'\n",
        encoding="utf-8",
    )
    (tmp_path / "other.ts").write_text("export type Foo = string\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    ProjectBrain(str(tmp_path)).index()

    # Valid edit to `index.ts` (preserving the pre-existing `export type * as Ns` grammar construct)
    barrel.write_text(
        "export function greet(name: string): string {\n  const prefix = 'hi '\n  return prefix + name\n}\n"
        "export type * as Ns from './other'\n",
        encoding="utf-8",
    )
    audit_ok = audit_edited_files(tmp_path, ["index.ts"])
    assert audit_ok["syntax_errors"] == []

    subprocess.run(["git", "add", "index.ts"], cwd=tmp_path, check=True, capture_output=True)
    rc, msg = run_pre_commit_hook(tmp_path)
    assert rc == 0 and msg == ""

    # Real newly-introduced syntax error in `index.ts` MUST be caught and pinpointed at the new line
    barrel.write_text(
        "export function broken( {\n  return 1\n}\n"
        "export type * as Ns from './other'\n",
        encoding="utf-8",
    )
    audit_bad = audit_edited_files(tmp_path, ["index.ts"])
    assert len(audit_bad["syntax_errors"]) == 1
    assert "index.ts:L1:" in audit_bad["syntax_errors"][0]


def test_unexpanded_symlink_stub_is_skipped(tmp_path: Path):
    (tmp_path / "real.js").write_text("export function ok() { return 1 }\n", encoding="utf-8")
    (tmp_path / "linked.js").write_text("./real.js", encoding="utf-8")

    b = ProjectBrain(str(tmp_path))
    b.index()
    assert "real.js" in b._index
    assert "linked.js" not in b._index
    assert b._skipped.get("linked.js") == "symlink_stub"


def test_dst_glob_boundary_rule_fast_sqlite_lookup(tmp_path: Path):
    for rel, src in {
        "client/app.ts": "export function boot() { return 1 }\n",
        "server/utils.ts": "export function normalize(p: string) { return p }\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(src, encoding="utf-8")

    b = ProjectBrain(str(tmp_path))
    b.index()
    b.remember(forbid="client/* -> server/*", note="No server imports in browser")

    # `server/utils.ts` matches `dst_glob` (`server/*`); fast SQLite lookup must return `[]` without returning None
    f_hit = b._db_store.get_file_fast("server/utils.ts")
    fast_clean = b._db_store.check_boundary_violations_fast(
        b._memory, {"server/utils.ts"}, {"server/utils.ts": f_hit["info"]},
    )
    assert fast_clean == []

    # When an incoming forbidden import exists in the index, fast SQLite `dst_glob` lookup catches it
    (tmp_path / "client/app.ts").write_text(
        "import { normalize } from '../server/utils'\nexport function boot() { return normalize('a') }\n",
        encoding="utf-8",
    )
    b.index()
    f_hit2 = b._db_store.get_file_fast("server/utils.ts")
    fast_viol = b._db_store.check_boundary_violations_fast(
        b._memory, {"server/utils.ts"}, {"server/utils.ts": f_hit2["info"]},
    )
    assert fast_viol is not None and len(fast_viol) == 1
    assert "client/app.ts imports server/utils.ts" in fast_viol[0]["message"]


def test_schema_v7_integer_edges_zlib_and_reason_packing_roundtrip(tmp_path: Path):
    import sqlite3
    from projectbrain.store import _pack_reason, _unpack_reason

    samples = [
        ("direct definition in file", "fn:a.py:foo"),
        ("method defined directly in class body", "fn:a.py:Cls.bar"),
        ("module import resolved to repository file", "file:b.py"),
        ("import statement in file", "mod:os"),
        ("named export resolved to same-file definition", "fn:a.ts:run"),
        ("same-file function call", "fn:a.py:helper"),
        ("'compute' resolved via import from pkg/impl.py", "fn:pkg/impl.py:compute"),
        ("@literal_custom_reason", "fn:a.py:foo"),
        ("@@double_at_reason", "fn:a.py:foo"),
    ]
    for rsn, dst in samples:
        packed = _pack_reason(rsn, dst)
        assert _unpack_reason(packed, dst) == rsn

    (tmp_path / "lib.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from lib import add\n\ndef run() -> int:\n    return add(1, 2)\n", encoding="utf-8")
    b1 = ProjectBrain(str(tmp_path))
    b1.index()

    with sqlite3.connect(str(tmp_path / ".projectbrain.db")) as conn:
        ed_cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        assert {"src_nid", "dst_nid", "edge_type", "line", "confidence", "reason"} == ed_cols
        raw_blob = conn.execute("SELECT info_json FROM files WHERE rel_path = 'lib.py';").fetchone()[0]
        assert isinstance(raw_blob, bytes)

    b2 = ProjectBrain(str(tmp_path))
    assert b2.load() is True
    assert b2._blobs_ready is False  # Deferred lexical blob hydration on non-lexical load
    assert {e["reason"] for e in b2._graph.edges} == {e["reason"] for e in b1._graph.edges}
    q_res = b2.query("add integers")
    assert b2._blobs_ready is True
    assert any(r["rel_path"] == "lib.py" for r in q_res)


def test_hub_module_single_symbol_mutation_skips_unrelated_importers(tmp_path: Path):
    """Editing 1 function in a widely-imported hub module must hydrate only callers of that function, not all importers."""
    (tmp_path / "hub.py").write_text(
        "def alpha(x: int) -> int:\n    return x + 1\n\n"
        "def beta(y: int) -> int:\n    return y * 2\n",
        encoding="utf-8",
    )
    for i in range(202):
        (tmp_path / f"consumers/c_{i}.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"consumers/c_{i}.py").write_text(
            f"from hub import alpha\n\ndef use_{i}() -> int:\n    return alpha({i})\n",
            encoding="utf-8",
        )
    for j in range(3):
        (tmp_path / f"beta_users/b_{j}.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"beta_users/b_{j}.py").write_text(
            f"from hub import beta\n\ndef call_b_{j}() -> int:\n    return beta({j})\n",
            encoding="utf-8",
        )
    ProjectBrain(str(tmp_path)).index()

    # Mutate ONLY `beta`'s signature; the 202 `alpha`-only consumers must NOT be hydrated!
    (tmp_path / "hub.py").write_text(
        "def alpha(x: int) -> int:\n    return x + 1\n\n"
        "def beta(y: int, scale: int) -> int:\n    return y * scale\n",
        encoding="utf-8",
    )
    cold = ProjectBrain(str(tmp_path))
    res = cold.check(files_modified=["hub.py"])
    assert cold._is_partial_index is True
    assert len(cold._index) <= 12, f"Expected <=12 hydrated files, got {len(cold._index)}"
    assert res["all_contracts_valid"] is False
    diags = "\n".join(res["contract_diagnostics"])
    for j in range(3):
        assert f"beta_users/b_{j}.py" in diags
    hub_detail = next(d for d in res["impact_analysis"]["details"] if d["rel_path"] == "hub.py")
    assert hub_detail["consumer_count"] == 205


def test_schema_v8_lazy_file_index_file_blob_and_symbols_view_roundtrip(tmp_path: Path):
    """Verify LazyFileIndex zero-decompression load, lossless _encode_file_blob/_decode_file_blob, and symbols view."""
    import sqlite3
    from projectbrain.parsers import parse
    from projectbrain.store import LazyFileIndex, _decode_file_blob, _encode_file_blob, _pack_reason, _unpack_reason

    src_code = (
        "class Engine:\n"
        "    def execute(self, cmd: str, timeout: int = 30, *, dry_run: bool = False) -> dict:\n"
        "        self.helper(cmd)\n"
        "        return {'ok': True}\n\n"
        "    def helper(self, val: str) -> None:\n"
        "        pass\n"
    )
    parsed = parse(src_code, ".py")
    blob = _encode_file_blob(parsed, level=6)
    decoded = _decode_file_blob(blob)
    assert decoded["functions"] == parsed["functions"]
    assert decoded["classes"] == parsed["classes"]

    ext_reasons = [
        ("'join' matches 2 symbol(s) by name; target ambiguous without import resolution", "fn:a.py:join"),
        ("'Base' matches 3 class(es) by name; target ambiguous without import resolution", "cls:a.py:Base"),
        ("'self.helper()' resolved to method on enclosing class Engine", "fn:a.py:Engine.helper"),
        ("'cls.make()' resolved to method on enclosing class Engine", "fn:a.py:Engine.make"),
        ("'super().run()' resolved to inherited method on base class BaseEngine", "fn:b.py:BaseEngine.run"),
        ("'eng.execute()' resolved via static receiver type 'Engine'", "fn:a.py:Engine.execute"),
        ("'Engine.execute()' resolved via imported class 'Engine' from a.py", "fn:a.py:Engine.execute"),
        ("'mod.run()' resolved via imported module 'mod' from pkg/mod.py", "fn:pkg/mod.py:run"),
        ("'Engine.execute()' matched uniquely to Engine.execute", "fn:a.py:Engine.execute"),
    ]
    for rsn, dst in ext_reasons:
        code = _pack_reason(rsn, dst)
        assert len(code) < len(rsn)
        assert _unpack_reason(code, dst) == rsn

    (tmp_path / "engine.py").write_text(src_code, encoding="utf-8")
    b1 = ProjectBrain(str(tmp_path))
    b1.index()

    with sqlite3.connect(str(tmp_path / ".projectbrain.db")) as conn:
        sym_type = conn.execute("SELECT type FROM sqlite_master WHERE name='symbols';").fetchone()
        assert sym_type == ("view",)
        sym_names = {r[0] for r in conn.execute("SELECT name FROM symbols;").fetchall()}
        assert {"Engine", "execute", "helper"} <= sym_names

    b2 = ProjectBrain(str(tmp_path))
    assert b2.load() is True
    assert isinstance(b2._index, LazyFileIndex)
    assert len(b2._index._decoded) == 0  # Zero files decompressed on warm load()!
    st = b2.stats()
    assert len(b2._index._decoded) == 0  # stats() reads precomputed summary without decompressing files!
    assert st["functions"] == 2 and st["classes"] == 1
    assert b2.is_stale() is False
    assert len(b2._index._decoded) == 0  # is_stale() checks mtimes without decompressing files!
    assert b2._index["engine.py"]["functions"][0]["name"] == "execute"
    assert len(b2._index._decoded) == 1




