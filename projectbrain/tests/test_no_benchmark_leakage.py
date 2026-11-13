"""CI Gate: Verify zero benchmark target leakage in src/projectbrain/ and test algorithmic static analysis."""

import ast
from pathlib import Path

from projectbrain.brain import ProjectBrain
from projectbrain.slicer import slice_lines


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "projectbrain"
BENCH_EXT = ROOT / "eval" / "bench_external.py"


def _extract_benchmark_multiword_targets() -> set[str]:
    """Extract all multi-word or CamelCase expected symbol names from eval/bench_external.py."""
    text = BENCH_EXT.read_text(encoding="utf-8")
    tree = ast.parse(text)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("expected", "expected_symbols") and isinstance(v, ast.List):
                    for elt in v.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            sym = elt.value
                            # Multi-word snake_case or CamelCase benchmark symbols
                            if "_" in sym or (any(c.isupper() for c in sym[1:]) and any(c.islower() for c in sym)):
                                targets.add(sym)
    return targets


def test_zero_benchmark_target_leakage_in_production_source():
    """No multi-word or CamelCase expected_symbols from bench_external.py may appear in src/projectbrain/*.py."""
    bench_targets = _extract_benchmark_multiword_targets()
    assert len(bench_targets) >= 15, f"Expected to extract benchmark targets, got {bench_targets}"

    leaks: list[str] = []
    for py_file in sorted(SRC_DIR.glob("*.py")):
        src_text = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src_text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                for target in bench_targets:
                    if target in val:
                        leaks.append(f"{py_file.name}:L{getattr(node, 'lineno', '?')} contains benchmark symbol '{target}' in string literal {val!r}")
    assert not leaks, "Benchmark target leakage detected in production source:\n" + "\n".join(leaks)


def test_slice_lines_pins_seed_lines_first_and_eliminates_micro_folds(tmp_path: Path):
    """Verify seed_lines are pinned before control/data guards and 1-2 line micro-folds are merged."""
    code_lines = [
        "def complex_handler(req, config, user):",
        "    SCHEMA_VERSION = 1",
        "    DEFAULT_TIMEOUT = 30",
        "    MAX_RETRIES = 3",
    ]
    # Add 12 control guards and intermediate assignments to stress pdg_budget
    for i in range(1, 13):
        code_lines.append(f"    if req.flag_{i}:")
        code_lines.append(f"        step_{i} = req.val_{i} + {i}")
    code_lines.extend([
        "    critical_signature = compute_hmac_digest(req.payload, config.secret)",
        "    intervening_line = critical_signature.strip()",
        "    verified_ok = verify_digest(intervening_line)",
        "    return verified_ok",
    ])
    f_path = tmp_path / "handler.py"
    f_path.write_text("\n".join(code_lines) + "\n", encoding="utf-8")

    out = slice_lines(
        tmp_path,
        "handler.py",
        1,
        len(code_lines),
        max_lines=16,
        focus_terms=["compute_hmac_digest", "verify_digest"],
    )
    assert "compute_hmac_digest" in out
    assert "verify_digest" in out
    # 1-line gap `intervening_line` between the two matched lines must be merged, never `[folded 1 lines]`
    assert "[folded 1 lines" not in out
    assert "[folded 2 lines" not in out


def test_slice_symbols_fuzzy_did_you_mean_recovery(tmp_path: Path):
    """Verify slice_symbols returns did_you_mean suggestions when a symbol name is slightly misspelled."""
    (tmp_path / "service.py").write_text(
        "class PaymentProcessor:\n"
        "    def authorize_transaction(self, amount: int) -> bool:\n"
        "        return amount > 0\n\n"
        "    def refund_transaction(self, tx_id: str) -> bool:\n"
        "        return bool(tx_id)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    res = brain.slice_symbols(["service.py:PaymentProcessor.authorise_transaction"])
    assert len(res["slices"]) == 1
    entry = res["slices"][0]
    assert "error" in entry
    assert "did_you_mean" in entry
    assert any("authorize_transaction" in s for s in entry["did_you_mean"])


def test_typed_receiver_and_base_class_inheritance_call_resolution(tmp_path: Path):
    """Verify parameter type annotations, constructor assignments, and inherited self.method() calls resolve in CodeGraph."""
    (tmp_path / "base.py").write_text(
        "class BaseValidator:\n"
        "    def check_constraints(self, val: int) -> bool:\n"
        "        return val >= 0\n\n"
        "class AuditLogger:\n"
        "    def record_event(self, msg: str) -> None:\n"
        "        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "worker.py").write_text(
        "from base import BaseValidator, AuditLogger\n\n"
        "class OrderValidator(BaseValidator):\n"
        "    def validate_order(self, logger: AuditLogger, total: int) -> bool:\n"
        "        ok = self.check_constraints(total)\n"
        "        logger.record_event('checked')\n"
        "        return ok\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    src_nid = "fn:worker.py:OrderValidator.validate_order"
    callees = {
        e["dst"]: e["confidence"]
        for e in brain._graph.edges
        if e["src"] == src_nid and e["type"] == "calls"
    }
    assert "fn:base.py:BaseValidator.check_constraints" in callees
    assert callees["fn:base.py:BaseValidator.check_constraints"] in ("exact", "import_resolved")
    assert "fn:base.py:AuditLogger.record_event" in callees
    assert callees["fn:base.py:AuditLogger.record_event"] in ("exact", "import_resolved")


def test_modular_decomposition_and_lean_mcp_surface():
    """Verify God-classes are decomposed into single-responsibility modules and MCP defaults to lean surface."""
    from projectbrain import contracts, dossier, verify, runner, server

    slicer_lines = len((SRC_DIR / "slicer.py").read_text(encoding="utf-8").splitlines())
    brain_lines = len((SRC_DIR / "brain.py").read_text(encoding="utf-8").splitlines())
    assert slicer_lines < 1400, f"slicer.py regressed into a monolith ({slicer_lines} lines)"
    assert brain_lines < 3000, f"brain.py regressed into a monolith ({brain_lines} lines)"

    assert callable(contracts.extract_4d_coordinate_matrix)
    assert callable(contracts.compile_6d_contract_diagnostics)
    assert callable(dossier.compile_engineering_dossier)
    assert callable(dossier.compile_evidence_packet)
    assert callable(verify.verify_claim)
    assert callable(verify.impact)
    assert callable(runner.compress_command_output)
    assert 5 <= len(server.LEAN_CORE_TOOLS) <= 8
    assert "brain_dossier" in server.LEAN_CORE_TOOLS
    assert "brain_slice" in server.LEAN_CORE_TOOLS
    assert "brain_explore" in server.LEAN_CORE_TOOLS
    assert "brain_check" in server.LEAN_CORE_TOOLS
    assert "brain_remember" in server.LEAN_CORE_TOOLS
    assert "brain_patch" not in server.LEAN_CORE_TOOLS


def test_cold_sqlite_fast_paths_do_not_hydrate_full_graph(tmp_path: Path, capsys):
    """Verify cold slice (incl. missing targets), explore (neighbors + path), and memories stay zero-hydration."""
    from projectbrain import cli

    (tmp_path / "ledger.py").write_text(
        "class BillingLedger:\n"
        "    def reconcile_invoice_balance(self, account_id: str) -> int:\n"
        "        return len(account_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "dispatcher.py").write_text(
        "from ledger import BillingLedger\n\n"
        "def run_settlement_cycle(ledger_instance) -> int:\n"
        "    return ledger_instance.reconcile_invoice_balance('acc_1')\n",
        encoding="utf-8",
    )
    b_init = ProjectBrain(str(tmp_path))
    b_init.index()
    b_init.remember("reconcile_invoice_balance", "Must remain idempotent on retries.")

    # 1. Cold slice with one valid range + one misspelled symbol must NOT hydrate _index
    b_cold_slice = ProjectBrain(str(tmp_path))
    res_s = b_cold_slice.slice_symbols(["ledger.py:1-3", "ledger.py:BillingLedger.reconcile_invoic_balanc"])
    assert b_cold_slice._index == {}
    assert len(res_s["slices"]) == 2
    assert "reconcile_invoice_balance" in res_s["slices"][0]["code"]
    assert "error" in res_s["slices"][1]
    assert any("reconcile_invoice_balance" in s for s in res_s["slices"][1]["did_you_mean"])

    # 2. Cold explore (neighbors AND bidirectional shortest path) must NOT hydrate _index
    b_cold_exp = ProjectBrain(str(tmp_path))
    res_nb = b_cold_exp.explore("reconcile_invoice_balance", depth=2)
    assert b_cold_exp._index == {}
    assert res_nb["center"]["name"] == "reconcile_invoice_balance"
    res_path = b_cold_exp.explore("run_settlement_cycle", target_b="reconcile_invoice_balance")
    assert b_cold_exp._index == {}
    assert len(res_path["path"]) >= 1

    # 3. Cold list_memories must NOT hydrate _index
    b_cold_mem = ProjectBrain(str(tmp_path))
    res_mem = b_cold_mem.list_memories()
    assert b_cold_mem._index == {}
    assert res_mem["count"] == 1
    assert res_mem["stale_count"] == 0

    # 4. CHA monomorphic receiver call expansion surfaces untyped OOP caller in compile_dossier
    dossier_res = b_init.compile_dossier("Bug inside reconcile_invoice_balance settlement")
    assert "run_settlement_cycle" in dossier_res["markdown"]

    # 5. CLI batched slice preview reports actual line count (never EndLine < StartLine)
    big_lines = "\n".join(f"def generated_helper_{i}(x):\n    return x + {i}\n" for i in range(260))
    (tmp_path / "large_mod.py").write_text(big_lines + "\n", encoding="utf-8")
    b_init.index()
    capsys.readouterr()
    rc = cli.main(["--path", str(tmp_path), "slice", "large_mod.py:1-380", "--max-lines", "380"])
    assert rc == 0
    cli_out = capsys.readouterr().out
    if "StartLine=" in cli_out:
        import re as _re
        m = _re.search(r"StartLine=(\d+),\s*EndLine=(\d+)", cli_out)
        assert m is not None
        assert int(m.group(2)) >= int(m.group(1))

    # 6. CLI check on file with callers formats text & markdown reports cleanly without KeyError
    rc_chk = cli.main(["--path", str(tmp_path), "check", "ledger.py"])
    assert rc_chk == 0
    chk_out = capsys.readouterr().out
    assert "dispatcher.py" in chk_out
    rc_chk_md = cli.main(["--path", str(tmp_path), "check", "ledger.py", "--format", "markdown"])
    assert rc_chk_md == 0
    assert "dispatcher.py" in capsys.readouterr().out



