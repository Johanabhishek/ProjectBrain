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


# ── 3.4 Universal Cross-File Contract Verification via brain.check() ────────

def test_check_detects_post_edit_arity_and_unknown_kwarg_on_disk(tmp_path):
    """Normal disk edits followed by brain.check(files_modified=[...]) catch arity & keyword breaks."""
    (tmp_path / "billing.py").write_text(
        "def charge_user(user_id: int, amount: float = 10.0) -> dict:\n"
        "    return {'charge_id': user_id, 'total': amount}\n",
        encoding="utf-8",
    )
    (tmp_path / "checkout.py").write_text(
        "from billing import charge_user\n\n"
        "def run_checkout(uid: int):\n"
        "    return charge_user(uid, amt=25.0)\n",
        encoding="utf-8",
    )
    b = ProjectBrain(str(tmp_path))
    b.index()

    # 1. Calling charge_user with unknown keyword `amt` (instead of `amount`)
    res_kw = b.check(files_modified=["checkout.py"])
    assert res_kw["all_contracts_valid"] is False
    assert res_kw["verdict"] == "UNSAFE_CONTRACT_VIOLATION"
    assert any(
        "6D Caller Keyword Contract Violation" in d and "amt" in d and "amount" in d
        for d in res_kw["contract_diagnostics"]
    )

    # 2. Edit billing.py on disk (outside brain.patch) to require 3 positional args
    (tmp_path / "billing.py").write_text(
        "def charge_user(user_id: int, amount: float, currency: str) -> dict:\n"
        "    return {'charge_id': user_id, 'total': amount, 'currency': currency}\n",
        encoding="utf-8",
    )
    (tmp_path / "checkout.py").write_text(
        "from billing import charge_user\n\n"
        "def run_checkout(uid: int):\n"
        "    return charge_user(uid)\n",
        encoding="utf-8",
    )
    res_arity = b.check(files_modified=["billing.py"])
    assert res_arity["all_contracts_valid"] is False
    assert any("6D Caller Arity Contract Violation" in d for d in res_arity["contract_diagnostics"])


def test_check_detects_deleted_or_renamed_symbol_and_broken_import(tmp_path):
    """Renaming a function on disk without updating importers flags both broken import and dangling caller."""
    (tmp_path / "auth.py").write_text(
        "def verify_token(tok: str) -> bool:\n"
        "    return bool(tok and len(tok) > 5)\n",
        encoding="utf-8",
    )
    (tmp_path / "api.py").write_text(
        "from auth import verify_token\n\n"
        "def handle_request(t: str) -> bool:\n"
        "    return verify_token(t)\n",
        encoding="utf-8",
    )
    b = ProjectBrain(str(tmp_path))
    b.index()

    # Rename verify_token -> validate_token on disk without updating api.py
    (tmp_path / "auth.py").write_text(
        "def validate_token(tok: str) -> bool:\n"
        "    return bool(tok and len(tok) > 5)\n",
        encoding="utf-8",
    )
    res = b.check(files_modified=["auth.py"])
    assert res["all_contracts_valid"] is False
    diags = "\n".join(res["contract_diagnostics"])
    assert "verify_token" in diags
    assert "validate_token" in diags


def test_check_detects_cross_file_return_dict_key_drift(tmp_path):
    """Renaming a key in a returned dict literal on disk flags callers subscripting the old key."""
    (tmp_path / "users.py").write_text(
        "def fetch_profile(uid: int) -> dict:\n"
        "    return {'user_id': uid, 'email': 'a@b.com'}\n",
        encoding="utf-8",
    )
    (tmp_path / "dashboard.py").write_text(
        "from users import fetch_profile\n\n"
        "def render_header(uid: int) -> str:\n"
        "    prof = fetch_profile(uid)\n"
        "    return str(prof['user_id'])\n",
        encoding="utf-8",
    )
    b = ProjectBrain(str(tmp_path))
    b.index()

    # Change 'user_id' -> 'id' in users.py on disk without updating dashboard.py
    (tmp_path / "users.py").write_text(
        "def fetch_profile(uid: int) -> dict:\n"
        "    return {'id': uid, 'email': 'a@b.com'}\n",
        encoding="utf-8",
    )
    res = b.check(files_modified=["users.py"])
    assert res["all_contracts_valid"] is False
    assert any(
        "6D Return-Shape Key Contract Violation" in d and "user_id" in d
        for d in res["contract_diagnostics"]
    )


def test_monorepo_workspace_and_barrel_reexport_contract_verification(tmp_path):
    """Monorepo @scope/pkg imports and barrel `export * from` resolve transitively and catch arity breaks."""
    pkg_dir = tmp_path / "packages" / "billing" / "src"
    pkg_dir.mkdir(parents=True)
    app_dir = tmp_path / "apps" / "web" / "src"
    app_dir.mkdir(parents=True)

    (pkg_dir / "stripe.ts").write_text(
        "export function chargeCustomer(customerId: string): { receiptId: string } {\n"
        "  return { receiptId: customerId };\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg_dir / "index.ts").write_text(
        "export * from './stripe';\n",
        encoding="utf-8",
    )
    (app_dir / "checkout.ts").write_text(
        "import { chargeCustomer } from '@acme/billing';\n\n"
        "export function submitOrder(cid: string) {\n"
        "  const { receiptId } = chargeCustomer(cid);\n"
        "  return receiptId;\n"
        "}\n",
        encoding="utf-8",
    )

    b = ProjectBrain(str(tmp_path))
    b.index()

    # Edit stripe.ts in packages/billing to require 2 arguments AND change return object key
    (pkg_dir / "stripe.ts").write_text(
        "export function chargeCustomer(customerId: string, currency: string): { transactionId: string } {\n"
        "  return { transactionId: customerId };\n"
        "}\n",
        encoding="utf-8",
    )

    res = b.check(files_modified=["packages/billing/src/stripe.ts"])
    assert res["all_contracts_valid"] is False
    diags = "\n".join(res["contract_diagnostics"])
    assert "6D Caller Arity Contract Violation" in diags
    assert "6D Return-Shape Key Contract Violation" in diags
    assert "receiptId" in diags
