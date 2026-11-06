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

    assert brain.stats()["cells_4d"] >= 4
    q_4d = brain.query("where is videoIndex reset when courseId changes")
    assert q_4d and q_4d[0]["rel_path"] == "src/DashboardWorkspace.tsx"
    # 1. First dossier on VideosPlayer / videos branch inside the monolith
    d1 = brain.compile_dossier(["where is VideosPlayer rendered in DashboardWorkspace"])
    md1 = d1["markdown"]
    assert "[4D Coordinate Matrix]" in md1
    assert "case:videos" in md1
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
    assert "[4D Coordinate Matrix]" in md
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
    assert "?(int) ? ?(str) = ?" in md

    # 2c. Nested dict return shape + `${API_BASE}` fetch URL must still catch `.missing_client_field`
    assert "[6D Cross-Stack HTTP Shape Violation]" in md
    assert "missing_client_field" in md
