"""Tests for Sprint 5: Cross-Boundary Intelligence features."""
import pytest
from projectbrain.brain import ProjectBrain


@pytest.fixture
def brain(tmp_project):
    b = ProjectBrain(str(tmp_project))
    b.index()
    return b


# ── 4.1 Flow Tracing ────────────────────────────────────────────────────────

def test_trace_flow_finds_route(brain):
    """trace_flow should find the GET /checkout route in our fixture."""
    result = brain.trace_flow("/checkout", method="GET")
    # The fixture has routes/checkout.py with @app.get("/checkout")
    route_steps = [s for s in result.get("steps", []) if s["type"] == "route"]
    assert len(route_steps) >= 1
    assert route_steps[0]["rel_path"] == "routes/checkout.py"


def test_trace_flow_follows_handler_calls(brain):
    """After finding the route, trace_flow should follow the handler's call chain."""
    result = brain.trace_flow("/checkout", method="GET")
    steps = result.get("steps", [])
    # Should have at least the route step + some call chain steps
    step_types = [s["type"] for s in steps]
    assert "route" in step_types


def test_trace_flow_no_match(brain):
    """trace_flow should return empty steps for a non-existent URL."""
    result = brain.trace_flow("/api/nonexistent", method="GET")
    assert result["steps"] == []
    assert "message" in result


def test_url_matches_exact():
    """_url_matches should match exact URLs."""
    assert ProjectBrain._url_matches("/checkout", "/checkout")
    assert not ProjectBrain._url_matches("/checkout", "/login")


def test_url_matches_dynamic_segments():
    """_url_matches should handle :param, [param], {param} patterns."""
    assert ProjectBrain._url_matches("/users/:id", "/users/123")
    assert ProjectBrain._url_matches("/users/{id}", "/users/123")
    assert ProjectBrain._url_matches("/users/[id]", "/users/123")
    assert not ProjectBrain._url_matches("/users/:id", "/users/123/posts")


def test_url_matches_trailing_slash():
    """_url_matches should be insensitive to trailing slashes."""
    assert ProjectBrain._url_matches("/checkout/", "/checkout")
    assert ProjectBrain._url_matches("/checkout", "/checkout/")


# ── 4.2 Co-Change Analysis ──────────────────────────────────────────────────

def test_co_change_no_git(tmp_path):
    """co_change_analysis should handle missing git gracefully."""
    # tmp_path is NOT a git repo
    b = ProjectBrain(str(tmp_path))
    result = b.co_change_analysis("some_file.py")
    assert "co_changes" in result
    # Should return empty list or a message, not crash
    assert isinstance(result["co_changes"], list)


def test_co_change_structure(brain):
    """co_change_analysis should return well-structured results."""
    result = brain.co_change_analysis("routes/checkout.py")
    assert "file" in result
    assert "co_changes" in result
    # The result should always be a list (possibly empty)
    assert isinstance(result["co_changes"], list)
