from projectbrain.graph import CodeGraph, file_id, fn_id, cls_id, route_id, mod_id


def _edge(g, src, dst, etype):
    for e in g.edges:
        if e["src"] == src and e["dst"] == dst and e["type"] == etype:
            return e
    return None


def _sample_index():
    index = {
        "a.py": {
            "functions": [
                {"name": "caller", "line_start": 1, "line_end": 3,
                 "calls": ["helper", "unmatched", "local_helper"]},
                {"name": "local_helper", "line_start": 4, "line_end": 5, "calls": []},
            ],
            "classes": [],
            "imports": ["b", "c"],
            "routes": [{"method": "GET", "path": "/x", "handler": "caller", "line": 1}],
        },
        "b.py": {
            "functions": [{"name": "helper", "line_start": 1, "line_end": 2, "calls": []}],
            "classes": [], "imports": [], "routes": [],
        },
        "c.py": {
            "functions": [{"name": "helper", "line_start": 1, "line_end": 2, "calls": []}],
            "classes": [], "imports": [], "routes": [],
        },
        "d.py": {
            "functions": [{"name": "helper", "line_start": 1, "line_end": 2, "calls": []}],
            "classes": [], "imports": [], "routes": [],
        },
        "e.py": {
            "functions": [
                {"name": "m1", "line_start": 2, "line_end": 2, "calls": [],
                 "is_method": True, "parent_class": "Sub"},
            ],
            "classes": [
                {"name": "Sub", "line_start": 1, "line_end": 3, "bases": ["Base"],
                 "methods": ["m1", "ghost"]},
                {"name": "Base", "line_start": 5, "line_end": 6, "bases": [], "methods": []},
            ],
            "imports": [],
            "routes": [],
            "exports": ["Sub"],
        },
    }
    return index


def test_nodes_created_with_expected_ids():
    g = CodeGraph.build(_sample_index())

    assert g.nodes[file_id("a.py")]["type"] == "file"
    assert g.nodes[fn_id("a.py", "caller")]["type"] == "fn"
    assert g.nodes[cls_id("e.py", "Sub")]["type"] == "cls"
    assert g.nodes[route_id("GET", "/x")]["type"] == "route"
    assert g.nodes[mod_id("b")]["type"] == "mod"
    assert g.nodes[fn_id("e.py", "m1", "Sub")]["type"] == "fn"


def test_defines_edges_file_to_fn_and_cls():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}

    assert (file_id("a.py"), fn_id("a.py", "caller"), "defines") in edge_pairs
    assert (file_id("e.py"), cls_id("e.py", "Sub"), "defines") in edge_pairs
    assert (file_id("e.py"), cls_id("e.py", "Base"), "defines") in edge_pairs

    e = _edge(g, file_id("a.py"), fn_id("a.py", "caller"), "defines")
    assert e["confidence"] == "exact"
    assert e["reason"]


def test_has_method_edge_requires_matching_fn_node():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}

    assert (cls_id("e.py", "Sub"), fn_id("e.py", "m1", "Sub"), "has_method") in edge_pairs
    # "ghost" is listed in methods but has no corresponding fn node -> no edge
    ghost_edges = [e for e in g.edges if e["type"] == "has_method" and "ghost" in e["dst"]]
    assert ghost_edges == []

    e = _edge(g, cls_id("e.py", "Sub"), fn_id("e.py", "m1", "Sub"), "has_method")
    assert e["confidence"] == "exact"


def test_calls_edges_capped_at_two_candidates_and_excludes_unmatched():
    g = CodeGraph.build(_sample_index())
    call_edges = [e for e in g.edges if e["type"] == "calls" and e["src"] == fn_id("a.py", "caller")]

    # "helper" matches b.py, c.py, d.py (3 candidates) but only the first 2 get edges
    cross_file = [e for e in call_edges if e["dst"] != fn_id("a.py", "local_helper")]
    assert len(cross_file) == 2
    dsts = {e["dst"] for e in cross_file}
    assert dsts == {fn_id("b.py", "helper"), fn_id("c.py", "helper")}
    # "unmatched" has zero candidates in the registry -> no fabricated edge
    assert not any("unmatched" in e["dst"] for e in g.edges)

    # cross-file calls are ambiguous name matches -> "candidate" confidence
    for e in cross_file:
        assert e["confidence"] == "candidate"
        assert "ambiguous" in e["reason"]


def test_same_file_call_gets_exact_confidence():
    g = CodeGraph.build(_sample_index())
    e = _edge(g, fn_id("a.py", "caller"), fn_id("a.py", "local_helper"), "calls")
    assert e is not None
    assert e["confidence"] == "exact"
    assert "same-file" in e["reason"]


def test_inherits_edge():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}
    assert (cls_id("e.py", "Sub"), cls_id("e.py", "Base"), "inherits") in edge_pairs

    # Sub and Base are defined in the same file (e.py) -> exact, not candidate
    e = _edge(g, cls_id("e.py", "Sub"), cls_id("e.py", "Base"), "inherits")
    assert e["confidence"] == "exact"


def test_imports_edges_file_to_mod():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}
    assert (file_id("a.py"), mod_id("b"), "imports") in edge_pairs
    assert (file_id("a.py"), mod_id("c"), "imports") in edge_pairs

    e = _edge(g, file_id("a.py"), mod_id("b"), "imports")
    assert e["confidence"] == "exact"


def test_exports_edge_only_for_resolvable_names():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}
    assert (file_id("e.py"), cls_id("e.py", "Sub"), "exports") in edge_pairs

    e = _edge(g, file_id("e.py"), cls_id("e.py", "Sub"), "exports")
    assert e["confidence"] == "exact"


def test_handles_edge_same_file_handler():
    g = CodeGraph.build(_sample_index())
    edge_pairs = {(e["src"], e["dst"], e["type"]) for e in g.edges}
    assert (route_id("GET", "/x"), fn_id("a.py", "caller"), "handles") in edge_pairs

    e = _edge(g, route_id("GET", "/x"), fn_id("a.py", "caller"), "handles")
    assert e["confidence"] == "exact"
    assert e["line"] == 1


def test_handles_edge_cross_file_handler_is_candidate():
    index = _sample_index()
    index["f.py"] = {
        "functions": [],
        "classes": [],
        "imports": [],
        "routes": [{"method": "POST", "path": "/y", "handler": "helper", "line": 9}],
    }
    g = CodeGraph.build(index)

    e = _edge(g, route_id("POST", "/y"), fn_id("b.py", "helper"), "handles")
    assert e is not None
    assert e["confidence"] == "candidate"
    assert e["line"] == 9


def test_calls_edge_promoted_to_import_resolved_when_resolver_confirms_target():
    index = _sample_index()
    # a.py's "caller" calls "helper", ambiguously matching b.py and c.py by name.
    # If a.py's resolved imports say it actually imports b.py, that candidate
    # should be promoted from "candidate" to "import_resolved".
    import_targets = {"a.py": ["b.py"]}
    g = CodeGraph.build(index, import_targets=import_targets)

    resolved_edge = _edge(g, fn_id("a.py", "caller"), fn_id("b.py", "helper"), "calls")
    ambiguous_edge = _edge(g, fn_id("a.py", "caller"), fn_id("c.py", "helper"), "calls")

    assert resolved_edge["confidence"] == "import_resolved"
    assert "resolved via import" in resolved_edge["reason"]
    # c.py is not in a.py's resolved imports -> stays ambiguous, unaffected by the upgrade
    assert ambiguous_edge["confidence"] == "candidate"


def test_calls_edge_without_import_targets_arg_defaults_to_candidate():
    # Backward-compatible: omitting import_targets entirely behaves like Phase 4a.
    g = CodeGraph.build(_sample_index())
    e = _edge(g, fn_id("a.py", "caller"), fn_id("b.py", "helper"), "calls")
    assert e["confidence"] == "candidate"


def test_neighbors_depth_clamped():
    g = CodeGraph.build(_sample_index())

    result_low = g.neighbors(file_id("a.py"), depth=0)
    result_normal = g.neighbors(file_id("a.py"), depth=1)
    assert result_low["nodes"] == result_normal["nodes"]

    # depth=5 should behave the same as the max clamp of 3, not raise or loop forever
    g.neighbors(file_id("a.py"), depth=5)


def test_neighbors_bidirectional_and_edge_type_filter():
    g = CodeGraph.build(_sample_index())

    # b.py has only an incoming "calls" edge (from a.py's caller) - neighbors from
    # b.py's function node should still surface it via the reverse adjacency index.
    result = g.neighbors(fn_id("b.py", "helper"), edge_types=["calls"], depth=1)
    node_ids = {n["id"] for n in result["nodes"]}
    assert fn_id("a.py", "caller") in node_ids

    # filtering to a non-matching edge type should exclude it
    result_filtered = g.neighbors(fn_id("b.py", "helper"), edge_types=["imports"], depth=1)
    assert result_filtered["nodes"] == []


def test_neighbors_unknown_node():
    g = CodeGraph.build(_sample_index())
    result = g.neighbors("file:nope.py")
    assert result == {"nodes": [], "edges": [], "error": "Unknown node: file:nope.py"}


def test_find_path_reverse_edges_get_inverse_prefix():
    g = CodeGraph.build(_sample_index())
    path = g.find_path(fn_id("b.py", "helper"), fn_id("a.py", "caller"))

    assert path
    assert any(e["type"].startswith("inverse_") for e in path)


def test_find_path_same_node_is_empty():
    g = CodeGraph.build(_sample_index())
    assert g.find_path(file_id("a.py"), file_id("a.py")) == []


def test_subgraph_for_files_only_includes_requested_files():
    g = CodeGraph.build(_sample_index())
    sub = g.subgraph_for_files(["a.py", "b.py"])

    rel_paths = {n.get("rel_path") for n in sub["nodes"]}
    assert rel_paths <= {"a.py", "b.py"}
    assert "e.py" not in rel_paths
    for e in sub["edges"]:
        assert e["src"] in {n["id"] for n in sub["nodes"]}
        assert e["dst"] in {n["id"] for n in sub["nodes"]}


def test_find_nodes_by_name_case_insensitive_substring():
    g = CodeGraph.build(_sample_index())
    matches = g.find_nodes_by_name("CALLER")
    assert fn_id("a.py", "caller") in matches


def test_to_json_from_json_round_trip_preserves_traversal():
    g = CodeGraph.build(_sample_index())
    text = g.to_json()
    g2 = CodeGraph.from_json(text)

    assert g2.nodes == g.nodes
    assert g2.edges == g.edges

    original = g.neighbors(fn_id("a.py", "caller"), depth=1)
    restored = g2.neighbors(fn_id("a.py", "caller"), depth=1)
    assert {n["id"] for n in original["nodes"]} == {n["id"] for n in restored["nodes"]}
