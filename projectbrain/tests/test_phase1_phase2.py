from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from projectbrain import __version__
from projectbrain.brain import ProjectBrain
from projectbrain.cli import main as cli_main
from projectbrain.reporters import PR_COMMENT_MARKER


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def _init_git_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "ci@projectbrain.dev")
    _git(root, "config", "user.name", "ProjectBrain CI")


# ── 1. PR Merge-Base Diffing on Clean Multi-Commit Branches ──────────────────


def test_pr_merge_base_clean_working_tree_multi_commit_contracts_and_blast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """`check --base main` and CI PR env vars must audit multi-commit PR diffs when working tree is clean."""
    _init_git_repo(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "routes").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "db" / "ledger_repo.py").write_text(
        "def load_rate() -> float:\n"
        "    return 0.05\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "pricing.py").write_text(
        "from db.ledger_repo import load_rate\n\n"
        "def compute_invoice_total(subtotal: float) -> dict:\n"
        "    rate = load_rate()\n"
        "    return {'total': subtotal * (1.0 + rate), 'currency': 'USD'}\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "billing.py").write_text(
        "from services.pricing import compute_invoice_total\n\n"
        "def post_checkout(amount: float) -> str:\n"
        "    res = compute_invoice_total(amount)\n"
        "    return f\"{res['total']}:{res['currency']}\"\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_billing.py").write_text(
        "from services.pricing import compute_invoice_total\n\n"
        "def test_total():\n"
        "    assert compute_invoice_total(100.0)['currency'] == 'USD'\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember(forbid="routes/* -> db/*", note="Routes must call services")
    brain.remember("compute_invoice_total", "Must always include currency key in returned dict")
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline main")

    # Feature branch with 2 commits:
    # Commit 1 breaks arity AND return-key contract on `compute_invoice_total`
    _git(tmp_path, "checkout", "-b", "feat/pricing-v2")
    (tmp_path / "services" / "pricing.py").write_text(
        "from db.ledger_repo import load_rate\n\n"
        "def compute_invoice_total(subtotal: float, region_code: str) -> dict:\n"
        "    rate = load_rate()\n"
        "    return {'total': subtotal * (1.0 + rate)}\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "services/pricing.py")
    _git(tmp_path, "commit", "-m", "require region_code and drop currency key")

    # Commit 2 touches an unrelated doc + code helper so HEAD~1 != merge-base
    (tmp_path / "README.md").write_text("# Billing Docs\n", encoding="utf-8")
    (tmp_path / "services" / "health_ping.py").write_text("def ping_ok() -> bool:\n    return True\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "docs and health helper")

    # Simulate CI step 1: `projectbrain index` runs on clean PR HEAD!
    ProjectBrain(str(tmp_path)).index()
    assert _git(tmp_path, "status", "--porcelain") == ""

    # Without `--base` or CI env vars, working tree vs HEAD is clean -> passes
    clean_head_res = ProjectBrain(str(tmp_path)).check()
    assert clean_head_res["all_contracts_valid"] is True

    # With `--base main`, ProjectBrain diffs merge-base -> catches arity break, return-key removal, blast radius, and stale invariant!
    pr_res = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert pr_res["all_contracts_valid"] is False
    assert pr_res["verdict"] == "UNSAFE_CONTRACT_VIOLATION"
    diags_blob = "\n".join(pr_res["contract_diagnostics"])
    assert "Caller Arity Contract Violation" in diags_blob
    assert "routes/billing.py" in diags_blob
    assert "Return-Shape Key Contract Violation" in diags_blob
    assert "currency" in diags_blob

    # Signature blast radius and stale invariant are populated even on a clean working tree
    assert len(pr_res.get("signature_blast", [])) >= 1
    assert pr_res["signature_blast"][0]["file"] == "services/pricing.py"
    assert any(c["caller_file"] == "routes/billing.py" for c in pr_res["signature_blast"][0]["callers"])
    assert len(pr_res.get("stale_memories", [])) == 1
    assert "compute_invoice_total" in pr_res["stale_memories"][0]["target_id"]

    # Automatic CI PR detection via GITHUB_BASE_REF and GitLab CI_MERGE_REQUEST_TARGET_BRANCH_NAME
    capsys.readouterr()
    monkeypatch.setenv("GITHUB_BASE_REF", "refs/heads/main")
    rc_gh = cli_main(["--path", str(tmp_path), "check", "--ci"])
    assert rc_gh == 1
    out_gh = capsys.readouterr().out
    assert "Caller Arity Contract Violation" in out_gh

    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "main")
    rc_gl = cli_main(["--path", str(tmp_path), "check", "--ci"])
    assert rc_gl == 1
    capsys.readouterr()


# ── 2. Deleted & Renamed Files + Invalid Base-Ref + Shallow Merge Fallback ──


def test_pr_deleted_and_renamed_file_broken_consumer_imports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Deleting or renaming a module in a PR must flag surviving untouched files that still import it."""
    _init_git_repo(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "routes").mkdir()

    (tmp_path / "lib" / "legacy_tax.py").write_text(
        "def calc_legacy_fee(cents: int) -> int:\n    return cents // 10\n",
        encoding="utf-8",
    )
    (tmp_path / "lib" / "signer.py").write_text(
        "def sign_receipt(payload: str) -> str:\n    return f'sig:{payload}'\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "receipts.py").write_text(
        "from lib.legacy_tax import calc_legacy_fee\n"
        "from lib.signer import sign_receipt\n\n"
        "def issue_receipt(cents: int) -> str:\n"
        "    fee = calc_legacy_fee(cents)\n"
        "    return sign_receipt(str(fee))\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember("sign_receipt", "Must prefix receipt signatures with sig:")
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")

    # PR branch deletes `lib/legacy_tax.py` and renames `lib/signer.py` -> `lib/crypto_signer.py`
    _git(tmp_path, "checkout", "-b", "feat/refactor-libs")
    _git(tmp_path, "rm", "lib/legacy_tax.py")
    _git(tmp_path, "mv", "lib/signer.py", "lib/crypto_signer.py")
    _git(tmp_path, "commit", "-m", "delete legacy_tax and rename signer")

    ProjectBrain(str(tmp_path)).index()
    res = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert res["all_contracts_valid"] is False
    diags = "\n".join(res["contract_diagnostics"])
    assert "Deleted/Renamed File Import Violation" in diags
    assert "lib/legacy_tax.py" in diags
    assert "deleted in this diff" in diags
    assert "lib/signer.py" in diags
    assert "renamed to `lib/crypto_signer.py`" in diags

    # Invariant on moved `lib/signer.py` is flagged stale
    assert any(m.get("stale") for m in res.get("stale_memories", []))

    # Pure deletion PR (zero active modified/renamed files) also fails `--ci`
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "checkout", "-b", "feat/delete-only")
    _git(tmp_path, "rm", "lib/legacy_tax.py")
    _git(tmp_path, "commit", "-m", "only delete file")
    capsys.readouterr()
    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--ci"]) == 1
    assert "lib/legacy_tax.py" in capsys.readouterr().out


def test_invalid_base_ref_fails_fast_and_shallow_merge_commit_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Explicit invalid `--base` must fail `--ci` (exit 1); CI shallow PR merge commits fall back to HEAD^1."""
    # Non-git directory with explicit `--base` fails fast
    (tmp_path / "non_git").mkdir()
    (tmp_path / "non_git" / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    assert cli_main(["--path", str(tmp_path / "non_git"), "check", "--base", "main", "--ci"]) == 1
    assert "GIT BASE ERROR" in capsys.readouterr().out

    # Git repo with nonexistent `--base` fails fast
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "svc.py").write_text("def calc(x: int) -> int:\n    return x\n", encoding="utf-8")
    (repo / "caller.py").write_text("from svc import calc\n\ndef run() -> int:\n    return calc(1)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    assert cli_main(["--path", str(repo), "check", "--base", "nonexistent-branch-999", "--ci"]) == 1
    out_bad = capsys.readouterr().out
    assert "GIT BASE ERROR" in out_bad

    # Simulate GitHub Actions shallow `refs/pull/<N>/merge` commit where target branch name is unfetched locally
    _git(repo, "checkout", "-b", "pr-head")
    (repo / "svc.py").write_text("def calc(x: int, y: int) -> int:\n    return x + y\n", encoding="utf-8")
    _git(repo, "add", "svc.py")
    _git(repo, "commit", "-m", "break calc signature")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "--detach")
    _git(repo, "merge", "--no-ff", "pr-head", "-m", "Merge pr-head into main")
    _git(repo, "branch", "-D", "main")  # `main` ref no longer exists locally, mimicking shallow PR merge checkout

    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    res_merge = ProjectBrain(str(repo)).check()
    assert res_merge["base_ref_info"]["resolved_ref"] == "HEAD^1"
    assert res_merge["all_contracts_valid"] is False
    assert "Caller Arity Contract Violation" in "\n".join(res_merge["contract_diagnostics"])


# ── 3. Governance Tamper Lock & Co-Change Contracts ──────────────────────────


def test_governance_tamper_lock_blocks_unauthorized_edits_and_allows_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Deleting/weakening boundary rules, invariants, or co-change rules in `.projectbrain_memory.json` must fail `--ci`."""
    _init_git_repo(tmp_path)
    (tmp_path / "routes").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "store.py").write_text("def save_tx(tx_id: str) -> bool:\n    return True\n", encoding="utf-8")
    (tmp_path / "services" / "pay.py").write_text(
        "from db.store import save_tx\n\ndef settle_payment(tx_id: str) -> bool:\n    return save_tx(tx_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "pay_api.py").write_text(
        "from services.pay import settle_payment\n\ndef handle_pay(tx_id: str) -> bool:\n    return settle_payment(tx_id)\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember(forbid="routes/* -> db/*", note="Encapsulate DB")
    brain.remember("settle_payment", "Must validate idempotency key")
    brain.remember(co_change="db/store.py <-> services/pay.py", note="Keep tx persistence coupled")

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "commit governance lock")
    baseline_gov = (tmp_path / ".projectbrain_memory.json").read_text(encoding="utf-8")

    _git(tmp_path, "checkout", "-b", "feat/agent-pr")

    # Vector A: Agent deletes `.projectbrain_memory.json` with ZERO code edits
    (tmp_path / ".projectbrain_memory.json").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete memory file")

    capsys.readouterr()
    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--ci"]) == 1
    out_del = capsys.readouterr().out
    assert "[GOVERNANCE TAMPER]" in out_del

    # Passing `--allow-governance-change` permits intentional governance change
    assert cli_main([
        "--path", str(tmp_path), "check", "--base", "main", "--ci", "--allow-governance-change",
    ]) == 0
    capsys.readouterr()

    # Vector B: Corrupt JSON
    (tmp_path / ".projectbrain_memory.json").write_text("{corrupt_json", encoding="utf-8")
    res_corrupt = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert res_corrupt["all_contracts_valid"] is False
    assert res_corrupt["verdict"] == "UNSAFE_GOVERNANCE_TAMPER"

    # Vector C: Narrow `edge_types` on `--forbid` rule + wipe `ast_hash` + weaken bidirectional co-change
    gov_obj = json.loads(baseline_gov)
    gov_obj["boundary_rules"][0]["edge_types"] = ["imports"]  # dropped "calls"
    gov_obj["notes"][0]["ast_hash"] = ""  # wiped ast_hash
    gov_obj["co_change_rules"][0]["bidirectional"] = False  # weakened <-> to ->
    (tmp_path / ".projectbrain_memory.json").write_text(json.dumps(gov_obj, indent=2), encoding="utf-8")

    res_weak = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert res_weak["all_contracts_valid"] is False
    kinds = {t["kind"] for t in res_weak["governance_tamper"]}
    assert "boundary_rule_weakened" in kinds
    assert "invariant_hash_wiped" in kinds
    assert "cochange_rule_weakened" in kinds

    # Vector D: Legitimate body edit + `memories --refresh` + adding a NEW rule must PASS with zero false positives!
    (tmp_path / ".projectbrain_memory.json").write_text(baseline_gov, encoding="utf-8")
    (tmp_path / "services" / "pay.py").write_text(
        "from db.store import save_tx\n\n"
        "def settle_payment(tx_id: str) -> bool:\n"
        "    cleaned = tx_id.strip()\n"
        "    return save_tx(cleaned)\n",
        encoding="utf-8",
    )
    (tmp_path / "db" / "store.py").write_text(
        "def save_tx(tx_id: str) -> bool:\n    return bool(tx_id)\n",
        encoding="utf-8",
    )
    assert cli_main(["--path", str(tmp_path), "memories", "--refresh"]) == 0
    assert cli_main(["--path", str(tmp_path), "remember", "--forbid", "db/* -> routes/*", "--reason", "No upward imports"]) == 0
    capsys.readouterr()

    res_legit = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert res_legit["all_contracts_valid"] is True
    assert "governance_tamper" not in res_legit


def test_co_change_cli_lifecycle_and_git_history_drift_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Explicit `--co-change` rules and git-history co-change mining must warn when coupled partner is omitted."""
    _init_git_repo(tmp_path)
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "serializer.py").write_text("def dump_item(x: int) -> dict:\n    return {'v': x}\n", encoding="utf-8")
    (tmp_path / "api" / "schema_doc.py").write_text("SCHEMA_VER = 1\n", encoding="utf-8")
    (tmp_path / "api" / "lone.py").write_text("LONE = 0\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    # Build 4 commits where `api/serializer.py` and `api/schema_doc.py` always co-change
    for ver in range(1, 5):
        (tmp_path / "api" / "serializer.py").write_text(
            f"def dump_item(x: int) -> dict:\n    return {{'v': x, 'ver': {ver}}}\n",
            encoding="utf-8",
        )
        (tmp_path / "api" / "schema_doc.py").write_text(f"SCHEMA_VER = {ver}\n", encoding="utf-8")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", f"sync schema v{ver}")

    ProjectBrain(str(tmp_path)).index()

    # Branch modifies ONLY `api/serializer.py` without `api/schema_doc.py`
    _git(tmp_path, "checkout", "-b", "feat/forgot-schema")
    (tmp_path / "api" / "serializer.py").write_text(
        "def dump_item(x: int) -> dict:\n    return {'v': x, 'ver': 5}\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "api/serializer.py")
    _git(tmp_path, "commit", "-m", "bump serializer only")

    res = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert res["all_contracts_valid"] is True  # Advisory warning, not hard block
    cw = res.get("co_change_warnings", [])
    assert any(w["trigger_file"] == "api/serializer.py" and w["missing_partner"] == "api/schema_doc.py" for w in cw)

    # Explicit CLI `--co-change` save, list, and forget
    capsys.readouterr()
    assert cli_main([
        "--path", str(tmp_path), "remember", "--co-change", "api/lone.py -> api/schema_doc.py", "--reason", "Sync docs",
    ]) == 0
    saved_cc = json.loads(capsys.readouterr().out)
    assert saved_cc["status"] == "saved_co_change_rule"
    cc_id = saved_cc["co_change_rule"]["id"]

    assert cli_main(["--path", str(tmp_path), "memories"]) == 0
    mem_out = capsys.readouterr().out
    assert "[CO-CHANGE]" in mem_out
    assert "api/lone.py -> api/schema_doc.py" in mem_out

    assert cli_main(["--path", str(tmp_path), "memories", "--forget", cc_id]) == 0
    forget_out = json.loads(capsys.readouterr().out)
    assert forget_out["removed_co_changes"] == 1


# ── 4. SARIF v2.1.0 & Zero-Noise Sticky PR Comment Reporters ─────────────────


def test_sarif_v2_1_0_and_sticky_pr_comment_mermaid_reporters(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Verify SARIF v2.1.0 schema invariants and zero-noise vs Mermaid sticky `pr-comment` output."""
    _init_git_repo(tmp_path)
    (tmp_path / "routes").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "db" / "users_db.py").write_text("def fetch_row(uid: int) -> dict:\n    return {'id': uid}\n", encoding="utf-8")
    (tmp_path / "services" / "users_svc.py").write_text(
        "from db.users_db import fetch_row\n\ndef get_account(uid: int) -> dict:\n    return fetch_row(uid)\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "users_api.py").write_text(
        "from services.users_svc import get_account\n\ndef handle_user(uid: int) -> dict:\n    return get_account(uid)\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "isolated.py").write_text("def helper_val() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_users.py").write_text(
        "from services.users_svc import get_account\n\ndef test_acc():\n    assert get_account(1)['id'] == 1\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember(forbid="routes/* -> db/*", note="No direct DB access in routes")
    brain.remember("get_account", "Must return dict with id")

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "main baseline")

    # 1. Clean isolated body edit -> Zero-Noise 1-liner PR comment!
    _git(tmp_path, "checkout", "-b", "feat/clean-isolated")
    (tmp_path / "services" / "isolated.py").write_text("def helper_val() -> int:\n    return 2\n", encoding="utf-8")
    _git(tmp_path, "add", "services/isolated.py")
    _git(tmp_path, "commit", "-m", "isolated tweak")

    capsys.readouterr()
    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--format", "pr-comment"]) == 0
    clean_pr_md = capsys.readouterr().out.strip()
    assert clean_pr_md.startswith(PR_COMMENT_MARKER)
    assert "PASS" in clean_pr_md
    assert "```mermaid" not in clean_pr_md
    assert len(clean_pr_md.splitlines()) == 2

    # 2. High-impact & violating PR -> Full Sticky PR Markdown with Mermaid `graph LR` + SARIF v2.1.0!
    (tmp_path / "services" / "users_svc.py").write_text(
        "from db.users_db import fetch_row\n\ndef get_account(uid: int, tenant: str) -> dict:\n    return fetch_row(uid)\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "users_api.py").write_text(
        "from db.users_db import fetch_row\n"
        "from services.users_svc import get_account\n\n"
        "def handle_user(uid: int) -> dict:\n"
        "    _ = fetch_row(uid)\n"
        "    return get_account(uid)\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "introduce boundary and arity violations")

    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--format", "pr-comment"]) == 0
    rich_pr_md = capsys.readouterr().out
    assert rich_pr_md.startswith(PR_COMMENT_MARKER)
    assert "ACTION REQUIRED" in rich_pr_md
    assert "```mermaid" in rich_pr_md
    assert "graph LR" in rich_pr_md
    assert "FORBIDDEN" in rich_pr_md
    assert "Covering Test Suite" in rich_pr_md
    assert "tests/test_users.py" in rich_pr_md

    # Validate SARIF v2.1.0 report structure & invariants
    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--format", "sarif", "--ci"]) == 1
    sarif_raw = capsys.readouterr().out
    sarif = json.loads(sarif_raw)
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    run0 = sarif["runs"][0]
    assert run0["tool"]["driver"]["name"] == "ProjectBrain"
    assert run0["tool"]["driver"]["version"] == __version__

    rules = run0["tool"]["driver"]["rules"]
    results = run0["results"]
    assert len(results) >= 3
    emitted_rule_ids = {r["ruleId"] for r in results}
    assert "PB002" in emitted_rule_ids  # BoundaryViolation
    assert "PB003" in emitted_rule_ids  # CallerArityViolation
    assert "PB008" in emitted_rule_ids  # StaleInvariantWarning

    for item in results:
        r_idx = item["ruleIndex"]
        assert 0 <= r_idx < len(rules)
        assert rules[r_idx]["id"] == item["ruleId"]
        loc = item["locations"][0]["physicalLocation"]
        uri = loc["artifactLocation"]["uri"]
        assert "\\" not in uri and not uri.startswith("/")
        assert loc["region"]["startLine"] >= 1
        fp = item["partialFingerprints"]["primaryLocationLineHash"]
        assert len(fp) == 32


# ── 5. OS-Portable Team Init, GitHub Action Workflow & Packaging Sync ────────


def test_portable_team_init_github_action_clean_and_packaging_sync(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """`init --team` must emit zero local paths in tracked configs, write GitHub workflow, and `clean` must remove all."""
    _init_git_repo(tmp_path)
    (tmp_path / "app.py").write_text("def serve() -> str:\n    return 'ok'\n", encoding="utf-8")

    assert cli_main(["--path", str(tmp_path), "init", "--team", "--git-hook"]) == 0
    init_res = json.loads(capsys.readouterr().out)
    assert init_res["team"] is True
    assert "wrote_.claude/settings.json_hooks" in init_res["actions"]
    assert "wrote_.cursor/hooks.json" in init_res["actions"]
    assert "wrote_.github/workflows/projectbrain.yml" in init_res["actions"]

    # Must write git-trackable `.claude/settings.json` instead of `.claude/settings.local.json`
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert (tmp_path / ".cursor" / "hooks.json").exists()
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (tmp_path / ".github" / "workflows" / "projectbrain.yml").exists()

    # Zero local machine/user/interpreter paths in any team config file
    resolved_str = str(tmp_path.resolve())
    for rel_cfg in (
        ".claude/settings.json",
        ".cursor/hooks.json",
        ".mcp.json",
        ".cursor/mcp.json",
        ".git/hooks/pre-commit",
    ):
        txt = (tmp_path / rel_cfg).read_text(encoding="utf-8")
        assert resolved_str not in txt, f"Hardcoded workspace root leaked into {rel_cfg}"
        assert sys.executable not in txt, f"Hardcoded Python executable leaked into {rel_cfg}"
        assert "\\\\" not in txt, f"Windows backslash path leaked into {rel_cfg}"

    claude_cfg = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert claude_cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "projectbrain hook post-edit"

    cursor_cfg = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert cursor_cfg["hooks"]["afterFileEdit"][0]["command"] == "projectbrain hook post-edit"

    mcp_cfg = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_cfg["mcpServers"]["projectbrain"] == {"command": "projectbrain-mcp", "args": ["--lean", "."]}

    wf_txt = (tmp_path / ".github" / "workflows" / "projectbrain.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in wf_txt
    assert "projectbrain check --ci" in wf_txt
    assert "projectbrain check --format sarif" in wf_txt
    assert "actions/upload-artifact@v4" in wf_txt
    assert "Upload SARIF to GitHub Code Scanning" in wf_txt
    assert "github/codeql-action/upload-sarif@v3" in wf_txt
    assert "sarif_file: projectbrain.sarif" in wf_txt
    assert "projectbrain check --format pr-comment" in wf_txt
    assert PR_COMMENT_MARKER in wf_txt

    # `projectbrain clean` removes portable hooks and generated workflow cleanly
    assert cli_main(["--path", str(tmp_path), "clean"]) == 0
    clean_res = json.loads(capsys.readouterr().out)
    assert "removed_.claude/settings.json_hooks" in clean_res["actions"]
    assert "removed_.github/workflows/projectbrain.yml" in clean_res["actions"]
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    assert not (tmp_path / ".github" / "workflows" / "projectbrain.yml").exists()

    # Repository-level packaging synchronization gate: root & subpackage pyproject.toml + README.md
    repo_root = Path(__file__).resolve().parents[2]
    root_toml = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    sub_toml = tomllib.loads((repo_root / "projectbrain" / "pyproject.toml").read_text(encoding="utf-8"))
    assert root_toml["project"]["name"] == sub_toml["project"]["name"] == "projectbrain-mcp"
    assert root_toml["project"]["version"] == sub_toml["project"]["version"] == __version__
    assert root_toml["project"]["dependencies"] == sub_toml["project"]["dependencies"]
    assert root_toml["project"]["scripts"] == sub_toml["project"]["scripts"]
    assert (repo_root / "README.md").read_text(encoding="utf-8") == (
        repo_root / "projectbrain" / "README.md"
    ).read_text(encoding="utf-8")


# ── 6. Phase 2: `projectbrain learn` Zero-Violation Governance Discovery ─────


def test_phase2_learn_discovers_zero_violation_boundaries_cochanges_and_hubs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """`learn` must discover zero-violation 3-tier boundaries, git co-changes, and hubs, and enforce them on `--apply`."""
    _init_git_repo(tmp_path)
    for d in ("routes", "services", "db", "utils"):
        (tmp_path / d).mkdir()

    (tmp_path / "utils" / "clock.py").write_text("def utc_ts() -> int:\n    return 1700000000\n", encoding="utf-8")
    (tmp_path / "db" / "models.py").write_text(
        "from utils.clock import utc_ts\n\ndef query_order(oid: int) -> dict:\n    return {'id': oid, 'ts': utc_ts()}\n",
        encoding="utf-8",
    )
    (tmp_path / "db" / "migrations.py").write_text("MIGRATION_REV = 1\n", encoding="utf-8")
    (tmp_path / "services" / "orders.py").write_text(
        "from db.models import query_order\n"
        "from utils.clock import utc_ts\n\n"
        "def load_order_summary(oid: int) -> dict:\n"
        "    row = query_order(oid)\n"
        "    row['checked_at'] = utc_ts()\n"
        "    return row\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "refunds.py").write_text(
        "from db.models import query_order\n\n"
        "def can_refund(oid: int) -> bool:\n"
        "    return bool(query_order(oid))\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "orders_http.py").write_text(
        "from services.orders import load_order_summary\n\n"
        "def get_order_endpoint(oid: int) -> dict:\n"
        "    return load_order_summary(oid)\n",
        encoding="utf-8",
    )
    (tmp_path / "routes" / "refunds_http.py").write_text(
        "from services.refunds import can_refund\n\n"
        "def post_refund_endpoint(oid: int) -> bool:\n"
        "    return can_refund(oid)\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    # Create 4 commits coupling `db/models.py` and `db/migrations.py`
    for rev in range(1, 5):
        (tmp_path / "db" / "migrations.py").write_text(f"MIGRATION_REV = {rev}\n", encoding="utf-8")
        (tmp_path / "db" / "models.py").write_text(
            f"from utils.clock import utc_ts\n\ndef query_order(oid: int) -> dict:\n    return {{'id': oid, 'ts': utc_ts(), 'rev': {rev}}}\n",
            encoding="utf-8",
        )
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", f"db migration rev {rev}")

    # 1. Dry-run `projectbrain learn` must NOT create `.projectbrain_memory.json`
    assert cli_main(["--path", str(tmp_path), "learn", "--json"]) == 0
    dry_res = json.loads(capsys.readouterr().out)
    assert dry_res["applied"] is False
    assert not (tmp_path / ".projectbrain_memory.json").exists()

    disc = dry_res["discovered"]
    proposed_rules = {r["rule"] for r in disc["boundary_rules"]}
    assert "routes/* -> db/*" in proposed_rules  # 3-tier encapsulation bypass rule!
    assert "db/* -> services/*" in proposed_rules  # upward DAG rule!
    assert all(r["existing_violations"] == 0 for r in disc["boundary_rules"])

    proposed_cc = {c["rule"] for c in disc["co_change_rules"]}
    assert "db/migrations.py <-> db/models.py" in proposed_cc or "db/models.py <-> db/migrations.py" in proposed_cc

    hub_names = {h["symbol"] for h in disc["hub_symbols"]}
    assert "query_order" in hub_names

    # 2. `projectbrain learn --apply --pin-hubs` saves governance with ZERO existing violations
    assert cli_main(["--path", str(tmp_path), "learn", "--apply", "--pin-hubs"]) == 0
    apply_txt = capsys.readouterr().out
    assert "APPLIED" in apply_txt
    assert (tmp_path / ".projectbrain_memory.json").exists()

    # Immediate CI gate check passes with 0 violations!
    assert cli_main(["--path", str(tmp_path), "check", "--ci"]) == 0
    capsys.readouterr()

    # 3. Idempotency: running `learn --apply` a second time adds 0 duplicate boundary/co-change rules
    assert cli_main(["--path", str(tmp_path), "learn", "--apply", "--json"]) == 0
    second_res = json.loads(capsys.readouterr().out)
    assert second_res["persisted_counts"]["boundary_rules_saved"] == 0
    assert second_res["persisted_counts"]["co_change_rules_saved"] == 0

    # 4. Learned boundary rule actively blocks a new `routes/* -> db/*` violation!
    (tmp_path / "routes" / "orders_http.py").write_text(
        "from db.models import query_order\n"
        "from services.orders import load_order_summary\n\n"
        "def get_order_endpoint(oid: int) -> dict:\n"
        "    _ = query_order(oid)\n"
        "    return load_order_summary(oid)\n",
        encoding="utf-8",
    )
    assert cli_main(["--path", str(tmp_path), "check", "--ci"]) == 1
    viol_out = capsys.readouterr().out
    assert "[BOUNDARY VIOLATION]" in viol_out
    assert "routes/* -> db/*" in viol_out


# ── 7. `init --learn`, `hook install --team`, MCP Tool Parity & Non-Git Safety ─


def test_init_learn_hook_install_team_mcp_and_non_git_edge_cases(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Verify `init --learn`, `hook install --team`, MCP tool surface, and non-git directory graceful fallback."""
    from projectbrain.server import LEAN_CORE_TOOLS, brain_check, brain_learn, brain_remember, configure_mcp_tools

    # 1. Non-git directory: `learn` and `check` must never crash when `.git` is absent
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    (non_git / "api").mkdir()
    (non_git / "store").mkdir()
    (non_git / "store" / "repo.py").write_text("def get_item(k: str) -> str:\n    return k\n", encoding="utf-8")
    (non_git / "api" / "handler.py").write_text(
        "from store.repo import get_item\n\ndef handle(k: str) -> str:\n    return get_item(k)\n",
        encoding="utf-8",
    )

    assert cli_main(["--path", str(non_git), "init", "--learn", "--team", "--no-github-action"]) == 0
    init_learn = json.loads(capsys.readouterr().out)
    assert "learned_governance" in init_learn
    assert init_learn["learned_governance"]["persisted_counts"]["boundary_rules_saved"] >= 1
    assert not (non_git / ".github" / "workflows" / "projectbrain.yml").exists()
    assert (non_git / ".projectbrain_memory.json").exists()

    # Non-git `check` runs cleanly without crashing on git subprocesses
    assert cli_main(["--path", str(non_git), "check", "--ci"]) == 0
    capsys.readouterr()

    # Non-git explicit `--base main` fails closed cleanly instead of crashing
    assert cli_main(["--path", str(non_git), "check", "--base", "main", "--ci"]) == 1
    bad_nongit = capsys.readouterr().out
    assert "GIT BASE ERROR" in bad_nongit or "not a git repository" in bad_nongit

    # 2. `hook install --team` writes portable hooks without local paths
    git_dir = tmp_path / "hook_repo"
    git_dir.mkdir()
    _init_git_repo(git_dir)
    (git_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
    assert cli_main(["--path", str(git_dir), "hook", "install", "--team", "--git"]) == 0
    hook_res = json.loads(capsys.readouterr().out)
    assert hook_res["team"] is True
    assert (git_dir / ".claude" / "settings.json").exists()
    assert not (git_dir / ".claude" / "settings.local.json").exists()
    pre_commit_txt = (git_dir / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "projectbrain hook pre-commit" in pre_commit_txt
    assert str(git_dir.resolve()) not in pre_commit_txt
    assert sys.executable not in pre_commit_txt

    # 3. MCP server parity: `--lean` keeps exact 5 tools; `--full` adds `brain_learn`, and MCP functions work directly
    lean_names = configure_mcp_tools(full_mode=False)
    assert lean_names == LEAN_CORE_TOOLS

    full_names = configure_mcp_tools(full_mode=True)
    assert "brain_learn" in full_names
    assert LEAN_CORE_TOOLS.issubset(full_names)

    # Restore default lean surface & invoke MCP entrypoints directly on `git_dir`
    configure_mcp_tools(full_mode=False)
    mcp_cc = brain_remember(co_change="mod.py <-> README.md", note="Sync docs", repo_path=str(git_dir))
    assert mcp_cc["status"] == "saved_co_change_rule"
    mcp_learn = brain_learn(apply=False, repo_path=str(non_git))
    assert mcp_learn["applied"] is False
    mcp_chk = brain_check(repo_path=str(non_git))
    assert mcp_chk["all_contracts_valid"] is True

    # 4. Pre-commit & post-edit hooks also intercept staged/in-editor `.projectbrain_memory.json` tampering
    from projectbrain.hooks import run_post_edit_hook, run_pre_commit_hook

    _git(git_dir, "add", ".projectbrain_memory.json", "mod.py")
    _git(git_dir, "commit", "-m", "commit governance rule")
    (git_dir / ".projectbrain_memory.json").unlink()
    _git(git_dir, "add", "-A")

    rc_post, post_out = run_post_edit_hook(explicit_files=[".projectbrain_memory.json"], default_root=git_dir)
    assert rc_post == 0 and "[GOVERNANCE TAMPER]" in post_out
    rc_pre, pre_out = run_pre_commit_hook(git_dir)
    assert rc_pre == 1 and "[GOVERNANCE TAMPER]" in pre_out


# ── 8. TypeScript / Next.js `src/` Layout Learn, Deleted TS Import & Mermaid ──


def test_typescript_src_layout_learn_deleted_import_and_mermaid_brackets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Verify `learn` + PR `--base` deleted-import detection + Mermaid sanitization on TypeScript `src/` repos."""
    _init_git_repo(tmp_path)
    (tmp_path / "src" / "routes" / "orders" / "[id]").mkdir(parents=True)
    (tmp_path / "src" / "services").mkdir(parents=True)
    (tmp_path / "src" / "db").mkdir(parents=True)

    (tmp_path / "src" / "db" / "client.ts").write_text(
        "export function queryOrderRow(id: string): { id: string } {\n  return { id };\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "services" / "orders.ts").write_text(
        "import { queryOrderRow } from '../db/client';\n\n"
        "export function getOrderDto(id: string): { id: string } {\n"
        "  return queryOrderRow(id);\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "routes" / "orders" / "[id]" / "route.ts").write_text(
        "import { getOrderDto } from '../../../services/orders';\n\n"
        "export function GET(id: string): { id: string } {\n"
        "  return getOrderDto(id);\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    # Learn `src/routes/* -> src/db/*` on TypeScript `src/` layout
    assert cli_main(["--path", str(tmp_path), "learn", "--apply", "--json"]) == 0
    ts_learn = json.loads(capsys.readouterr().out)
    learned_rules = {r["rule"] for r in ts_learn["discovered"]["boundary_rules"]}
    assert "src/routes/* -> src/db/*" in learned_rules

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial ts repo with learned governance")

    # PR deletes `src/db/client.ts` while `src/services/orders.ts` still imports it
    _git(tmp_path, "checkout", "-b", "feat/remove-db-client")
    _git(tmp_path, "rm", "src/db/client.ts")
    _git(tmp_path, "commit", "-m", "delete client.ts without updating consumer")

    assert cli_main(["--path", str(tmp_path), "check", "--base", "main", "--ci", "--format", "pr-comment"]) == 1
    pr_md = capsys.readouterr().out
    assert "Deleted/Renamed File Import Violation" in pr_md
    assert "src/services/orders.ts" in pr_md
    assert "src/db/client.ts" in pr_md
    # Mermaid edge labels must be safely quoted (`|"..."|`) and table rows well-formed
    assert '|"broken import"|' in pr_md


# ── 9. Monorepo Subpackage (`--path packages/billing`) Git Base & Learn ──────


def test_monorepo_subpackage_base_diff_governance_tamper_and_learn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Verify `--path packages/billing` resolves parent `.git` cleanly for `--base`, tamper lock, and `learn`."""
    _init_git_repo(tmp_path)
    pkg = tmp_path / "packages" / "billing"
    (pkg / "routes").mkdir(parents=True)
    (pkg / "services").mkdir(parents=True)
    (pkg / "db").mkdir(parents=True)

    (pkg / "db" / "ledger.py").write_text("def record_entry(amount: int) -> int:\n    return amount\n", encoding="utf-8")
    (pkg / "services" / "charge.py").write_text(
        "from db.ledger import record_entry\n\ndef run_charge(amount: int) -> int:\n    return record_entry(amount)\n",
        encoding="utf-8",
    )
    (pkg / "routes" / "charge_api.py").write_text(
        "from services.charge import run_charge\n\ndef post_charge(amount: int) -> int:\n    return run_charge(amount)\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("**/.projectbrain.db*\n**/.projectbrain_ledger.json\n", encoding="utf-8")

    # Learn governance inside subpackage `packages/billing`
    assert cli_main(["--path", str(pkg), "learn", "--apply", "--json"]) == 0
    sub_learn = json.loads(capsys.readouterr().out)
    assert "routes/* -> db/*" in {r["rule"] for r in sub_learn["discovered"]["boundary_rules"]}

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "monorepo billing baseline")

    _git(tmp_path, "checkout", "-b", "feat/billing-pr")
    # Introduce breaking arity change inside subpackage and commit cleanly
    (pkg / "services" / "charge.py").write_text(
        "from db.ledger import record_entry\n\ndef run_charge(amount: int, currency: str) -> int:\n    return record_entry(amount)\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "require currency param in run_charge")

    assert cli_main(["--path", str(pkg), "check", "--base", "main", "--ci"]) == 1
    sub_out = capsys.readouterr().out
    assert "Caller Arity Contract Violation" in sub_out
    assert "routes/charge_api.py" in sub_out

    # Fix caller, then test subpackage Governance Tamper Lock when `.projectbrain_memory.json` is deleted
    (pkg / "routes" / "charge_api.py").write_text(
        "from services.charge import run_charge\n\ndef post_charge(amount: int) -> int:\n    return run_charge(amount, 'USD')\n",
        encoding="utf-8",
    )
    (pkg / ".projectbrain_memory.json").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "fix caller but delete subpackage governance")

    assert cli_main(["--path", str(pkg), "check", "--base", "main", "--ci"]) == 1
    tamper_sub_out = capsys.readouterr().out
    assert "[GOVERNANCE TAMPER]" in tamper_sub_out


# ── 10. Unicode Paths, Deleted Submodules & Deleted Monorepo Workspace Aliases ─


def test_unicode_paths_and_deleted_submodule_and_workspace_alias(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Unicode paths (`café.py`), `from pkg import submod` deletions, and `@scope/pkg` deletions must be caught."""
    # Part A: Unicode filename + Python `from services import discount` submodule deletion
    py_repo = tmp_path / "py_repo"
    py_repo.mkdir()
    _init_git_repo(py_repo)
    (py_repo / "services").mkdir()
    (py_repo / "services" / "__init__.py").write_text("", encoding="utf-8")
    (py_repo / "services" / "discount.py").write_text(
        "def calc_discount(amount: float) -> float:\n    return amount * 0.1\n",
        encoding="utf-8",
    )
    (py_repo / "café.py").write_text(
        "def brew_espresso(shots: int) -> str:\n    return f'{shots}x'\n",
        encoding="utf-8",
    )
    (py_repo / "barista.py").write_text(
        "from café import brew_espresso\nfrom services import discount\n\n"
        "def serve_order() -> str:\n"
        "    _ = discount.calc_discount(10.0)\n"
        "    return brew_espresso(2)\n",
        encoding="utf-8",
    )
    (py_repo / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(py_repo, "add", ".")
    _git(py_repo, "commit", "-m", "baseline unicode and submodule")

    _git(py_repo, "checkout", "-b", "feat/unicode-and-submodule")
    # Modify `café.py` signature (require 2 args) AND delete `services/discount.py` while `__init__.py` remains
    (py_repo / "café.py").write_text(
        "def brew_espresso(shots: int, roast: str) -> str:\n    return f'{shots}x-{roast}'\n",
        encoding="utf-8",
    )
    _git(py_repo, "rm", "services/discount.py")
    _git(py_repo, "add", "café.py")
    _git(py_repo, "commit", "-m", "break café arity and delete discount submodule")

    brain = ProjectBrain(str(py_repo))
    brain.index()
    res = brain.check(base_ref="main")
    assert res["all_contracts_valid"] is False
    assert "café.py" in res["git_diff"]["modified_files"]
    diags_joined = "\n".join(res["contract_diagnostics"])
    assert "Caller Arity Contract Violation" in diags_joined
    assert "Deleted/Renamed File Import Violation" in diags_joined
    assert "services/discount.py" in diags_joined

    # Part B: Monorepo workspace package `@acme/billing` (`packages/billing/src/index.ts`) deleted
    ws_repo = tmp_path / "ws_repo"
    (ws_repo / "packages" / "billing" / "src").mkdir(parents=True)
    (ws_repo / "apps" / "web" / "src").mkdir(parents=True)
    _init_git_repo(ws_repo)
    (ws_repo / "package.json").write_text(
        json.dumps({"name": "acme-root", "private": True, "workspaces": ["packages/*", "apps/*"]}),
        encoding="utf-8",
    )
    (ws_repo / "packages" / "billing" / "package.json").write_text(
        json.dumps({"name": "@acme/billing", "main": "src/index.ts"}),
        encoding="utf-8",
    )
    (ws_repo / "packages" / "billing" / "src" / "index.ts").write_text(
        "export function chargeCard(cents: number): boolean { return cents > 0; }\n",
        encoding="utf-8",
    )
    (ws_repo / "apps" / "web" / "src" / "checkout.ts").write_text(
        "import { chargeCard } from '@acme/billing';\n"
        "export function pay(): boolean { return chargeCard(500); }\n",
        encoding="utf-8",
    )
    (ws_repo / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(ws_repo, "add", ".")
    _git(ws_repo, "commit", "-m", "workspace baseline")

    _git(ws_repo, "checkout", "-b", "feat/drop-billing-entry")
    _git(ws_repo, "rm", "packages/billing/src/index.ts")
    _git(ws_repo, "commit", "-m", "delete workspace billing index")

    assert cli_main(["--path", str(ws_repo), "check", "--base", "main", "--ci"]) == 1
    ws_out = capsys.readouterr().out
    assert "Deleted/Renamed File Import Violation" in ws_out
    assert "apps/web/src/checkout.ts" in ws_out
    assert "packages/billing/src/index.ts" in ws_out


# ── 11. Warm `ProjectBrain` State Isolation & `RepoMemory` Disk Auto-Reload ───


def test_warm_brain_state_isolation_and_memory_disk_reload(tmp_path: Path):
    """Warm `ProjectBrain` / MCP instances must not leak `_pre_edit_index` or overwrite CLI-saved rules."""
    _init_git_repo(tmp_path)
    (tmp_path / "svc.py").write_text("def compute(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "api.py").write_text(
        "from svc import compute\n\ndef handle() -> int:\n    return compute(5)\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "main baseline")

    _git(tmp_path, "checkout", "-b", "feat/arity-change")
    (tmp_path / "svc.py").write_text("def compute(x: int, y: int) -> int:\n    return x + y\n", encoding="utf-8")
    _git(tmp_path, "add", "svc.py")
    _git(tmp_path, "commit", "-m", "change compute arity")

    warm_brain = ProjectBrain(str(tmp_path))
    warm_brain.index()

    # 1. `check(base_ref="main")` catches the PR contract break...
    pr_res = warm_brain.check(base_ref="main")
    assert pr_res["all_contracts_valid"] is False
    assert pr_res["verdict"] == "UNSAFE_CONTRACT_VIOLATION"

    # ...and a subsequent `check()` on the clean working tree is NOT polluted by `main`'s pre-edit snapshot!
    wt_res = warm_brain.check()
    assert wt_res["all_contracts_valid"] is True
    assert wt_res["verdict"] == "SAFE_TO_PROCEED"
    assert wt_res.get("contract_diagnostics", []) == []

    # 2. External CLI writes boundary & co-change rules on disk while `warm_brain` is alive in memory
    assert cli_main(["--path", str(tmp_path), "remember", "--forbid", "api.py -> db/*", "No direct DB"]) == 0
    assert cli_main(["--path", str(tmp_path), "remember", "--co-change", "svc.py <-> api.py", "Coupled"]) == 0

    assert warm_brain.stats()["boundary_rules"] == 1
    assert warm_brain.stats()["co_change_rules"] == 1
    mem_listing = warm_brain.list_memories()
    assert mem_listing["boundary_rules_count"] == 1
    assert mem_listing["co_change_rules_count"] == 1

    # Saving a new invariant on `warm_brain` must preserve the externally-added rules on disk
    warm_brain.remember("compute", "Core math invariant")
    disk_data = json.loads((tmp_path / ".projectbrain_memory.json").read_text(encoding="utf-8"))
    assert len(disk_data["boundary_rules"]) == 1
    assert len(disk_data["co_change_rules"]) == 1
    assert len(disk_data["notes"]) == 1

    # 3. Deleting the target file marks the invariant stale in `list_memories()`
    (tmp_path / "svc.py").unlink()
    stale_check = warm_brain.list_memories()
    assert stale_check["stale_count"] == 1
    assert stale_check["memories"][0]["stale"] is True


# ── 12. Forged `ast_hash`, Duplicate Rules, `git rm --cached` & Non-Code Co-Change


def test_governance_forged_hash_duplicate_rules_cached_rm_and_noncode_cochange(tmp_path: Path):
    """Forged `ast_hash`, duplicate rule removal, `git rm --cached`, and non-code co-change drift must be caught."""
    from projectbrain.hooks import run_pre_commit_hook

    _init_git_repo(tmp_path)
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    (tmp_path / "api.py").write_text(
        "def verify_sig(token: str) -> bool:\n    return len(token) > 8\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    brain.remember("verify_sig", "Token length check invariant")
    brain.remember(co_change="openapi.yaml <-> api.py", note="Spec and handler must co-change")

    # Add duplicate invariant note (same target_id, distinct note) to verify consumed-index matching
    mem_path = tmp_path / ".projectbrain_memory.json"
    mem_raw = json.loads(mem_path.read_text(encoding="utf-8"))
    dup_note = dict(mem_raw["notes"][0])
    dup_note["note"] = "Second distinct security invariant on verify_sig"
    mem_raw["notes"].append(dup_note)
    mem_path.write_text(json.dumps(mem_raw, indent=2) + "\n", encoding="utf-8")

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline governance")

    # 1. `git rm --cached .projectbrain_memory.json` leaves file on disk but stages deletion -> blocked!
    _git(tmp_path, "rm", "--cached", ".projectbrain_memory.json")
    assert mem_path.exists()
    rc_pre, pre_msg = run_pre_commit_hook(tmp_path)
    assert rc_pre == 1
    assert "[GOVERNANCE TAMPER]" in pre_msg
    _git(tmp_path, "reset", "HEAD", ".projectbrain_memory.json")

    # 2. Removing 1 of 2 notes sharing `target_id` OR forging `ast_hash` -> caught!
    _git(tmp_path, "checkout", "-b", "feat/tamper-attempts")
    forged_data = json.loads(mem_path.read_text(encoding="utf-8"))
    forged_data["notes"].pop()  # delete second note
    forged_data["notes"][0]["ast_hash"] = "deadbeef00000000"  # forged hash not matching `api.py`
    mem_path.write_text(json.dumps(forged_data, indent=2) + "\n", encoding="utf-8")
    # Also modify ONLY `openapi.yaml` (non-code file!) to verify non-code co-change drift
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0\ninfo:\n  version: 2.0.0\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "forged hash, dropped duplicate note, and edited openapi.yaml alone")

    chk = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert chk["verdict"] == "UNSAFE_GOVERNANCE_TAMPER"
    tamper_kinds = {t["kind"] for t in chk["governance_tamper"]}
    assert "invariant_removed" in tamper_kinds
    assert "invariant_hash_forged" in tamper_kinds
    assert any(cw["missing_partner"] == "api.py" for cw in chk["co_change_warnings"])

    # 3. Legitimate `memories --refresh` after editing `verify_sig` matches real AST hash and passes!
    _git(tmp_path, "checkout", "main", "--", ".projectbrain_memory.json")
    (tmp_path / "api.py").write_text(
        "def verify_sig(token: str) -> bool:\n    return len(token.strip()) >= 12\n",
        encoding="utf-8",
    )
    assert cli_main(["--path", str(tmp_path), "memories", "--refresh"]) == 0
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "legitimate verify_sig update with refreshed ast_hash")
    clean_chk = ProjectBrain(str(tmp_path)).check(base_ref="main")
    assert clean_chk.get("governance_tamper", []) == []
    assert clean_chk["all_contracts_valid"] is True


# ── 13. SARIF/GitHub Bracketed Paths & Mermaid Overflow / Sanitization ────────


def test_sarif_and_github_special_paths_and_mermaid_overflow():
    """SARIF & GitHub reporters must preserve `[id]`/`(auth)`/`+page` paths and Mermaid overflow must count unique files."""
    from projectbrain.reporters import build_mermaid_blast_diagram, format_check_report, format_sarif_report

    synthetic = {
        "verdict": "UNSAFE_CONTRACT_VIOLATION",
        "all_contracts_valid": False,
        "boundary_violations": [],
        "governance_tamper": [],
        "contract_diagnostics": [
            "Caller Arity Contract Violation in src/routes/orders/[id]/route.ts:L14: GET() missing arg",
            "[SYNTAX ERROR] src/routes/(auth)/+page.ts:L7:3: unexpected token",
        ],
        "stale_memories": [
            {
                "target_id": "fn:src/routes/orders/[id]/route.ts:GET",
                "rel_path": "",
                "note": "Keep dynamic route cache header",
            }
        ],
        "co_change_warnings": [],
        "signature_blast": [],
        "impact_analysis": {
            "details": [
                {
                    "rel_path": "core\\engine_a.py",
                    "called_by": [
                        {"in_file": f"consumers\\c{i}.py", "called_by": "run"}
                        for i in range(5)
                    ],
                },
                {
                    "rel_path": "core\\engine_b.py",
                    "called_by": [
                        # Shares `consumers\\c3.py` and `consumers\\c4.py` in overflow!
                        {"in_file": f"consumers\\c{i}.py", "called_by": "run"}
                        for i in range(5)
                    ],
                },
            ]
        },
    }

    sarif = json.loads(format_sarif_report(synthetic))
    uris = [
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in sarif["runs"][0]["results"]
    ]
    assert "src/routes/orders/[id]/route.ts" in uris
    assert "src/routes/(auth)/+page.ts" in uris
    assert uris.count("src/routes/orders/[id]/route.ts") == 2  # PB002 + PB008 fallback from target_id

    gh_out = format_check_report(synthetic, fmt="github")
    assert "file=src/routes/orders/[id]/route.ts,line=14" in gh_out
    assert "file=src/routes/(auth)/+page.ts,line=7" in gh_out
    assert "::warning file=src/routes/orders/[id]/route.ts,title=Stale AST Invariant::" in gh_out

    # `max_nodes=4` holds 2 modified files (`engine_a.py`, `engine_b.py`) + 2 consumers (`c0.py`, `c1.py`);
    # `c2.py`, `c3.py`, `c4.py` overflow from BOTH modified files -> 3 unique overflow files (NOT 6 incident edges!).
    mermaid = build_mermaid_blast_diagram(synthetic, max_nodes=4)
    assert "+3 more connected file(s)" in mermaid
    assert "class N_MORE consumer;" in mermaid
    assert '|"calls"|' in mermaid
    assert "calls calls()" not in mermaid
    assert "core/engine_a.py" in mermaid
    assert "\\" not in mermaid


# ── 14. `learn` Solo-Commit Lift Filter, Linked `git worktree` & Subdir Hooks ─


def test_learn_cochange_solo_commits_and_lift_and_worktree_and_subdir_hooks(tmp_path: Path):
    """`learn` must not inflate solo-commit probabilities; linked `git worktree` and subdir hooks must work."""
    from projectbrain.hooks import (
        install_git_pre_commit_hook,
        remove_projectbrain_hooks,
        run_post_edit_hook,
        write_github_action_workflow,
    )

    main_repo = tmp_path / "main_repo"
    (main_repo / "packages" / "app").mkdir(parents=True)
    _init_git_repo(main_repo)
    (main_repo / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")

    # Create `hub.py` (edited in almost EVERY commit -> ubiquitous), `solo.py` (edited 10x solo + 3x with `rare.py`),
    # and truly coupled `schema.py` + `serializer.py` (edited together in 4/4 commits, never solo).
    for name in ("hub.py", "solo.py", "rare.py", "schema.py", "serializer.py"):
        (main_repo / name).write_text(f"def fn_{name[:-3]}() -> int:\n    return 0\n", encoding="utf-8")
    (main_repo / "packages" / "app" / "handler.py").write_text(
        "def run_handler(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    _git(main_repo, "add", ".")
    _git(main_repo, "commit", "-m", "init files")

    # 1. Pure 1-file solo commits on `solo.py` (8 times alone, 3 times with `rare.py`):
    #    Without 1-file commit counting, `file_counts['solo.py']` would be 3 (`3/3 = 100%` false bidirectional rule!).
    for i in range(1, 9):
        (main_repo / "solo.py").write_text(f"def fn_solo() -> int:\n    return {i}\n", encoding="utf-8")
        _git(main_repo, "add", "solo.py")
        _git(main_repo, "commit", "-m", f"pure solo {i}")

    # 2. Ubiquitous `hub.py` edited in 45 solo commits -> >82% of all repo commits (`lift < 1.25`)
    for i in range(1, 46):
        (main_repo / "hub.py").write_text(f"def fn_hub() -> int:\n    return {i}\n", encoding="utf-8")
        _git(main_repo, "add", "hub.py")
        _git(main_repo, "commit", "-m", f"ubiquitous hub {i}")

    # 3. Truly coupled `schema.py` + `serializer.py` (plus ubiquitous `hub.py`) in 4 commits
    for i in range(1, 5):
        (main_repo / "schema.py").write_text(f"def fn_schema() -> int:\n    return {i}\n", encoding="utf-8")
        (main_repo / "serializer.py").write_text(f"def fn_serializer() -> int:\n    return {i}\n", encoding="utf-8")
        (main_repo / "hub.py").write_text(f"def fn_hub() -> int:\n    return {100 + i}\n", encoding="utf-8")
        _git(main_repo, "add", "schema.py", "serializer.py", "hub.py")
        _git(main_repo, "commit", "-m", f"coupled schema+serializer {i}")

    # 4. 3 commits touching `solo.py` + `rare.py` + `hub.py` (`p(hub|rare) = 100%`, but `hub.py` lift < 1.25!)
    for i in range(1, 4):
        (main_repo / "solo.py").write_text(f"def fn_solo() -> int:\n    return {100 + i}\n", encoding="utf-8")
        (main_repo / "rare.py").write_text(f"def fn_rare() -> int:\n    return {i}\n", encoding="utf-8")
        (main_repo / "hub.py").write_text(f"def fn_hub() -> int:\n    return {200 + i}\n", encoding="utf-8")
        _git(main_repo, "add", "solo.py", "rare.py", "hub.py")
        _git(main_repo, "commit", "-m", f"solo+rare+hub {i}")

    b = ProjectBrain(str(main_repo))
    b.index()
    learned = b.learn(apply=False)
    co_rules = {r["rule"] for r in learned["discovered"]["co_change_rules"]}
    assert "schema.py <-> serializer.py" in co_rules
    # `solo.py -> rare.py` and `solo.py <-> rare.py` MUST NOT appear (`3/12 = 25% < 70%`)!
    assert "solo.py -> rare.py" not in co_rules
    assert "solo.py <-> rare.py" not in co_rules
    # Ubiquitous `hub.py` (`53/61 = 86.9%` base rate -> lift `1.15 < 1.25`) MUST be filtered out!
    assert "rare.py -> hub.py" not in co_rules
    assert "schema.py -> hub.py" not in co_rules

    # Portable Claude post-edit hook invoked from subdirectory `packages/app` with relative path `handler.py`
    subdir = main_repo / "packages" / "app"
    (subdir / "handler.py").write_text("def run_handler(x: int) ->\n", encoding="utf-8")  # syntax error
    claude_stdin = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "cwd": str(subdir),
        "tool_input": {"file_path": "handler.py"},
    })
    rc_h, out_h = run_post_edit_hook(raw_stdin=claude_stdin, default_root=None)
    assert rc_h == 0
    assert "packages/app/handler.py" in out_h
    (subdir / "handler.py").write_text("def run_handler(x: int) -> int:\n    return x\n", encoding="utf-8")

    # Generated GitHub workflow uses `github.paginate` so >100 PR comments never break sticky updates
    wf = write_github_action_workflow(main_repo)
    assert "github.paginate(github.rest.issues.listComments" in wf.read_text(encoding="utf-8")

    # Linked `git worktree`: `.git` is a regular file, not a directory!
    wt_dir = tmp_path / "linked_wt"
    _git(main_repo, "worktree", "add", "-b", "feat/wt-test", str(wt_dir), "main")
    assert (wt_dir / ".git").is_file()
    assert install_git_pre_commit_hook(wt_dir, str(wt_dir), portable=True) is True
    wt_check = ProjectBrain(str(wt_dir)).check(base_ref="main")
    assert wt_check["base_ref_info"]["active"] is True
    assert wt_check["base_ref_info"]["valid"] is True
    assert wt_check["all_contracts_valid"] is True
    removed_wt = remove_projectbrain_hooks(wt_dir)
    assert "removed_.git/hooks/pre-commit" in removed_wt


# ── 15. Mermaid Test-File Role Classification & Duplicate Edge Suppression ───


def test_mermaid_test_node_classification_and_duplicate_edge_suppression():
    """Test files in `called_by` must get role `test` (unless modified/violation/deleted) without duplicate `tests` edges."""
    import re
    from projectbrain.reporters import build_mermaid_blast_diagram

    payload = {
        "verdict": "UNSAFE_CONTRACT_VIOLATION",
        "all_contracts_valid": False,
        "boundary_violations": [],
        "governance_tamper": [],
        "contract_diagnostics": [
            "Caller Arity Contract Violation in tests/test_violating.py:L12: missing required arg",
            "Caller Arity Contract Violation in tests/test_unmodified_broken.py:L8: missing required arg",
        ],
        "git_diff": {
            "deleted_files": [
                "old/f1.py",
                "old/f2.py",
                "old/f3.py",
                "old/f4.py",
                "tests/test_fifth_deleted.py",
            ],
            "renamed_files": {"tests/test_renamed_new.py": "tests/test_fifth_deleted.py"},
        },
        "impact_analysis": {
            "details": [
                {
                    "rel_path": "src/projectbrain/brain.py",
                    "called_by": [
                        {"in_file": "tests/test_beast_mode.py", "function": "index", "line": 44},
                        {"in_file": "test_root_suite.py", "function": "index", "line": 10},
                        {"in_file": "src/ui/Button.spec.tsx", "function": "index", "line": 18},
                        {"in_file": "src/ui/Modal.test.ts", "function": "index", "line": 22},
                        {"in_file": "src/__tests__/hook.ts", "function": "index", "line": 9},
                        {"in_file": "tests/test_unmodified_broken.py", "function": "index", "line": 8},
                    ],
                    "direct_consumers": ["services/contest.py", "services/inspector.py"],
                    "test_consumers": [
                        "tests/test_beast_mode.py",
                        "tests/test_import_only.py",
                        "tests/test_unmodified_broken.py",
                    ],
                },
                {
                    "rel_path": "tests/test_modified.py",
                    "called_by": [],
                    "direct_consumers": [],
                    "test_consumers": [],
                },
                {
                    "rel_path": "tests/test_violating.py",
                    "called_by": [],
                    "direct_consumers": [],
                    "test_consumers": [],
                },
            ],
        },
    }

    mermaid = build_mermaid_blast_diagram(payload, max_nodes=24, max_edges=30)

    # Extract node_id -> label and node_id -> assigned class from rendered Mermaid
    id_by_label = {
        lbl: nid
        for nid, lbl in re.findall(r'^\s+(N\d+)\["([^"]+)"\]$', mermaid, flags=re.MULTILINE)
    }
    cls_by_id = {
        nid: cls
        for nid, cls in re.findall(r"^\s+class\s+(N\d+)\s+(\w+);$", mermaid, flags=re.MULTILINE)
    }

    # Unmodified test files in `called_by` AND `test_consumers` must all receive `test` role
    for test_lbl in (
        "tests/test_beast_mode.py",
        "test_root_suite.py",
        "src/ui/Button.spec.tsx",
        "src/ui/Modal.test.ts",
        "src/__tests__/hook.ts",
        "tests/test_import_only.py",
    ):
        assert test_lbl in id_by_label, f"Missing node {test_lbl} in Mermaid"
        assert cls_by_id[id_by_label[test_lbl]] == "test", f"Expected `test` role for {test_lbl}"

    # Non-test files with substrings like `test` or `spec` (`contest.py`, `inspector.py`) remain `consumer`
    assert cls_by_id[id_by_label["services/contest.py"]] == "consumer"
    assert cls_by_id[id_by_label["services/inspector.py"]] == "consumer"

    # Precedence: test files in `modified`, `violation` (both modified AND unmodified callers!), or `deleted` (>4th) keep higher rank
    assert cls_by_id[id_by_label["tests/test_modified.py"]] == "modified"
    assert cls_by_id[id_by_label["tests/test_renamed_new.py"]] == "modified"
    assert cls_by_id[id_by_label["tests/test_violating.py"]] == "violation"
    assert cls_by_id[id_by_label["tests/test_unmodified_broken.py"]] == "violation"
    assert cls_by_id[id_by_label["tests/test_fifth_deleted.py"]] == "deleted"

    # Duplicate edge suppression: `tests/test_beast_mode.py` already has `-->|"calls index()"|` to `brain.py`,
    # so it must NOT also emit `-.->|"tests"|` to `brain.py`; `tests/test_import_only.py` has no call edge, so it DOES.
    brain_id = id_by_label["src/projectbrain/brain.py"]
    beast_id = id_by_label["tests/test_beast_mode.py"]
    broken_test_id = id_by_label["tests/test_unmodified_broken.py"]
    imp_only_id = id_by_label["tests/test_import_only.py"]

    assert f'{beast_id} -->|"calls index()"| {brain_id}' in mermaid
    assert f'{beast_id} -.->|"tests"| {brain_id}' not in mermaid
    assert f'{broken_test_id} -->|"calls index()"| {brain_id}' in mermaid
    assert f'{broken_test_id} -.->|"tests"| {brain_id}' not in mermaid
    assert f'{imp_only_id} -.->|"tests"| {brain_id}' in mermaid


def test_end_to_end_mermaid_violating_unmodified_test_and_self_call_isolation(tmp_path: Path):
    """End-to-end `ProjectBrain.check()` must not crowd out external callers with self-calls or misclassify `contest.py`/`inspector.py`."""
    import re
    from projectbrain.reporters import build_mermaid_blast_diagram, format_check_report, format_pr_comment_report

    _init_git_repo(tmp_path)
    for d in ("core", "services", "tests", "unit_tests"):
        (tmp_path / d).mkdir()

    # >25 internal helper calls inside `core/engine.py` + isolated multi-function `services/isolated_multi.py`
    internal_calls = "\n".join(f"def step_{i}() -> int:\n    return _helper({i})" for i in range(25))
    (tmp_path / "core" / "engine.py").write_text(
        "def _helper(x: int) -> int:\n    return x\n\n"
        + internal_calls
        + "\n\ndef calc(a: int, b: int = 0) -> int:\n    return _helper(a + b)\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "contest.py").write_text(
        "from core.engine import calc\n\ndef run_contest() -> int:\n    return calc(1)\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "inspector.py").write_text(
        "from core.engine import calc\n\ndef inspect_calc() -> int:\n    return calc(1, 2)\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "isolated_multi.py").write_text(
        "def _inner(v: int) -> int:\n    return v + 1\n\ndef compute_local() -> int:\n    return _inner(10)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_engine.py").write_text(
        "from core.engine import calc\n\ndef test_calc_arity() -> None:\n    assert calc(1) == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "unit_tests" / "check_engine.py").write_text(
        "from core.engine import calc\n\ndef verify_two_arg_calc() -> None:\n    assert calc(1, 2) == 3\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Clean body edit on multi-function self-calling `services/isolated_multi.py` preserves Zero-Noise 1-liner PR comment!
    (tmp_path / "services" / "isolated_multi.py").write_text(
        "def _inner(v: int) -> int:\n    return v + 2\n\ndef compute_local() -> int:\n    return _inner(10)\n",
        encoding="utf-8",
    )
    clean_iso = ProjectBrain(str(tmp_path)).check(files_modified=["services/isolated_multi.py"])
    iso_pr = format_pr_comment_report(clean_iso).strip()
    assert iso_pr.startswith(PR_COMMENT_MARKER)
    assert len(iso_pr.splitlines()) == 2
    assert "```mermaid" not in iso_pr

    # 2. Mutate `calc(a: int, b: int)` (now 2 required args) -> breaks unmodified `contest.py` AND `tests/test_engine.py`!
    (tmp_path / "core" / "engine.py").write_text(
        "def _helper(x: int) -> int:\n    return x\n\n"
        + internal_calls
        + "\n\ndef calc(a: int, b: int) -> int:\n    return _helper(a + b)\n",
        encoding="utf-8",
    )
    res = ProjectBrain(str(tmp_path)).check(files_modified=["core/engine.py"])
    det = res["impact_analysis"]["details"][0]
    assert det["direct_consumers"] == ["services/contest.py", "services/inspector.py"]
    assert sorted(det["test_consumers"]) == ["tests/test_engine.py", "unit_tests/check_engine.py"]
    assert all(c["in_file"] != "core/engine.py" for c in det["called_by"])
    assert len(res.get("signature_blast", [])) == 1

    mermaid = build_mermaid_blast_diagram(res)
    id_by_label = {
        lbl: nid
        for nid, lbl in re.findall(r'^\s+(N\d+)\["([^"]+)"\]$', mermaid, flags=re.MULTILINE)
    }
    cls_by_id = {
        nid: cls
        for nid, cls in re.findall(r"^\s+class\s+(N\d+)\s+(\w+);$", mermaid, flags=re.MULTILINE)
    }
    assert cls_by_id[id_by_label["core/engine.py"]] == "modified"
    assert cls_by_id[id_by_label["services/contest.py"]] == "violation"
    assert cls_by_id[id_by_label["services/inspector.py"]] == "consumer"
    assert cls_by_id[id_by_label["tests/test_engine.py"]] == "violation"
    assert cls_by_id[id_by_label["unit_tests/check_engine.py"]] == "test"
    assert '-.->|"tests"|' not in mermaid

    txt_rep = format_check_report(res, fmt="text")
    assert "called by core/engine.py" not in txt_rep
    assert "services/contest.py:L4" in txt_rep
    assert "pytest tests/test_engine.py unit_tests/check_engine.py" in txt_rep


# ── 16. Nested-Caller `(file, line)` Dedup, Code Scanning SARIF & v1.1 Docs ──


def test_nested_caller_deduplication_sarif_upload_and_v1_1_docs_sync(tmp_path: Path):
    """Nested functions sharing `(file, line)` must be deduplicated across verify/hooks/reporters; workflow & docs must match v1.1.0."""
    from projectbrain.hooks import audit_edited_files, write_github_action_workflow
    from projectbrain.reporters import format_check_report, format_pr_comment_report, format_sarif_report

    _init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parsers.py").write_text(
        "def parse_source(code: str) -> dict:\n"
        "    return {'ok': bool(code)}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "contracts.py").write_text(
        "from src.parsers import parse_source\n\n"
        "def verify_cross_file_contracts(src: str) -> dict:\n"
        "    def _get_info(raw: str) -> dict:\n"
        "        return parse_source(raw)\n"
        "    return _get_info(src)\n\n"
        "def audit_second_pass(src: str) -> dict:\n"
        "    return parse_source(src)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "runner.py").write_text(
        "from src.parsers import parse_source\n\n"
        "def execute_job() -> dict:\n"
        "    return parse_source('x')\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".projectbrain.db*\n.projectbrain_ledger.json\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline nested caller repo")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Mutate `parse_source` signature (adding an optional default arg so arity stays valid while signature_blast fires)
    (tmp_path / "src" / "parsers.py").write_text(
        "def parse_source(code: str, strict: bool = False) -> dict:\n"
        "    return {'ok': bool(code), 'strict': strict}\n",
        encoding="utf-8",
    )

    # 1. `hooks.audit_edited_files()` deduplicates `(caller_file, line)` on nested `_get_info` at `src/contracts.py:L5`
    audit = audit_edited_files(tmp_path, ["src/parsers.py"])
    assert len(audit["signature_blast"]) == 1
    hook_callers = audit["signature_blast"][0]["callers"]
    hook_locs = [(c["caller_file"], c["line"]) for c in hook_callers]
    assert len(hook_locs) == len(set(hook_locs)), f"Duplicate (file, line) in hook callers: {hook_locs}"
    assert ("src/contracts.py", 5) in hook_locs
    assert ("src/contracts.py", 9) in hook_locs
    assert ("src/runner.py", 4) in hook_locs
    assert audit["report"].count("src/contracts.py:L5") == 1
    assert "src/contracts.py:L9" in audit["report"]

    # 2. `verify.run_check()` (`brain.check()`) deduplicates `(caller_file, line)` in both `called_by` and `signature_blast`
    chk = ProjectBrain(str(tmp_path)).check()
    det0 = chk["impact_analysis"]["details"][0]
    det_locs = [(c["in_file"], c["line"]) for c in det0["called_by"]]
    assert len(det_locs) == len(set(det_locs)), f"Duplicate (in_file, line) in impact called_by: {det_locs}"
    sb_locs = [(c["caller_file"], c["line"]) for c in chk["signature_blast"][0]["callers"]]
    assert len(sb_locs) == len(set(sb_locs))
    assert ("src/contracts.py", 5) in sb_locs
    assert ("src/contracts.py", 9) in sb_locs

    # 3. `reporters.py` also deduplicates synthetic duplicate `(file, line)` entries while preserving deterministic order
    dup_res = {
        "verdict": "SAFE_TO_PROCEED",
        "all_contracts_valid": True,
        "contract_diagnostics": [],
        "boundary_violations": [],
        "governance_tamper": [],
        "stale_memories": [],
        "memories": [],
        "co_change_warnings": [],
        "signature_blast": [
            {
                "file": "src/projectbrain/parsers.py",
                "mutations": [
                    {
                        "symbol": "parse",
                        "old_signature": "def parse(src: str)",
                        "new_signature": "def parse(src: str, ext: str = '.py')",
                        "line": 10,
                    }
                ],
                "callers": [
                    {
                        "caller_file": "src/projectbrain/contracts.py",
                        "caller": "fn:src/projectbrain/contracts.py:verify_cross_file_contracts",
                        "callee": "parse",
                        "line": 2527,
                    },
                    {
                        "caller_file": "src/projectbrain/contracts.py",
                        "caller": "fn:src/projectbrain/contracts.py:_get_info",
                        "callee": "parse",
                        "line": 2527,
                    },
                    {
                        "caller_file": "src/projectbrain/git_diff.py",
                        "caller": "fn:src/projectbrain/git_diff.py:load_parsed_snapshots_at_ref",
                        "callee": "parse",
                        "line": 310,
                    },
                ],
                "consumers": ["src/projectbrain/contracts.py", "src/projectbrain/git_diff.py"],
            }
        ],
        "impact_analysis": {
            "files_analyzed": 1,
            "total_impact_zone": 2,
            "details": [
                {
                    "rel_path": "src/projectbrain/parsers.py",
                    "risk": "medium",
                    "called_by": [
                        {
                            "in_file": "src/projectbrain/contracts.py",
                            "called_by": "fn:src/projectbrain/contracts.py:verify_cross_file_contracts",
                            "function": "parse",
                            "line": 2527,
                        },
                        {
                            "in_file": "src/projectbrain/contracts.py",
                            "called_by": "fn:src/projectbrain/contracts.py:_get_info",
                            "function": "parse",
                            "line": 2527,
                        },
                        {
                            "in_file": "src/projectbrain/git_diff.py",
                            "called_by": "fn:src/projectbrain/git_diff.py:load_parsed_snapshots_at_ref",
                            "function": "parse",
                            "line": 310,
                        },
                    ],
                    "direct_consumers": ["src/projectbrain/contracts.py", "src/projectbrain/git_diff.py"],
                    "test_consumers": [],
                }
            ],
        },
    }

    pr_md = format_pr_comment_report(dup_res)
    assert pr_md.count("`src/projectbrain/contracts.py:L2527`") == 1
    assert "`src/projectbrain/contracts.py:L2527`, `src/projectbrain/git_diff.py:L310`" in pr_md

    txt_out = format_check_report(dup_res, fmt="text")
    assert txt_out.count("src/projectbrain/contracts.py:L2527") == 1
    assert "called by src/projectbrain/contracts.py:L2527, src/projectbrain/git_diff.py:L310" in txt_out

    md_out = format_check_report(dup_res, fmt="markdown")
    assert md_out.count("`src/projectbrain/contracts.py:L2527`") == 1

    sarif_out = json.loads(format_sarif_report(dup_res))
    pb009_msgs = [r["message"]["text"] for r in sarif_out["runs"][0]["results"] if r["ruleId"] == "PB009"]
    assert pb009_msgs and "affects 2 caller(s)" in pb009_msgs[0]

    # 4. Generated GitHub Action workflow includes both artifact upload and Code Scanning SARIF upload
    wf_file = write_github_action_workflow(tmp_path)
    wf_yaml = wf_file.read_text(encoding="utf-8")
    assert "security-events: write" in wf_yaml
    step_blocks = wf_yaml.split("      - name: ")[1:]
    step_names = [blk.splitlines()[0].strip() for blk in step_blocks]
    assert "Generate SARIF v2.1.0 report" in step_names
    assert "Upload SARIF report Artifact" in step_names
    assert "Upload SARIF to GitHub Code Scanning" in step_names
    assert "Enforce ProjectBrain CI gate" in step_names
    assert (
        step_names.index("Generate SARIF v2.1.0 report")
        < step_names.index("Upload SARIF report Artifact")
        < step_names.index("Upload SARIF to GitHub Code Scanning")
        < step_names.index("Enforce ProjectBrain CI gate")
    )
    artifact_blk = step_blocks[step_names.index("Upload SARIF report Artifact")]
    assert "if: always()" in artifact_blk
    assert "uses: actions/upload-artifact@v4" in artifact_blk
    assert "path: projectbrain.sarif" in artifact_blk

    codeql_blk = step_blocks[step_names.index("Upload SARIF to GitHub Code Scanning")]
    assert "if: always()" in codeql_blk
    assert "continue-on-error: true" in codeql_blk
    assert "uses: github/codeql-action/upload-sarif@v3" in codeql_blk
    assert "with:\n          sarif_file: projectbrain.sarif" in codeql_blk
    try:
        import yaml  # type: ignore[import-untyped]

        parsed_wf = yaml.safe_load(wf_yaml)
        codeql_step = next(
            s for s in parsed_wf["jobs"]["guardrail"]["steps"]
            if s.get("name") == "Upload SARIF to GitHub Code Scanning"
        )
        assert codeql_step["if"] == "always()"
        assert codeql_step["continue-on-error"] is True
        assert codeql_step["uses"] == "github/codeql-action/upload-sarif@v3"
        assert codeql_step["with"] == {"sarif_file": "projectbrain.sarif"}
    except ImportError:
        pass

    # 5. Repo-wide v1.2.0 docs, READMEs, and `server.py` docstring synchronization gate
    repo_root = Path(__file__).resolve().parents[2]
    docs_html = (repo_root / "docs" / "index.html").read_text(encoding="utf-8")
    assert "v1.2" in docs_html and "v1.2.0" in docs_html
    assert "v1.0" not in docs_html and "v1.1" not in docs_html
    for expected_doc_feature in (
        "init --team --learn",
        "projectbrain learn",
        "check --base main",
        "Governance Tamper Lock",
        "SARIF v2.1.0",
        "pr-comment",
    ):
        assert expected_doc_feature in docs_html, f"Missing `{expected_doc_feature}` in docs/index.html"

    server_first_line = (
        repo_root / "projectbrain" / "src" / "projectbrain" / "server.py"
    ).read_text(encoding="utf-8").splitlines()[0]
    assert "v1.2" in server_first_line and "v1.0" not in server_first_line and "v1.1" not in server_first_line

    root_readme_bytes = (repo_root / "README.md").read_bytes()
    sub_readme_bytes = (repo_root / "projectbrain" / "README.md").read_bytes()
    assert root_readme_bytes == sub_readme_bytes
    root_readme = root_readme_bytes.decode("utf-8")
    assert "(220 tests)" not in root_readme and "(256 tests)" not in root_readme
    assert "(260 tests)" in root_readme





