"""Zero-Config Day-1 Governance Discovery Engine (`projectbrain learn` & `init --learn`).

Automatically discovers enforceable repository governance from static dependency structure & git history:
  1. Zero-violation directional `--forbid` architectural layer boundaries (both upward cycle
     prevention and 3-tier layer-bypass encapsulation) verified against `CodeGraph` & imports.
  2. High-confidence co-change coupling contracts mined from git commit history (`A <-> B` / `A -> B`).
  3. High-fan-in architectural hub symbols ranked by cross-file caller and importer blast radius.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .git_diff import _LOCKFILE_NAMES, _is_test_or_noise_cochange_path, _to_local_rel, find_git_context
from .memory import RepoMemory, _compile_path_glob

if TYPE_CHECKING:
    from .brain import ProjectBrain

_NON_LAYER_DIRS = {
    "tests", "test", "testing", "__tests__", "spec", "specs", "e2e",
    "docs", "doc", "documentation", "examples", "example", "demo", "demos",
    "scripts", "script", "benchmarks", "benchmark", "eval", "evals",
    "fixtures", "fixture", "migrations", "alembic", "static", "assets", "templates",
}

_CROSS_CUTTING_UTILITY_NAMES = {
    "utils", "util", "common", "shared", "helpers", "helper",
    "types", "typing", "constants", "const", "config", "configs", "settings",
    "errors", "error", "exceptions", "logging", "logger", "log",
    "telemetry", "metrics", "lib", "pkg", "internal", "versions",
}

_PRESENTATION_LAYER_NAMES = {
    "routes", "routers", "router", "api", "apis", "controllers", "controller",
    "views", "view", "handlers", "handler", "endpoints", "ui", "pages",
    "components", "cli", "commands", "web", "http", "graphql", "grpc",
}

_PERSISTENCE_LAYER_NAMES = {
    "db", "database", "dao", "daos", "repositories", "repository", "repo", "repos",
    "storage", "store", "stores", "orm", "sql", "persistence", "models", "data",
}


def _is_prod_layer_file(rel: str) -> bool:
    p = Path(rel)
    parts_low = [seg.lower() for seg in p.parts[:-1]]
    if any(seg in _NON_LAYER_DIRS for seg in parts_low):
        return False
    name_low = p.name.lower()
    stem_low = p.stem.lower()
    if (
        name_low.endswith(("_test.go", "_test.py", "_test.rs", "test.java", "tests.java"))
        or stem_low.endswith((".test", ".spec", "_test", "_spec"))
        or stem_low.startswith(("test_", "spec_"))
    ):
        return False
    return True


def _common_dir_prefix(dir_tuples: list[tuple[str, ...]]) -> tuple[str, ...]:
    """Find deepest shared directory prefix covering >=75% of multi-directory production files."""
    if not dir_tuples:
        return ()
    non_root = [d for d in dir_tuples if len(d) >= 1]
    if not non_root:
        return ()
    threshold = max(2, int(len(non_root) * 0.75))
    prefix: list[str] = []
    depth = 0
    while True:
        counts: dict[str, int] = {}
        eligible = [d for d in non_root if len(d) > depth and d[:depth] == tuple(prefix)]
        if len(eligible) < threshold:
            break
        for d in eligible:
            counts[d[depth]] = counts.get(d[depth], 0) + 1
        if len(counts) != 1:
            break
        only_seg, cnt = next(iter(counts.items()))
        if cnt < threshold:
            break
        # Stop descending if there are no deeper subdirectories beneath `only_seg`
        has_deeper_subdirs = any(len(d) > depth + 1 for d in eligible if d[depth] == only_seg)
        if not has_deeper_subdirs:
            # All files live flat inside `prefix + [only_seg]`
            prefix.append(only_seg)
            break
        prefix.append(only_seg)
        depth += 1
    return tuple(prefix)


def discover_boundary_rules(
    brain: "ProjectBrain",
    *,
    max_rules: int = 12,
) -> list[dict]:
    """Discover zero-violation directional `--forbid` rules from resolved imports & call graph."""
    prod_files = sorted(r for r in brain._index.keys() if _is_prod_layer_file(r))
    if len(prod_files) < 2:
        return []

    dir_tuples = [tuple(Path(r).parts[:-1]) for r in prod_files]
    prefix = _common_dir_prefix(dir_tuples)
    p_len = len(prefix)

    # Partition files into directory layers under `prefix`
    layer_files: dict[str, list[str]] = {}
    layer_short_name: dict[str, str] = {}
    for rel in prod_files:
        parts = Path(rel).parts[:-1]
        if len(parts) > p_len and parts[:p_len] == prefix:
            sub_dir = parts[p_len]
            glob_pat = "/".join((*prefix, sub_dir, "*"))
            layer_files.setdefault(glob_pat, []).append(rel)
            layer_short_name[glob_pat] = sub_dir.lower()

    is_module_mode = False
    if len(layer_files) < 2:
        # Flat single-package architecture: partition by module files inside `prefix`
        layer_files.clear()
        layer_short_name.clear()
        is_module_mode = True
        for rel in prod_files:
            stem = Path(rel).stem
            if stem == "__init__":
                continue
            layer_files[rel] = [rel]
            layer_short_name[rel] = stem.lower()

    if len(layer_files) < 2:
        return []

    file_to_layer: dict[str, str] = {}
    for l_glob, f_list in layer_files.items():
        for f_rel in f_list:
            file_to_layer[f_rel] = l_glob

    # Build directed cross-layer dependency edge matrix
    edge_pairs: set[tuple[str, str, str]] = set()
    for src_rel, dst_list in brain._import_targets.items():
        la = file_to_layer.get(src_rel)
        if not la:
            continue
        for dst_rel in dst_list:
            lb = file_to_layer.get(dst_rel)
            if lb and la != lb:
                edge_pairs.add((src_rel, dst_rel, "imports"))

    if brain._graph:
        nodes = brain._graph.nodes
        for edge in brain._graph.edges:
            if edge.get("type") != "calls":
                continue
            if edge.get("confidence") not in ("exact", "qualified", "import_resolved"):
                continue
            s_rel = nodes.get(edge.get("src", ""), {}).get("rel_path", "")
            d_rel = nodes.get(edge.get("dst", ""), {}).get("rel_path", "")
            la = file_to_layer.get(s_rel)
            lb = file_to_layer.get(d_rel)
            if la and lb and la != lb:
                edge_pairs.add((s_rel, d_rel, "calls"))

    layer_weight: dict[tuple[str, str], int] = {}
    out_neighbors: dict[str, set[str]] = {lg: set() for lg in layer_files}
    in_neighbors: dict[str, set[str]] = {lg: set() for lg in layer_files}

    for s_rel, d_rel, _ in edge_pairs:
        la = file_to_layer[s_rel]
        lb = file_to_layer[d_rel]
        layer_weight[(la, lb)] = layer_weight.get((la, lb), 0) + 1
        out_neighbors[la].add(lb)
        in_neighbors[lb].add(la)

    existing_pairs = {
        (r.get("src_glob", ""), r.get("dst_glob", ""))
        for r in brain._memory._boundary_rules
    }

    candidates: dict[tuple[str, str], dict] = {}

    def _propose(src_g: str, dst_g: str, score: float, kind: str, reason: str, support: int) -> None:
        if (src_g, dst_g) in existing_pairs or src_g == dst_g:
            return
        if layer_weight.get((src_g, dst_g), 0) > 0:
            return
        prev = candidates.get((src_g, dst_g))
        if prev is None or score > prev["score"]:
            candidates[(src_g, dst_g)] = {
                "src_glob": src_g,
                "dst_glob": dst_g,
                "rule": f"{src_g} -> {dst_g}",
                "kind": kind,
                "supporting_edges": support,
                "score": round(score, 2),
                "reason": reason,
            }

    # Pattern 1: 3-Tier Layer-Bypass Encapsulation (`A -> M -> C` with 0 direct `A -> C`)
    if not is_module_mode:
        for la in sorted(layer_files):
            a_name = layer_short_name[la]
            a_is_top = (len(in_neighbors[la]) == 0) or (a_name in _PRESENTATION_LAYER_NAMES)
            for lm in sorted(out_neighbors[la]):
                if layer_weight.get((lm, la), 0) > 0:
                    continue
                for lc in sorted(out_neighbors[lm]):
                    if lc in (la, lm):
                        continue
                    if layer_weight.get((la, lc), 0) > 0 or layer_weight.get((lc, lm), 0) > 0 or layer_weight.get((lc, la), 0) > 0:
                        continue
                    c_name = layer_short_name[lc]
                    if c_name in _CROSS_CUTTING_UTILITY_NAMES:
                        continue
                    c_encapsulated = (in_neighbors[lc] == {lm}) or (c_name in _PERSISTENCE_LAYER_NAMES)
                    if a_is_top and c_encapsulated:
                        sup = layer_weight[(la, lm)] + layer_weight[(lm, lc)]
                        tier_bonus = 6.0 if (a_name in _PRESENTATION_LAYER_NAMES and c_name in _PERSISTENCE_LAYER_NAMES) else 3.5
                        _propose(
                            la,
                            lc,
                            score=10.0 + tier_bonus + sup * 1.5,
                            kind="layer_bypass_encapsulation",
                            reason=f"Layer encapsulation: `{la}` reaches `{lc}` exclusively via `{lm}` (0 direct edges)",
                            support=sup,
                        )
                    # Also forbid 2-hop upward reverse dependency `C -> A`
                    sup_rev = layer_weight[(la, lm)] + layer_weight[(lm, lc)]
                    _propose(
                        lc,
                        la,
                        score=6.5 + sup_rev * 1.2,
                        kind="upward_layer_boundary",
                        reason=f"Directional hierarchy: `{la}` -> `{lm}` -> `{lc}` (0 reverse dependencies)",
                        support=sup_rev,
                    )

    # Pattern 2: Direct One-Way Layer Hierarchy (`A -> B` with 0 `B -> A` => forbid `B -> A`)
    for (la, lb), w_ab in sorted(layer_weight.items()):
        if layer_weight.get((lb, la), 0) > 0:
            continue
        if is_module_mode:
            # In flat-package module mode, prioritize leaf/foundation modules not importing top entrypoints
            b_out_deg = len(out_neighbors[lb])
            a_in_deg = len(in_neighbors[la])
            if b_out_deg > 2 and a_in_deg > 1 and w_ab < 2:
                continue
        a_name = layer_short_name[la]
        b_name = layer_short_name[lb]
        sem_bonus = 0.0
        if a_name in _PRESENTATION_LAYER_NAMES:
            sem_bonus += 2.5
        if b_name in _PERSISTENCE_LAYER_NAMES or b_name in _CROSS_CUTTING_UTILITY_NAMES:
            sem_bonus += 2.0
        files_factor = min(4.0, 0.5 * (len(layer_files[la]) + len(layer_files[lb])))
        _propose(
            lb,
            la,
            score=5.0 + w_ab * 2.0 + sem_bonus + files_factor,
            kind="directional_dag_boundary",
            reason=f"Directional dependency: `{la}` depends on `{lb}` ({w_ab} edge(s), 0 reverse edges)",
            support=w_ab,
        )

    ranked = sorted(
        candidates.values(),
        key=lambda item: (-item["score"], item["rule"]),
    )

    # Strict Zero-Violation Proof Gate: verify every candidate rule via RepoMemory matcher
    verified: list[dict] = []
    probe_mem = RepoMemory(brain.root / "__nonexistent_probe_dir__")
    probe_mem._notes = []
    for cand in ranked:
        if len(verified) >= max_rules:
            break
        probe_mem._boundary_rules = [{
            "id": "probe",
            "src_glob": cand["src_glob"],
            "dst_glob": cand["dst_glob"],
            "rule": cand["rule"],
            "edge_types": ["imports", "calls"],
        }]
        violations = probe_mem.check_boundary_violations(
            brain._index, brain._import_targets, brain._graph, focus_rels=None,
        )
        if not violations:
            # Double-check regex actually matches at least 1 source and 1 destination file
            s_rx = _compile_path_glob(cand["src_glob"])
            d_rx = _compile_path_glob(cand["dst_glob"])
            if any(s_rx.match(f) for f in prod_files) and any(d_rx.match(f) for f in prod_files):
                verified.append({
                    **cand,
                    "existing_violations": 0,
                })

    return verified


def mine_co_change_contracts(
    brain: "ProjectBrain",
    *,
    days: int = 180,
    min_commits: int = 3,
    min_confidence: float = 0.70,
    max_rules: int = 10,
) -> list[dict]:
    """Mine git commit history for high-confidence file co-change coupling contracts."""
    git_root, sub_prefix = find_git_context(brain.root)
    if git_root is None:
        return []
    log_cmd = [
        "git", "-c", "core.quotepath=false", "log",
        f"--since={days} days ago",
        "-n", "200",
        "--name-only",
        "--diff-filter=AMRC",
        "--pretty=format:__PB_COMMIT__",
    ]
    if sub_prefix:
        log_cmd.extend(["--", sub_prefix.rstrip("/")])
    try:
        proc = subprocess.run(
            log_cmd,
            cwd=str(git_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if proc.returncode != 0 or not proc.stdout:
        return []

    commits: list[set[str]] = []
    cur: set[str] = set()
    exists_cache: dict[str, bool] = {}

    def _file_alive(rel: str) -> bool:
        if rel not in exists_cache:
            exists_cache[rel] = rel in brain._index or (brain.root / rel).is_file()
        return exists_cache[rel]

    for raw_ln in proc.stdout.splitlines():
        ln = raw_ln.strip()
        if ln == "__PB_COMMIT__":
            if 1 <= len(cur) <= 10:
                commits.append(cur)
            cur = set()
            continue
        if ln:
            norm = _to_local_rel(ln, sub_prefix)
            if norm and not _is_test_or_noise_cochange_path(norm) and norm.lower() not in _LOCKFILE_NAMES:
                if _file_alive(norm):
                    cur.add(norm)
    if 1 <= len(cur) <= 10:
        commits.append(cur)

    total_commits = len(commits)
    if total_commits < min_commits:
        return []

    file_counts: dict[str, int] = {}
    pair_counts: dict[tuple[str, str], int] = {}

    for c_files in commits:
        ordered = sorted(c_files)
        for f in ordered:
            file_counts[f] = file_counts.get(f, 0) + 1
        n = len(ordered)
        if n < 2:
            continue
        for i in range(n):
            for j in range(i + 1, n):
                pair = (ordered[i], ordered[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    existing_rules = brain._memory.list_co_change_rules()
    existing_pairs: set[tuple[str, str]] = set()
    for er in existing_rules:
        ea, eb = er.get("file_a", ""), er.get("file_b", "")
        existing_pairs.add((ea, eb))
        if er.get("bidirectional", True):
            existing_pairs.add((eb, ea))

    discovered: list[dict] = []
    for (fa, fb), co_cnt in pair_counts.items():
        if co_cnt < min_commits:
            continue
        na = file_counts.get(fa, co_cnt)
        nb = file_counts.get(fb, co_cnt)
        p_b_given_a = co_cnt / max(1, na)
        p_a_given_b = co_cnt / max(1, nb)
        lift_ab = p_b_given_a / max(0.01, nb / total_commits) if total_commits >= 6 else 99.0
        lift_ba = p_a_given_b / max(0.01, na / total_commits) if total_commits >= 6 else 99.0

        if p_b_given_a >= min_confidence and p_a_given_b >= min_confidence and lift_ab >= 1.25 and lift_ba >= 1.25:
            if (fa, fb) in existing_pairs:
                continue
            conf = round(min(p_b_given_a, p_a_given_b), 3)
            pct = round(conf * 100, 1)
            discovered.append({
                "file_a": fa,
                "file_b": fb,
                "bidirectional": True,
                "rule": f"{fa} <-> {fb}",
                "co_change_count": co_cnt,
                "confidence": conf,
                "co_change_pct": pct,
                "score": round(co_cnt * conf, 3),
                "reason": f"Co-changed in {co_cnt} commits ({pct}% bidirectional coupling)",
            })
        elif p_b_given_a >= min_confidence and lift_ab >= 1.25:
            if (fa, fb) in existing_pairs:
                continue
            conf = round(p_b_given_a, 3)
            pct = round(conf * 100, 1)
            discovered.append({
                "file_a": fa,
                "file_b": fb,
                "bidirectional": False,
                "rule": f"{fa} -> {fb}",
                "co_change_count": co_cnt,
                "confidence": conf,
                "co_change_pct": pct,
                "score": round(co_cnt * conf, 3),
                "reason": f"When `{fa}` changed, `{fb}` co-changed in {co_cnt}/{na} ({pct}%) commits",
            })
        elif p_a_given_b >= min_confidence and lift_ba >= 1.25:
            if (fb, fa) in existing_pairs:
                continue
            conf = round(p_a_given_b, 3)
            pct = round(conf * 100, 1)
            discovered.append({
                "file_a": fb,
                "file_b": fa,
                "bidirectional": False,
                "rule": f"{fb} -> {fa}",
                "co_change_count": co_cnt,
                "confidence": conf,
                "co_change_pct": pct,
                "score": round(co_cnt * conf, 3),
                "reason": f"When `{fb}` changed, `{fa}` co-changed in {co_cnt}/{nb} ({pct}%) commits",
            })

    discovered.sort(key=lambda item: (-item["score"], -item["co_change_count"], item["rule"]))
    return discovered[:max_rules]


def discover_hub_symbols(brain: "ProjectBrain", *, top_k: int = 10) -> list[dict]:
    """Identify high-fan-in production functions and classes ranked by external blast radius."""
    if not brain._graph:
        return []

    nodes = brain._graph.nodes
    edges = brain._graph.edges
    radj = brain._graph._radj

    _GENERIC_METHOD_NAMES = {
        "get", "set", "items", "keys", "values", "pop", "update",
        "clear", "copy", "close", "read", "write", "run", "main", "__init__",
    }
    hubs: list[dict] = []
    for nid, nd in nodes.items():
        ntype = nd.get("type")
        if ntype not in ("fn", "cls"):
            continue
        rel = nd.get("rel_path", "")
        if not rel or not _is_prod_layer_file(rel):
            continue
        name = nd.get("name", "")
        parent_cls = nd.get("parent_class")
        short_name = name.split(".")[-1]
        if short_name.startswith("_") and short_name != "__init__":
            continue
        if parent_cls and str(parent_cls).startswith("_"):
            continue

        ext_caller_files: set[str] = set()
        ext_call_sites = 0
        for ei in radj.get(nid, ()):
            e = edges[ei]
            if e.get("type") not in ("calls", "inherits"):
                continue
            conf = e.get("confidence")
            if conf not in ("exact", "qualified", "import_resolved"):
                continue
            if conf == "qualified" and short_name in _GENERIC_METHOD_NAMES:
                continue
            src_rel = nodes.get(e.get("src", ""), {}).get("rel_path", "")
            if src_rel and src_rel != rel and _is_prod_layer_file(src_rel):
                ext_caller_files.add(src_rel)
                ext_call_sites += 1

        prod_importers = [
            imp for imp in brain._imported_by.get(rel, ())
            if imp != rel and _is_prod_layer_file(imp)
        ]
        if not ext_caller_files and len(prod_importers) < 3:
            continue

        fan_in_score = len(ext_caller_files) * 3 + ext_call_sites + len(prod_importers)
        if fan_in_score < 2:
            continue

        risk = "critical" if len(ext_caller_files) >= 6 or len(prod_importers) >= 10 else (
            "high" if len(ext_caller_files) >= 3 or len(prod_importers) >= 5 else "medium"
        )
        parent_cls = nd.get("parent_class")
        qname = f"{parent_cls}.{name}" if parent_cls and not name.startswith(f"{parent_cls}.") else name
        hubs.append({
            "node_id": nid,
            "symbol": qname,
            "kind": "class" if ntype == "cls" else "function",
            "rel_path": rel,
            "signature": nd.get("signature", qname),
            "external_caller_files": len(ext_caller_files),
            "external_call_sites": ext_call_sites,
            "file_importers": len(prod_importers),
            "fan_in_score": fan_in_score,
            "risk": risk,
        })

    hubs.sort(key=lambda h: (-h["fan_in_score"], -h["external_caller_files"], h["node_id"]))
    return hubs[:top_k]


def learn_repository_governance(
    brain: "ProjectBrain",
    *,
    apply: bool = False,
    pin_hubs: bool = False,
    max_rules: int = 12,
    days: int = 180,
    min_cochanges: int = 3,
    min_confidence: float = 0.70,
) -> dict:
    """Discover Day-1 architectural `--forbid` boundaries, git co-change contracts, and hub symbols."""
    brain._ensure_fresh()

    proposed_boundaries = discover_boundary_rules(brain, max_rules=max_rules)
    proposed_cochanges = mine_co_change_contracts(
        brain,
        days=days,
        min_commits=min_cochanges,
        min_confidence=min_confidence,
    )
    hub_symbols = discover_hub_symbols(brain, top_k=10)

    applied_boundaries = 0
    applied_cochanges = 0
    applied_hub_notes = 0

    if apply:
        for br in proposed_boundaries:
            brain._memory.add_boundary_rule(
                br["src_glob"],
                br["dst_glob"],
                reason=br["reason"],
            )
            applied_boundaries += 1
        for cc in proposed_cochanges:
            brain._memory.add_co_change_rule(
                cc["file_a"],
                cc["file_b"],
                bidirectional=bool(cc["bidirectional"]),
                confidence=float(cc["confidence"]),
                co_change_count=int(cc["co_change_count"]),
                reason=cc["reason"],
            )
            applied_cochanges += 1
        if pin_hubs:
            for hub in hub_symbols[:5]:
                nid = hub["node_id"]
                ast_h = brain._ast_hashes.get(nid, "")
                brain._memory.remember(
                    nid,
                    (
                        f"High-fan-in architectural hub ({hub['external_caller_files']} caller file(s), "
                        f"{hub['file_importers']} importer(s)) — verify signature & return shape stability."
                    ),
                    rel_path=hub["rel_path"],
                    ast_hash=ast_h,
                    tags=["learned-hub"],
                )
                applied_hub_notes += 1

    return {
        "status": "applied" if apply else "dry_run_preview",
        "applied": bool(apply),
        "governance_file": str(brain._memory.path),
        "discovered": {
            "boundary_rules": proposed_boundaries,
            "boundary_rules_count": len(proposed_boundaries),
            "co_change_rules": proposed_cochanges,
            "co_change_rules_count": len(proposed_cochanges),
            "hub_symbols": hub_symbols,
            "hub_symbols_count": len(hub_symbols),
        },
        "persisted_counts": {
            "boundary_rules_saved": applied_boundaries,
            "co_change_rules_saved": applied_cochanges,
            "hub_invariants_saved": applied_hub_notes,
            "total_boundary_rules": len(brain._memory._boundary_rules),
            "total_co_change_rules": len(brain._memory._co_change_rules),
            "total_invariants": len(brain._memory._notes),
        },
    }


def format_learn_report(res: dict) -> str:
    """Format `learn_repository_governance()` output for human-readable CLI onboarding."""
    applied = bool(res.get("applied"))
    disc = res.get("discovered", {})
    persisted = res.get("persisted_counts", {})
    b_rules: list[dict] = disc.get("boundary_rules", [])
    c_rules: list[dict] = disc.get("co_change_rules", [])
    hubs: list[dict] = disc.get("hub_symbols", [])

    mode_lbl = "APPLIED TO .projectbrain_memory.json" if applied else "DRY-RUN PREVIEW"
    lines: list[str] = [
        f"ProjectBrain Architecture & Governance Discovery [{mode_lbl}]",
        "=" * 68,
    ]

    lines.append(f"\n1. Zero-Violation Architectural Boundaries ({len(b_rules)} discovered):")
    if b_rules:
        for br in b_rules:
            lines.append(f"  [FORBID] {br['rule']}  ({br['reason']})")
    else:
        lines.append("  (No new directional layer boundaries discovered beyond existing rules)")

    lines.append(f"\n2. Git History Co-Change Contracts ({len(c_rules)} discovered):")
    if c_rules:
        for cc in c_rules:
            lines.append(f"  [CO-CHANGE] {cc['rule']}  [{cc['co_change_pct']}% | {cc['co_change_count']} commits]")
    else:
        lines.append("  (No high-confidence multi-commit co-change pairs met threshold)")

    lines.append(f"\n3. High-Fan-In Hub Symbols ({len(hubs)} discovered):")
    if hubs:
        for h in hubs[:8]:
            lines.append(
                f"  [HUB:{h['risk'].upper()}] {h['rel_path']} :: {h['symbol']} "
                f"({h['external_caller_files']} caller files, {h['file_importers']} importers)"
            )
    else:
        lines.append("  (No cross-file hub symbols detected)")

    lines.append("")
    if applied:
        lines.append(
            f"Saved {persisted.get('boundary_rules_saved', 0)} boundary rule(s), "
            f"{persisted.get('co_change_rules_saved', 0)} co-change rule(s), and "
            f"{persisted.get('hub_invariants_saved', 0)} hub invariant(s) to {res.get('governance_file')}."
        )
    else:
        lines.append("Tip: Run `projectbrain learn --apply` to activate these rules in `.projectbrain_memory.json`.")
    return "\n".join(lines)
