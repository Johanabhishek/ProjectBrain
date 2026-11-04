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
    rc = cli.main(["--path", str(tmp_project), "init"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "initialized"
    assert (tmp_project / ".gitignore").exists()
    assert ".projectbrain.json" in (tmp_project / ".gitignore").read_text(encoding="utf-8")
    # Antigravity + Claude root MCP & rules
    assert (tmp_project / ".mcp.json").exists()
    assert (tmp_project / "AGENTS.md").exists()
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
