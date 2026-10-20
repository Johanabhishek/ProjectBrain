from pathlib import Path
from projectbrain.brain import ProjectBrain
from projectbrain.patcher import apply_patch_spec, parse_patch_spec


def test_patcher_sub_replace_insert_delete_and_syntax(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    app_py = src_dir / "app.py"
    app_py.write_text(
        "import os\n"
        "\n"
        "TIMEOUT = 10\n"
        "\n"
        "def check_user(user, hydrated=False):\n"
        "    if not user:\n"
        "        return 'redirect:/login'\n"
        "    if not user.get('profile'):\n"
        "        return 'redirect:/profile'\n"
        "    # legacy debug block\n"
        "    x = 1\n"
        "    y = 2\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )

    spec = """
@file src/app.py
@sub L3 `10` -> `30`
@sub L8 `not user.get('profile')` -> `hydrated and not user.get('profile')`
@delete L10-L12
@insert_before L13
    if not hydrated:
        return 'wait'
@end
"""
    # 1. Dry-run first
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    dry_res = brain.patch(spec, apply=False)
    assert dry_res["mode"] == "dry_run"
    assert dry_res["all_syntax_valid"] is True
    assert dry_res["total_ops_applied"] == 4
    telem = dry_res["output_token_telemetry"]
    assert telem["output_savings_pct_vs_edit_file"] > 40.0
    assert (tmp_path / ".projectbrain_patch.diff").exists()

    # Original file untouched in dry_run
    assert "TIMEOUT = 10" in app_py.read_text(encoding="utf-8")

    # 2. Apply patch
    app_res = brain.patch(spec, apply=True)
    assert app_res["mode"] == "applied"
    assert app_res["all_syntax_valid"] is True

    updated = app_py.read_text(encoding="utf-8")
    assert "TIMEOUT = 30" in updated
    assert "if hydrated and not user.get('profile'):" in updated
    assert "legacy debug block" not in updated
    assert "if not hydrated:\n        return 'wait'\n    return 'ok'" in updated


def test_patcher_fuzzy_line_window_and_replace(tmp_path: Path):
    f = tmp_path / "service.py"
    f.write_text(
        "def a():\n"
        "    return 1\n"
        "\n"
        "def b():\n"
        "    val = fetch_data()\n"
        "    return val\n",
        encoding="utf-8",
    )
    # Give slightly off line number L2 instead of L5 for @sub within ±8 window
    spec = """
@file service.py
@sub L2 `fetch_data()` -> `fetch_data(timeout=5)`
@replace L1-L2
def a(x=0):
    return x + 1
@end
"""
    res = apply_patch_spec(tmp_path, spec, apply=True)
    assert res["all_syntax_valid"] is True
    txt = f.read_text(encoding="utf-8")
    assert "def a(x=0):" in txt
    assert "fetch_data(timeout=5)" in txt


def test_patcher_all_and_symbol_anchors(tmp_path: Path):
    f = tmp_path / "client.py"
    f.write_text(
        "def get_catalog(tid):\n"
        "    return call_api(headers=get_headers(tid))\n"
        "\n"
        "def get_chapter(tid):\n"
        "    return call_api(headers=get_headers(tid))\n",
        encoding="utf-8",
    )
    spec = """
@file client.py
@sub ALL `headers=get_headers(tid)` -> `credentials=True, headers=get_headers(tid)`
@sub get_chapter `return call_api` -> `return call_api_v2`
"""
    res = apply_patch_spec(tmp_path, spec, apply=True)
    assert res["all_syntax_valid"] is True
    assert res["total_ops_applied"] == 3
    txt = f.read_text(encoding="utf-8")
    assert txt.count("credentials=True, headers=get_headers(tid)") == 2
    assert "return call_api_v2(credentials=True" in txt


def test_savings_ledger_and_stats(tmp_path: Path):
    f = tmp_path / "service.py"
    f.write_text(
        "\n".join([f"def helper_{i}(x):\n    return x + {i}\n" for i in range(40)])
        + "\ndef target_auth_flow(token):\n    return helper_1(token)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    d_res = brain.compile_dossier(["trace target_auth_flow"])
    assert d_res["input_tokens_saved"] >= 0

    p_res = brain.patch("@file service.py\n@sub * `helper_1(token)` -> `helper_2(token)`\n", apply=False)
    assert p_res["all_syntax_valid"] is True

    st = brain.stats()
    assert st["total_operations"] == 2
    assert st["output_tokens"]["saved"] > 0
    assert st["estimated_usd_saved"] >= 0.0


