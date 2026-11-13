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
    telem = dry_res["mutation_telemetry"]
    assert telem["syntax_verified_files"] == 1
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
        "\n".join([f"def helper_{i}(x):\n    val = x + {i}\n    return val * 2\n" for i in range(120)])
        + "\ndef target_auth_flow(token):\n    return helper_1(token)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    d_res = brain.compile_dossier(["trace target_auth_flow"])
    assert d_res["files_resolved"] == 1
    assert d_res["slices_included"] >= 1
    assert "naive_full_file_tokens" not in d_res
    assert "input_tokens_saved" not in d_res

    brain.slice_symbols(["service.py:helper_10"])
    brain.slice_symbols(["service.py:helper_20"])

    spec_lines = ["@file service.py", "@sub * `helper_1(token)` -> `helper_2(token)`"]
    p_res = brain.patch("\n".join(spec_lines) + "\n", apply=False)
    assert p_res["all_syntax_valid"] is True

    st = brain.stats()
    assert st["total_operations"] == 4
    assert st["resolution"]["ast_slices_compiled"] >= 3
    assert st["resolution"]["patch_ops_applied"] == 1
    assert st["resolution"]["files_in_working_memory"] == 1


def test_zero_waste_guard_unfolds_previously_folded_lines(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    cloud = src / "CloudStateContext.tsx"
    lines = ["export function scheduleCloudKvFlush() {"]
    for i in range(1, 220):
        lines.append(f"  const step_{i} = computeStep({i});")
    lines.append("  return step_1;")
    lines.append("}")
    cloud.write_text("\n".join(lines) + "\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. First slice with summary_mode=True and max_lines_per_symbol=60 folds the middle of L1-222
    res1 = brain.slice_symbols(["src/CloudStateContext.tsx:1-222"], max_lines_per_symbol=60, summary_mode=True)
    code1 = res1["slices"][0]["code"]
    assert "[folded " in code1

    # 2. Immediately slicing the folded inner range (L70-150) without summary_mode MUST unfold those lines
    res2 = brain.slice_symbols(["src/CloudStateContext.tsx:70-150"], summary_mode=False)
    code2 = res2["slices"][0]["code"]
    assert "already delivered in this session (file unchanged — 0 duplicate lines emitted)" not in code2
    assert "const step_100 = computeStep(100);" in code2
    assert "[folded " not in code2


def test_slicer_preserves_object_literal_payload_mappings(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    cloud = src / "CloudStateContext.tsx"
    filler_top = "\n".join([f"  // comment line {i}\n  console.debug('log {i}');" for i in range(35)])
    payload_block = "\n".join([
        "  const payload = {",
        "    questionnaire_completed: memoryKv.get('questionnaire_completed'),",
        "    sv_video_open_counts: memoryKv.get('sv_video_open_counts'),",
        "    app_user_id: memoryKv.get('app_user_id'),",
        "    last_synced_at: Date.now(),",
        "  };",
        "  await setDoc(userDocRef, payload);",
    ])
    filler_bot = "\n".join([f"  // trailing comment {i}\n  console.debug('tail {i}');" for i in range(35)])
    cloud.write_text(
        f"export async function scheduleCloudKvFlush(userDocRef: any, memoryKv: any) {{\n"
        f"{filler_top}\n{payload_block}\n{filler_bot}\n  return true;\n}}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    res = brain.slice_symbols(["src/CloudStateContext.tsx:scheduleCloudKvFlush"], max_lines_per_symbol=45)
    code = res["slices"][0]["code"]
    assert "questionnaire_completed: memoryKv.get('questionnaire_completed')" in code
    assert "await setDoc(userDocRef, payload);" in code


def test_closed_world_scan_skips_unrelated_keys_and_nested_projectbrain(tmp_path: Path):
    # Create an app subproject with package.json AND a nested tool checkout brian/projectbrain/src/projectbrain/session.py
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    (app_dir / "sessionStore.ts").write_text(
        "export function trackUserSession(uid: string) {\n"
        "  localStorage.setItem('questionnaire_completed', 'true');\n"
        "  localStorage.setItem('sv_video_open_counts', '1');\n"
        "  return uid;\n"
        "}\n",
        encoding="utf-8",
    )
    nested_pb = tmp_path / "brian" / "projectbrain" / "src" / "projectbrain"
    nested_pb.mkdir(parents=True)
    (nested_pb / "brain.py").write_text("class ProjectBrain:\n    pass\n", encoding="utf-8")
    (nested_pb / "session.py").write_text("def start_session(task):\n    return task\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    # Nested projectbrain checkout should be skipped automatically when an app project exists
    assert not any("brian/projectbrain" in k for k in brain._index)

    dossier = brain.compile_dossier(["how usersession is tracked and saved in database and session state"])
    md = dossier["markdown"]
    assert "questionnaire_completed" not in md.split("## Closed-World Exhaustive Repo Scan")[1] if "## Closed-World Exhaustive Repo Scan" in md else True


def test_big_repo_anti_inflation_and_truthful_scan(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    # Primary file with TypeScript interface + explicit target function
    primary = src / "authSession.ts"
    primary.write_text(
        "export interface AuthSessionPayload {\n"
        "  tenant_token: string;\n"
        "  expires_at: number;\n"
        "}\n\n"
        "export function syncTenantToken(token: string) {\n"
        "  localStorage.setItem('tenant_auth_token_key', token);\n"
        "  return token;\n"
        "}\n"
        + "\n".join([f"export function extra_{i}() {{ return {i}; }}" for i in range(40)]),
        encoding="utf-8",
    )
    primary_tokens = primary.stat().st_size // 4

    # 10 peripheral files that reference 'tenant_auth_token_key' (simulating a big repo)
    for idx in range(12):
        pf = src / f"consumer_{idx}.ts"
        pf.write_text(
            "\n".join([f"// padding line {k}" for k in range(80)])
            + f"\nexport function read_{idx}() {{\n"
            + "  return localStorage.getItem('tenant_auth_token_key');\n"
            + "}\n",
            encoding="utf-8",
        )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    res = brain.compile_dossier(["how syncTenantToken saves tenant_auth_token_key"])
    md = res["markdown"]

    # 1. TypeScript interface in globals must be preserved, not stripped
    assert "export interface AuthSessionPayload" in md
    assert "tenant_token: string;" in md

    # 2. Only the primary file (authSession.ts) is counted in files_resolved, NOT the 10 peripheral grep-hit files
    assert res["files_resolved"] == 1
    # 3. Because 10 files exceed the scan line cap, it must report PARTIAL SCAN NOTE (not EXHAUSTIVE GUARANTEE)
    assert "PARTIAL SCAN NOTE" in md
    assert "EXHAUSTIVE GUARANTEE" not in md


def test_4d_coordinate_matrix_and_session_dedup(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    monolith = src / "DashboardWorkspace.tsx"
    body_lines = [
        "import React, { useState, useEffect } from 'react';",
        "export function DashboardWorkspace({ courseId }: { courseId: string }) {",
        "  const [activeTool, setActiveTool] = useState('mindmap');",
        "  const [videoIndex, setVideoIndex] = useState(0);",
        "  const [completedTools, setCompletedTools] = useState<string[]>([]);",
        "",
        "  useEffect(() => {",
        "    setVideoIndex(0);",
        "    setActiveTool('mindmap');",
        "  }, [courseId]);",
        "",
        "  const handleVideoWatched = () => {",
        "    setCompletedTools(prev => [...prev, 'videos']);",
        "  };",
        "",
        "  switch (activeTool) {",
        "    case 'mindmap': {",
    ]
    # Add 220 lines inside case 'mindmap' so DashboardWorkspace is a >250 line monolith
    body_lines.extend([f"      const node_{k} = {k};" for k in range(220)])
    body_lines.extend([
        "      return <div>MindMap</div>;",
        "    }",
        "    case 'videos': {",
        "      return (",
        "        <section>",
        "          <VideosPlayer index={videoIndex} onWatched={handleVideoWatched} />",
        "        </section>",
        "      );",
        "    }",
        "    default:",
        "      return null;",
        "  }",
        "}",
    ])
    monolith.write_text("\n".join(body_lines) + "\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    assert brain.stats()["cells_6d"] >= 4
    assert brain.stats()["cells_4d"] >= 4
    q_4d = brain.query("where is videoIndex reset when courseId changes")
    assert q_4d and q_4d[0]["rel_path"] == "src/DashboardWorkspace.tsx"
    # 1. First dossier on VideosPlayer / videos branch inside the monolith
    d1 = brain.compile_dossier(["where is VideosPlayer rendered in DashboardWorkspace"])
    md1 = d1["markdown"]
    assert "[6D Coordinate Matrix]" in md1
    assert "VideosPlayer" in md1

    # 2. Second dossier in the same session on a completely unfolded small function
    small = src / "smallHelper.ts"
    small.write_text(
        "export function computeStreakDays(days: number) {\n"
        "  return days + 1;\n"
        "}\n",
        encoding="utf-8",
    )
    brain.index()
    d_small_1 = brain.compile_dossier(["how computeStreakDays works"])
    assert "return days + 1;" in d_small_1["markdown"]
    d_small_2 = brain.compile_dossier(["how computeStreakDays works"])
    assert "[Session Cache] Already delivered in this session" in d_small_2["markdown"]


def test_v050_treesitter_4d_sqlite_and_1call_zoom(tmp_path: Path, capsys):
    from projectbrain import cli
    from projectbrain.slicer import extract_4d_coordinate_matrix

    src = tmp_path / "src"
    src.mkdir()
    store_comp = src / "StoreEditor.tsx"
    lines = [
        "import React from 'react';",
        "export function StoreEditor() {",
        "  const { profile, updateProfile, dispatch } = useAuthStore();",
        "  const items = useCartStore((s) => s.items);",
        "",
        "  const handleSaveProfile = async () => {",
        "    updateProfile({ name: 'Ada' });",
        "    await prisma.account.update({ where: { id: 1 }, data: {} });",
        "    dispatch({ type: 'SAVED' });",
        "  };",
        *[f"  const pad_{k} = {k};" for k in range(205)],
        "",
        "  return (",
        "    <div>",
        "      {isEditing ? (",
        "        <section>",
        "          <ProfileForm value={profile} onSave={handleSaveProfile} />",
        "          <span>Editing mode</span>",
        "        </section>",
        "      ) : null}",
        "      {items.map((it) => (",
        "        <article key={it.id}>",
        "          <CartItemCard item={it} />",
        "        </article>",
        "      ))}",
        "    </div>",
        "  );",
        "}",
    ]
    store_comp.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 1. Verify Tree-sitter 4D extractor catches Zustand/Redux hooks, Prisma ORM writes, ternary JSX, and .map() JSX
    cells = extract_4d_coordinate_matrix(lines, ext=".tsx")
    scopes = {c["X_scope"]: c for c in cells}
    assert "fn:handleSaveProfile" in scopes
    save_cell = scopes["fn:handleSaveProfile"]
    assert save_cell["Y_role"] == "IO"
    assert "profile" in save_cell["Z_write"]
    assert "account" in save_cell["Z_write"]
    assert "jsx[isEditing]" in scopes
    assert "jsx[items.map]" in scopes

    # 2. Verify SQLite-only (.projectbrain.db) storage backend & incremental sync (no .projectbrain.json dual-write)
    brain = ProjectBrain(str(tmp_path))
    st = brain.index()
    assert st["storage_backend"] == "sqlite"
    assert (tmp_path / ".projectbrain.db").exists()
    assert not (tmp_path / ".projectbrain.json").exists()
    assert not (tmp_path / ".projectbrain_graph.json").exists()
    db_data = brain._db_store.load_all()
    assert db_data is not None
    assert "src/StoreEditor.tsx" in db_data["files"]
    assert "graph_json" in db_data
    fresh_brain = ProjectBrain(str(tmp_path))
    assert fresh_brain.load() is True
    assert "src/StoreEditor.tsx" in fresh_brain._index

    # 3. Verify 1-call CLI dossier --zoom includes markdown in JSON and --stdout prints raw markdown
    capsys.readouterr()
    rc_z = cli.main(["--path", str(tmp_path), "dossier", "--zoom", "how handleSaveProfile updates profile"])
    assert rc_z == 0
    import json
    out_z = json.loads(capsys.readouterr().out)
    assert "markdown" in out_z
    assert "handleSaveProfile" in out_z["markdown"]

    rc_s = cli.main(["--path", str(tmp_path), "dossier", "--stdout", "how handleSaveProfile updates profile"])
    assert rc_s == 0
    out_s = capsys.readouterr().out
    assert out_s.startswith("# ProjectBrain")


def test_interprocedural_effect_bubbling_and_state_channels(tmp_path: Path):
    """Verify Pass A (3-hop transitive ?Z/IO/throw bubbling across Python & TS) and Pass B (cross-file state_flow edges)."""
    src = tmp_path / "src"
    src.mkdir()

    # 1. Leaf DB/Gateway writer in Python (2 hops deep): writes to `invoices_ledger` SQL table and raises `PaymentDeclinedError`
    (src / "gateway.py").write_text(
        "class PaymentDeclinedError(Exception):\n"
        "    pass\n\n"
        "async def commit_invoice_row(db, amount: int):\n"
        "    if amount <= 0:\n"
        "        raise PaymentDeclinedError('invalid amount')\n"
        "    await db.execute('INSERT INTO invoices_ledger (amount) VALUES (100)')\n"
        "    session['billing_audit_token'] = 'ok'\n"
        "    return True\n",
        encoding="utf-8",
    )

    # 2. Mid-tier service wrapper (1 hop deep): calls commit_invoice_row, has no direct SQL/session strings of its own
    (src / "billing_service.py").write_text(
        "from gateway import commit_invoice_row\n\n"
        "async def process_subscription_charge(db, amount: int):\n"
        "    return await commit_invoice_row(db, amount)\n",
        encoding="utf-8",
    )

    # 3. Top-level entrypoint controller (0 hops): calls process_subscription_charge
    (src / "checkout_controller.py").write_text(
        "from billing_service import process_subscription_charge\n\n"
        "async def handle_checkout_endpoint(db, payload: dict):\n"
        "    amt = payload.get('amount', 0)\n"
        "    return await process_subscription_charge(db, amt)\n",
        encoding="utf-8",
    )

    # 4. Completely decoupled consumer (never imports or calls gateway/service/controller, but reads `billing_audit_token`)
    (src / "audit_worker.py").write_text(
        "def verify_audit_trail(session: dict):\n"
        "    tok = session.get('billing_audit_token')\n"
        "    return bool(tok)\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()
    assert brain._graph is not None

    # Verify Leaf 6D IR axes (Z_write, E_exit, T_mode, Y_role=IO)
    leaf_nd = brain._graph.nodes["fn:src/gateway.py:commit_invoice_row"]
    assert "invoices_ledger" in leaf_nd.get("local_z_write", [])
    assert "billing_audit_token" in leaf_nd.get("local_z_write", [])
    assert "throw:PaymentDeclinedError" in leaf_nd.get("exit_contracts", [])
    assert "guard_return" not in leaf_nd.get("exit_contracts", [])
    assert leaf_nd.get("temporal_mode") == "ASYNC"
    assert "IO" in leaf_nd.get("local_effects", [])

    # Verify Pass A: 2-Hop Interprocedural Effect & ?Z Bubbling up to `handle_checkout_endpoint`
    ctrl_nd = brain._graph.nodes["fn:src/checkout_controller.py:handle_checkout_endpoint"]
    assert "invoices_ledger" in ctrl_nd.get("transitive_z_write", [])
    assert "billing_audit_token" in ctrl_nd.get("transitive_z_write", [])
    assert "IO" in ctrl_nd.get("transitive_effects", [])
    assert "throw:PaymentDeclinedError" in ctrl_nd.get("transitive_exits", [])
    prov = ctrl_nd.get("effect_provenance", {})
    assert "process_subscription_charge -> commit_invoice_row (2 hops)" in prov.get("?Z:invoices_ledger", "")

    # Verify Pass B: Cross-File State-Channel Edge (`state_flow`) between decoupled `commit_invoice_row` and `verify_audit_trail`
    sf_edges = [
        e for e in brain._graph.edges
        if e["type"] == "state_flow"
        and e["src"] == "fn:src/gateway.py:commit_invoice_row"
        and e["dst"] == "fn:src/audit_worker.py:verify_audit_trail"
    ]
    assert len(sf_edges) == 1
    assert "billing_audit_token" in sf_edges[0]["reason"]

    # Verify Dossier surfaces both Interprocedural Bubbled Effects and State-Channel Flow
    d_res = brain.compile_dossier(["how handle_checkout_endpoint processes charges and commit_invoice_row updates state"])
    md = d_res["markdown"]
    assert "interprocedural_bubble" in md
    assert "invoices_ledger" in md
    assert "state_flow" in md


def test_6d_subblocks_sql_schema_and_contract_diagnostics(tmp_path: Path, monkeypatch, capsys):
    """Verify 6D sub-block extraction inside <200-line functions, .sql indexing, and Pass C 6D Contract Diagnostics."""
    import io
    from projectbrain import cli

    src = tmp_path / "backend"
    src.mkdir()

    # 1. SQL migration file (.sql)
    (src / "001_init.sql").write_text(
        "CREATE TABLE audit_logs (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  action TEXT NOT NULL\n"
        ");\n\n"
        "CREATE OR REPLACE FUNCTION record_audit(p_id TEXT) RETURNS VOID AS $$\n"
        "BEGIN\n"
        "  INSERT INTO audit_logs (id, action) VALUES (p_id, 'init');\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n",
        encoding="utf-8",
    )

    # 2. Local SQLite adapter with inline CREATE TABLE (where `created_at` is NOT NULL without DEFAULT)
    #    and an incomplete SQL translator that does NOT handle `interval '...'` or `ANY(%s)`.
    (src / "local_db.py").write_text(
        "import sqlite3\n\n"
        "def init_schema(conn):\n"
        "    conn.executescript('''\n"
        "    CREATE TABLE IF NOT EXISTS source_files (\n"
        "        id TEXT PRIMARY KEY,\n"
        "        batch_id TEXT NOT NULL,\n"
        "        created_at TEXT NOT NULL\n"
        "    );\n"
        "    ''')\n\n"
        "class SqliteAdapter:\n"
        "    def _translate_sql(self, sql: str) -> str:\n"
        "        return sql.replace('%s', '?')\n"
        "    def execute(self, sql: str, params=()):\n"
        "        conn = sqlite3.connect(':memory:')\n"
        "        return conn.execute(self._translate_sql(sql), params)\n",
        encoding="utf-8",
    )

    # 3. Modular (<60-line) router function with:
    #    - `with db.tx() as conn:` sub-block
    #    - `try / except UniqueViolation:` sub-block
    #    - `INSERT INTO source_files (id, batch_id)` omitting required `created_at`
    #    - Postgres `interval '5 minutes'` dialect mismatch
    (src / "uploads.py").write_text(
        "class UniqueViolation(Exception):\n"
        "    pass\n\n"
        "async def upload_file_handler(db, file_id: str, batch_id: str):\n"
        "    if not file_id:\n"
        "        return None\n"
        "    try:\n"
        "        with db.tx() as conn:\n"
        "            conn.execute(\n"
        "                '''INSERT INTO source_files (id, batch_id)\n"
        "                   VALUES (%s, %s)''',\n"
        "                (file_id, batch_id),\n"
        "            )\n"
        "            conn.execute(\n"
        "                '''SELECT id FROM source_files\n"
        "                   WHERE created_at > now() + interval '5 minutes' '''\n"
        "            )\n"
        "    except UniqueViolation:\n"
        "        raise RuntimeError('duplicate upload')\n"
        "    return {'id': file_id}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    st = brain.index()
    assert "backend/001_init.sql" in brain._index
    assert any(c["name"] == "audit_logs" for c in brain._index["backend/001_init.sql"]["classes"])
    assert any(f["name"] == "record_audit" for f in brain._index["backend/001_init.sql"]["functions"])

    # Verify sub-blocks inside the 22-line `upload_file_handler` were extracted into 6D cells
    up_cells = brain._index["backend/uploads.py"]["cells_4d"]
    up_scopes = [c["X_scope"] for c in up_cells]
    assert "fn:upload_file_handler" in up_scopes
    assert any(s.startswith("with[") for s in up_scopes)
    assert any(s.startswith("sql[INSERT source_files]") for s in up_scopes)

    # Verify dossier on open-ended DB bug prompt surfaces 6D Coordinate Matrix + all 3 Contract Warnings
    d_res = brain.compile_dossier(["there are some backend bugs . cant connect to db"])
    md = d_res["markdown"]
    assert "[6D Coordinate Matrix]" in md
    assert "Static 6D Contract, Schema & Dialect Diagnostics" in md
    assert "[6D Schema Contract Violation]" in md
    assert "created_at" in md
    assert "[6D SQL Dialect Mismatch]" in md
    assert "interval_literal" in md
    assert "[6D Exception Contract Mismatch]" in md
    assert "UniqueViolation" in md
    assert brain.stats()["resolution"]["cells_4d_resolved"] >= 1

    # Verify CLI `patch --spec -` reads from stdin
    patch_dsl = "@file backend/uploads.py\n@sub L19 `'duplicate upload'` -> `'conflict 409'`\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(patch_dsl))
    capsys.readouterr()
    rc = cli.main(["--path", str(tmp_path), "patch", "--spec", "-", "--apply"])
    assert rc == 0
    assert "conflict 409" in (src / "uploads.py").read_text(encoding="utf-8")


def test_multiplex_ppr_submodular_and_type_route_algebra(tmp_path):
    """Verify Multiplex PageRank across HTTP->Backend->SQL->Worker, Submodular selection, and Type/Method Algebra."""
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS background_jobs (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    status TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend_api.ts").write_text(
        "export async function uploadBatchFile(batchId: string, file: File) {\n"
        "    const form = new FormData();\n"
        "    form.append('file', file);\n"
        "    const res = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'POST', body: form });\n"
        "    const bad = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'DELETE' });\n"
        "    if (!res.ok) {\n"
        "        throw new Error('Upload failed');\n"
        "    }\n"
        "    return res.json();\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "routes.py").write_text(
        "import sqlite3\n"
        "from uuid import UUID\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/v1/batches')\n\n"
        "@router.post('/{batch_id}/files')\n"
        "def upload_file_endpoint(batch_id: str, user_id: UUID):\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    row = {'uploaded_by': 'some-text-uuid'}\n"
        "    if row['uploaded_by'] != user_id:\n"
        "        raise PermissionError('Forbidden')\n"
        "    with conn:\n"
        "        conn.execute(\"INSERT INTO background_jobs (id, status) VALUES ('1', 'queued')\")\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "worker.py").write_text(
        "import sqlite3\n\n"
        "def process_next_job():\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    with conn:\n"
        "        rows = conn.execute(\"SELECT id, status FROM background_jobs WHERE status = 'queued'\").fetchall()\n"
        "        for r in rows:\n"
        "            print(r)\n"
        "    return len(rows)\n",
        encoding="utf-8",
    )

    from projectbrain.brain import ProjectBrain

    brain = ProjectBrain(root=str(tmp_path))
    brain.index()

    # 1. Verify cross-stack http_flow edge was built from frontend_api.ts -> routes.py
    http_edges = [e for e in brain._graph.edges if e["type"] == "http_flow"]
    assert len(http_edges) >= 1, f"Expected http_flow bridge edge, got: {brain._graph.edges}"
    assert any("uploadBatchFile" in e["src"] and "upload_file_endpoint" in e["dst"] for e in http_edges)

    # 2. Verify Multiplex PageRank diffuses from frontend uploadBatchFile -> routes -> worker
    ppr_results = brain._graph.multiplex_ppr(["frontend_api.ts::uploadBatchFile"])
    ppr_ids = [nid for nid, _, _ in ppr_results]
    assert any("upload_file_endpoint" in k for k in ppr_ids), f"Backend route missing from PPR: {ppr_results}"
    assert any("process_next_job" in k for k in ppr_ids), f"Worker missing from 2-hop Multiplex PPR: {ppr_results}"

    # 3. Verify Pass C catches both Type Disjointness (UUID vs str row) and HTTP Route Method Mismatch (DELETE vs POST)
    res = brain.compile_dossier(["uploadBatchFile fails with Forbidden or method error"])
    dossier = res["markdown"]
    assert "[6D Type Disjointness Violation]" in dossier
    assert "user_id" in dossier and "uploaded_by" in dossier
    assert "[6D HTTP Route Method Mismatch]" in dossier
    assert "DELETE" in dossier


def test_causal_symptom_chop_shape_algebra_and_predisk_patch_contracts(tmp_path):
    """Verify Causal Symptom Chop (403 -> nested callback -> load_batch), Producer-Consumer SQL/HTTP Shape Algebra, and Pre-Disk Patch Verification."""
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS background_jobs (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    status TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    (tmp_path / "deps.py").write_text(
        "from uuid import UUID\n\n"
        "class ApiError(Exception):\n"
        "    pass\n\n"
        "def load_batch(conn, batch_id: UUID, user: UUID):\n"
        "    b = conn.execute(\"SELECT id, created_by FROM ingestion_batches WHERE id = %s\", (batch_id,)).fetchone()\n"
        "    if not b:\n"
        "        raise ApiError(404, 'batch_not_found', 'Batch not found')\n"
        "    if str(b['created_by']) != str(user):\n"
        "        raise ApiError(403, 'forbidden', 'Only the batch creator can do this')\n"
        "    bad_val = b['unselected_column']\n"
        "    return b\n",
        encoding="utf-8",
    )
    (tmp_path / "routes.py").write_text(
        "import sqlite3\n"
        "from uuid import UUID\n"
        "from fastapi import APIRouter\n"
        "from deps import load_batch\n\n"
        "router = APIRouter(prefix='/api/v1/batches')\n\n"
        "@router.post('/{batch_id}/files')\n"
        "def upload_file_endpoint(batch_id: UUID, user_id: UUID):\n"
        "    conn = sqlite3.connect(':memory:')\n"
        "    def on_file_headers():\n"
        "        return load_batch(conn, batch_id, user_id)\n"
        "    on_file_headers()\n"
        "    with conn:\n"
        "        conn.execute(\"INSERT INTO background_jobs (id, status) VALUES ('1', 'queued')\")\n"
        "    return {'ok': True, 'file_id': '123'}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend_api.ts").write_text(
        "export async function uploadBatchFile(batchId: string, file: File) {\n"
        "    const res = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'POST' });\n"
        "    const data = await res.json();\n"
        "    return data.non_existent_prop;\n"
        "}\n",
        encoding="utf-8",
    )

    from projectbrain.brain import ProjectBrain

    brain = ProjectBrain(root=str(tmp_path))
    brain.index()

    # 1. Verify Causal Symptom Chop finds 4-hop cross-stack path from uploadBatchFile -> upload_file_endpoint -> on_file_headers -> load_batch (status:403)
    chops = brain._graph.causal_symptom_chop(["frontend_api.ts::uploadBatchFile"], "uploadBatchFile fails with 403")
    assert len(chops) >= 1, f"Expected causal symptom chop hit, got: {chops}"
    origin_nid, sym_str, path_list = chops[0]
    assert "load_batch" in origin_nid
    assert "status:403" in sym_str

    # 2. Verify Dossier surfaces Causal Symptom Chop, SQL SELECT Projection Shape Violation, and Cross-Stack HTTP Shape Violation
    res = brain.compile_dossier(["uploadBatchFile fails with 403 database error"])
    md = res["markdown"]
    assert "[Causal Symptom Chop]" in md
    assert "load_batch" in md
    assert "[6D Producer-Consumer SQL Shape Violation]" in md
    assert "unselected_column" in md
    assert "[6D Cross-Stack HTTP Shape Violation]" in md
    assert "non_existent_prop" in md

    # 3. Verify Pre-Disk Differential 6D Contract Verification in `brain.patch(..., apply=False)`
    bad_patch_spec = (
        "@file routes.py\n"
        "@sub L15 `INSERT INTO background_jobs (id, status) VALUES ('1', 'queued')` -> `INSERT INTO background_jobs (id) VALUES ('1')`\n"
    )
    patch_res = brain.patch(bad_patch_spec, apply=False)
    assert patch_res["all_syntax_valid"] is True
    assert patch_res["all_contracts_valid"] is False
    assert any("[6D Schema Contract Violation]" in d and "status" in d for d in patch_res["contract_diagnostics"])


def test_debiased_ast_dataflow_alter_table_nested_return_and_general_chop(tmp_path: Path):
    """Verify general, zero-overfit CS laws:
      1. Relational schema evolution via chronological SQL `ALTER TABLE ... DROP NOT NULL` folding.
      2. Python AST intra-procedural dataflow (`_analyze_python_ast_contracts`) across multi-line variable aliases
         (`owner = row['uploaded_by']; if owner != user_id:`) and general disjoint nominal types (`int` vs `str`).
      3. Nested dictionary return shapes (`return {'ok': True, 'meta': {'nested': 1}, 'item_id': '123'}`) +
         `${API_BASE}/api/...` frontend URL template normalization.
      4. Universal lexical-stem Causal Symptom Chop on arbitrary custom exceptions (`QuotaLimitExceededError`).
    """
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "001_init.sql").write_text(
        "CREATE TABLE validation_documents (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    doc_name TEXT NOT NULL,\n"
        "    validator_id TEXT NOT NULL,\n"
        "    approver_id TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    (mig_dir / "002_relax_validators.sql").write_text(
        "ALTER TABLE validation_documents ALTER COLUMN validator_id DROP NOT NULL;\n"
        "ALTER TABLE validation_documents ALTER COLUMN approver_id DROP NOT NULL;\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "from uuid import UUID\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "class QuotaLimitExceededError(Exception):\n"
        "    pass\n\n"
        "def verify_quota_and_owner(\n"
        "    conn,\n"
        "    doc_id: str,\n"
        "    *,  # multi-line keyword-only signature beyond 8 lines\n"
        "    flag_a: int = 1,\n"
        "    flag_b: int = 2,\n"
        "    flag_c: int = 3,\n"
        "    flag_d: int = 4,\n"
        "    flag_e: int = 5,\n"
        "    user_id: UUID,\n"
        "    expected_tag: str = 'v1',\n"
        "):\n"
        "    row = conn.execute('SELECT id, uploaded_by FROM validation_documents').fetchone()\n"
        "    owner = row['uploaded_by']\n"
        "    if owner != user_id:\n"
        "        raise QuotaLimitExceededError('quota or owner mismatch')\n"
        "    if flag_a == expected_tag:\n"
        "        return False\n"
        "    return True\n\n"
        "@router.post('/api/v1/docs/{doc_id}/validate')\n"
        "def validate_doc_endpoint(conn, doc_id: str, user_id: UUID):\n"
        "    verify_quota_and_owner(conn, doc_id, user_id=user_id)\n"
        "    conn.execute(\"INSERT INTO validation_documents (id, doc_name) VALUES ('1', 'spec.pdf')\")\n"
        "    return {'ok': True, 'meta': {'nested_depth': 2}, 'doc_id': doc_id}\n",
        encoding="utf-8",
    )
    (tmp_path / "client.ts").write_text(
        "const API_BASE = 'https://api.example.com';\n"
        "export async function triggerDocValidation(docId: string) {\n"
        "    const res = await fetch(`${API_BASE}/api/v1/docs/${docId}/validate`, { method: 'POST' });\n"
        "    const payload = await res.json();\n"
        "    return payload.missing_client_field;\n"
        "}\n",
        encoding="utf-8",
    )

    from projectbrain.brain import ProjectBrain

    brain = ProjectBrain(root=str(tmp_path))
    brain.index()

    # 1. Verify universal stem-based Causal Symptom Chop traces from client.ts -> validate_doc_endpoint -> verify_quota_and_owner (throw:QuotaLimitExceededError)
    chops = brain._graph.causal_symptom_chop(
        ["client.ts::triggerDocValidation"],
        "triggerDocValidation fails when quota limit is exceeded",
    )
    assert len(chops) >= 1, f"Expected custom exception chop hit, got: {chops}"
    origin_nid, sym_str, _ = chops[0]
    assert "verify_quota_and_owner" in origin_nid
    assert "throw:QuotaLimitExceededError" in sym_str

    # 2. Compile dossier and check diagnostics
    res = brain.compile_dossier(["triggerDocValidation fails when quota limit is exceeded or database error"])
    md = res["markdown"]

    # 2a. ALTER TABLE DROP NOT NULL must prevent false-positive NOT NULL warning on validator_id / approver_id!
    assert "validator_id" not in md, f"False positive NOT NULL violation on dropped NOT NULL column: {md}"
    assert "approver_id" not in md, f"False positive NOT NULL violation on dropped NOT NULL column: {md}"

    # 2b. AST dataflow must catch BOTH multi-line row alias (`owner != user_id`) AND general disjoint types (`flag_a == expected_tag`, int vs str)
    assert "[6D Type Disjointness Violation]" in md
    assert "uploaded_by" in md
    assert "\u03c4(int) \u2229 \u03c4(str) = \u2205" in md

    # 2c. Nested dict return shape + `${API_BASE}` fetch URL must still catch `.missing_client_field`
    assert "[6D Cross-Stack HTTP Shape Violation]" in md
    assert "missing_client_field" in md

    # 2d. Verify Transformer-Native Attention compilation:
    #     - Causal-Mask Topological Pre-Conditioning: Diagnostics header precedes code slices
    #     - Delta-p=1 inline annotations (`# >>> [6D-CONTRACT ...]` / `# >>> [HTTP-BRIDGE ...]` / `# >>> [CAUSAL-CHOP ...]`)
    assert md.index("### Static 6D Contract, Schema & Dialect Diagnostics") < md.index("### [client.ts :: triggerDocValidation")
    assert "# >>> [HTTP-BRIDGE " in md
    assert "# >>> [CAUSAL-CHOP " in md
    assert "# >>> [6D-CONTRACT " in md


def test_universal_js_ts_ast_contracts_and_css_folding(tmp_path: Path):
    """Verify:
      1. Inline CSS/SVG styling properties are folded away in `slice_lines` while executable code is retained.
      2. Bijective `const_aliases` lookup tables propagate query aliases automatically.
      3. Multi-call-site `call_sites_all` and JSX `<Child />` call edges are indexed.
      4. Universal JS/TS/React 6D AST contract laws (`premature_effect_event_handler`, `stale_hook_closure`,
         `param_outer_state_conflict`) are detected and annotated inline; omitting a defaulted parameter is not flagged.
    """
    src = tmp_path / "src"
    src.mkdir()

    (src / "storage.ts").write_text(
        "export function persistStepProgress(stepId: string, scopeKey = 'default'): void {\n"
        "  localStorage.setItem(`progress_${scopeKey}`, stepId);\n"
        "}\n",
        encoding="utf-8",
    )

    # Synthetic React component exhibiting all 4 universal AST/closure laws + heavy inline CSS noise
    css_noise = "\n".join(
        f"        paddingTop: '{k}px',\n        borderRadius: '{k}px',\n        fontSize: '{k}px',"
        for k in range(1, 35)
    )
    (src / "CarouselWidget.tsx").write_text(
        "import React, { useState, useEffect } from 'react';\n"
        "import { persistStepProgress } from './storage';\n\n"
        "const MODE_ALIAS_MAP = {\n"
        "  slides: 'carousel',\n"
        "  digest: 'summary',\n"
        "};\n\n"
        "export function CarouselWidget({ items, onItemFinished }: { items: string[]; onItemFinished?: (idx: number) => void }) {\n"
        "  const [activeIndex, setActiveIndex] = useState(0);\n"
        "  const [isExpanded, setIsExpanded] = useState(false);\n\n"
        "  useEffect(() => {\n"
        "    if (onItemFinished) {\n"
        "      onItemFinished(activeIndex);\n"
        "    }\n"
        "  }, [activeIndex, onItemFinished]);\n\n"
        "  useEffect(() => {\n"
        "    window.addEventListener('keydown', handleShortcutKey);\n"
        "    return () => window.removeEventListener('keydown', handleShortcutKey);\n"
        "  }, []);\n\n"
        "  const advanceSlide = () => {\n"
        "    if (activeIndex < items.length - 1) {\n"
        "      setActiveIndex(activeIndex + 1);\n"
        "    }\n"
        "  };\n\n"
        "  const handleItemDone = (targetIdx: number) => {\n"
        "    if (targetIdx === activeIndex) {\n"
        "      persistStepProgress('carousel', 'unit_1');\n"
        "    }\n"
        "    persistStepProgress('carousel');\n"
        "    advanceSlide();\n"
        "  };\n\n"
        "  const handleShortcutKey = (ev: KeyboardEvent) => {\n"
        "    if (ev.key === 'ArrowRight' && isExpanded) {\n"
        "      handleItemDone(activeIndex);\n"
        "    }\n"
        "  };\n\n"
        "  return (\n"
        "    <div style={{\n"
        f"{css_noise}\n"
        "    }}>\n"
        "      <button onClick={() => onItemFinished && onItemFinished(activeIndex)}>Finish</button>\n"
        "      <button onClick={() => handleItemDone(0)}>Complete First</button>\n"
        "    </div>\n"
        "  );\n"
        "}\n",
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Verify const_aliases extracted `slides <-> carousel`
    assert "slides" in brain._const_aliases.get("carousel", [])
    assert "carousel" in brain._const_aliases.get("slides", [])

    # Verify call_sites_all captured both call sites of `persistStepProgress`
    fn_entries = brain._index["src/CarouselWidget.tsx"]["functions"]
    item_done_fn = next(f for f in fn_entries if f["name"] == "handleItemDone")
    assert len(item_done_fn["call_sites_all"].get("persistStepProgress", [])) == 2

    # Compile dossier and verify all 4 universal JS/TS/React 6D AST contracts are surfaced & inline-annotated
    res = brain.compile_dossier(["search for a bug in slides widget"])
    md = res["markdown"]
    assert "premature_effect_event_handler" in md
    assert "stale_hook_closure" in md
    assert "param_outer_state_conflict" in md
    # `persistStepProgress('carousel')` omits a DEFAULTED parameter -> legal, must not be reported.
    assert "missing_required_argument" not in md
    assert "inconsistent_call_arity" not in md
    assert "# >>> [6D-CONTRACT " in md
    # Verify executable handlers were kept while inline CSS paddingTop/borderRadius lines were folded
    assert "handleItemDone" in md
    assert "advanceSlide" in md


def test_required_arity_and_listener_pairing_laws_are_sound():
    """Law 4 fires only on calls missing REQUIRED params of a visible declaration; Law 5 fires only
    when add/removeEventListener use different function identities (or the pair is never removed)."""
    from projectbrain.slicer import _analyze_js_ts_ast_contracts

    src = [
        "import { useEffect } from 'react';",
        "function track(eventName, payload) {",
        "  send(eventName, payload);",
        "}",
        "function tag(label: string, scope?: string) {",
        "  return label + (scope || '');",
        "}",
        "function store(key: string, value = 'x') {",
        "  localStorage.setItem(key, value);",
        "}",
        "function pick({ a, b }: Props, mode: Map<string, number>) {",
        "  return a;",
        "}",
        "export function Player() {",
        "  useEffect(() => {",
        "    const el = ref.current;",
        "    el.addEventListener('canplay', onReady);",
        "    el.addEventListener('ended', onEnded);",
        "    el.addEventListener('pause', onPause);",
        "    return () => {",
        "      el.removeEventListener('canplay', onDetect);",
        "      el.removeEventListener('ended', onEnded);",
        "    };",
        "  }, [src]);",
        "  track('open');",
        "  track('open', { id: 1 });",
        "  tag('a');",
        "  store('k');",
        "  pick(props);",
        "  return null;",
        "}",
    ]
    v = _analyze_js_ts_ast_contracts(src, ".tsx")
    arity = [x for x in v if x["kind"] == "missing_required_argument"]
    leaks = [x for x in v if x["kind"] == "listener_cleanup_mismatch"]

    # Only `track('open')` omits a required parameter; optional `?`, defaulted and
    # destructured-first signatures are judged by their real required count.
    assert [(x["symbol"], x["line"]) for x in arity] == [("track", 25), ("pick", 29)]
    # canplay: removed with a different function; pause: never removed while cleanup exists.
    # ended: correctly paired -> not reported.
    assert sorted((x["symbol"], x["line"]) for x in leaks) == [("onPause", 19), ("onReady", 17)]


def test_query_ranking_resists_hub_centrality_and_honours_aliases(tmp_path: Path):
    """A widely-imported hub whose only contact with a query word is as a SUBSTRING of a different
    token (`audit` inside `auditors`) must not outrank the file whose own name is the identifier the
    codebase itself declares equivalent to that word (`ledger: 'audit'`)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "labels.ts").write_text(
        "export const TAB_LABELS = {\n  ledger: 'audit',\n  invoices: 'billing',\n};\n",
        encoding="utf-8",
    )
    (src / "Registry.ts").write_text(
        "export const auditors = [];\n"
        "export function register(fn) { auditors.push(fn); }\n"
        "export function getTab(tab) { return tab; }\n",
        encoding="utf-8",
    )
    for k in range(12):
        (src / f"Screen{k}.ts").write_text(
            f"import {{ register }} from './Registry';\nexport function screen{k}() {{ return register({k}); }}\n",
            encoding="utf-8",
        )
    (src / "LedgerPanel.tsx").write_text(
        "export function LedgerPanel({ ledger }) {\n"
        "  const total = ledger.reduce((a, b) => a + b.amount, 0);\n"
        "  return total;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    ranked = [r["rel_path"] for r in brain.query("audit tab bugs", top_n=6)]
    assert ranked.index("src/LedgerPanel.tsx") < ranked.index("src/Registry.ts")

def test_contract_diagnostics_are_gated_by_query_relevance(tmp_path: Path):
    """A finding in an unrelated region of a large host file must not be reported for a query about a
    different feature; it IS reported when the query names that region."""
    src = tmp_path / "src"
    src.mkdir()
    filler = "\n".join(f"  const pad{k} = {k};" for k in range(40))
    (src / "Host.tsx").write_text(
        "import { useEffect, useState } from 'react';\n"
        "export function Host() {\n"
        "  const [mode, setMode] = useState('a');\n"
        f"{filler}\n"
        "  useEffect(() => {\n"
        "    if (mode === 'gallery') {\n"
        "      const img = document.getElementById('g');\n"
        "      img.addEventListener('load', onGalleryLoad);\n"
        "      return () => img.removeEventListener('load', onGalleryDone);\n"
        "    }\n"
        "  }, [mode]);\n"
        "  const renderCheckout = () => setMode('checkout');\n"
        "  return renderCheckout;\n"
        "}\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    unrelated = brain.compile_dossier(["checkout button bugs"])["markdown"]
    related = brain.compile_dossier(["gallery image load bugs"])["markdown"]
    assert "listener_cleanup_mismatch" not in unrelated
    assert "listener_cleanup_mismatch" in related


def test_security_path_traversal_rejected(tmp_path: Path):
    """C-01: Verify apply_patch_spec strictly blocks ../ and absolute path escapes outside repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")

    spec = "@file ../outside.py\n@sub L1 `secret = 1` -> `secret = 2`\n"
    res = apply_patch_spec(repo, spec, apply=True)
    assert res["all_syntax_valid"] is False
    assert any("Security sandbox violation" in e for e in res["files"][0]["errors"])
    assert outside.read_text(encoding="utf-8") == "secret = 1\n"


def test_ast_symbol_anchored_patching_and_lazy_elision_guard(tmp_path: Path):
    """H-01 & H-02: Verify @replace_body, @add_method, two-phase @sub + @replace invariance, and lazy elision guard."""
    (tmp_path / "service.py").write_text(
        "class AccountService:\n"
        "    def deposit(self, amount: int) -> int:\n"
        "        bal = amount\n"
        "        return bal\n"
        "\n"
        "    def withdraw(self, amount: int) -> int:\n"
        "        return -amount\n",
        encoding="utf-8",
    )

    # 1. Lazy elision placeholder must be rejected
    bad_spec = (
        "@file service.py\n"
        "@replace_body AccountService.deposit\n"
        "        # ... existing code ...\n"
        "        return amount\n"
        "@end\n"
    )
    res_bad = apply_patch_spec(tmp_path, bad_spec, apply=False)
    assert res_bad["all_syntax_valid"] is False
    all_errs = res_bad.get("parse_errors", []) + [e for f in res_bad.get("files", []) for e in f.get("errors", [])]
    assert any("Lazy elision" in e or "elision" in e.lower() for e in all_errs), f"Expected lazy elision error, got: {all_errs}"

    # 2. Multi-op in single file: @replace_body on top method (expanding lines), @sub on bottom method (original line number L7), and @add_method
    good_spec = (
        "@file service.py\n"
        "@replace_body AccountService.deposit\n"
        "        if amount <= 0:\n"
        "            raise ValueError('bad')\n"
        "        bal = amount + 10\n"
        "        return bal\n"
        "@end\n"
        "@sub L7 `return -amount` -> `return -abs(amount)`\n"
        "@add_method AccountService\n"
        "    def reset(self) -> int:\n"
        "        return 0\n"
        "@end\n"
    )
    res_ok = apply_patch_spec(tmp_path, good_spec, apply=True)
    assert res_ok["all_syntax_valid"] is True and res_ok["total_ops_applied"] == 3, f"Unexpected patch failure: {res_ok}"
    updated = (tmp_path / "service.py").read_text(encoding="utf-8")
    assert "bal = amount + 10" in updated
    assert "return -abs(amount)" in updated
    assert "def reset(self) -> int:" in updated


def test_pre_commit_caller_arity_contract_verification(tmp_path: Path):
    """Verify CodePlan pre-commit obligation check flags callers broken by a patched signature."""
    (tmp_path / "billing.py").write_text(
        "def charge(user_id: str) -> bool:\n"
        "    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "checkout.py").write_text(
        "from billing import charge\n\n"
        "def run_checkout(uid: str) -> bool:\n"
        "    return charge(uid)\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Patch `charge` to require a second mandatory argument `currency: str` without updating `checkout.py`
    spec = (
        "@file billing.py\n"
        "@sub L1 `def charge(user_id: str) -> bool:` -> `def charge(user_id: str, currency: str) -> bool:`\n"
    )
    res = brain.patch(spec, apply=False)
    assert res["all_contracts_valid"] is False
    assert any("[6D Caller Arity Contract Violation]" in d and "checkout.py" in d for d in res.get("contract_diagnostics", []))


def test_uncalled_nested_function_does_not_hallucinate_call_edge(tmp_path: Path):
    """H-06: Defining a nested helper without invoking it must emit defines_nested, not calls."""
    (tmp_path / "nested_demo.py").write_text(
        "def outer_fn(x: int) -> int:\n"
        "    def unused_inner(y: int) -> int:\n"
        "        return y + 1\n"
        "    return x * 2\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()
    verdict = brain.verify_claim("outer_fn", "calls", "unused_inner")
    assert verdict["verdict"] == "REFUTED", f"Expected REFUTED for uncalled nested fn, got: {verdict}"


def test_dyck_constrained_acl_push_and_ochiai_hub_damping(tmp_path: Path):
    """Verify Dyck CFL-reachability prevents Down->Up caller leakage through shared utility hubs."""
    (tmp_path / "logger.py").write_text(
        "def log_info(msg: str) -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "feature_a.py").write_text(
        "from logger import log_info\n"
        "def handle_payment() -> None:\n"
        "    log_info('paid')\n",
        encoding="utf-8",
    )
    for k in range(10):
        (tmp_path / f"unrelated_{k}.py").write_text(
            f"from logger import log_info\ndef noise_task_{k}() -> None:\n    log_info('n{k}')\n",
            encoding="utf-8",
        )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Seed PPR at feature_a.py::handle_payment (which calls logger.py::log_info).
    # In Dyck-constrained push: handle_payment --(calls: DOWN/phase 1)--> log_info.
    # From phase 1 at log_info, reverse called_by edges (UP) to noise_task_k are forbidden!
    ppr = brain._graph.multiplex_ppr({"feature_a.py::handle_payment": 1.0})
    noise_mass = sum(sc for nid, sc, _ in ppr if "unrelated_" in nid)
    assert noise_mass == 0.0, f"Dyck constraint violated: leaked {noise_mass} mass into unrelated callers of utility hub"


def test_normalized_sqlite_wal_store_and_salsa_early_cutoff(tmp_path: Path):
    """Verify normalized SQLite tables (files, symbols, edges) and Salsa Early Cutoff on body-only edits."""
    f = tmp_path / "math_ops.py"
    f.write_text(
        "def compute_tax(amount: float) -> float:\n"
        "    return amount * 0.15\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # Body-only edit (signature and exports unchanged) -> Salsa Early Cutoff must fire
    f.write_text(
        "def compute_tax(amount: float) -> float:\n"
        "    # updated tax rate comment\n"
        "    return amount * 0.18\n",
        encoding="utf-8",
    )
    inc1 = brain.incremental_index()
    assert inc1["salsa_early_cutoff"] is True

    # Interface edit (parameter added to signature) -> Salsa Early Cutoff must NOT fire
    f.write_text(
        "def compute_tax(amount: float, rate: float = 0.18) -> float:\n"
        "    return amount * rate\n",
        encoding="utf-8",
    )
    inc2 = brain.incremental_index()
    assert inc2["salsa_early_cutoff"] is False


def test_cross_instance_ledger_isolation_and_source_cache_invalidation(tmp_path: Path):
    """C-02 & C-03: Verify _source_cache invalidates on re-index and ledger seen_spans do not leak across instances."""
    f = tmp_path / "mod.py"
    f.write_text("def greet() -> str:\n    return 'v1'\n", encoding="utf-8")

    b1 = ProjectBrain(str(tmp_path))
    b1.index()
    s1 = b1.slice_symbols("mod.py:greet")
    assert "'v1'" in s1["slices"][0]["code"]

    # Modify file on disk and incremental_index -> _source_cache must serve 'v2', not stale 'v1'
    f.write_text("def greet() -> str:\n    return 'v2'\n", encoding="utf-8")
    b1.incremental_index()
    s2 = b1.slice_symbols("mod.py:greet")
    assert "'v2'" in s2["slices"][0]["code"]

    # A second independent ProjectBrain instance must have a fresh session_seen set
    b2 = ProjectBrain(str(tmp_path))
    b2.load()
    assert len(b2._ledger._session_seen) == 0


def test_polyglot_go_rust_java_treesitter_codecs_and_routes(tmp_path: Path):
    """Phase 3: Verify first-class Go, Rust, and Java Tree-sitter CST codecs, methods, calls, and routes."""
    (tmp_path / "server.go").write_text(
        'package main\n\n'
        'import "net/http"\n\n'
        'type OrderHandler struct {}\n\n'
        'func (h *OrderHandler) CreateOrder(w http.ResponseWriter, r *http.Request) {\n'
        '    ValidateOrderPayload(r)\n'
        '}\n\n'
        'func ValidateOrderPayload(r *http.Request) bool {\n'
        '    return true\n'
        '}\n\n'
        'func RegisterRoutes(router *GinEngine) {\n'
        '    router.POST("/api/orders", CreateOrder)\n'
        '}\n',
        encoding="utf-8",
    )
    (tmp_path / "engine.rs").write_text(
        'use std::collections::HashMap;\n\n'
        'pub struct PaymentEngine {}\n\n'
        'impl PaymentEngine {\n'
        '    pub fn settle_invoice(&self, invoice_id: u64) -> bool {\n'
        '        verify_ledger_entry(invoice_id)\n'
        '    }\n'
        '}\n\n'
        '#[get("/api/invoices")]\n'
        'pub fn list_invoices() -> String {\n'
        '    String::from("ok")\n'
        '}\n\n'
        'pub fn verify_ledger_entry(id: u64) -> bool {\n'
        '    id > 0\n'
        '}\n',
        encoding="utf-8",
    )
    (tmp_path / "BillingController.java").write_text(
        'package com.example.billing;\n\n'
        'import java.util.List;\n\n'
        '@RequestMapping("/api/v1/billing")\n'
        'public class BillingController {\n'
        '    @PostMapping("/charge")\n'
        '    public String chargeCustomer(String customerId) {\n'
        '        return auditCharge(customerId);\n'
        '    }\n\n'
        '    public String auditCharge(String customerId) {\n'
        '        return customerId;\n'
        '    }\n'
        '}\n',
        encoding="utf-8",
    )

    brain = ProjectBrain(str(tmp_path))
    stats = brain.index()
    assert stats["files"] == 3

    # Verify Go struct, method, call edge, and Gin route
    go_info = brain._index["server.go"]
    assert any(c["name"] == "OrderHandler" for c in go_info["classes"])
    assert any(f["name"] == "CreateOrder" and f["parent_class"] == "OrderHandler" for f in go_info["functions"])
    assert any(r["method"] == "POST" and r["path"] == "/api/orders" for r in go_info["routes"])
    assert brain.verify_claim("CreateOrder", "calls", "ValidateOrderPayload")["verdict"] == "SUPPORTED"

    # Verify Rust struct, impl method, call edge, and Actix route
    rs_info = brain._index["engine.rs"]
    assert any(c["name"] == "PaymentEngine" for c in rs_info["classes"])
    assert any(f["name"] == "settle_invoice" and f["parent_class"] == "PaymentEngine" for f in rs_info["functions"])
    assert any(r["method"] == "GET" and r["path"] == "/api/invoices" for r in rs_info["routes"])
    assert brain.verify_claim("settle_invoice", "calls", "verify_ledger_entry")["verdict"] == "SUPPORTED"

    # Verify Java class, Spring class+method route concatenation, and call edge
    java_info = brain._index["BillingController.java"]
    assert any(c["name"] == "BillingController" for c in java_info["classes"])
    assert any(r["method"] == "POST" and r["path"] == "/api/v1/billing/charge" for r in java_info["routes"])
    assert brain.verify_claim("chargeCustomer", "calls", "auditCharge")["verdict"] == "SUPPORTED"


def test_sqlite_fts5_recursive_cte_and_delta_overlay(tmp_path: Path):
    """Phase 1 Completion: Verify FTS5 sub-token search, 3-hop recursive CTE, and Stacked Delta Overlay."""
    (tmp_path / "a.py").write_text("from b import step_two\ndef step_oneCamelCase():\n    return step_two()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from c import step_three\ndef step_two():\n    return step_three()\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def step_three():\n    return 42\n", encoding="utf-8")

    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. FTS5 sub-token search finds `step_oneCamelCase` when querying subtoken `camel`
    fts_res = brain._db_store.search_fts("camel", limit=5)
    assert any(r["name"] == "step_oneCamelCase" for r in fts_res)

    # 2. 3-hop WITH RECURSIVE SQLite CTE reaches b.py::step_two (hop 1) and c.py::step_three (hop 2)
    cte_res = brain._db_store.query_reachable_cte(["a.py::step_oneCamelCase"], max_hops=3, direction="forward")
    reached_nids = {r["node_id"]: r["hop"] for r in cte_res}
    assert reached_nids.get("fn:b.py:step_two") == 1
    assert reached_nids.get("fn:c.py:step_three") == 2

    # 3. Stacked In-Memory Delta Overlay replaces c.py symbols in temp.unified_symbols without touching disk
    from projectbrain.parsers import parse
    delta_info = parse("def step_three_patched():\n    return 99\n", ".py")
    conn = brain._db_store.open_delta_overlay({"c.py": delta_info})
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM temp.unified_symbols;").fetchall()}
        assert "step_three_patched" in names
        assert "step_three" not in names
    finally:
        conn.close()
    # Persistent DB on disk is untouched
    conn2 = brain._db_store._connect()
    try:
        disk_names = {r[0] for r in conn2.execute("SELECT name FROM symbols;").fetchall()}
        assert "step_three" in disk_names
        assert "step_three_patched" not in disk_names
    finally:
        conn2.close()


def test_stack_trace_frame_seeder_and_greenfield_sibling_mode(tmp_path: Path):
    """Phase 2 Completion: Verify Stack-Trace Frame Seeder pins crash lines and Greenfield Mode emits exemplars."""
    (tmp_path / "worker.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        "@router.post('/api/jobs/run')\n"
        "def run_job_endpoint(job_id: str):\n"
        "    return execute_heavy_pipeline(job_id)\n\n"
        "def execute_heavy_pipeline(job_id: str):\n"
        + "".join(f"    step_{k} = {k}\n" for k in range(1, 35))
        + "    crash_value = 1 / 0  # L43 exact crash line\n"
        + "".join(f"    tail_{k} = {k}\n" for k in range(1, 15))
        + "    return crash_value\n",
        encoding="utf-8",
    )
    brain = ProjectBrain(str(tmp_path))
    brain.index()

    # 1. Stack trace prompt automatically seeds worker.py:43 and unfolds the exact crash line L43
    tb_prompt = (
        'Traceback (most recent call last):\n'
        '  File "worker.py", line 43, in execute_heavy_pipeline\n'
        'ZeroDivisionError: division by zero'
    )
    ctx = brain.compile_context(tb_prompt, budget_tokens=1500)
    assert ctx["intent"].get("stack_trace_frames")
    sliced_code = "\n".join(s["code"] for s in ctx["target_slices"])
    assert "crash_value = 1 / 0" in sliced_code

    # 2. Greenfield prompt triggers greenfield_mode and returns structural_siblings
    gf_ctx = brain.compile_context("add a new endpoint for cancelling jobs in worker", budget_tokens=1500)
    assert gf_ctx["intent"].get("greenfield_mode") is True
    assert len(gf_ctx.get("structural_siblings", [])) >= 1


def test_cli_init_hooks_and_lean_mcp_surface(tmp_path: Path):
    """Phase 3 Completion: Verify `projectbrain init --hooks` and `server.LEAN_CORE_TOOLS`."""
    from projectbrain.cli import _run_init
    from projectbrain.server import LEAN_CORE_TOOLS

    (tmp_path / "app.py").write_text("def hello(): return 1\n", encoding="utf-8")
    res = _run_init(tmp_path, write_mcp=False, write_rules=False, write_hooks=True)
    assert "wrote_.claude/settings.local.json_hooks" in res["actions"]
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert {"brain_dossier", "brain_slice", "brain_explore", "brain_check", "brain_remember"} == LEAN_CORE_TOOLS


def test_pure_graph_as_storage_zero_disk_retrieval(tmp_path: Path):
    """Verify Kythe/Glean-style Graph-as-Storage:
    1. Graph nodes and edges are stored natively in `graph_nodes` and `edges` WITHOUT ROWID tables (no `graph_json` blob in `meta`).
    2. Content-addressed source blobs are stored in `source_blobs`, allowing `get_symbol_slice_from_graph` and `compile_context` to slice code even if source files are removed from disk.
    """
    import sqlite3

    src_file = tmp_path / "ledger_engine.py"
    src_file.write_text(
        "def settle_clearing_house(tx_id: str, amount_cents: int) -> dict:\n"
        '    """Settle interbank clearing house transaction and emit receipt."""\n'
        "    fee = amount_cents // 100\n"
        "    return {'tx_id': tx_id, 'net': amount_cents - fee}\n",
        encoding="utf-8",
    )
    b1 = ProjectBrain(str(tmp_path))
    b1.index()

    # 1. Verify `meta` has no duplicate `graph_json` blob and `graph_nodes` / `source_blobs` are populated
    conn = sqlite3.connect(str(tmp_path / ".projectbrain.db"))
    try:
        meta_keys = {r[0] for r in conn.execute("SELECT key FROM meta;").fetchall()}
        assert "graph_json" not in meta_keys
        n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes;").fetchone()[0]
        n_blobs = conn.execute("SELECT COUNT(*) FROM source_blobs;").fetchone()[0]
        assert n_nodes >= 2
        assert n_blobs == 1
    finally:
        conn.close()

    # 2. Direct SQLite Graph-as-Storage symbol slice without touching Python index
    direct_slice = b1._db_store.get_symbol_slice_from_graph("settle_clearing_house")
    assert direct_slice is not None
    assert "amount_cents - fee" in direct_slice["code"]

    # 3. Delete the physical `.py` file from disk and load a fresh ProjectBrain solely from `.projectbrain.db`!
    src_file.unlink()
    assert not src_file.exists()

    b2 = ProjectBrain(str(tmp_path))
    assert b2.load() is True
    assert b2._graph is not None and len(b2._graph.nodes) == n_nodes

    sliced = b2.slice_symbols("settle_clearing_house")
    assert sliced["slices"]
    assert "amount_cents - fee" in sliced["slices"][0]["code"]


def test_v081_single_turn_dossier_terse_rules_and_6d_run_compressor(tmp_path: Path, capsys):
    """Verify v0.8.1 post-dossier token-saving upgrades:
    1. Default `projectbrain dossier` prints markdown directly to stdout in 1 turn while saving `.projectbrain_dossier.md` (`--json` outputs metadata).
    2. `projectbrain init --hooks` embeds Terse Output Mode ('Say Less, Keep Code Exact') and AST Symbol-First Patching (`@replace_body <Symbol>`).
    3. `projectbrain run` / `compress_command_output` folds passing test noise, deduplicates repetitive log lines, and resolves traceback frames to 6D Code Graph symbols + callers.
    """
    import json
    from projectbrain import cli

    pkg = tmp_path / "billing.py"
    pkg.write_text(
        "def compute_invoice_tax(subtotal: float, rate: float) -> float:\n"
        "    if rate < 0:\n"
        "        raise ValueError('negative tax rate')\n"
        "    return round(subtotal * rate, 2)\n\n"
        "def checkout_order(order_id: str, amount: float) -> dict:\n"
        "    tax = compute_invoice_tax(amount, -0.05)\n"
        "    return {'order_id': order_id, 'total': amount + tax}\n",
        encoding="utf-8",
    )

    # 1. Verify `init --hooks` writes non-intrusive rules that preserve native rg/read_file usage
    capsys.readouterr()
    rc_init = cli.main(["--path", str(tmp_path), "init", "--no-mcp", "--hooks"])
    assert rc_init == 0
    agents_txt = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "do NOT read large monolith files" not in agents_txt
    assert "brain_dossier" in agents_txt
    assert "projectbrain run" in agents_txt
    hooks_json = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert len(hooks_json["hooks"]["PostToolUse"]) >= 1

    # 2. Verify default `projectbrain dossier` prints full markdown in 1 turn (no extra read_file required)
    capsys.readouterr()
    rc_dos = cli.main(["--path", str(tmp_path), "dossier", "how checkout_order computes invoice tax"])
    assert rc_dos == 0
    out_md = capsys.readouterr().out
    assert out_md.startswith("# ProjectBrain Complete Engineering Dossier")
    assert "compute_invoice_tax" in out_md
    assert "brain_slice" in out_md
    assert (tmp_path / ".projectbrain_dossier.md").exists()

    # And `--json` still returns metadata dict
    rc_json = cli.main(["--path", str(tmp_path), "dossier", "--json", "how checkout_order computes invoice tax"])
    assert rc_json == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["files_resolved"] >= 1
    assert "markdown" not in meta

    # 3. Verify 6D Graph-Aware Command/Test Output Compressor (`compress_command_output`)
    brain = ProjectBrain(str(tmp_path))
    brain.load()
    noisy_output = "\n".join([
        *[f"tests/test_suite.py::test_case_{i} PASSED [ {i}%]" for i in range(1, 51)],
        *[f"2026-11-07T12:00:{i:02d}Z INFO [worker] polling queue item_id={1000 + i}" for i in range(1, 25)],
        "================================== FAILURES ===================================",
        "Traceback (most recent call last):",
        '  File "billing.py", line 7, in checkout_order',
        "    tax = compute_invoice_tax(amount, -0.05)",
        '  File "billing.py", line 3, in compute_invoice_tax',
        "    raise ValueError('negative tax rate')",
        "ValueError: negative tax rate",
        "=========================== 1 failed, 50 passed in 0.42s ======================",
    ])
    comp = brain.compress_command_output(noisy_output)
    assert comp["passing_tests_folded"] == 50
    assert comp["repeated_lines_folded"] >= 20
    assert comp["savings_pct"] > 50.0
    assert "# [6D Failing Frame Resolution]" in comp["compressed_output"]
    resolved_syms = {f["symbol"] for f in comp["resolved_6d_frames"]}
    assert "compute_invoice_tax" in resolved_syms
    assert "checkout_order" in resolved_syms
    tax_frame = next(f for f in comp["resolved_6d_frames"] if f["symbol"] == "compute_invoice_tax")
    assert any("checkout_order" in c for c in tax_frame["callers"])


def test_codex_review_hardening_suite(tmp_path: Path, monkeypatch):
    """Verify all 5 Codex audit fixes:
    1. `.gitignore` compliance in `_walk()` (directory rules, wildcard globs, anchored rules, nested `.gitignore`).
    2. Non-destructive `projectbrain init` preserving existing `AGENTS.md`, `.cursor/rules`, and `.claude/settings.local.json`.
    3. `brain_run` MCP security lockdown (blocks `command` unless `PROJECTBRAIN_ALLOW_EXEC=1`).
    4. Polyglot extension coverage (`.vue`, `.svelte`, `.cs`, `.kt`, `.rb`, `.php`, `.cpp`, `.prisma`, `.proto`).
    """
    import json
    from projectbrain.brain import ProjectBrain
    from projectbrain.cli import _run_init
    from projectbrain import server

    # 1. .gitignore compliance
    (tmp_path / ".gitignore").write_text(
        "# Custom ignores\n"
        "generated_sdk/\n"
        "*.pb2.py\n"
        "/root_only_ignore.py\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def run_main():\n    return 42\n", encoding="utf-8")
    (tmp_path / "src" / "root_only_ignore.py").write_text("def nested_allowed():\n    return 1\n", encoding="utf-8")
    (tmp_path / "root_only_ignore.py").write_text("def should_skip_root():\n    return 0\n", encoding="utf-8")
    (tmp_path / "src" / "schema.pb2.py").write_text("def should_skip_glob():\n    return 0\n", encoding="utf-8")
    (tmp_path / "generated_sdk").mkdir()
    (tmp_path / "generated_sdk" / "client.py").write_text("def should_skip_dir():\n    return 0\n", encoding="utf-8")

    # 4. Polyglot files (.vue, .cs, .kt)
    (tmp_path / "src" / "Widget.vue").write_text(
        "<template><div>{{ count }}</div></template>\n"
        "<script lang=\"ts\">\n"
        "export function computeWidgetTotal(price: number, qty: number): number {\n"
        "  return price * qty;\n"
        "}\n"
        "</script>\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "OrderController.cs").write_text(
        "using System;\n"
        "public class OrderController {\n"
        "    [HttpPost(\"/api/orders\")]\n"
        "    public string CreateOrder(string orderId) {\n"
        "        return orderId;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    # 2. Non-destructive `init` on pre-existing user files
    (tmp_path / "AGENTS.md").write_text("# Team Custom Instructions\nAlways run linter before commit.\n", encoding="utf-8")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps({
            "permissions": {"allow": ["Bash(pytest:*)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo 'team hook'"}]}
                ]
            },
        }),
        encoding="utf-8",
    )

    _run_init(tmp_path, write_mcp=True, write_rules=True, write_hooks=True)
    # Run a second time to verify idempotency (no duplicate sections or hooks)
    _run_init(tmp_path, write_mcp=True, write_rules=True, write_hooks=True)

    agents_txt = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Team Custom Instructions" in agents_txt
    assert "Always run linter before commit." in agents_txt
    assert agents_txt.count("<!-- projectbrain:start -->") == 1

    claude_cfg = json.loads((claude_dir / "settings.local.json").read_text(encoding="utf-8"))
    assert claude_cfg["permissions"]["allow"] == ["Bash(pytest:*)"]
    pre_hooks = claude_cfg["hooks"]["PreToolUse"]
    assert any("team hook" in json.dumps(h) for h in pre_hooks)
    assert len(claude_cfg["hooks"]["PostToolUse"]) == 1 and "hook post-edit" in json.dumps(claude_cfg["hooks"]["PostToolUse"])

    brain = ProjectBrain(str(tmp_path))
    brain.load()
    indexed_files = set(brain._index.keys())
    assert "src/main.py" in indexed_files
    assert "src/root_only_ignore.py" in indexed_files
    assert "src/Widget.vue" in indexed_files
    assert "src/OrderController.cs" in indexed_files
    assert "root_only_ignore.py" not in indexed_files
    assert "src/schema.pb2.py" not in indexed_files
    assert "generated_sdk/client.py" not in indexed_files

    vue_fns = brain._index["src/Widget.vue"]["functions"]
    assert any(f["name"] == "computeWidgetTotal" and f["line_start"] == 3 for f in vue_fns)
    cs_routes = brain._index["src/OrderController.cs"]["routes"]
    assert any(r["method"] == "POST" and r["path"] == "/api/orders" for r in cs_routes)

    # 3. MCP `brain_run` security lockdown
    monkeypatch.delenv("PROJECTBRAIN_ALLOW_EXEC", raising=False)
    blocked = server.brain_run(command="echo pwned", repo_path=str(tmp_path))
    assert blocked.get("exit_code") == 126
    assert "disabled by default" in blocked.get("error", "")

    safe_comp = server.brain_run(raw_output="tests/test_a.py::test_1 PASSED\n", repo_path=str(tmp_path))
    assert safe_comp["passing_tests_folded"] == 1
