"""Symbol-level directed graph for projectbrain.

Nodes: file, fn, cls, route, mod
Edges: defines, imports, calls, inherits, handles, has_method, exports
"""
from __future__ import annotations

import json
from collections import deque


# ── Node ID helpers ─────────────────────────────────────────────────────────

def file_id(rel_path: str) -> str:
    return f"file:{rel_path}"

def fn_id(rel_path: str, name: str, parent_class: str | None = None) -> str:
    qualified = f"{parent_class}.{name}" if parent_class else name
    return f"fn:{rel_path}:{qualified}"

def cls_id(rel_path: str, name: str) -> str:
    return f"cls:{rel_path}:{name}"

def route_id(method: str, path: str) -> str:
    return f"route:{method}:{path}"

def mod_id(name: str) -> str:
    return f"mod:{name}"


# ── CodeGraph ────────────────────────────────────────────────────────────────

class CodeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._adj:  dict[str, list[int]] = {}   # src → edge indices
        self._radj: dict[str, list[int]] = {}   # dst → edge indices

    # ── construction ─────────────────────────────────────────────────────────

    def _build_index(self) -> None:
        self._adj = {}
        self._radj = {}
        for i, edge in enumerate(self.edges):
            self._adj.setdefault(edge["src"], []).append(i)
            self._radj.setdefault(edge["dst"], []).append(i)

    def _add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: str = "unknown",
        reason: str = "",
        line: int | None = None,
    ) -> None:
        edge = {"src": src, "dst": dst, "type": etype, "confidence": confidence, "reason": reason}
        if line is not None:
            edge["line"] = line
        self.edges.append(edge)

    @classmethod
    def build(
        cls,
        index: dict,
        import_targets: dict[str, list[str]] | None = None,
        symbol_imports: dict[str, dict[str, str]] | None = None,
    ) -> "CodeGraph":
        g = cls()
        import_targets = import_targets or {}
        symbol_imports = symbol_imports or {}
        registry: dict[str, list[str]] = {}  # name → [node_id, ...]

        def _reg(name: str, node_id: str) -> None:
            registry.setdefault(name, []).append(node_id)

        # ── Pass 1: nodes ────────────────────────────────────────────────────
        for rel_path, info in index.items():
            fid = file_id(rel_path)
            g.nodes[fid] = {"type": "file", "rel_path": rel_path}

            for fn in info.get("functions", []):
                parent = fn.get("parent_class")
                nid = fn_id(rel_path, fn["name"], parent)
                g.nodes[nid] = {
                    "type": "fn",
                    "rel_path": rel_path,
                    "name": fn["name"],
                    "signature": fn.get("signature", ""),
                    "line_start": fn.get("line_start"),
                    "line_end": fn.get("line_end"),
                    "is_method": fn.get("is_method", False),
                    "parent_class": parent,
                    "ast_hash": fn.get("ast_hash", ""),
                }
                _reg(fn["name"], nid)
                if parent:
                    _reg(f"{parent}.{fn['name']}", nid)

            for c in info.get("classes", []):
                nid = cls_id(rel_path, c["name"])
                g.nodes[nid] = {
                    "type": "cls",
                    "rel_path": rel_path,
                    "name": c["name"],
                    "signature": c.get("signature", ""),
                    "line_start": c.get("line_start"),
                    "line_end": c.get("line_end"),
                    "fields": c.get("fields", []),
                    "ast_hash": c.get("ast_hash", ""),
                }
                _reg(c["name"], nid)

            for r in info.get("routes", []):
                nid = route_id(r["method"], r["path"])
                g.nodes[nid] = {
                    "type": "route",
                    "rel_path": rel_path,
                    "method": r["method"],
                    "path": r["path"],
                    "handler": r.get("handler"),
                    "line": r.get("line"),
                }

            for imp in info.get("imports", []):
                nid = mod_id(imp)
                if nid not in g.nodes:
                    g.nodes[nid] = {"type": "mod", "name": imp}

        # ── Pass 2: edges ────────────────────────────────────────────────────
        for rel_path, info in index.items():
            fid = file_id(rel_path)
            file_imports = set(import_targets.get(rel_path, []))
            file_sym_map = symbol_imports.get(rel_path, {})

            for fn in info.get("functions", []):
                parent = fn.get("parent_class")
                nid = fn_id(rel_path, fn["name"], parent)
                g._add_edge(fid, nid, "defines", "exact", "direct definition in file")

                call_sites = fn.get("call_sites", {})
                qualified_calls = fn.get("qualified_calls", {})
                added_call_dsts: set[str] = set()

                # 1. Resolve receiver-qualified calls first (e.g. self.foo(), ClassName.foo(), mod.foo())
                for qual_expr, q_line in qualified_calls.items():
                    parts = qual_expr.split(".")
                    if len(parts) < 2:
                        continue
                    receiver = parts[0]
                    callee_name = parts[-1]

                    # Case A: `self.method()` or `this.method()` inside a class method
                    if receiver in ("self", "this", "cls") and parent:
                        same_cls_id = fn_id(rel_path, callee_name, parent)
                        if same_cls_id in g.nodes and same_cls_id != nid:
                            g._add_edge(
                                nid, same_cls_id, "calls", "exact",
                                f"'{qual_expr}()' resolved to method on enclosing class {parent}",
                                line=q_line,
                            )
                            added_call_dsts.add(same_cls_id)
                            continue

                    # Case B: `ReceiverClass.method()` or `receiver_instance.method()`
                    # Check if receiver is an imported module/symbol
                    if receiver in file_sym_map:
                        target_file = file_sym_map[receiver]
                        # Could be `module.func()` or `ImportedClass.method()`
                        direct_fn = fn_id(target_file, callee_name)
                        qual_fn = fn_id(target_file, callee_name, receiver)
                        if qual_fn in g.nodes and qual_fn != nid:
                            g._add_edge(
                                nid, qual_fn, "calls", "import_resolved",
                                f"'{qual_expr}()' resolved via imported class '{receiver}' from {target_file}",
                                line=q_line,
                            )
                            added_call_dsts.add(qual_fn)
                            continue
                        if direct_fn in g.nodes and direct_fn != nid:
                            g._add_edge(
                                nid, direct_fn, "calls", "import_resolved",
                                f"'{qual_expr}()' resolved via imported module '{receiver}' from {target_file}",
                                line=q_line,
                            )
                            added_call_dsts.add(direct_fn)
                            continue

                    # Case C: Qualified match by class name (`ClassName.method` in registry)
                    qual_candidates = registry.get(f"{receiver}.{callee_name}", [])
                    if len(qual_candidates) == 1:
                        cand = qual_candidates[0]
                        if cand != nid and cand not in added_call_dsts:
                            cand_file = g.nodes.get(cand, {}).get("rel_path")
                            conf = "exact" if cand_file == rel_path else (
                                "import_resolved" if cand_file in file_imports else "qualified"
                            )
                            g._add_edge(
                                nid, cand, "calls", conf,
                                f"'{qual_expr}()' matched uniquely to {receiver}.{callee_name}",
                                line=q_line,
                            )
                            added_call_dsts.add(cand)

                # 2. Resolve bare/remaining calls
                for callee_name in fn.get("calls", []):
                    line = call_sites.get(callee_name)
                    candidates = [c for c in registry.get(callee_name, []) if c != nid]
                    if not candidates:
                        continue

                    same_file = [
                        cand for cand in candidates
                        if g.nodes.get(cand, {}).get("rel_path") == rel_path
                    ]
                    cross_file = [
                        cand for cand in candidates
                        if g.nodes.get(cand, {}).get("rel_path") != rel_path
                    ]

                    for cand in same_file:
                        if cand not in added_call_dsts:
                            g._add_edge(nid, cand, "calls", "exact", "same-file function call", line=line)
                            added_call_dsts.add(cand)

                    cross_file.sort(
                        key=lambda c: (
                            0
                            if (
                                g.nodes.get(c, {}).get("rel_path") in file_imports
                                or file_sym_map.get(callee_name) == g.nodes.get(c, {}).get("rel_path")
                            )
                            else 1
                        )
                    )
                    for cand in cross_file[:2]:
                        if cand in added_call_dsts:
                            continue
                        cand_rel_path = g.nodes.get(cand, {}).get("rel_path")
                        if cand_rel_path in file_imports or file_sym_map.get(callee_name) == cand_rel_path:
                            g._add_edge(
                                nid, cand, "calls", "import_resolved",
                                f"'{callee_name}' resolved via import from {cand_rel_path}",
                                line=line,
                            )
                        else:
                            g._add_edge(
                                nid, cand, "calls", "candidate",
                                f"'{callee_name}' matches {len(candidates)} symbol(s) by "
                                "name; target ambiguous without import resolution",
                                line=line,
                            )
                        added_call_dsts.add(cand)

            for c in info.get("classes", []):
                cid = cls_id(rel_path, c["name"])
                g._add_edge(fid, cid, "defines", "exact", "direct definition in file")

                # has_method edges
                for method_name in c.get("methods", []):
                    mid = fn_id(rel_path, method_name, c["name"])
                    if mid in g.nodes:
                        g._add_edge(
                            cid, mid, "has_method", "exact",
                            "method defined directly in class body",
                        )

                # inherits edges
                for base_name in c.get("bases", []):
                    candidates = [cand for cand in registry.get(base_name, []) if cand != cid]
                    for cand in candidates[:2]:
                        cand_rel_path = g.nodes.get(cand, {}).get("rel_path")
                        if cand_rel_path == rel_path:
                            g._add_edge(cid, cand, "inherits", "exact", "same-file base class")
                        elif cand_rel_path in file_imports or file_sym_map.get(base_name) == cand_rel_path:
                            g._add_edge(
                                cid, cand, "inherits", "import_resolved",
                                f"'{base_name}' resolved via import from {cand_rel_path}",
                            )
                        else:
                            g._add_edge(
                                cid, cand, "inherits", "candidate",
                                f"'{base_name}' matches {len(candidates)} class(es) by name; "
                                "target ambiguous without import resolution",
                            )

            for imp in info.get("imports", []):
                g._add_edge(fid, mod_id(imp), "imports", "exact", "import statement in file")

            for exp_name in info.get("exports", []):
                for try_id in (fn_id(rel_path, exp_name), cls_id(rel_path, exp_name)):
                    if try_id in g.nodes:
                        g._add_edge(
                            fid, try_id, "exports", "exact",
                            "named export resolved to same-file definition",
                        )
                        break

            for r in info.get("routes", []):
                rid = route_id(r["method"], r["path"])
                handler = r.get("handler")
                if handler:
                    local = fn_id(rel_path, handler)
                    if local in g.nodes:
                        g._add_edge(
                            rid, local, "handles", "exact",
                            "handler function defined in same file as route decorator",
                            line=r.get("line"),
                        )
                    else:
                        for cand in registry.get(handler, [])[:1]:
                            cand_file = g.nodes.get(cand, {}).get("rel_path")
                            conf = "import_resolved" if cand_file in file_imports else "candidate"
                            g._add_edge(
                                rid, cand, "handles", conf,
                                f"handler '{handler}' matched in {cand_file}",
                                line=r.get("line"),
                            )

        g._build_index()
        g._propagate_interprocedural_effects(index)
        return g

    def _propagate_interprocedural_effects(self, index: dict) -> None:
        """Pass A: Bind Universal 6D IR to fn nodes & bubble effects/?Z 1-3 hops up the call graph.
        Pass B: Wire cross-function `state_flow` edges (?Z_write -> Z_read) across shared state/event channels.
        """
        _GENERIC_STATE_KEYS = {
            "data", "res", "result", "item", "items", "val", "value", "err", "error",
            "req", "resp", "args", "kwargs", "options", "config", "params", "state",
            "props", "dispatch", "user", "id", "key", "obj", "row", "rows",
        }
        # 1. Bind file 6D cells onto enclosing function nodes
        for rel_path, info in index.items():
            cells = info.get("cells_4d", [])
            if not cells:
                continue
            for fn in info.get("functions", []):
                nid = fn_id(rel_path, fn["name"], fn.get("parent_class"))
                nd = self.nodes.get(nid)
                if not nd:
                    continue
                f_s = fn.get("line_start", 1)
                f_e = fn.get("line_end", f_s)
                fn_cells = [
                    c for c in cells
                    if c.get("line_start", 0) >= f_s and c.get("line_end", 0) <= f_e
                ]
                if not fn_cells:
                    continue
                local_zw: list[str] = []
                local_zr: list[str] = []
                local_exits: list[str] = []
                local_roles: set[str] = set()
                t_modes: set[str] = set()
                for c in fn_cells:
                    local_roles.add(c.get("Y_role", "ACTION"))
                    t_modes.add(c.get("T_mode", "SYNC"))
                    for zw in c.get("Z_write", []):
                        if zw not in local_zw:
                            local_zw.append(zw)
                    for zr in c.get("Z_read", []):
                        if zr not in local_zr and zr not in local_zw:
                            local_zr.append(zr)
                    for ex in c.get("E_exit", []):
                        if ex not in local_exits:
                            local_exits.append(ex)
                local_eff = [r for r in ("IO", "EFFECT", "VIEW") if r in local_roles]
                if local_zw and "MUTATE" not in local_eff:
                    local_eff.append("MUTATE")
                t_mode = next((m for m in ("REACTIVE", "TIMER", "ASYNC") if m in t_modes), "SYNC")
                if local_zw:
                    nd["local_z_write"] = local_zw
                if local_zr:
                    nd["local_z_read"] = local_zr
                if local_exits:
                    nd["exit_contracts"] = local_exits
                if local_eff:
                    nd["local_effects"] = local_eff
                if t_mode != "SYNC":
                    nd["temporal_mode"] = t_mode

        # 2. Pass A: Bottom-Up 3-Hop Interprocedural Effect & ?Z Bubbling along `calls` edges
        for nid, nd in self.nodes.items():
            if nd.get("type") != "fn":
                continue
            own_zw = set(nd.get("local_z_write", []))
            own_exits = set(nd.get("exit_contracts", []))
            trans_zw: list[str] = []
            trans_eff: list[str] = []
            trans_exits: list[str] = []
            provenance: dict[str, str] = {}

            q: deque[tuple[str, int, list[str]]] = deque([(nid, 0, [])])
            visited_callees: set[str] = {nid}
            while q:
                curr_id, hop, chain = q.popleft()
                if hop >= 3:
                    continue
                for ei in self._adj.get(curr_id, []):
                    e = self.edges[ei]
                    if e["type"] != "calls" or e.get("confidence") not in ("exact", "import_resolved"):
                        continue
                    callee_id = e["dst"]
                    if callee_id in visited_callees:
                        continue
                    visited_callees.add(callee_id)
                    callee_nd = self.nodes.get(callee_id, {})
                    callee_name = callee_nd.get("name") or callee_id.split(":")[-1]
                    nxt_chain = chain + [callee_name]
                    nxt_hop = hop + 1
                    via_label = f"via {' -> '.join(nxt_chain)} ({nxt_hop} hop{'s' if nxt_hop > 1 else ''})"

                    for zw in callee_nd.get("local_z_write", []):
                        if zw not in own_zw and zw not in trans_zw:
                            trans_zw.append(zw)
                            provenance[f"?Z:{zw}"] = via_label
                    for eff in callee_nd.get("local_effects", []):
                        if eff not in trans_eff:
                            trans_eff.append(eff)
                            provenance.setdefault(f"effect:{eff}", via_label)
                    for ex in callee_nd.get("exit_contracts", []):
                        if ex.startswith("throw:") and ex not in own_exits and ex not in trans_exits:
                            trans_exits.append(ex)
                            provenance[ex] = via_label

                    q.append((callee_id, nxt_hop, nxt_chain))

            if trans_zw:
                nd["transitive_z_write"] = trans_zw[:12]
            if trans_eff:
                nd["transitive_effects"] = trans_eff
            if trans_exits:
                nd["transitive_exits"] = trans_exits[:8]
            if provenance:
                nd["effect_provenance"] = provenance

        # 3. Pass B: Cross-Function / Cross-File State-Channel Edges (`state_flow`: ?Z_write -> Z_read)
        writers_by_key: dict[str, list[str]] = {}
        readers_by_key: dict[str, list[str]] = {}
        for nid, nd in self.nodes.items():
            if nd.get("type") != "fn":
                continue
            for zw in nd.get("local_z_write", []):
                if len(zw) >= 3 and zw.lower() not in _GENERIC_STATE_KEYS:
                    writers_by_key.setdefault(zw, []).append(nid)
            for zr in nd.get("local_z_read", []):
                if len(zr) >= 3 and zr.lower() not in _GENERIC_STATE_KEYS:
                    readers_by_key.setdefault(zr, []).append(nid)

        added_state_edges = 0
        seen_state_pairs: set[tuple[str, str, str]] = set()
        for k, writers in writers_by_key.items():
            readers = readers_by_key.get(k, [])
            if not readers or len(writers) > 8 or len(readers) > 12:
                continue
            for w_id in writers:
                for r_id in readers:
                    if w_id != r_id and (w_id, r_id, k) not in seen_state_pairs:
                        seen_state_pairs.add((w_id, r_id, k))
                        self._add_edge(
                            w_id,
                            r_id,
                            "state_flow",
                            "exact",
                            f"shared state channel '{k}' (?Z_write -> Z_read)",
                        )
                        added_state_edges += 1
        if added_state_edges > 0:
            self._build_index()
    # ── traversal ────────────────────────────────────────────────────────────

    def neighbors(
        self,
        node_id: str,
        edge_types: list[str] | None = None,
        depth: int = 1,
    ) -> dict:
        depth = min(max(depth, 1), 3)
        if node_id not in self.nodes:
            return {"nodes": [], "edges": [], "error": f"Unknown node: {node_id}"}

        visited_nodes: set[str] = {node_id}
        visited_edge_idx: set[int] = set()
        frontier: set[str] = {node_id}

        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                for ei in self._adj.get(nid, []):
                    edge = self.edges[ei]
                    if edge_types and edge["type"] not in edge_types:
                        continue
                    visited_edge_idx.add(ei)
                    if edge["dst"] not in visited_nodes:
                        next_frontier.add(edge["dst"])
                for ei in self._radj.get(nid, []):
                    edge = self.edges[ei]
                    if edge_types and edge["type"] not in edge_types:
                        continue
                    visited_edge_idx.add(ei)
                    if edge["src"] not in visited_nodes:
                        next_frontier.add(edge["src"])
            visited_nodes.update(next_frontier)
            frontier = next_frontier

        visited_nodes.discard(node_id)
        return {
            "center": {"id": node_id, **self.nodes[node_id]},
            "nodes": [{"id": nid, **self.nodes[nid]} for nid in visited_nodes if nid in self.nodes],
            "edges": [self.edges[i] for i in visited_edge_idx],
        }

    def find_path(self, from_id: str, to_id: str) -> list[dict]:
        """BFS shortest path; follows edges in both directions."""
        if from_id not in self.nodes or to_id not in self.nodes:
            return []
        if from_id == to_id:
            return []

        queue: deque = deque([(from_id, [])])
        visited: set[str] = {from_id}

        while queue:
            current, path = queue.popleft()
            if current == to_id:
                return path

            for ei in self._adj.get(current, []):
                edge = self.edges[ei]
                nxt = edge["dst"]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, path + [edge]))

            for ei in self._radj.get(current, []):
                edge = self.edges[ei]
                nxt = edge["src"]
                if nxt not in visited:
                    visited.add(nxt)
                    rev = {"src": edge["dst"], "dst": edge["src"], "type": f"inverse_{edge['type']}"}
                    queue.append((nxt, path + [rev]))

        return []

    def find_directed_path(
        self, from_id: str, to_id: str, edge_types: set[str] | None = None
    ) -> list[dict]:
        """BFS shortest path following edges in the FORWARD direction only."""
        if from_id not in self.nodes or to_id not in self.nodes or from_id == to_id:
            return []
        queue: deque = deque([(from_id, [])])
        visited: set[str] = {from_id}
        while queue:
            current, path = queue.popleft()
            for ei in self._adj.get(current, []):
                edge = self.edges[ei]
                if edge_types and edge["type"] not in edge_types:
                    continue
                nxt = edge["dst"]
                if nxt == to_id:
                    return path + [edge]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, path + [edge]))
        return []

    def subgraph_for_files(self, rel_paths: list[str]) -> dict:
        file_set = set(rel_paths)
        relevant: set[str] = {
            nid for nid, nd in self.nodes.items()
            if nd.get("rel_path") in file_set
        }
        edges = [e for e in self.edges if e["src"] in relevant and e["dst"] in relevant]
        return {
            "nodes": [{"id": nid, **self.nodes[nid]} for nid in relevant],
            "edges": edges,
        }

    def find_nodes_by_name(self, name: str) -> list[str]:
        name_lower = name.lower().strip()
        exact_matches = []
        partial_matches = []
        for node_id, nd in self.nodes.items():
            nd_name = (nd.get("name") or nd.get("path") or nd.get("rel_path") or "").lower()
            route_label = f"{nd.get('method', '')} {nd.get('path', '')}".strip().lower()
            if nd_name == name_lower or node_id.lower() == name_lower or (route_label and route_label == name_lower):
                exact_matches.append(node_id)
                continue
            searchable = " ".join([
                node_id,
                nd.get("name", ""),
                nd.get("rel_path", ""),
                nd.get("path", ""),
                route_label,
            ]).lower()
            if name_lower in searchable:
                partial_matches.append(node_id)
        return exact_matches + partial_matches

    # ── serialization ────────────────────────────────────────────────────────

    def to_json(self) -> str:
        return json.dumps({"nodes": self.nodes, "edges": self.edges}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CodeGraph":
        data = json.loads(text)
        g = cls()
        g.nodes = data["nodes"]
        g.edges = data["edges"]
        g._build_index()
        return g
