"""Reproducible External-Repository Benchmark Suite for ProjectBrain.

Evaluates ProjectBrain on real clones of `pallets/flask` and `django/django`:
  1. Suite 1 (`flask_dossiers`): 6 architectural questions on `pallets/flask`,
     measuring target-function slice recall (21/21), total dossier size (KB),
     cost per correct hit (KB), and visible `# [PDG Slice: ...]` banners.
  2. Suite 2 (`flask_e2e_bugfix`): End-to-end bug-fix verification on `pallets/flask`
     (seed blueprint errorhandler regression -> verify test RED -> locate via
     `compile_dossier` + `slice_symbols` -> fix via `patch(apply=True)` ->
     verify test GREEN -> restore working tree).
  3. Suite 3 (`django_scale`): Cold `load()`, warm `is_stale()`/`incremental_index()`
     no-op, and single-file body/interface edit latency on `django/django` (~3,051 files).

Usage:
  python projectbrain/eval/bench_external.py [--repos-dir /tmp/pb_eval_repos] [--suite all|flask|django]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from projectbrain.brain import ProjectBrain


FLASK_QUESTIONS = [
    {
        "question": "how does a WSGI request flow through full_dispatch_request",
        "expected": [
            "wsgi_app",
            "full_dispatch_request",
            "dispatch_request",
            "preprocess_request",
            "finalize_request",
        ],
    },
    {
        "question": "how are blueprints registered on the Flask app and how do they record deferred functions",
        "expected": [
            "register_blueprint",
            "record",
            "register",
        ],
    },
    {
        "question": "how do request and app contexts work with LocalProxy globals",
        "expected": [
            "request_context",
            "app_context",
            "push",
            "pop",
        ],
    },
    {
        "question": "how are exceptions routed to errorhandlers",
        "expected": [
            "register_error_handler",
            "_find_error_handler",
            "handle_user_exception",
            "handle_http_exception",
        ],
    },
    {
        "question": "where is SECRET_KEY used for session signing",
        "expected": [
            "open_session",
            "save_session",
            "get_signing_serializer",
        ],
    },
    {
        "question": "how does url_for build URLs and handle build errors",
        "expected": [
            "url_for",
            "handle_url_build_error",
        ],
    },
]


def ensure_clone(repo_url: str, dest: Path) -> Path:
    if dest.exists() and (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(dest)],
        check=True,
    )
    return dest


def run_flask_dossier_suite(flask_dir: Path) -> dict:
    brain = ProjectBrain(str(flask_dir))
    t0 = time.perf_counter()
    brain.index()
    index_ms = (time.perf_counter() - t0) * 1000.0

    total_bytes = 0
    total_expected = 0
    total_found_slices = 0
    total_pdg_banners = 0
    per_question = []

    for item in FLASK_QUESTIONS:
        q = item["question"]
        expected = item["expected"]
        total_expected += len(expected)

        brain.stats(reset=True)
        res = brain.compile_dossier(q)
        md = res.get("markdown", "")
        b_len = len(md.encode("utf-8"))
        total_bytes += b_len
        kb = b_len / 1024.0

        pdg_count = md.count("# [PDG Slice:")
        total_pdg_banners += pdg_count

        found_in_slices = []
        missing = []
        for fn in expected:
            if f":: {fn} (" in md or f".{fn} (" in md or f"def {fn}(" in md:
                found_in_slices.append(fn)
                total_found_slices += 1
            else:
                missing.append(fn)

        per_question.append({
            "question": q,
            "kb": round(kb, 2),
            "expected": expected,
            "found_in_slices": found_in_slices,
            "missing": missing,
            "pdg_banners": pdg_count,
        })

    total_kb = total_bytes / 1024.0
    cost_per_hit_kb = total_kb / max(1, total_found_slices)
    return {
        "index_ms": round(index_ms, 1),
        "total_kb": round(total_kb, 2),
        "recall_slices": f"{total_found_slices}/{total_expected}",
        "cost_per_hit_kb": round(cost_per_hit_kb, 2),
        "total_pdg_banners": total_pdg_banners,
        "questions": per_question,
    }


def _check_flask_blueprint_errorhandler(flask_dir: Path) -> bool:
    """Verify blueprint errorhandler precedence in an isolated Python subprocess."""
    code = "\n".join([
        f"import sys; sys.path.insert(0, {str(flask_dir / 'src')!r})",
        "from flask import Flask, Blueprint",
        "app = Flask(__name__)",
        "bp = Blueprint('bp', __name__)",
        "@bp.route('/boom')",
        "def boom():",
        "    raise ValueError('bad')",
        "@bp.errorhandler(ValueError)",
        "def handle_bp_val(e):",
        "    return 'bp_handled', 418",
        "@app.errorhandler(ValueError)",
        "def handle_app_val(e):",
        "    return 'app_handled', 500",
        "app.register_blueprint(bp)",
        "client = app.test_client()",
        "resp = client.get('/boom')",
        "assert resp.status_code == 418 and resp.data == b'bp_handled', f'Got {resp.status_code} {resp.data!r}'",
    ])
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return proc.returncode == 0


def run_flask_e2e_bugfix_suite(flask_dir: Path) -> dict:
    """End-to-end bug-fix verification on `pallets/flask`:
    1. Seed a real bug in `src/flask/sansio/app.py:_find_error_handler` (`names = (None,)`).
    2. Confirm functional test fails (RED).
    3. Use `compile_dossier` + `slice_symbols` to locate and inspect `App._find_error_handler`.
    4. Apply `@replace_body` via `brain.patch(..., apply=True)`.
    5. Confirm functional test passes (GREEN) and restore file cleanly.
    """
    target_file = flask_dir / "src" / "flask" / "sansio" / "app.py"
    orig_source = target_file.read_text(encoding="utf-8")
    buggy_source = orig_source.replace(
        "names = (*blueprints, None)",
        "names = (None,)  # BUG: blueprint errorhandlers skipped",
        1,
    )
    assert buggy_source != orig_source, "Target line not found in src/flask/sansio/app.py"

    t0 = time.perf_counter()
    try:
        target_file.write_text(buggy_source, encoding="utf-8")
        red_ok = not _check_flask_blueprint_errorhandler(flask_dir)

        brain = ProjectBrain(str(flask_dir))
        if not brain.load():
            brain.index()
        else:
            brain.incremental_index()

        # Step 1: Dossier from symptom prompt
        dossier = brain.compile_dossier("blueprint errorhandlers are ignored when routing exceptions")
        dossier_md = dossier.get("markdown", "")
        located_in_dossier = "_find_error_handler" in dossier_md and "names = (None,)" in dossier_md

        # Step 2: Slice target symbol directly via cold/warm symbol slicer
        sliced = ProjectBrain(str(flask_dir)).slice_symbols(["src/flask/sansio/app.py:App._find_error_handler"])
        slice_code = "\n".join(s.get("code", "") for s in sliced.get("slices", []))
        located_bug_line = "names = (None,)" in slice_code

        # Step 3: Patch symbol body with syntax + contract validation
        patch_spec = """@file src/flask/sansio/app.py
@replace_body App._find_error_handler
        \"\"\"Return a registered error handler for an exception in this order:
        blueprint handler for a specific code, app handler for a specific code,
        blueprint handler for an exception class, app handler for an exception
        class, or ``None`` if a suitable handler is not found.
        \"\"\"
        exc_class, code = self._get_exc_class_and_code(type(e))
        names = (*blueprints, None)

        for c in (code, None) if code is not None else (None,):
            for name in names:
                handler_map = self.error_handler_spec[name][c]

                if not handler_map:
                    continue

                for cls in exc_class.__mro__:
                    handler = handler_map.get(cls)

                    if handler is not None:
                        return handler
        return None
@end
"""
        patch_res = brain.patch(patch_spec, apply=True)
        green_ok = _check_flask_blueprint_errorhandler(flask_dir)
    finally:
        target_file.write_text(orig_source, encoding="utf-8")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "bug_seeded_test_red": red_ok,
        "located_in_dossier": located_in_dossier,
        "located_bug_in_slice": located_bug_line,
        "patch_applied_ops": patch_res.get("total_ops_applied", 0),
        "all_syntax_valid": patch_res.get("all_syntax_valid", False),
        "all_contracts_valid": patch_res.get("all_contracts_valid", False),
        "post_patch_test_green": green_ok,
        "elapsed_ms": round(elapsed_ms, 1),
    }


def run_django_scale_suite(django_dir: Path) -> dict:
    brain = ProjectBrain(str(django_dir))
    t0 = time.perf_counter()
    if not brain.load():
        brain.index()
    load_or_index_ms = (time.perf_counter() - t0) * 1000.0

    # Measure cold load() specifically on a fresh ProjectBrain instance
    cold_brain = ProjectBrain(str(django_dir))
    t_cold = time.perf_counter()
    cold_loaded = cold_brain.load()
    cold_load_ms = (time.perf_counter() - t_cold) * 1000.0

    t_walk = time.perf_counter()
    walk_entries = cold_brain._walk_entries()
    cold_walk_ms = (time.perf_counter() - t_walk) * 1000.0

    # Warm no-op incremental_index
    t0 = time.perf_counter()
    noop_res = cold_brain.incremental_index()
    noop_ms = (time.perf_counter() - t0) * 1000.0

    # 1. Single-file body-only edit on django/http/request.py
    target = django_dir / "django" / "http" / "request.py"
    orig = target.read_text(encoding="utf-8")
    try:
        target.write_text(orig + "\n# benchmark body-only edit\n", encoding="utf-8")
        t0 = time.perf_counter()
        inc_body_res = cold_brain.incremental_index()
        body_edit_ms = (time.perf_counter() - t0) * 1000.0

        # 2. Single-file interface edit on django/http/request.py
        target.write_text(
            orig + "\ndef _pb_bench_helper(val: str) -> str:\n    return val.strip()\n",
            encoding="utf-8",
        )
        t0 = time.perf_counter()
        inc_iface_res = cold_brain.incremental_index()
        iface_edit_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        target.write_text(orig, encoding="utf-8")
        cold_brain.incremental_index()

    return {
        "total_indexed_files": len(cold_brain._index),
        "total_walked_files": len(walk_entries),
        "cold_load_ok": cold_loaded,
        "cold_load_ms": round(cold_load_ms, 1),
        "cold_walk_ms": round(cold_walk_ms, 1),
        "warm_noop_ms": round(noop_ms, 1),
        "warm_noop_status": noop_res.get("status"),
        "single_file_body_edit_ms": round(body_edit_ms, 1),
        "single_file_body_res": inc_body_res,
        "single_file_iface_edit_ms": round(iface_edit_ms, 1),
        "single_file_iface_res": inc_iface_res,
    }


def _run_question_list(brain: ProjectBrain, questions: list[dict]) -> dict:
    total_bytes = 0
    total_expected = 0
    total_found_slices = 0
    bare_init_headers = 0
    per_question = []

    for item in questions:
        q = item["question"]
        expected = item["expected"]
        total_expected += len(expected)

        brain.stats(reset=True)
        res = brain.compile_dossier(q)
        md = res.get("markdown", "")
        b_len = len(md.encode("utf-8"))
        total_bytes += b_len
        kb = b_len / 1024.0
        bare_init_headers += md.count(":: __init__ (")

        found_in_slices = []
        missing = []
        for fn in expected:
            if (
                f":: {fn} (" in md
                or f".{fn} (" in md
                or f"def {fn}(" in md
                or f"function {fn}(" in md
            ):
                found_in_slices.append(fn)
                total_found_slices += 1
            else:
                missing.append(fn)

        per_question.append({
            "question": q,
            "kb": round(kb, 2),
            "expected": expected,
            "found_in_slices": found_in_slices,
            "missing": missing,
        })

    total_kb = total_bytes / 1024.0
    return {
        "total_kb": round(total_kb, 2),
        "recall_slices": f"{total_found_slices}/{total_expected}",
        "found": total_found_slices,
        "expected": total_expected,
        "cost_per_hit_kb": round(total_kb / max(1, total_found_slices), 2),
        "bare_init_headers": bare_init_headers,
        "questions": per_question,
    }


def run_heldout_repos_suite(repos_dir: Path) -> dict:
    """Evaluate ProjectBrain on held-out repos (`psf/requests`, `pallets/click`, `expressjs/express`)."""
    req_dir = ensure_clone("https://github.com/psf/requests.git", repos_dir / "requests")
    click_dir = ensure_clone("https://github.com/pallets/click.git", repos_dir / "click")
    express_dir = ensure_clone("https://github.com/expressjs/express.git", repos_dir / "express")

    # 1. psf/requests
    req_brain = ProjectBrain(str(req_dir))
    t0 = time.perf_counter()
    req_brain.index()
    req_index_ms = (time.perf_counter() - t0) * 1000.0
    req_verify = req_brain.verify_claim("Session.send", "calls", "HTTPAdapter.send")
    req_slice = req_brain.slice_symbols(["should_strip_auth", "get_environ_proxies"])
    req_questions = [
        {
            "question": "how are proxies resolved and merged with environment settings",
            "expected": ["rebuild_proxies", "merge_environment_settings", "resolve_proxies", "get_environ_proxies"],
        },
        {
            "question": "how are redirects handled and when is auth stripped",
            "expected": ["resolve_redirects", "rebuild_auth", "should_strip_auth"],
        },
        {
            "question": "how does cookie handling work across requests and responses",
            "expected": ["extract_cookies_to_jar", "merge_cookies", "cookiejar_from_dict", "prepare_cookies"],
        },
        {
            "question": "how are adapters mounted and how is the connection pool initialized",
            "expected": ["mount", "get_adapter", "init_poolmanager", "send"],
        },
    ]
    req_dossiers = _run_question_list(req_brain, req_questions)

    # 2. pallets/click
    click_brain = ProjectBrain(str(click_dir))
    t0 = time.perf_counter()
    click_brain.index()
    click_index_ms = (time.perf_counter() - t0) * 1000.0
    click_questions = [
        {
            "question": "how are options and arguments parsed from command line args",
            "expected": ["parse_args", "handle_parse_result", "consume_value", "process_value"],
        },
        {
            "question": "how does Group dispatch subcommands and resolve commands",
            "expected": ["invoke", "resolve_command", "get_command"],
        },
        {
            "question": "how are parameter types and custom converters validated",
            "expected": ["ParamType.convert", "type_cast_value", "convert_type", "ParamType.fail"],
        },
    ]
    click_dossiers = _run_question_list(click_brain, click_questions)

    # 3. expressjs/express
    exp_brain = ProjectBrain(str(express_dir))
    t0 = time.perf_counter()
    exp_brain.index()
    exp_index_ms = (time.perf_counter() - t0) * 1000.0
    exp_slice = exp_brain.slice_symbols(["res.send", "app.use", "app.handle"])
    exp_questions = [
        {
            "question": "how does res.send set content type and send responses",
            "expected": ["res.send", "res.contentType"],
        },
        {
            "question": "how does app.use mount middleware and sub-apps",
            "expected": ["app.use", "app.handle"],
        },
        {
            "question": "how does app.handle dispatch requests through router",
            "expected": ["app.handle", "app"],
        },
    ]
    exp_dossiers = _run_question_list(exp_brain, exp_questions)

    total_found = req_dossiers["found"] + click_dossiers["found"] + exp_dossiers["found"]
    total_expected = req_dossiers["expected"] + click_dossiers["expected"] + exp_dossiers["expected"]
    total_kb = req_dossiers["total_kb"] + click_dossiers["total_kb"] + exp_dossiers["total_kb"]

    return {
        "overall_recall": f"{total_found}/{total_expected}",
        "overall_cost_per_hit_kb": round(total_kb / max(1, total_found), 2),
        "requests": {
            "index_ms": round(req_index_ms, 1),
            "verify_dynamic_dispatch_verdict": req_verify.get("verdict"),
            "verify_dynamic_dispatch_explanation": req_verify.get("explanation"),
            "sliced_symbols": [s["symbol"] for s in req_slice.get("slices", [])],
            "dossiers": req_dossiers,
        },
        "click": {
            "index_ms": round(click_index_ms, 1),
            "dossiers": click_dossiers,
        },
        "express": {
            "index_ms": round(exp_index_ms, 1),
            "response_js_functions": len(exp_brain._index.get("lib/response.js", {}).get("functions", [])),
            "application_js_functions": len(exp_brain._index.get("lib/application.js", {}).get("functions", [])),
            "sliced_symbols": [s["symbol"] for s in exp_slice.get("slices", [])],
            "dossiers": exp_dossiers,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run external repository benchmarks for ProjectBrain.")
    parser.add_argument(
        "--repos-dir",
        default=str(Path(tempfile.gettempdir()) / "pb_eval_repos"),
        help="Directory to clone/reuse external benchmark repositories.",
    )
    parser.add_argument(
        "--suite",
        choices=("all", "flask", "django", "heldout"),
        default="all",
        help="Which benchmark suite to execute.",
    )
    args = parser.parse_args()
    repos_dir = Path(args.repos_dir)

    report: dict = {}
    if args.suite in ("all", "flask"):
        flask_dir = ensure_clone("https://github.com/pallets/flask.git", repos_dir / "flask")
        report["flask_dossiers"] = run_flask_dossier_suite(flask_dir)
        report["flask_e2e_bugfix"] = run_flask_e2e_bugfix_suite(flask_dir)

    if args.suite in ("all", "django"):
        django_dir = ensure_clone("https://github.com/django/django.git", repos_dir / "django")
        report["django_scale"] = run_django_scale_suite(django_dir)

    if args.suite in ("all", "heldout"):
        report["heldout_repos"] = run_heldout_repos_suite(repos_dir)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
