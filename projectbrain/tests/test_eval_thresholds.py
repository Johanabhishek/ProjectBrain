import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))

from run_eval import run  # noqa: E402


def test_file_retrieval_recall_regression_floor():
    data = run()
    fr = data["file_retrieval"]
    assert fr

    avg_recall = sum(r["recall"] for r in fr) / len(fr)
    assert avg_recall >= 0.7, f"file-retrieval recall regressed: {avg_recall:.0%}"


def test_relationship_tasks_pass_rate_regression_floor():
    data = run()
    rel = data["relationship"]
    assert rel

    pass_rate = sum(r["passed"] for r in rel) / len(rel)
    assert pass_rate >= 0.8, f"relationship task pass rate regressed: {pass_rate:.0%}"


def test_relationship_tasks_with_grep_baseline_confirm_grep_misses():
    """The core claim of this eval harness: for tasks explicitly designed to
    require multi-hop graph traversal, a naive grep over the same fixture
    must NOT find the connection. If this ever flips, the task was not
    actually testing what it claims to and should be redesigned."""
    data = run()
    checked = [r for r in data["relationship"] if r.get("grep_would_find_it") is not None]
    assert checked

    for r in checked:
        assert r["grep_would_find_it"] is False, (
            f"{r['id']}: grep found the connection too - this task no longer "
            "demonstrates a graph-only capability"
        )


def test_claim_verification_never_hallucinates():
    """The anti-hallucination contract. A ground-truth tool that itself asserts
    a false claim (false_support) or denies a true one (false_refutation) is
    worse than useless — these must be exactly zero, always."""
    data = run()
    claims = data["claim_verification"]
    assert claims

    false_support = [c for c in claims if c["false_support"]]
    false_refutation = [c for c in claims if c["false_refutation"]]
    assert not false_support, f"verify_claim SUPPORTED false claim(s): {[c['id'] for c in false_support]}"
    assert not false_refutation, f"verify_claim REFUTED true claim(s): {[c['id'] for c in false_refutation]}"


def test_claim_verification_exact_match_floor():
    """Every curated claim task should land on its exact expected verdict. This
    is stricter than the zero-hallucination floor above (which also allows an
    over-cautious UNKNOWN) — it locks in current decisiveness so a regression
    to UNKNOWN is caught, not silently tolerated."""
    data = run()
    claims = data["claim_verification"]
    assert claims

    wrong = [(c["id"], c["expected"], c["actual"]) for c in claims if not c["correct"]]
    assert not wrong, f"claim verdicts drifted from expected: {wrong}"


def test_claim_decisiveness_floor():
    """Guard against the tool degrading into always-UNKNOWN (trivially zero
    hallucinations, zero value). Decisiveness is the share of claims answered
    SUPPORTED or REFUTED; accuracy work should raise this, never lower it."""
    data = run()
    claims = data["claim_verification"]
    assert claims

    decisive = sum(c["actual"] != "UNKNOWN" for c in claims) / len(claims)
    assert decisive >= 0.6, f"claim decisiveness regressed: {decisive:.0%}"


def test_dogfood_context_compiler_token_savings_and_recall_floor():
    """Verify that ProjectBrain v2's surgical AST context compiler achieves high
    symbol recall (>= 80%) and massive token savings (>= 75% vs reading full
    matched files) when dogfooding on ProjectBrain's own codebase."""
    data = run()
    dogfood = data.get("dogfood_context_compiler", [])
    assert dogfood

    avg_recall = sum(d["symbol_recall"] for d in dogfood) / len(dogfood)
    avg_saved = sum(d["tokens_saved_vs_matched_files_pct"] for d in dogfood) / len(dogfood)
    assert avg_recall >= 0.8, f"dogfood context compiler recall regressed: {avg_recall:.0%}"
    assert avg_saved >= 75.0, f"dogfood token savings regressed: {avg_saved:.1f}%"

