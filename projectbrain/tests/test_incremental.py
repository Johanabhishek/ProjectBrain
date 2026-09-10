"""Tests for Sprint 6: Incremental Indexing."""
import time

import pytest
from projectbrain.brain import ProjectBrain


@pytest.fixture
def brain(tmp_project):
    b = ProjectBrain(str(tmp_project))
    b.index()
    return b, tmp_project


def test_incremental_no_changes(brain):
    """When nothing changed, incremental_index returns up_to_date."""
    b, _ = brain
    result = b.incremental_index()
    assert result["status"] == "up_to_date"
    assert result["added"] == 0
    assert result["modified"] == 0
    assert result["deleted"] == 0


def test_incremental_detects_modified_file(brain):
    """When a file is modified, incremental_index detects it."""
    b, tmp = brain
    original_count = len(b._index)

    # Modify a file (need to wait a bit for mtime to change)
    util_path = tmp / "services" / "util.py"
    time.sleep(0.05)
    util_path.write_text(
        'def helper():\n    """Updated helper."""\n    return 42\n\n'
        'def new_function():\n    return "new"\n',
        encoding="utf-8",
    )

    result = b.incremental_index()
    assert result["status"] == "incremental_update"
    assert result["modified"] >= 1
    assert result["total_files"] == original_count


def test_incremental_detects_added_file(brain):
    """When a new file is added, incremental_index picks it up."""
    b, tmp = brain
    original_count = len(b._index)

    # Add a new file
    new_file = tmp / "services" / "new_service.py"
    new_file.write_text(
        'def serve():\n    return "hello"\n',
        encoding="utf-8",
    )

    result = b.incremental_index()
    assert result["status"] == "incremental_update"
    assert result["added"] >= 1
    assert result["total_files"] == original_count + 1
    assert "services/new_service.py" in b._index


def test_incremental_detects_deleted_file(brain):
    """When a file is deleted, incremental_index removes it."""
    b, tmp = brain
    original_count = len(b._index)

    # Delete a file
    pricing = tmp / "services" / "pricing.py"
    pricing.unlink()

    result = b.incremental_index()
    assert result["status"] == "incremental_update"
    assert result["deleted"] >= 1
    assert result["total_files"] == original_count - 1
    assert "services/pricing.py" not in b._index


def test_incremental_rebuilds_graph(brain):
    """After incremental update, graph should reflect new state."""
    b, tmp = brain

    # Add a file that imports util
    new_file = tmp / "services" / "analytics.py"
    new_file.write_text(
        'from services.util import helper\n\n'
        'def track():\n    helper()\n    return "tracked"\n',
        encoding="utf-8",
    )

    result = b.incremental_index()
    assert result["added"] >= 1
    # The new file should appear in _imported_by for util.py
    util_importers = b._imported_by.get("services/util.py", [])
    assert "services/analytics.py" in util_importers


def test_incremental_updates_cache(brain):
    """Incremental index should persist the updated cache."""
    b, tmp = brain

    # Modify a file
    time.sleep(0.05)
    (tmp / "services" / "util.py").write_text(
        'def helper():\n    return 99\n',
        encoding="utf-8",
    )
    b.incremental_index()

    # Create a new brain instance and load from cache
    b2 = ProjectBrain(str(tmp))
    loaded = b2.load()
    assert loaded
    assert "services/util.py" in b2._index


def test_ensure_fresh_uses_incremental(brain):
    """_ensure_fresh should use incremental_index when stale."""
    b, tmp = brain
    original_count = len(b._index)

    # Add a file to make it stale
    (tmp / "extra.py").write_text(
        'def extra():\n    return True\n',
        encoding="utf-8",
    )

    # _ensure_fresh should detect staleness and do incremental update
    b._ensure_fresh()
    assert len(b._index) == original_count + 1
    assert "extra.py" in b._index
