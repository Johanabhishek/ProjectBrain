"""Tests for Sprint 4: Verification & Safety features."""
import pytest
from projectbrain.brain import ProjectBrain


@pytest.fixture
def brain(tmp_project):
    b = ProjectBrain(str(tmp_project))
    b.index()
    return b


# ── 3.1 Batch Verification ──────────────────────────────────────────────────

def test_verify_batch_returns_all_verdicts(brain):
    result = brain.verify_batch([
        ("app.py", "defines", "main"),
        ("services/discount.py", "defines", "apply_discount"),
        ("app.py", "defines", "nonexistent_fn_xyz"),
    ])
    assert result["total"] == 3
    assert len(result["verdicts"]) == 3
    assert "summary" in result
    assert isinstance(result["summary"], dict)
    # At least one should be SUPPORTED (app.py defines main)
    verdicts_set = {v["verdict"] for v in result["verdicts"]}
    assert "SUPPORTED" in verdicts_set or "UNKNOWN" in verdicts_set


def test_verify_batch_empty(brain):
    result = brain.verify_batch([])
    assert result["total"] == 0
    assert result["verdicts"] == []


# ── 3.2 Plan Validation ─────────────────────────────────────────────────────

def test_verify_plan_missing_files(brain):
    """Editing util.py alone should flag its importers as missing."""
    result = brain.verify_plan(
        task="change utils",
        files_to_edit=["services/util.py"],
    )
    # util.py is imported by checkout.py, discount.py, pricing.py
    # So editing it alone should flag those as missing
    assert result["verdict"] == "INCOMPLETE"
    assert len(result.get("missing_files", [])) > 0
    missing_paths = {mf["rel_path"] for mf in result["missing_files"]}
    # At least one of the importers should be flagged
    importers = {"routes/checkout.py", "services/discount.py", "services/pricing.py"}
    assert missing_paths & importers  # intersection should be non-empty


def test_verify_plan_detects_risks(brain):
    """Editing util.py should show medium/high risk since it's a hub file."""
    result = brain.verify_plan(
        task="change utils",
        files_to_edit=["services/util.py"],
    )
    assert "risks" in result
    assert len(result["risks"]) > 0
    # util.py is imported by 3 files, so risk should be medium
    util_risk = next(
        (r for r in result["risks"] if r["rel_path"] == "services/util.py"),
        None,
    )
    assert util_risk is not None
    assert util_risk["risk"] in ("medium", "high")


def test_verify_plan_complete_when_all_covered(brain):
    """When plan covers all connected files, verdict should not be INCOMPLETE."""
    result = brain.verify_plan(
        task="refactor checkout",
        files_to_edit=[
            "app.py",
            "routes/checkout.py",
            "services/discount.py",
            "services/util.py",
            "services/pricing.py",
        ],
    )
    # With all files covered, should not be INCOMPLETE
    assert result["verdict"] != "INCOMPLETE"


def test_verify_plan_with_new_files(brain):
    """Plan with files_to_create should include them in the plan set."""
    result = brain.verify_plan(
        task="add discounts",
        files_to_edit=["services/discount.py"],
        files_to_create=["validators/discount_code.py"],
    )
    assert "validators/discount_code.py" in result["planned_files"]


# ── 3.3 Impact Analysis ─────────────────────────────────────────────────────

def test_impact_finds_consumers(brain):
    """Changing util.py should find its importers as consumers."""
    result = brain.impact(["services/util.py"])
    assert result["files_analyzed"] == 1
    assert result["total_impact_zone"] > 0
    detail = result["details"][0]
    assert detail["rel_path"] == "services/util.py"
    assert detail["consumer_count"] > 0
    assert len(result["all_impacted_files"]) > 0


def test_impact_risk_level(brain):
    """util.py imported by 3+ files should have at least medium risk."""
    result = brain.impact(["services/util.py"])
    detail = result["details"][0]
    assert detail["risk"] in ("medium", "high", "critical")
    assert result["overall_risk"] in ("medium", "high", "critical")


def test_impact_not_indexed(brain):
    """Non-existent file should get 'not_indexed' status."""
    result = brain.impact(["nonexistent_file.py"])
    assert result["files_analyzed"] == 1
    detail = result["details"][0]
    assert detail["status"] == "not_indexed"
    assert detail["risk"] == "unknown"


def test_impact_separates_test_consumers(brain):
    """Impact should separate test consumers from production consumers."""
    result = brain.impact(["services/util.py"])
    detail = result["details"][0]
    assert "direct_consumers" in detail
    assert "test_consumers" in detail
    assert isinstance(detail["direct_consumers"], list)
    assert isinstance(detail["test_consumers"], list)


def test_impact_multiple_files(brain):
    """Impact analysis on multiple files should aggregate results."""
    result = brain.impact(["services/util.py", "services/discount.py"])
    assert result["files_analyzed"] == 2
    assert len(result["details"]) == 2
