"""Tests for session-based iterative routing — the core 'Brain' feature."""
import pytest
from projectbrain.brain import ProjectBrain


@pytest.fixture
def brain(tmp_project):
    b = ProjectBrain(str(tmp_project))
    b.index()
    return b


def test_start_session_returns_initial_files(brain):
    result = brain.start_session("checkout discount")
    assert "session_id" in result
    assert len(result["session_id"]) == 12
    assert "initial_files" in result
    assert len(result["initial_files"]) > 0
    assert "next_suggestions" in result
    assert "message" in result


def test_mark_read_expands_frontier(brain):
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    # Mark the first file as read
    result = brain.session_mark_read(sid, "routes/checkout.py")
    assert result["marked"] == "routes/checkout.py"
    assert result["read_count"] == 1
    assert "completeness" in result

    # Frontier should now contain files imported by checkout.py
    # (services/discount.py, services/util.py)
    suggestions = result["next_suggestions"]
    suggested_paths = {s["rel_path"] for s in suggestions}
    # checkout.py imports discount and util, so they should appear
    assert "services/discount.py" in suggested_paths or "services/util.py" in suggested_paths


def test_mark_modified_shows_impact(brain):
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    # Mark util.py as modified — many files import it
    result = brain.session_mark_modified(sid, "services/util.py", "changed helper signature")
    assert result["marked_modified"] == "services/util.py"
    assert "impact_zone" in result
    # services/util.py is imported by checkout.py, discount.py, pricing.py
    assert len(result["impact_zone"]) >= 2


def test_session_next_pops_from_frontier(brain):
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    # Pop the first file from the frontier
    result = brain.session_next(sid, count=1)
    assert "files" in result
    assert len(result["files"]) <= 1
    assert "completeness" in result
    assert "frontier_remaining" in result


def test_session_status_reports_progress(brain):
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    status = brain.session_status(sid)
    assert status["session_id"] == sid
    assert status["task"] == "checkout discount"
    assert status["read_count"] == 0
    assert status["modified_count"] == 0
    assert "completeness" in status
    assert "next_suggestions" in status


def test_iterative_routing_full_cycle(brain):
    """The end-to-end iterative routing experience:
    start → read → read → read → completeness increases."""
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    initial_suggestions = session["next_suggestions"]
    assert len(initial_suggestions) > 0

    # Read the first suggestion
    first = initial_suggestions[0]["rel_path"]
    r1 = brain.session_mark_read(sid, first)
    assert r1["read_count"] == 1

    # Read another file
    if r1["next_suggestions"]:
        second = r1["next_suggestions"][0]["rel_path"]
        r2 = brain.session_mark_read(sid, second)
        assert r2["read_count"] == 2
        # Completeness should be higher now
        assert r2["completeness"]["files_read"] == 2


def test_session_not_found_error(brain):
    result = brain.session_mark_read("nonexistent", "foo.py")
    assert "error" in result


def test_already_read_files_skipped_in_frontier(brain):
    """Files already read should not appear in frontier suggestions."""
    session = brain.start_session("checkout discount")
    sid = session["session_id"]

    # Read a file
    brain.session_mark_read(sid, "routes/checkout.py")

    # The read file should NOT appear in suggestions
    status = brain.session_status(sid)
    suggested_paths = {s["rel_path"] for s in status["next_suggestions"]}
    assert "routes/checkout.py" not in suggested_paths
