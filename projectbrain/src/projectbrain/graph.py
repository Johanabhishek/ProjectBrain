"""Symbol-level directed graph for projectbrain.

Nodes: file, fn, cls, route, mod
Edges: defines, imports, calls, inherits, handles, has_method, exports
"""
from __future__ import annotations

import hashlib
import json
import re
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
        self._registry: dict[str, list[str]] = {}
        self._by_file_name: dict[tuple[str, str], list[str]] = {}
        self._nodes_by_file: dict[str, list[str]] = {}

    # ── construction ─────────────────────────────────────────────────────────

    def _build_index(self) -> None:
        self._adj = {}
        self._radj = {}
        for i, edge in enumerate(self.edges):
            self._adj.setdefault(edge["src"], []).append(i)
            self._radj.setdefault(edge["dst"], []).append(i)

    def _ensure_registries(self) -> None:
        if self._registry and self._nodes_by_file:
            return
        self._registry = {}
        self._by_file_name = {}
        self._nodes_by_file = {}
        for nid, nd in self.nodes.items():
            rpath = nd.get("rel_path", "")
            if rpath:
                self._nodes_by_file.setdefault(rpath, []).append(nid)
            ntype = nd.get("type")
            nm = nd.get("name", "")
            if ntype == "fn" and nm:
                self._registry.setdefault(nm, []).append(nid)
                if rpath:
                    self._by_file_name.setdefault((rpath, nm), []).append(nid)
                parent = nd.get("parent_class")
                if parent:
                    qnm = f"{parent}.{nm}"
                    self._registry.setdefault(qnm, []).append(nid)
                    if rpath:
                        self._by_file_name.setdefault((rpath, qnm), []).append(nid)
            elif ntype == "cls" and nm:
                self._registry.setdefault(nm, []).append(nid)
                if rpath:
                    self._by_file_name.setdefault((rpath, nm), []).append(nid)

    def _add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: str = "unknown",
        reason: str = "",
        line: int | None = None,
        lines: list[int] | None = None,
    ) -> None:
        edge = {"src": src, "dst": dst, "type": etype, "confidence": confidence, "reason": reason}
        if line is not None:
            edge["line"] = line
        if lines and len(lines) > 1:
            edge["lines"] = lines[:8]
        self.edges.append(edge)

    def _populate_file_nodes(self, rel_path: str, info: dict) -> None:
        def _reg(name: str, node_id: str) -> None:
            self._registry.setdefault(name, []).append(node_id)
            self._by_file_name.setdefault((rel_path, name), []).append(node_id)

        f_nids: list[str] = []
        fid = file_id(rel_path)
        self.nodes[fid] = {"type": "file", "rel_path": rel_path}
        f_nids.append(fid)

        for fn in info.get("functions", []):
            parent = fn.get("parent_class")
            nid = fn_id(rel_path, fn["name"], parent)
            raw_cs = ",".join(fn.get("calls", ())) + "|" + ",".join(sorted((fn.get("qualified_calls") or {}).keys()))
            calls_sig = hashlib.blake2b(raw_cs.encode("utf-8"), digest_size=6).hexdigest()
            fn_nd: dict = {
                "type": "fn",
                "rel_path": rel_path,
                "name": fn["name"],
                "signature": fn.get("signature", ""),
                "line_start": fn.get("line_start"),
                "line_end": fn.get("line_end"),
                "is_method": fn.get("is_method", False),
                "parent_class": parent,
                "interface_hash": fn.get("interface_hash", ""),
                "ast_hash": fn.get("ast_hash", ""),
                "calls_sig": calls_sig,
            }
            if fn.get("return_keys"):
                fn_nd["return_keys"] = fn["return_keys"]
            if fn.get("has_local_state"):
                fn_nd["has_local_state"] = True
            if fn.get("http_wrapper_spec"):
                fn_nd["http_wrapper_spec"] = fn["http_wrapper_spec"]
            if fn.get("http_transport_spec"):
                fn_nd["http_transport_spec"] = fn["http_transport_spec"]
            if fn.get("http_pending_transport_call"):
                fn_nd["http_pending_transport_call"] = fn["http_pending_transport_call"]
            self.nodes[nid] = fn_nd
            f_nids.append(nid)
            _reg(fn["name"], nid)
            if parent:
                _reg(f"{parent}.{fn['name']}", nid)

        for c in info.get("classes", []):
            nid = cls_id(rel_path, c["name"])
            cls_nd: dict = {
                "type": "cls",
                "rel_path": rel_path,
                "name": c["name"],
                "signature": c.get("signature", ""),
                "line_start": c.get("line_start"),
                "line_end": c.get("line_end"),
                "bases": c.get("bases", []),
                "fields": c.get("fields", []),
                "interface_hash": c.get("interface_hash", ""),
                "ast_hash": c.get("ast_hash", ""),
            }
            if c.get("schema_spec"):
                cls_nd["schema_spec"] = c["schema_spec"]
            self.nodes[nid] = cls_nd
            f_nids.append(nid)
            _reg(c["name"], nid)

        for r in info.get("routes", []):
            eff_path = r.get("full_path") or r["path"]
            nid = route_id(r["method"], eff_path)
            r_nd: dict = {
                "type": "route",
                "rel_path": rel_path,
                "method": r["method"],
                "path": r["path"],
                "full_path": eff_path,
                "handler": r.get("handler"),
                "line": r.get("line"),
            }
            for rk_opt in (
                "body_schema",
                "body_param",
                "body_is_dynamic",
                "multi_body_keys",
                "response_schema",
                "response_is_list",
                "response_is_dynamic",
                "required_query_params",
            ):
                if rk_opt in r:
                    r_nd[rk_opt] = r[rk_opt]
            self.nodes[nid] = r_nd
            f_nids.append(nid)
        for imp in info.get("imports", []):
            nid = mod_id(imp)
            if nid not in self.nodes:
                self.nodes[nid] = {"type": "mod", "name": imp}
        self._nodes_by_file[rel_path] = f_nids

    def _populate_file_edges(
        self,
        rel_path: str,
        info: dict,
        import_targets: dict[str, list[str]],
        symbol_imports: dict[str, dict[str, str]],
    ) -> None:
        registry = self._registry
        by_file_name = self._by_file_name
        fid = file_id(rel_path)
        file_imports = set(import_targets.get(rel_path, []))
        file_sym_map = symbol_imports.get(rel_path, {})
        has_star_import = (
            any(k.startswith("__star__") for k in (info.get("import_bindings") or {}))
            or ("import_bindings" not in info and not file_sym_map)
        )

        for fn in info.get("functions", []):
            parent = fn.get("parent_class")
            nid = fn_id(rel_path, fn["name"], parent)
            self._add_edge(fid, nid, "defines", "exact", "direct definition in file")

            call_sites = fn.get("call_sites", {})
            qualified_calls = fn.get("qualified_calls", {})
            param_types = fn.get("param_types", {})
            added_call_dsts: set[str] = set()
            qual_receivers_for_callee: dict[str, set[str]] = {}
            resolved_qual_callees: set[str] = set()

            # 1. Resolve receiver-qualified calls first (e.g. self.foo(), store.save(), ClassName.foo(), mod.foo())
            for qual_expr, q_line in qualified_calls.items():
                parts = qual_expr.split(".")
                if len(parts) < 2:
                    continue
                receiver = parts[0]
                recv_prefix = ".".join(parts[:-1])
                callee_name = parts[-1]
                qual_receivers_for_callee.setdefault(callee_name, set()).update((receiver, recv_prefix))

                # Case A: `self.method()`, `this.method()`, `cls.method()`, or `super().method()` inside a class method
                if receiver in ("self", "this", "cls", "super") and parent and len(parts) == 2:
                    if receiver != "super":
                        same_cls_id = fn_id(rel_path, callee_name, parent)
                        if same_cls_id in self.nodes:
                            resolved_qual_callees.add(callee_name)
                            if same_cls_id != nid:
                                self._add_edge(
                                    nid, same_cls_id, "calls", "exact",
                                    f"'{qual_expr}()' resolved to method on enclosing class {parent}",
                                    line=q_line,
                                )
                                added_call_dsts.add(same_cls_id)
                            continue
                    # Check base classes of `parent` (1-2 levels of inheritance)
                    parent_cls_nd = self.nodes.get(cls_id(rel_path, parent), {})
                    base_queue = list(parent_cls_nd.get("bases", []))
                    resolved_base = False
                    for b_raw in base_queue[:6]:
                        b_name = b_raw.split(".")[-1].split("[")[0].strip()
                        for b_cand in registry.get(f"{b_name}.{callee_name}", []):
                            if b_cand != nid and b_cand not in added_call_dsts:
                                b_rel = self.nodes.get(b_cand, {}).get("rel_path")
                                b_conf = "exact" if b_rel == rel_path else "import_resolved"
                                self._add_edge(
                                    nid, b_cand, "calls", b_conf,
                                    f"'{qual_expr}()' resolved to inherited method on base class {b_name}",
                                    line=q_line,
                                )
                                added_call_dsts.add(b_cand)
                                resolved_qual_callees.add(callee_name)
                                resolved_base = True
                                break
                        if resolved_base:
                            break
                    if resolved_base:
                        continue

                # Case A2: Statically typed parameter or constructor-assigned local/field receiver (`store: SQLiteStore`, `self.db = DB()`)
                recv_type = param_types.get(recv_prefix) or param_types.get(receiver)
                if recv_type:
                    typed_cands = registry.get(f"{recv_type}.{callee_name}", [])
                    if typed_cands:
                        t_cand = typed_cands[0]
                        if t_cand != nid and t_cand not in added_call_dsts:
                            t_rel = self.nodes.get(t_cand, {}).get("rel_path")
                            t_conf = "exact" if t_rel == rel_path else "import_resolved"
                            self._add_edge(
                                nid, t_cand, "calls", t_conf,
                                f"'{qual_expr}()' resolved via static receiver type '{recv_type}'",
                                line=q_line,
                            )
                            added_call_dsts.add(t_cand)
                            resolved_qual_callees.add(callee_name)
                            continue

                # Case B: `ReceiverClass.method()` or `receiver_instance.method()`
                if receiver in file_sym_map:
                    target_file = file_sym_map[receiver]
                    direct_fn = fn_id(target_file, callee_name)
                    qual_fn = fn_id(target_file, callee_name, receiver)
                    if qual_fn in self.nodes and qual_fn != nid:
                        self._add_edge(
                            nid, qual_fn, "calls", "import_resolved",
                            f"'{qual_expr}()' resolved via imported class '{receiver}' from {target_file}",
                            line=q_line,
                        )
                        added_call_dsts.add(qual_fn)
                        continue
                    r_bind = (info.get("import_bindings") or {}).get(receiver) or {}
                    is_obj_from_same_named_mod = (
                        r_bind.get("symbol") not in (None, "", "*", "default")
                        and r_bind.get("module", "").split(".")[-1] == r_bind.get("symbol")
                    )
                    if direct_fn in self.nodes and direct_fn != nid and not is_obj_from_same_named_mod:
                        self._add_edge(
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
                        cand_file = self.nodes.get(cand, {}).get("rel_path")
                        conf = "exact" if cand_file == rel_path else (
                            "import_resolved" if cand_file in file_imports else "qualified"
                        )
                        self._add_edge(
                            nid, cand, "calls", conf,
                            f"'{qual_expr}()' matched uniquely to {receiver}.{callee_name}",
                            line=q_line,
                        )
                        added_call_dsts.add(cand)
                        resolved_qual_callees.add(callee_name)

            # 2. Resolve bare/remaining calls in O(1) same-file + O(|imports|) cross-file time
            call_sites_all = fn.get("call_sites_all", {})
            for callee_name in fn.get("calls", []):
                all_cands = registry.get(callee_name)
                if not all_cands:
                    continue
                line = call_sites.get(callee_name)
                all_lines = call_sites_all.get(callee_name)

                q_recvs = qual_receivers_for_callee.get(callee_name, set())
                only_self_recv = bool(q_recvs) and q_recvs <= {"self", "this", "cls", "super"}

                same_file_matched = False
                for cand in by_file_name.get((rel_path, callee_name), ()):
                    if cand == nid:
                        continue
                    if cand in added_call_dsts:
                        same_file_matched = True
                        continue
                    cand_parent = self.nodes.get(cand, {}).get("parent_class")
                    if cand_parent and only_self_recv:
                        if callee_name in resolved_qual_callees:
                            continue
                        cand_bases = [
                            b.split(".")[-1].split("[")[0].strip()
                            for b in self.nodes.get(cls_id(rel_path, cand_parent), {}).get("bases", ())
                        ]
                        if not parent or parent not in cand_bases:
                            continue
                    self._add_edge(nid, cand, "calls", "exact", "same-file function call", line=line, lines=all_lines)
                    added_call_dsts.add(cand)
                    same_file_matched = True

                if same_file_matched and callee_name not in file_sym_map and callee_name not in qual_receivers_for_callee:
                    continue

                imp_cands: list[str] = []
                sym_imp_rel = file_sym_map.get(callee_name)
                if sym_imp_rel and sym_imp_rel != rel_path:
                    for c_imp in by_file_name.get((sym_imp_rel, callee_name), ()):
                        if c_imp != nid and c_imp not in imp_cands:
                            imp_cands.append(c_imp)
                if not imp_cands or callee_name in qual_receivers_for_callee:
                    for imp_rel in file_imports:
                        if imp_rel != rel_path and imp_rel != sym_imp_rel:
                            imp_stem = imp_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                            for c_imp in by_file_name.get((imp_rel, callee_name), ()):
                                if c_imp == nid or c_imp in imp_cands:
                                    continue
                                c_parent = self.nodes.get(c_imp, {}).get("parent")
                                if c_parent is None and not has_star_import:
                                    r_bind = (info.get("import_bindings") or {}).get(imp_stem) or {}
                                    if r_bind.get("module", "").split(".")[-1] == r_bind.get("symbol") == imp_stem:
                                        continue
                                    is_unique_bare = len(all_cands) == 1 and callee_name not in qual_receivers_for_callee
                                    if not is_unique_bare and not any(
                                        r == imp_stem or r.endswith("." + imp_stem) or file_sym_map.get(r) == imp_rel
                                        for r in qual_receivers_for_callee.get(callee_name, ())
                                    ):
                                        continue
                                imp_cands.append(c_imp)

                cross_added = 0
                for cand in imp_cands[:2]:
                    if cand in added_call_dsts:
                        continue
                    cand_rel_path = self.nodes.get(cand, {}).get("rel_path")
                    self._add_edge(
                        nid, cand, "calls", "import_resolved",
                        f"'{callee_name}' resolved via import from {cand_rel_path}",
                        line=line, lines=all_lines,
                    )
                    added_call_dsts.add(cand)
                    cross_added += 1

                if cross_added < 2 and len(all_cands) <= 4:
                    num_other = sum(1 for c0 in all_cands if c0 != nid)
                    for cand in all_cands:
                        if cross_added >= 2:
                            break
                        if cand == nid or cand in added_call_dsts:
                            continue
                        cand_rel_path = self.nodes.get(cand, {}).get("rel_path")
                        if cand_rel_path == rel_path:
                            continue
                        self._add_edge(
                            nid, cand, "calls", "candidate",
                            f"'{callee_name}' matches {num_other} symbol(s) by "
                            "name; target ambiguous without import resolution",
                            line=line, lines=all_lines,
                        )
                        added_call_dsts.add(cand)
                        cross_added += 1
            # 3. Connect lexically nested helper/callback functions
            fn_s = fn.get("line_start", 0)
            fn_e = fn.get("line_end", 0)
            if 0 < (fn_e - fn_s) <= 300:
                fn_calls_set = set(fn.get("calls", []))
                for inner_fn in info.get("functions", []):
                    in_s = inner_fn.get("line_start", 0)
                    in_e = inner_fn.get("line_end", 0)
                    if fn_s < in_s and in_e <= fn_e:
                        in_nid = fn_id(rel_path, inner_fn["name"], inner_fn.get("parent_class"))
                        if in_nid != nid and in_nid in self.nodes and in_nid not in added_call_dsts:
                            if inner_fn["name"] in fn_calls_set:
                                call_ln = fn.get("call_sites", {}).get(inner_fn["name"], in_s)
                                self._add_edge(nid, in_nid, "calls", "exact", "nested helper/callback called in function", line=call_ln)
                            else:
                                self._add_edge(nid, in_nid, "defines_nested", "exact", "nested helper/callback defined in function", line=in_s)
                            added_call_dsts.add(in_nid)

        for c in info.get("classes", []):
            cid = cls_id(rel_path, c["name"])
            self._add_edge(fid, cid, "defines", "exact", "direct definition in file")

            for method_name in c.get("methods", []):
                mid = fn_id(rel_path, method_name, c["name"])
                if mid in self.nodes:
                    self._add_edge(
                        cid, mid, "has_method", "exact",
                        "method defined directly in class body",
                    )

            for base_name in c.get("bases", []):
                candidates = [cand for cand in registry.get(base_name, []) if cand != cid]
                for cand in candidates[:2]:
                    cand_rel_path = self.nodes.get(cand, {}).get("rel_path")
                    if cand_rel_path == rel_path:
                        self._add_edge(cid, cand, "inherits", "exact", "same-file base class")
                    elif cand_rel_path in file_imports or file_sym_map.get(base_name) == cand_rel_path:
                        self._add_edge(
                            cid, cand, "inherits", "import_resolved",
                            f"'{base_name}' resolved via import from {cand_rel_path}",
                        )
                    else:
                        self._add_edge(
                            cid, cand, "inherits", "candidate",
                            f"'{base_name}' matches {len(candidates)} class(es) by name; "
                            "target ambiguous without import resolution",
                        )

        for imp in info.get("imports", []):
            self._add_edge(fid, mod_id(imp), "imports", "exact", "import statement in file")

        for exp_name in info.get("exports", []):
            for try_id in (fn_id(rel_path, exp_name), cls_id(rel_path, exp_name)):
                if try_id in self.nodes:
                    self._add_edge(
                        fid, try_id, "exports", "exact",
                        "named export resolved to same-file definition",
                    )
                    break

        for r in info.get("routes", []):
            eff_path = r.get("full_path") or r["path"]
            rid = route_id(r["method"], eff_path)
            handler = r.get("handler")
            if handler:
                local = fn_id(rel_path, handler)
                if local in self.nodes:
                    self._add_edge(
                        rid, local, "handles", "exact",
                        "handler function defined in same file as route decorator",
                        line=r.get("line"),
                    )
                else:
                    for cand in registry.get(handler, [])[:1]:
                        cand_file = self.nodes.get(cand, {}).get("rel_path")
                        conf = "import_resolved" if cand_file in file_imports else "candidate"
                        self._add_edge(
                            rid, cand, "handles", conf,
                            f"handler '{handler}' matched in {cand_file}",
                            line=r.get("line"),
                        )

    @staticmethod
    def propagate_router_mounts(
        index: dict,
        import_targets: dict[str, list[str]],
        symbol_imports: dict[str, dict[str, str]],
    ) -> None:
        """Resolve cross-file sub-router and sub-application prefix mounts into `route['full_path']`."""
        mount_edges: list[tuple[str, str, str]] = []
        for src_rel, info in index.items():
            mounts = info.get("router_mounts")
            if not mounts:
                continue
            sym_map = symbol_imports.get(src_rel, {})
            imp_rels = import_targets.get(src_rel, [])
            bindings = info.get("import_bindings") or {}
            for m in mounts:
                tgt_expr = m.get("target") or ""
                pref = (m.get("prefix") or "").rstrip("/")
                if not tgt_expr or not pref:
                    continue
                root_id = tgt_expr.split(".")[0]
                target_rel = sym_map.get(root_id)
                if not target_rel and root_id in bindings:
                    b_entry = bindings[root_id] or {}
                    b_mod = (b_entry.get("qualified_module") or b_entry.get("module") or "").strip(".").replace(".", "/")
                    b_sym = (b_entry.get("name") or b_entry.get("symbol") or "").strip()
                    cand_mod_paths: list[str] = []
                    if b_mod and b_sym and b_sym != "*":
                        cand_mod_paths.append(f"{b_mod}/{b_sym}")
                    if b_mod:
                        cand_mod_paths.append(b_mod)
                    search_pool = list(imp_rels) + [rk for rk in index if rk not in imp_rels]
                    for cm in cand_mod_paths:
                        for ir in search_pool:
                            ir_no_ext = ir.rsplit(".", 1)[0]
                            if ir_no_ext == cm or ir_no_ext.endswith("/" + cm) or ir_no_ext.endswith(f"{cm}/__init__"):
                                target_rel = ir
                                break
                        if target_rel:
                            break
                if not target_rel:
                    cleaned = re.sub(r"(?:_?router|_?routes|_?bp)$", "", root_id, flags=re.IGNORECASE).lower()
                    search_pool = list(imp_rels) + [rk for rk, rv in index.items() if rv.get("routes")]
                    for ir in search_pool:
                        stem = ir.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
                        if stem in (root_id.lower(), cleaned) and stem:
                            target_rel = ir
                            break
                if target_rel and target_rel in index and target_rel != src_rel:
                    mount_edges.append((src_rel, target_rel, pref))

        if not mount_edges:
            return

        # Support 2-hop nested routers (e.g. main -> api_v1_router('/api/v1') -> sub_router('/billing'))
        file_prefix: dict[str, str] = {}
        for _ in range(2):
            changed = False
            for src_rel, dst_rel, pref in mount_edges:
                parent_pref = file_prefix.get(src_rel, "")
                combined = f"{parent_pref.rstrip('/')}/{pref.lstrip('/')}".rstrip("/") or "/"
                if file_prefix.get(dst_rel) != combined:
                    file_prefix[dst_rel] = combined
                    changed = True
            if not changed:
                break

        for dst_rel, full_pref in file_prefix.items():
            dst_info = index.get(dst_rel)
            if not dst_info:
                continue
            for r in dst_info.get("routes", []):
                cur_fp = r.get("full_path") or r.get("path") or "/"
                if cur_fp == "/" or cur_fp == "":
                    new_fp = full_pref
                elif not cur_fp.startswith(full_pref + "/") and cur_fp != full_pref:
                    new_fp = f"{full_pref}/{cur_fp.lstrip('/')}".rstrip("/") or "/"
                else:
                    new_fp = cur_fp
                r["full_path"] = new_fp

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
        cls.propagate_router_mounts(index, import_targets, symbol_imports)

        # ── Pass 1: nodes ────────────────────────────────────────────────────
        for rel_path, info in index.items():
            g._populate_file_nodes(rel_path, info)

        # ── Pass 2: edges ────────────────────────────────────────────────────
        for rel_path, info in index.items():
            g._populate_file_edges(rel_path, info, import_targets, symbol_imports)

        g._build_index()
        g._propagate_interprocedural_effects(index)
        return g

    def update_files(
        self,
        changed_rels: set[str],
        deleted_rels: set[str],
        index: dict,
        import_targets: dict[str, list[str]] | None = None,
        symbol_imports: dict[str, dict[str, str]] | None = None,
        imported_by: dict[str, list[str]] | None = None,
        interfaces_unchanged: bool = False,
    ) -> dict:
        """Incrementally update graph nodes, edges, and 6D effects for `changed_rels` and `deleted_rels` in O(Δ) time."""
        import_targets = import_targets or {}
        symbol_imports = symbol_imports or {}
        imported_by = imported_by or {}
        self._ensure_registries()

        dirty_rels = set(changed_rels) | set(deleted_rels)
        old_nodes_snapshot: dict[str, dict] = {}
        all_old_node_ids: set[str] = set()
        for rel in dirty_rels:
            old_nids = self._nodes_by_file.pop(rel, [])
            for nid in old_nids:
                nd = self.nodes.pop(nid, None)
                all_old_node_ids.add(nid)
                if nd:
                    old_nodes_snapshot[nid] = nd
                    nm = nd.get("name", "")
                    if nm and nm in self._registry:
                        self._registry[nm] = [x for x in self._registry[nm] if x != nid]
                    self._by_file_name.pop((rel, nm), None)
                    parent = nd.get("parent_class")
                    if parent and nm:
                        qnm = f"{parent}.{nm}"
                        if qnm in self._registry:
                            self._registry[qnm] = [x for x in self._registry[qnm] if x != nid]
                        self._by_file_name.pop((rel, qnm), None)

        # Pass 1 (Δ): Propagate router mounts & add updated nodes for changed_rels
        self.propagate_router_mounts(index, import_targets, symbol_imports)
        for rel in changed_rels:
            if rel in index:
                self._populate_file_nodes(rel, index[rel])

        # Fast Salsa cutoff path: if interfaces are unchanged, no nodes were added/removed,
        # and every function's outgoing call signature is unchanged, skip 200k-edge rebuild
        new_node_ids = {nid for r in changed_rels for nid in self._nodes_by_file.get(r, ())}
        truly_removed_node_ids = all_old_node_ids - new_node_ids
        added_node_ids = new_node_ids - all_old_node_ids
        if (
            interfaces_unchanged
            and not deleted_rels
            and not truly_removed_node_ids
            and not added_node_ids
            and all(
                (
                    self.nodes[nid].get("calls_sig") == old_nodes_snapshot[nid].get("calls_sig")
                    if "calls_sig" in old_nodes_snapshot[nid]
                    else self.nodes[nid].get("ast_hash") == old_nodes_snapshot[nid].get("ast_hash")
                )
                for nid in new_node_ids
                if self.nodes[nid].get("type") == "fn"
            )
        ):
            for nid in new_node_ids:
                old_nd = old_nodes_snapshot[nid]
                for k_keep in (
                    "local_z_write", "local_z_read", "exit_contracts", "http_calls",
                    "return_dict_keys", "local_effects", "temporal_mode",
                    "transitive_z_write", "transitive_effects", "transitive_exits", "effect_provenance",
                ):
                    if k_keep in old_nd:
                        self.nodes[nid][k_keep] = old_nd[k_keep]
            upserted_nodes = {nid: self.nodes[nid] for nid in new_node_ids}
            return {
                "affected_rels": list(changed_rels),
                "removed_rels": [],
                "removed_node_ids": [],
                "rebuild_src_ids": [],
                "upserted_nodes": upserted_nodes,
                "new_edges": [],
            }

        # Determine files whose outgoing edges must be recomputed.
        # Importers' outgoing edges only depend on symbol node existence and class inheritance bases,
        # never on callee parameter signatures — so surviving symbol IDs keep all incoming edges intact.
        cls_bases_changed = any(
            self.nodes[nid].get("bases") != old_nodes_snapshot[nid].get("bases")
            for nid in (new_node_ids & all_old_node_ids)
            if self.nodes[nid].get("type") == "cls"
        )
        edge_rebuild_rels = {r for r in changed_rels if r in index}
        if truly_removed_node_ids or added_node_ids or cls_bases_changed:
            for r in dirty_rels:
                for imp_rel in imported_by.get(r, ())[:25]:
                    if imp_rel in index:
                        edge_rebuild_rels.add(imp_rel)

        rebuild_src_ids: set[str] = set(all_old_node_ids)
        for rel in edge_rebuild_rels:
            rebuild_src_ids.update(self._nodes_by_file.get(rel, ()))

        # Filter existing edges in-place and append rebuilt edges for edge_rebuild_rels
        self.edges = [
            e for e in self.edges
            if e["src"] not in rebuild_src_ids and e["dst"] not in truly_removed_node_ids and e["dst"] in self.nodes
        ]
        prev_edge_len = len(self.edges)
        for rel in edge_rebuild_rels:
            self._populate_file_edges(rel, index[rel], import_targets, symbol_imports)

        self._build_index()
        self._propagate_interprocedural_effects(index, target_rels=edge_rebuild_rels)

        new_edges = self.edges[prev_edge_len:]
        upserted_nodes = {}
        for rel in edge_rebuild_rels:
            for nid in self._nodes_by_file.get(rel, ()):
                if nid in self.nodes:
                    upserted_nodes[nid] = self.nodes[nid]
            for imp in index.get(rel, {}).get("imports", []):
                mnid = mod_id(imp)
                if mnid in self.nodes:
                    upserted_nodes[mnid] = self.nodes[mnid]

        return {
            "affected_rels": list(edge_rebuild_rels),
            "removed_rels": list(dirty_rels),
            "removed_node_ids": list(truly_removed_node_ids),
            "rebuild_src_ids": list(rebuild_src_ids),
            "upserted_nodes": upserted_nodes,
            "new_edges": new_edges,
        }

    def _propagate_interprocedural_effects(self, index: dict, target_rels: set[str] | None = None) -> None:
        """Pass A: Bind Universal 6D IR to fn nodes & bubble effects/?Z 1-3 hops up the call graph.
        Pass B: Wire cross-function `state_flow` edges (?Z_write -> Z_read) across shared state/event channels.
        """
        _GENERIC_STATE_KEYS = {
            "data", "res", "result", "item", "items", "val", "value", "err", "error",
            "req", "resp", "args", "kwargs", "options", "config", "params", "state",
            "props", "dispatch", "user", "id", "key", "obj", "row", "rows",
        }
        # 1. Bind file 6D cells onto enclosing function nodes
        iter_rels = (target_rels & set(index.keys())) if target_rels is not None else index.keys()
        for rel_path in iter_rels:
            info = index[rel_path]
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
                local_http: list[dict] = []
                local_ret_keys: list[str] = []
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
                    for hc in c.get("http_calls", []):
                        if hc not in local_http:
                            local_http.append(hc)
                    if c.get("X_scope", "").startswith("fn:"):
                        for rk in c.get("return_dict_keys", []):
                            if rk not in local_ret_keys:
                                local_ret_keys.append(rk)
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
                if local_http:
                    nd["http_calls"] = local_http[:8]
                if local_ret_keys:
                    nd["return_dict_keys"] = local_ret_keys[:16]
                if local_eff:
                    nd["local_effects"] = local_eff
                if t_mode != "SYNC":
                    nd["temporal_mode"] = t_mode
        # 2. Pass A: Bottom-Up 3-Hop Interprocedural Effect & ?Z Bubbling along `calls` edges
        if target_rels is not None:
            bubble_nids: list[str] = []
            for r in iter_rels:
                for nid in self._nodes_by_file.get(r, ()):
                    if self.nodes.get(nid, {}).get("type") == "fn":
                        bubble_nids.append(nid)
                        for ei_in in self._radj.get(nid, ()):
                            caller_id = self.edges[ei_in]["src"]
                            if self.nodes.get(caller_id, {}).get("type") == "fn":
                                bubble_nids.append(caller_id)
            iter_fn_items = [(nid, self.nodes[nid]) for nid in dict.fromkeys(bubble_nids) if nid in self.nodes]
        else:
            iter_fn_items = [(nid, nd) for nid, nd in self.nodes.items() if nd.get("type") == "fn"]

        for nid, nd in iter_fn_items:
            own_zw = set(nd.get("local_z_write", []))
            own_exits = set(nd.get("exit_contracts", []))
            trans_zw: list[str] = []
            trans_eff: list[str] = []
            trans_exits: list[str] = []
            provenance: dict[str, str] = {}

            q: deque[tuple[str, int, list[str]]] = deque([(nid, 0, [])])
            visited_callees: set[str] = {nid}
            while q and len(visited_callees) <= 24:
                curr_id, hop, chain = q.popleft()
                if hop >= 3:
                    continue
                for ei in self._adj.get(curr_id, []):
                    e = self.edges[ei]
                    if e["type"] not in ("calls", "defines_nested") or e.get("confidence") not in ("exact", "import_resolved"):
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

                    if callee_name.startswith("use") and len(callee_name) > 3 and callee_name[3].isupper():
                        if callee_nd.get("has_local_state") and nd.get("name", "")[:1].isupper():
                            nd["has_local_state"] = True
                    pend_tc = nd.get("http_pending_transport_call")
                    if nxt_hop == 1 and pend_tc and not nd.get("http_wrapper_spec"):
                        t_spec = callee_nd.get("http_transport_spec")
                        if t_spec and callee_name == pend_tc.get("callee"):
                            vb = t_spec.get("fixed_method") or pend_tc.get("inline_method") or "GET"
                            b_idx = None
                            if t_spec.get("body_param_idx") == 1 and "arg1_param_idx" in pend_tc:
                                b_idx = pend_tc["arg1_param_idx"]
                            elif "inline_body_param_idx" in pend_tc:
                                b_idx = pend_tc["inline_body_param_idx"]
                            w_spec: dict = {
                                "method": vb,
                                "url": pend_tc["url"],
                                "norm_url": pend_tc["norm_url"],
                                "returns_json": bool(t_spec.get("returns_json")),
                            }
                            if b_idx is not None:
                                w_spec["body_param_idx"] = b_idx
                            nd["http_wrapper_spec"] = w_spec
                            nd_rel = nd.get("rel_path", "")
                            if nd_rel in index:
                                for f_ent in index[nd_rel].get("functions", ()):
                                    if f_ent.get("name") == nd.get("name") and f_ent.get("parent_class") == nd.get("parent_class"):
                                        f_ent["http_wrapper_spec"] = w_spec
                    for zw in callee_nd.get("local_z_write", []):
                        if len(trans_zw) < 12 and zw not in own_zw and zw not in trans_zw:
                            trans_zw.append(zw)
                            if len(provenance) < 24:
                                provenance[f"ΔZ:{zw}"] = via_label
                                provenance[f"?Z:{zw}"] = via_label
                    for eff in callee_nd.get("local_effects", []):
                        if eff not in trans_eff:
                            trans_eff.append(eff)
                            if len(provenance) < 16:
                                provenance.setdefault(f"effect:{eff}", via_label)
                    for ex in callee_nd.get("exit_contracts", []):
                        if len(trans_exits) < 12 and ex.startswith(("throw:", "status:", "err:")) and ex not in own_exits and ex not in trans_exits:
                            trans_exits.append(ex)
                            if len(provenance) < 16:
                                provenance[ex] = via_label

                    if len(visited_callees) <= 24:
                        q.append((callee_id, nxt_hop, nxt_chain))

            if trans_zw:
                nd["transitive_z_write"] = trans_zw[:12]
            if trans_eff:
                nd["transitive_effects"] = trans_eff
            if trans_exits:
                nd["transitive_exits"] = trans_exits[:12]
            if provenance:
                nd["effect_provenance"] = provenance

        # 3. Pass B: Cross-Function / Cross-File State-Channel Edges (`state_flow`: ?Z_write -> Z_read)
        multiplex_edge_start = len(self.edges)
        writers_by_key: dict[str, list[str]] = {}
        readers_by_key: dict[str, list[str]] = {}
        for nid, nd in iter_fn_items:
            for zw in nd.get("local_z_write", []):
                if len(zw) >= 3 and zw.lower() not in _GENERIC_STATE_KEYS:
                    writers_by_key.setdefault(zw, []).append(nid)
            for zr in nd.get("local_z_read", []):
                if len(zr) >= 3 and zr.lower() not in _GENERIC_STATE_KEYS:
                    readers_by_key.setdefault(zr, []).append(nid)

        added_multiplex_edges = 0
        seen_state_pairs: set[tuple[str, str, str]] = set()
        max_w = 8 if len(self.nodes) > 800 else 18
        max_r = 12 if len(self.nodes) > 800 else 25
        for k, writers in writers_by_key.items():
            readers = readers_by_key.get(k, [])
            if not readers or len(writers) > max_w or len(readers) > max_r:
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
                            f"shared state channel '{k}' (ΔZ_write -> Z_read)",
                        )
                        added_multiplex_edges += 1

        # 4. Pass B2: Cross-Stack HTTP Bridge Edges (`http_flow`: Frontend fetch/axios -> Backend route handler)
        route_handlers: list[tuple[str, str, str, str]] = []
        if target_rels is None or any(nd.get("http_calls") for _, nd in iter_fn_items):
            for rnid, rnd in self.nodes.items():
                if rnd.get("type") != "route":
                    continue
                rmeth = (rnd.get("method") or "GET").upper()
                rfull = rnd.get("full_path") or rnd.get("path") or ""
                rnorm = re.sub(r"\{[^}]+\}|:[a-zA-Z_]\w*", "{*}", rfull).rstrip("/") or "/"
                rshort = re.sub(r"\{[^}]+\}|:[a-zA-Z_]\w*", "{*}", rnd.get("path") or "").rstrip("/") or "/"
                for ei in self._adj.get(rnid, []):
                    e_h = self.edges[ei]
                    if e_h["type"] == "handles":
                        route_handlers.append((rmeth, rnorm, rshort, e_h["dst"]))

        if route_handlers:
            seen_http_pairs: set[tuple[str, str, str]] = set()
            for fnid, fnd in iter_fn_items:
                for hc in fnd.get("http_calls", []):
                    hm = hc["method"]
                    hu = hc["norm_url"]
                    for rmeth, rnorm, rshort, h_dst in route_handlers:
                        if rmeth not in ("ANY", "ALL", hm) and hm not in ("ANY", "ALL"):
                            continue
                        matched_route = (
                            (hu == rnorm)
                            or (len(rshort) > 4 and rshort.count("/") >= 2 and hu.endswith(rshort))
                            or (len(hu) > 4 and hu.count("/") >= 2 and rnorm.endswith(hu))
                        )
                        if matched_route and fnid != h_dst and (fnid, h_dst, hu) not in seen_http_pairs:
                            seen_http_pairs.add((fnid, h_dst, hu))
                            self._add_edge(
                                fnid,
                                h_dst,
                                "http_flow",
                                "exact",
                                f"HTTP {hm} {hc['url']} -> {h_dst.split(':')[-1]}",
                                line=hc.get("line"),
                            )
                            added_multiplex_edges += 1

        if added_multiplex_edges > 0:
            if target_rels is not None:
                for idx in range(multiplex_edge_start, len(self.edges)):
                    e = self.edges[idx]
                    self._adj.setdefault(e["src"], []).append(idx)
                    self._radj.setdefault(e["dst"], []).append(idx)
            else:
                self._build_index()

    def multiplex_ppr(
        self,
        seed_weights: dict[str, float] | list[str] | set[str] | tuple[str, ...],
        alpha: float = 0.30,
        iterations: int = 12,
        top_k: int = 12,
        epsilon: float = 1e-4,
    ) -> list[tuple[str, float, str]]:
        """Dyck-Constrained Sublinear Andersen-Chung-Lang (ACL) Multiplex Push with Ochiai Hub Damping:
          - Enforces Horwitz-Reps-Binkley (HRB) two-phase realizability (`phase="UP"` vs `phase="DOWN"`):
            once a walk steps DOWN into a shared callee via a forward `calls` edge, it cannot step UP via
            `inverse_calls` into unrelated callers of that utility helper.
          - Weights target transitions by Ochiai in-degree specificity `(1 + deg_in(v))^-0.6` so ubiquitous
            utility hubs (`logger`, `len`, formatters) do not steal probability mass from domain callees.
          - Uses an ACL residual push queue (`residual >= epsilon`) for sublinear O(1 / (alpha * epsilon)) runtime.
        """
        if not isinstance(seed_weights, dict):
            raw_seeds = {str(k): 1.0 for k in seed_weights}
        else:
            raw_seeds = {str(k): float(v) for k, v in seed_weights.items() if v > 0}
        valid_seeds: dict[str, float] = {}
        for k, v in raw_seeds.items():
            if k in self.nodes:
                valid_seeds[k] = v
            else:
                norm_k = k.replace("::", ":")
                if f"fn:{norm_k}" in self.nodes:
                    valid_seeds[f"fn:{norm_k}"] = v
                else:
                    for nid in self.nodes:
                        if nid.endswith(f":{norm_k}") or nid.endswith(f":{k.split(':')[-1]}"):
                            valid_seeds[nid] = v
                            break
        if not valid_seeds:
            return []
        total_s = sum(valid_seeds.values())
        v0 = {k: v / total_s for k, v in valid_seeds.items()}

        self._ensure_registries()
        _FWD_W = {"http_flow": 1.45, "state_flow": 1.25, "handles": 1.15, "calls": 1.00, "defines_nested": 0.95}
        _BWD_W = {"http_flow": 0.85, "state_flow": 0.80, "handles": 0.70, "calls": 0.65}
        _CONF_W = {"exact": 1.0, "qualified": 0.9, "import_resolved": 0.85}

        def _edge_conf_weight(e_obj: dict) -> float:
            conf = e_obj.get("confidence", "")
            w0 = _CONF_W.get(conf)
            if w0 is not None:
                return w0
            if conf == "candidate" and e_obj.get("type") == "calls":
                dst_nm = self.nodes.get(e_obj.get("dst", ""), {}).get("name") or ""
                # Closed-world CHA: compound selector (>=2 subtokens, avoiding 1-word stdlib protocols like .execute/.write)
                # with a globally unique definition across the repository.
                if (
                    len(dst_nm) >= 6
                    and ("_" in dst_nm.strip("_") or (any(c.isupper() for c in dst_nm[1:]) and any(c.islower() for c in dst_nm)))
                    and len(self._registry.get(dst_nm, ())) == 1
                ):
                    return 0.72
            return 0.0

        # Lazily compute incoming call degree on touched nodes only (preserving sublinear ACL runtime)
        call_in_deg: dict[str, int] = {}

        def _hub_damp(target_nid: str) -> float:
            deg = call_in_deg.get(target_nid)
            if deg is None:
                deg = sum(
                    1 for ei in self._radj.get(target_nid, ())
                    if self.edges[ei]["type"] == "calls" and _edge_conf_weight(self.edges[ei]) > 0.0
                )
                call_in_deg[target_nid] = deg
            return 1.0 / ((1.0 + deg) ** 0.6)

        # State is (node_id, phase) where phase=0 is UP/ENTRY and phase=1 is DOWN (inside callee)
        reserve: dict[str, float] = {}
        residual: dict[tuple[str, int], float] = {}
        in_queue: set[tuple[str, int]] = set()
        push_q: deque[tuple[str, int]] = deque()
        best_prov: dict[str, tuple[float, str]] = {}

        for k, val in v0.items():
            st = (k, 0)
            residual[st] = val
            in_queue.add(st)
            push_q.append(st)

        max_pushes = max(500, iterations * 350)
        pushes = 0
        while push_q and pushes < max_pushes:
            u, phase = push_q.popleft()
            in_queue.discard((u, phase))
            r_u = residual.get((u, phase), 0.0)
            if r_u < epsilon:
                continue
            residual[(u, phase)] = 0.0
            pushes += 1

            reserve[u] = reserve.get(u, 0.0) + alpha * r_u
            rem = (1.0 - alpha) * r_u

            transitions: list[tuple[str, int, float, str]] = []
            u_nm = self.nodes.get(u, {}).get("name") or u.split(":")[-1]

            # Forward edges
            for ei in self._adj.get(u, []):
                e = self.edges[ei]
                et = e["type"]
                if et in _FWD_W:
                    cw = _edge_conf_weight(e)
                    if cw > 0.0:
                        dst = e["dst"]
                        w = _FWD_W[et] * cw * _hub_damp(dst)
                        # Stepping down a `calls` edge enters DOWN phase (1); cross-stack bridges reset to ENTRY (0)
                        nxt_phase = 1 if et in ("calls", "defines_nested") else 0
                        rsn = e.get("reason") or f"{et} from {u_nm}"
                        transitions.append((dst, nxt_phase, w, f"PPR [{et}] via {u_nm}: {rsn}"))

            # Backward edges (subject to Dyck two-phase realizability)
            for ei in self._radj.get(u, []):
                e = self.edges[ei]
                et = e["type"]
                if et in _BWD_W:
                    cw = _edge_conf_weight(e)
                    if cw > 0.0:
                        # HRB / Dyck law: if we arrived at `u` by stepping DOWN into a callee (phase == 1),
                        # never step UP via `inverse_calls` into other callers of `u`!
                        if phase == 1 and et == "calls":
                            continue
                        src = e["src"]
                        w = _BWD_W[et] * cw * _hub_damp(src)
                        transitions.append((src, 0, w, f"PPR [inverse_{et}] -> {u_nm}"))

            if not transitions:
                for k, val in v0.items():
                    st0 = (k, 0)
                    residual[st0] = residual.get(st0, 0.0) + rem * val
                    if residual[st0] >= epsilon and st0 not in in_queue:
                        in_queue.add(st0)
                        push_q.append(st0)
                continue

            w_sum = sum(t[2] for t in transitions)
            for dst, nxt_phase, w, prov_str in transitions:
                contrib = rem * (w / w_sum)
                dst_state = (dst, nxt_phase)
                residual[dst_state] = residual.get(dst_state, 0.0) + contrib
                if residual[dst_state] >= epsilon and dst_state not in in_queue:
                    in_queue.add(dst_state)
                    push_q.append(dst_state)
                prev_best = best_prov.get(dst, (0.0, ""))[0]
                if dst not in valid_seeds and contrib > prev_best:
                    best_prov[dst] = (contrib, prov_str)

        ranked_out: list[tuple[str, float, str]] = []
        for nid, prob in reserve.items():
            if nid in valid_seeds:
                continue
            nd = self.nodes.get(nid, {})
            if nd.get("type") not in ("fn", "cls"):
                continue
            prov_reason = best_prov.get(nid, (0.0, "multiplex diffusion"))[1]
            ranked_out.append((nid, prob, prov_reason))
        ranked_out.sort(key=lambda x: x[1], reverse=True)
        return ranked_out[:top_k]

    def causal_symptom_chop(
        self,
        seed_ids: list[str] | set[str],
        task_text: str,
        max_hops: int = 4,
    ) -> list[tuple[str, str, list[str]]]:
        """Compute the Causal Program Chop X_chop(S_0, Sigma) = F_multiplex(S_0) ? B(Sigma).
        Intersects the forward 4-layer execution cone of `seed_ids` with the backward origin set
        of HTTP status codes / exception symptoms extracted from `task_text`.
        Returns list of (origin_node_id, matched_symptom_str, causal_path_node_ids).
        """
        t_low = task_text.lower()
        target_exits: set[str] = set()
        for m_code in re.finditer(r"\b(4\d\d|5\d\d)\b", t_low):
            target_exits.add(f"status:{m_code.group(1)}")
        # Universal RFC-9110 HTTP status & standard runtime exception alignment
        if "unauthorized" in t_low or "401" in t_low or "unauthenticated" in t_low:
            target_exits.update({"status:401", "err:unauthorized"})
        if "forbidden" in t_low or "403" in t_low or "permission" in t_low or "access denied" in t_low:
            target_exits.update({"status:403", "err:forbidden", "throw:PermissionError"})
        if "not found" in t_low or "404" in t_low or "missing" in t_low:
            target_exits.update({"status:404", "err:not_found", "throw:KeyError", "throw:FileNotFoundError"})
        if "conflict" in t_low or "409" in t_low or "duplicate" in t_low or "unique" in t_low:
            target_exits.update({"status:409", "err:conflict", "throw:UniqueViolation", "throw:IntegrityError"})
        if "422" in t_low or "unprocessable" in t_low or "validation" in t_low:
            target_exits.update({"status:422", "throw:ValueError", "throw:ValidationError"})
        if "400" in t_low or "bad request" in t_low:
            target_exits.update({"status:400", "throw:ValueError"})
        if "429" in t_low or "rate limit" in t_low or "too many requests" in t_low:
            target_exits.update({"status:429"})

        # Universal lexical stem extraction for matching arbitrary domain exceptions/error codes
        _chop_stop = {
            "the", "and", "for", "with", "when", "from", "into", "that", "this",
            "why", "how", "what", "does", "fail", "fails", "failed", "failing",
            "error", "errors", "exception", "bug", "issue", "status", "code",
            "http", "route", "endpoint", "function", "method", "call", "calls",
            "return", "returns", "throw", "throws", "raise", "raises",
            "app", "application", "flask", "django", "user", "request", "response",
            "file", "files", "data", "value", "values", "type", "types", "key", "keys",
            "name", "names", "runtime", "system", "index", "attribute", "not",
            "implemented", "work", "works", "registered", "register", "record",
        }
        raw_words = re.findall(r"[A-Za-z][a-z0-9]+|[A-Z]+(?=[A-Z][a-z]|\b)|[a-z0-9]+", task_text)
        query_stems: set[str] = {
            w.lower() for w in raw_words if len(w) >= 4 and w.lower() not in _chop_stop
        }
        if not target_exits and not query_stems:
            return []

        valid_starts: list[str] = []
        for k in seed_ids:
            if k in self.nodes:
                valid_starts.append(k)
            else:
                norm_k = k.replace("::", ":")
                if f"fn:{norm_k}" in self.nodes:
                    valid_starts.append(f"fn:{norm_k}")
        if not valid_starts:
            return []

        def _matches_exit_contract(ec: str) -> bool:
            if ec in target_exits:
                return True
            if ec.startswith(("err:", "throw:")) and query_stems:
                payload = ec.split(":", 1)[1]
                ec_parts = re.findall(r"[A-Za-z][a-z0-9]+|[A-Z]+(?=[A-Z][a-z]|\b)|[a-z0-9]+", payload)
                ec_stems = {
                    p.lower()
                    for p in ec_parts
                    if len(p) >= 3 and p.lower() not in _chop_stop and p.lower() != "err"
                }
                if ec_stems and (ec_stems & query_stems):
                    return True
            return False

        results: list[tuple[str, str, list[str]]] = []
        seen_origins: set[str] = set()
        q: deque[tuple[str, list[str], int]] = deque([(s, [s], 0) for s in valid_starts])
        visited_hops: dict[str, int] = {s: 0 for s in valid_starts}

        while q:
            curr_id, path, hop = q.popleft()
            if hop >= 1 and curr_id not in seen_origins:
                c_nd = self.nodes.get(curr_id, {})
                matched = sorted(
                    ec for ec in set(c_nd.get("exit_contracts", [])) if _matches_exit_contract(ec)
                )
                if matched:
                    seen_origins.add(curr_id)
                    results.append((curr_id, ", ".join(matched), path))
            if hop >= max_hops:
                continue
            for ei in self._adj.get(curr_id, []):
                e = self.edges[ei]
                if e["type"] in ("http_flow", "calls", "defines_nested", "handles", "state_flow") and e.get("confidence") in ("exact", "import_resolved"):
                    dst = e["dst"]
                    if dst not in visited_hops or visited_hops[dst] > hop + 1:
                        visited_hops[dst] = hop + 1
                        q.append((dst, path + [dst], hop + 1))
        return results
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
        name_clean = name.strip().replace("\\", "/")
        name_lower = name_clean.lower()
        norm_key = normalize_ident_key(name_clean)
        wants_file = "." in name_clean.rsplit("/", 1)[-1] and not name_clean.startswith(("fn:", "cls:", "route:", "mod:"))

        exact_file_matches: list[str] = []
        exact_matches: list[str] = []
        norm_matches: list[str] = []
        partial_matches: list[str] = []

        for node_id, nd in self.nodes.items():
            ntype = nd.get("type", "")
            rel_p = (nd.get("rel_path") or "").lower()
            base_p = rel_p.rsplit("/", 1)[-1] if rel_p else ""
            nd_name = (nd.get("name") or nd.get("full_path") or nd.get("path") or nd.get("rel_path") or "").lower()
            route_label = f"{nd.get('method', '')} {nd.get('full_path') or nd.get('path', '')}".strip().lower()

            if ntype == "file" and (rel_p == name_lower or base_p == name_lower or rel_p.endswith("/" + name_lower)):
                exact_file_matches.append(node_id)
                continue

            if nd_name == name_lower or node_id.lower() == name_lower or (route_label and route_label == name_lower):
                exact_matches.append(node_id)
                continue

            if len(norm_key) >= 4 and ntype in ("fn", "cls"):
                raw_nm = nd.get("name") or ""
                if normalize_ident_key(raw_nm) == norm_key:
                    norm_matches.append(node_id)
                    continue

            searchable = " ".join([
                node_id,
                nd.get("name", ""),
                nd.get("rel_path", ""),
                nd.get("full_path", ""),
                nd.get("path", ""),
                route_label,
            ]).lower()
            if name_lower in searchable or (len(norm_key) >= 5 and norm_key in normalize_ident_key(nd.get("name", ""))):
                partial_matches.append(node_id)

        if wants_file and exact_file_matches:
            return exact_file_matches + exact_matches + norm_matches + partial_matches
        return exact_matches + exact_file_matches + norm_matches + partial_matches

    # ── serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges}

    def to_json(self) -> str:
        return json.dumps({"nodes": self.nodes, "edges": self.edges}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | dict) -> "CodeGraph":
        data = json.loads(text) if isinstance(text, str) else text
        g = cls()
        g.nodes = data["nodes"]
        g.edges = data["edges"]
        g._build_index()
        return g


_IDENT_NORM_RE = re.compile(r"[^a-z0-9]+")

_EXPLORE_NODE_KEEP_KEYS = (
    "id",
    "type",
    "rel_path",
    "name",
    "signature",
    "line_start",
    "line_end",
    "parent_class",
    "method",
    "path",
    "full_path",
    "handler",
    "line",
)

_EXPLORE_EDGE_PRIORITY = {
    "http_flow": 0,
    "handles": 1,
    "calls": 2,
    "inherits": 3,
    "imports": 4,
    "state_flow": 5,
    "exports": 6,
    "defines": 7,
    "defines_nested": 8,
}


def normalize_ident_key(raw: str) -> str:
    """Alphanumeric lowercase key unifying `snake_case`, `camelCase`, and `kebab-case` (`send_email` == `sendEmail`)."""
    return _IDENT_NORM_RE.sub("", raw.lower())


def compact_explore_node(node: dict) -> dict:
    """Strip internal 6D IR arrays/hashes (`?Z:`, `effect_provenance`, `ast_hash`, etc.) from an explore node."""
    out: dict = {}
    for k in _EXPLORE_NODE_KEEP_KEYS:
        val = node.get(k)
        if val is not None and val != "":
            out[k] = val
    return out


def compact_explore_result(
    res: dict,
    max_nodes: int = 28,
    max_edges: int = 34,
) -> dict:
    """Bound and sanitize `explore()` / `explore_fast()` output so it always stays <6 KB inline."""
    if not isinstance(res, dict):
        return res

    out = dict(res)
    if "from_node" in out and isinstance(out["from_node"], dict):
        out["from_node"] = compact_explore_node(out["from_node"])
    if "to_node" in out and isinstance(out["to_node"], dict):
        out["to_node"] = compact_explore_node(out["to_node"])
    if "path" in out and isinstance(out["path"], list):
        out["path"] = out["path"][:30]
        return out

    if "center" not in out or not isinstance(out.get("center"), dict):
        return out

    center_clean = compact_explore_node(out["center"])
    center_id = center_clean.get("id", "")
    center_rel = center_clean.get("rel_path", "")
    out["center"] = center_clean

    raw_nodes: list[dict] = out.get("nodes") or []
    raw_edges: list[dict] = out.get("edges") or []
    node_by_id: dict[str, dict] = {
        nd["id"]: compact_explore_node(nd)
        for nd in raw_nodes
        if isinstance(nd, dict) and nd.get("id")
    }

    def _edge_rank(e: dict) -> tuple[int, int, int, int]:
        et = e.get("type", "")
        direct = 0 if (e.get("src") == center_id or e.get("dst") == center_id) else 1
        src_rel = center_rel if e.get("src") == center_id else node_by_id.get(e.get("src", ""), {}).get("rel_path", "")
        dst_rel = center_rel if e.get("dst") == center_id else node_by_id.get(e.get("dst", ""), {}).get("rel_path", "")
        cross_file = 0 if (src_rel and dst_rel and src_rel != dst_rel) else 1
        conf_rank = 0 if e.get("confidence") in (None, "exact", "import_resolved") else 1
        return (direct, cross_file, _EXPLORE_EDGE_PRIORITY.get(et, 6), conf_rank)

    # Drop noisy structural `defines` fanout when real call/import/route edges already exceed budget
    non_def_edges = [e for e in raw_edges if e.get("type") not in ("defines", "defines_nested")]
    candidate_edges = non_def_edges if len(non_def_edges) >= 12 else raw_edges
    sorted_edges = sorted(candidate_edges, key=_edge_rank)
    kept_edges_raw = sorted_edges[:max_edges]

    kept_edges: list[dict] = []
    referenced_ids: set[str] = set()
    for e in kept_edges_raw:
        s_id, d_id = e.get("src", ""), e.get("dst", "")
        if s_id and s_id != center_id:
            referenced_ids.add(s_id)
        if d_id and d_id != center_id:
            referenced_ids.add(d_id)
        ce: dict = {"src": s_id, "dst": d_id, "type": e.get("type", "")}
        if e.get("line"):
            ce["line"] = e["line"]
        if e.get("confidence") and e.get("confidence") != "exact":
            ce["confidence"] = e["confidence"]
        rsn = e.get("reason")
        if rsn and e.get("type") in ("http_flow", "state_flow"):
            ce["reason"] = rsn[:90]
        kept_edges.append(ce)

    kept_nodes: list[dict] = []
    for nid in referenced_ids:
        if nid in node_by_id:
            kept_nodes.append(node_by_id[nid])
            if len(kept_nodes) >= max_nodes:
                break
    if len(kept_nodes) < max_nodes:
        kept_set = {nd["id"] for nd in kept_nodes}
        for nid, nd in node_by_id.items():
            if nid not in kept_set:
                kept_nodes.append(nd)
                if len(kept_nodes) >= max_nodes:
                    break

    out["nodes"] = kept_nodes
    out["edges"] = kept_edges
    omitted_n = max(0, len(raw_nodes) - len(kept_nodes))
    omitted_e = max(0, len(raw_edges) - len(kept_edges))
    if omitted_n or omitted_e:
        out["truncated"] = {
            "nodes_omitted": omitted_n,
            "edges_omitted": omitted_e,
        }
    if "co_changes" in out and isinstance(out["co_changes"], list):
        out["co_changes"] = out["co_changes"][:10]
    return out
