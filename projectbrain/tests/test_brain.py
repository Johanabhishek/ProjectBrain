import json
import os
import time

from projectbrain.brain import ProjectBrain
from projectbrain.versions import SCHEMA_VERSION, PARSER_VERSION, GRAPH_VERSION


def test_index_writes_cache_and_returns_stats(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    stats = brain.index()

    assert stats["files"] == 8
    assert stats["routes"] == 1
    assert stats["storage_backend"] == "sqlite"
    assert (tmp_project / ".projectbrain.db").exists()
    assert not (tmp_project / ".projectbrain.json").exists()
    assert not (tmp_project / ".projectbrain_graph.json").exists()

def test_query_defines_outranks_mentions(tmp_project):
    """The +2.0 (defines) vs +1.0 (mentions) branch in query()."""
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    scores = {r["rel_path"]: r["score"] for r in brain.query("discount")}

    assert scores["services/discount.py"] > scores["notes.py"]


def test_query_deprioritizes_test_dirs(tmp_project):
    """DEPRIORITY_DIRS: an otherwise-identical file under tests/ scores lower."""
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    scores = {r["rel_path"]: r["score"] for r in brain.query("discount")}

    assert scores["tests/notes_test.py"] < scores["notes.py"]
    assert scores["tests/notes_test.py"] == scores["notes.py"] * 0.3


def test_query_importer_count_boost(tmp_project):
    """A file imported by several others outranks an equally-matching isolated file."""
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    scores = {r["rel_path"]: r["score"] for r in brain.query("helper")}

    assert scores["services/util.py"] > scores["standalone.py"]


def test_load_reproduces_index(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    fresh = ProjectBrain(str(tmp_project))
    loaded = fresh.load()

    assert loaded is True
    assert fresh._index.keys() == brain._index.keys()


def test_is_stale_detects_mtime_change(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()
    assert brain.is_stale() is False

    target = tmp_project / "notes.py"
    future = time.time() + 100
    os.utime(target, (future, future))

    assert brain.is_stale() is True


def test_query_with_graph_entrypoints(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    result = brain.query_with_graph("checkout", top_n=5)
    rel_paths = {r["rel_path"] for r in result["results"]}
    assert "routes/checkout.py" in rel_paths

    # With the fix, entrypoints correctly use the _imported_by map.
    # Since app.py imports routes/checkout.py, checkout.py is not an entrypoint
    # if app.py is also in the result set.
    if "app.py" in rel_paths:
        assert "file:app.py" in result["entrypoints"]
        assert "file:routes/checkout.py" not in result["entrypoints"]


def test_index_writes_versioned_cache_envelope(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    data = brain._db_store.load_all()
    assert data is not None

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["parser_version"] == PARSER_VERSION
    assert data["graph_version"] == GRAPH_VERSION
    assert data["root"] == str(tmp_project)
    assert "graph_json" in data
    assert "notes.py" in data["files"]
    assert data["files"]["notes.py"]["info"]["functions"][0]["name"] == "print_notes"
    assert "mtime" not in data["files"]["notes.py"]["info"]


def test_load_rejects_old_flat_cache_and_forces_reindex(tmp_project):
    old_shape = {
        "notes.py": {"functions": [], "classes": [], "imports": [], "routes": [], "mtime": 1.0},
    }
    (tmp_project / ".projectbrain.json").write_text(json.dumps(old_shape), encoding="utf-8")

    brain = ProjectBrain(str(tmp_project))
    assert brain.load() is False

    # _ensure_fresh treats a failed load like "no cache" and reindexes from scratch
    results = brain.query("discount")
    assert results
    assert (tmp_project / ".projectbrain.db").exists()
    assert not (tmp_project / ".projectbrain.json").exists()


def test_load_rejects_version_mismatch_even_if_schema_matches(tmp_project):
    import sqlite3

    brain = ProjectBrain(str(tmp_project))
    brain.index()

    with sqlite3.connect(str(tmp_project / ".projectbrain.db")) as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'parser_version'",
            (json.dumps("stale-version"),),
        )
        conn.commit()

    fresh = ProjectBrain(str(tmp_project))
    assert fresh.load() is False

def test_load_rejects_corrupt_json(tmp_project):
    (tmp_project / ".projectbrain.json").write_text("not valid json{", encoding="utf-8")

    brain = ProjectBrain(str(tmp_project))
    assert brain.load() is False


def test_find_relationship_finds_multi_hop_path_past_unreachable_first_candidate(tmp_project):
    """Regression lock: find_relationship tries candidate pairs in
    find_nodes_by_name order, and the first pair tried is often an
    unreachable one (e.g. a mod: node). A prior bug treated that unreachable
    pair's `[]` as a valid zero-length "best path", permanently blocking any
    later, real, non-empty path from ever winning (len(p) < len([]) is never
    true). checkout -> apply_discount -> helper is a genuine 2-hop path that
    must be found despite earlier candidate pairs coming back unreachable."""
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    result = brain.find_relationship("checkout", "util")

    assert result["path"], f"expected a real path, got: {result}"
    assert len(result["path"]) >= 1


def test_get_context_for_file_imports_and_importers(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    ctx = brain.get_context_for_file(str(tmp_project / "services/util.py"))

    assert set(ctx["imported_by"]) == {
        "routes/checkout.py", "services/discount.py", "services/pricing.py",
    }
    assert ctx["imports_resolved"] == []
