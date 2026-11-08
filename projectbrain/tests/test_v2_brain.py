"""Tests for ProjectBrain v2 features:
- Deep AST signature, parameter, return type, and class schema extraction
- TypeScript interface & path-alias (@/, ~/) resolution
- Symbol-level import resolution (`from pkg import mod`)
- Receiver-aware call graph (`self.method()`, `ClassName.method()`, `mod.fn()`)
- Surgical AST Slicer (`compile_context` and `slice_symbols`)
- Automatic session deduplication (no manual `mark_read` ceremony required)
- Persistent AST-anchored Repo Memory (`.projectbrain_memory.json`) with staleness detection
- One-command `projectbrain init` packaging setup
"""
import json
from projectbrain import cli
from projectbrain.brain import ProjectBrain
from projectbrain.parsers import parse
from projectbrain.resolve import resolve_js_import, resolve_file_imports_and_symbols, build_python_module_index


def test_python_ast_signatures_and_class_fields_extracted():
    src = '''\
class CouponSchema(BaseModel):
    """Coupon model schema."""
    code: str
    discount_pct: float = 10.0

    async def apply_to_total(self, subtotal: float, strict: bool = True) -> float:
        """Applies coupon discount."""
        return subtotal * (1.0 - self.discount_pct / 100.0)
'''
    info = parse(src, ".py")
    cls = info["classes"][0]
    assert cls["name"] == "CouponSchema"
    assert cls["signature"] == "class CouponSchema(BaseModel):"
    assert "code: str" in cls["fields"]
    assert any("discount_pct" in f for f in cls["fields"])
    assert cls["ast_hash"]

    fn = info["functions"][0]
    assert fn["name"] == "apply_to_total"
    assert fn["signature"] == "async def apply_to_total(self, subtotal: float, strict: bool = True) -> float"
    assert fn["return_type"] == "float"
    assert fn["ast_hash"]


def test_ts_interface_and_method_signatures_extracted():
    src = '''\
export interface CheckoutPayload {
    cartId: string;
    couponCode?: string;
}

export class OrderProcessor extends BaseProcessor {
    async processOrder(payload: CheckoutPayload): Promise<boolean> {
        return true;
    }
}
'''
    info = parse(src, ".ts")
    by_cls = {c["name"]: c for c in info["classes"]}
    assert "CheckoutPayload" in by_cls
    assert by_cls["CheckoutPayload"]["is_interface"] is True
    assert "cartId: string" in by_cls["CheckoutPayload"]["fields"]

    assert "OrderProcessor" in by_cls
    assert by_cls["OrderProcessor"]["bases"] == ["BaseProcessor"]

    fn = info["functions"][0]
    assert fn["name"] == "processOrder"
    assert "CheckoutPayload" in fn["signature"]
    assert "Promise<boolean>" in fn["signature"]


def test_ts_path_alias_resolution():
    files = {"src/components/Button.tsx", "src/utils/math.ts"}
    assert resolve_js_import("src/pages/Home.tsx", "@/components/Button", files) == "src/components/Button.tsx"
    assert resolve_js_import("src/pages/Home.tsx", "~/utils/math", files) == "src/utils/math.ts"


def test_python_from_package_import_submodule_resolved():
    src = "from services import discount\n"
    info = parse(src, ".py")
    index = {
        "routes/checkout.py": info,
        "services/__init__.py": {},
        "services/discount.py": {},
    }
    mod_idx = build_python_module_index(index)
    targets, sym_map = resolve_file_imports_and_symbols(
        "routes/checkout.py", info, set(index), mod_idx,
    )
    assert "services/discount.py" in targets
    assert sym_map["discount"] == "services/discount.py"


def test_receiver_aware_self_and_module_calls_in_graph(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "a.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return self.helper()\n"
        "    def helper(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "b.py").write_text(
        "class Other:\n"
        "    def helper(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Worker.run calls self.helper() -> should resolve EXACT to Worker.helper, NOT candidate to Other.helper
    v_exact = brain.verify_claim("Worker.run", "calls", "helper")
    assert v_exact["verdict"] == "SUPPORTED"
    assert v_exact["confidence"] == "exact"


def test_compile_context_slices_code_and_auto_deduplicates_session(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    # First call: compiles surgical slices + neighbor skeletons
    packet1 = brain.compile_context("add discount code to checkout", budget_tokens=2000)
    assert packet1["target_slices"], "Expected surgical target code slices"
    assert packet1["neighbor_skeletons"], "Expected signature-only neighbor skeletons"
    assert "resolution_telemetry" in packet1

    # Verify actual code with line numbers is in target_slices
    first_slice = packet1["target_slices"][0]
    assert "|" in first_slice["code"]
    assert first_slice["signature"].startswith("def ")

    # Second call in the same session: already-served slices are automatically deduplicated!
    packet2 = brain.compile_context("add discount code to checkout", budget_tokens=2000)
    assert "deduplicated_in_session" in packet2
    assert len(packet2["deduplicated_in_session"]) > 0


def test_slice_symbols_by_name_and_file_skeleton(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    # Slice a single function by name + a file skeleton by path
    res = brain.slice_symbols(["apply_discount", "routes/checkout.py"])
    assert res["count"] == 2
    fn_slice = next(s for s in res["slices"] if s.get("symbol") == "apply_discount")
    assert "def apply_discount(code)" in fn_slice["code"]

    skel_slice = next(s for s in res["slices"] if s.get("rel_path") == "routes/checkout.py" and "skeleton" in s)
    assert "# File Skeleton: routes/checkout.py" in skel_slice["skeleton"]
    assert "def get_checkout(): ..." in skel_slice["skeleton"]


def test_repo_memory_persistence_and_ast_staleness(tmp_project):
    brain = ProjectBrain(str(tmp_project))
    brain.index()

    # Save an architectural note anchored to `apply_discount`
    saved = brain.remember("apply_discount", "Discount codes must be uppercase and validated before tax.")
    assert saved["status"] == "saved"
    assert (tmp_project / ".projectbrain_memory.json").exists()

    # Compile context for discount -> memory should be automatically attached and not stale
    packet = brain.compile_context("apply discount")
    assert "memories" in packet
    mem = packet["memories"][0]
    assert "uppercase" in mem["note"]
    assert mem["stale"] is False

    # Now modify `apply_discount` in `services/discount.py` so its AST hash changes!
    discount_file = tmp_project / "services" / "discount.py"
    discount_file.write_text(
        "from services.util import helper\n\n"
        "def apply_discount(code, tax_rate=0.08):\n"
        "    return code.upper()\n",
        encoding="utf-8",
    )
    brain.index()

    # Query memories again -> should now flag `stale: True` because AST changed!
    memories = brain.list_memories()["memories"]
    assert memories[0]["stale"] is True
    assert "stale_warning" in memories[0]


def test_cli_init_creates_gitignore_mcp_and_agent_rules(tmp_project, capsys):
    # 1. Default (--ide auto) creates minimal root files (.gitignore, .mcp.json, AGENTS.md) without IDE sprawl
    rc = cli.main(["--path", str(tmp_project), "init"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "initialized"
    assert out["ide"] == "auto"
    assert (tmp_project / ".gitignore").exists()
    assert ".projectbrain.json" in (tmp_project / ".gitignore").read_text(encoding="utf-8")
    assert (tmp_project / ".mcp.json").exists()
    assert (tmp_project / "AGENTS.md").exists()
    assert not (tmp_project / ".cursor").exists()
    assert not (tmp_project / ".vscode").exists()

    # 2. Explicit --ide all creates all IDE configs & rules
    rc_all = cli.main(["--path", str(tmp_project), "init", "--ide", "all"])
    assert rc_all == 0
    capsys.readouterr()
    assert (tmp_project / ".agents" / "rules" / "projectbrain.md").exists()
    assert (tmp_project / "CLAUDE.md").exists()
    # Cursor MCP & .mdc rule
    assert (tmp_project / ".cursor" / "mcp.json").exists()
    assert (tmp_project / ".cursor" / "rules" / "projectbrain.mdc").exists()
    # VS Code MCP & Copilot/Cline rules
    vscode_mcp = json.loads((tmp_project / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert "projectbrain" in vscode_mcp["servers"]
    assert (tmp_project / ".github" / "copilot-instructions.md").exists()
    assert (tmp_project / ".clinerules").exists()


def test_multidim_program_slice_backward_forward_and_control_guards(tmp_path):
    from projectbrain.slicer import compute_multidim_program_slice, slice_lines

    # Construct a 62-line function where:
    # - 0-idx 2 (L3) defines `signing_key = derive_key(raw_secret)` (backward data dependency of L35)
    # - L4..L32 are unrelated metrics/logging lines
    # - 0-idx 32 (L33) is an enclosing control guard `if request_authenticated:`
    # - 0-idx 34 (L35) is the slicing criterion: `token_payload = sign_payload(data, signing_key)`
    # - L36..L59 are unrelated telemetry lines
    # - 0-idx 59 (L60) uses `token_payload`: `return verify_and_emit(token_payload)` (forward data dependency of L35)
    lines = [
        "def process_secure_token(raw_secret, data, request_authenticated):",
        "    metrics_counter = 0",
        "    signing_key = derive_key(raw_secret)",
    ]
    for i in range(4, 33):
        lines.append(f"    metrics_counter += {i}")
    lines.append("    if request_authenticated:")
    lines.append("        audit_step = 1")
    lines.append("        token_payload = sign_payload(data, signing_key)")
    for i in range(36, 60):
        lines.append(f"        audit_step += {i}")
    lines.append("        return verify_and_emit(token_payload)")
    lines.append("    return None")

    pdg = compute_multidim_program_slice(
        lines,
        line_start=1,
        focus_terms=["sign_payload"],
    )
    assert 34 in pdg["seed_lines"]
    assert 2 in pdg["backward_def_lines"], "Backward PDG slice must trace signing_key back to L3 (idx 2)"
    assert 59 in pdg["forward_use_lines"], "Forward PDG slice must trace token_payload forward to L60 (idx 59)"
    assert 32 in pdg["control_guard_lines"], "Upward control-dependence slice must capture enclosing if-guard at L33 (idx 32)"

    src_file = tmp_path / "auth_service.py"
    src_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sliced = slice_lines(
        tmp_path,
        "auth_service.py",
        line_start=1,
        line_end=len(lines),
        max_lines=22,
        focus_terms=["sign_payload"],
    )
    assert "signing_key = derive_key(raw_secret)" in sliced
    assert "if request_authenticated:" in sliced
    assert "token_payload = sign_payload(data, signing_key)" in sliced
    assert "return verify_and_emit(token_payload)" in sliced
    assert "expand: brain_slice" in sliced  # Unrelated telemetry lines were collapsed


def test_flask_methods_tuple_and_secret_key_state_slicing(tmp_path):
    (tmp_path / "src" / "flask_app").mkdir(parents=True)
    (tmp_path / "tests").mkdir(parents=True)

    app_py = tmp_path / "src" / "flask_app" / "app.py"
    app_py.write_text(
        "class ConfigAttribute:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n\n"
        "class App:\n"
        "    default_config = {\n"
        '        "DEBUG": False,\n'
        '        "SECRET_KEY": None,\n'
        "    }\n"
        '    secret_key = ConfigAttribute("SECRET_KEY")\n\n'
        '    @bp.route("/<int:id>/update", methods=("GET", "POST"))\n'
        "    def update(self, id):\n"
        "        return id\n",
        encoding="utf-8",
    )

    sessions_py = tmp_path / "src" / "flask_app" / "sessions.py"
    sessions_py.write_text(
        "def get_signing_serializer(app):\n"
        "    if not app.secret_key:\n"
        "        return None\n"
        "    return [app.secret_key]\n",
        encoding="utf-8",
    )

    test_py = tmp_path / "tests" / "test_basic.py"
    test_py.write_text(
        "def test_login(client):\n"
        '    client.post("/<int:id>/update", data={"title": "x"})\n'
        '    assert client.application.secret_key is None\n',
        encoding="utf-8",
    )

    b = ProjectBrain(tmp_path)
    b.index()

    # 1. Verify tuple methods=("GET", "POST") extracted both GET and POST routes
    app_info = b._index["src/flask_app/app.py"]
    routes = {(r["method"], r["path"]) for r in app_info.get("routes", [])}
    assert ("GET", "/<int:id>/update") in routes
    assert ("POST", "/<int:id>/update") in routes

    # 2. Verify SECRET_KEY query finds both ConfigAttribute("SECRET_KEY") and get_signing_serializer
    #    without pulling in tests/test_basic.py or emitting false-positive contract warnings
    dossier_res = b.compile_dossier("where is SECRET_KEY used")
    dossier_md = dossier_res["markdown"]
    assert 'ConfigAttribute("SECRET_KEY")' in dossier_md
    assert "get_signing_serializer" in dossier_md
    assert "tests/test_basic.py" not in dossier_md
    assert "CONTRACT WARNING" not in dossier_md


def test_skipped_minified_js_files_do_not_trigger_perpetual_rebuild_and_fast_cold_slice(tmp_path):
    """Regression test for I-02 (Django 60 vendored .js files bug) and I-03 (fast cold-start SQLite slice)."""
    (tmp_path / "django" / "db" / "models" / "sql").mkdir(parents=True)
    (tmp_path / "django" / "contrib" / "admin" / "static").mkdir(parents=True)

    query_py = tmp_path / "django" / "db" / "models" / "sql" / "query.py"
    query_py.write_text(
        "class Query:\n"
        "    def build_filter(self, filter_expr, branch_negated=False):\n"
        "        arg, value = filter_expr\n"
        "        return (arg, value, branch_negated)\n",
        encoding="utf-8",
    )

    # Create a vendored minified JS file (>500 chars on a single line) that _is_minified skips
    vendored_js = tmp_path / "django" / "contrib" / "admin" / "static" / "jquery.js"
    vendored_js.write_text("var a=1;" * 150 + "\n", encoding="utf-8")

    b = ProjectBrain(tmp_path)
    b.index()
    assert "django/contrib/admin/static/jquery.js" in b._skipped

    # I-02 check: is_stale() must be False, and incremental_index() must return 'up_to_date' with added=0
    assert b.is_stale() is False
    inc = b.incremental_index()
    assert inc["status"] == "up_to_date"
    assert inc["added"] == 0

    # I-03 check: A cold ProjectBrain instance must slice `Query.build_filter` directly from SQLite
    # without loading `_index` into memory!
    b_cold = ProjectBrain(tmp_path)
    assert not b_cold._index
    res = b_cold.slice_symbols("Query.build_filter")
    assert res["count"] == 1
    assert "def build_filter" in res["slices"][0]["code"]
    assert not b_cold._index, "Fast cold-start SQLite slice should not load the full repo _index into memory"


def test_errorhandlers_compound_routing_pdg_banner_and_docstring_folding(tmp_path):
    """Verify compound 'errorhandlers' query finds dispatch functions, folds long docstrings, and emits [PDG Slice: ...]."""
    pkg = tmp_path / "src" / "flask"
    pkg.mkdir(parents=True)

    (pkg / "app.py").write_text(
        "class Flask:\n"
        "    def register_error_handler(self, code_or_exception, f):\n"
        '        """Register a function to handle errors by code or exception.\n'
        "        Line 2 of docstring.\n"
        "        Line 3 of docstring.\n"
        "        Line 4 of docstring.\n"
        "        Line 5 of docstring.\n"
        '        """\n'
        "        exc_class, code = self._get_exc_class_and_code(code_or_exception)\n"
        "        self.error_handler_spec[None][code][exc_class] = f\n\n"
        "    def _find_error_handler(self, e, blueprints):\n"
        '        """Return a registered error handler for an exception in MRO order.\n'
        "        Extra doc 1.\n"
        "        Extra doc 2.\n"
        "        Extra doc 3.\n"
        '        """\n'
        "        exc_class, code = self._get_exc_class_and_code(type(e))\n"
        "        handler_map = self.error_handler_spec.get(None, {}).get(code, {})\n"
        "        for cls in exc_class.__mro__:\n"
        "            handler = handler_map.get(cls)\n"
        "            if handler is not None:\n"
        "                return handler\n"
        "        return None\n\n"
        "    def handle_http_exception(self, e):\n"
        '        """Handles an HTTP exception by routing to registered error handlers.\n'
        "        Doc detail A.\n"
        "        Doc detail B.\n"
        "        Doc detail C.\n"
        '        """\n'
        "        if e.code is None:\n"
        "            return e\n"
        "        handler = self._find_error_handler(e, [])\n"
        "        if handler is None:\n"
        "            return e\n"
        "        return self.ensure_sync(handler)(e)\n\n"
        "    def handle_user_exception(self, e):\n"
        '        """This method is called whenever an exception occurs that should be handled.\n'
        "        Doc detail 1.\n"
        "        Doc detail 2.\n"
        "        Doc detail 3.\n"
        '        """\n'
        "        if isinstance(e, Exception) and hasattr(e, 'code'):\n"
        "            return self.handle_http_exception(e)\n"
        "        handler = self._find_error_handler(e, [])\n"
        "        if handler is None:\n"
        "            raise\n"
        "        return self.ensure_sync(handler)(e)\n\n"
        "    def teardown_appcontext(self, f):\n"
        '        """Registers a function to be called when the application context ends.\n'
        "        This runs even if an unhandled exception occurred after errorhandlers.\n"
        "        Exceptions are passed in.\n"
        '        """\n'
        "        self.teardown_appcontext_funcs.append(f)\n"
        "        return f\n",
        encoding="utf-8",
    )

    b = ProjectBrain(tmp_path)
    b.index()
    res = b.compile_dossier(["how are exceptions routed to errorhandlers"])
    md = res["markdown"]

    assert "_find_error_handler" in md
    assert "handle_http_exception" in md
    assert "handle_user_exception" in md
    assert "### [src/flask/app.py :: teardown_appcontext" not in md
    assert "[PDG Slice:" in md
    assert "docstring lines" in md



