"""Deep Semantic & Cross-Stack Contract Verifiers (v1.3).

Implements three deterministic, zero-LLM static analysis pillars:
  1. PB011 — Cross-Stack HTTP Request & Response Schema Solver:
             Verifies JS/TS `fetch`, `axios`, transport helpers, and 1-hop API client wrappers
             against mounted FastAPI/Pydantic/SQLModel/TypedDict/dataclass schemas and FastAPI/Express HTTP verb & route mounts.
  2. PB012 — React Stateful Lifecycle, Remount & Async Race Verifier:
             Proves post-mount early-return/ternary unmounts of stateful child components,
             inline nested component definitions, and uncancelled `useEffect([deps])` async races.
  3. PB013 — Python Pydantic, ORM & Transactional I/O Hazard Verifier:
             Proves partial-update `.model_dump()` / `.dict()` default clobbering without
             `exclude_unset=True` and completely silent `except Exception: pass` write/IO drops.
"""
from __future__ import annotations

import ast
import difflib
import fnmatch
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import resolve
from .graph import CodeGraph, fn_id, normalize_ident_key
from .parsers import get_parser
from .reporters import _clean_uri, _extract_location, _is_test_file_path

if TYPE_CHECKING:
    from .brain import ProjectBrain

_JS_TS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_SCHEMA_NON_FIELD_BASES = frozenset({
    "BaseModel", "SQLModel", "TypedDict", "Schema", "APIModel", "CamelModel",
    "ModelSchema", "Generic", "ABC", "object", " Dict", "dict",
})
_JS_ARRAY_AND_RES_BUILTINS = frozenset({
    "ok", "status", "statusText", "headers", "config", "request",
    "json", "text", "blob", "arrayBuffer", "formData", "clone",
    "then", "catch", "finally",
    "length", "map", "filter", "forEach", "find", "findIndex", "some", "every",
    "reduce", "reduceRight", "flat", "flatMap", "slice", "concat", "includes",
    "indexOf", "join", "keys", "values", "entries", "at", "sort", "reverse",
    "toString", "valueOf", "hasOwnProperty",
    "error", "errors", "detail", "message",
})
_AXIOS_LIKE_RECEIVERS = frozenset({"axios", "api", "client", "http", "httpClient", "apiClient"})
_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_URL_DYN_SEG_RE = re.compile(r"\{[^}]+\}|\$\{[^}]+\}|:[A-Za-z_]\w*|\[[^\]]+\]")
_URL_LEADING_HOST_RE = re.compile(r"^(?:\$\{[^}]+\}|https?://[^/]+)+")
_PY_SIDE_EFFECT_CALLS = frozenset({
    "commit",
    "send_email",
    "send_mail",
    "sendmail",
    "send_message",
    "send_notification",
    "send_webhook",
    "dispatch_webhook",
    "publish_event",
    "charge",
    "create_payment_intent",
    "refund",
})
_PY_HTTP_MUTATION_RE = re.compile(
    r"^(?:self\.)?(?:requests|httpx|aiohttp|client|session|http|http_client|api_client)\.(?:post|put|patch|delete)$"
)
_GENERIC_SCHEMA_TOKENS = frozenset({
    "id", "ids", "data", "item", "items", "val", "value", "type", "key", "keys", "obj", "info", "req", "res",
})
_STABLE_HOOK_DEPS = frozenset({"dispatch", "navigate", "router", "toast", "notify", "t", "i18n"})
_IDENT_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_ident_tokens(name: str) -> set[str]:
    spaced = _IDENT_CAMEL_RE.sub(" ", name or "")
    return {tok.lower() for tok in re.split(r"[\s_\-]+", spaced) if len(tok) >= 3}


def _norm_http_path(raw_path: str) -> str:
    s = _URL_LEADING_HOST_RE.sub("", (raw_path or "").strip())
    s = s.split("?", 1)[0].split("#", 1)[0].strip()
    if not s:
        return "/"
    if not s.startswith("/"):
        s = "/" + s
    norm = _URL_DYN_SEG_RE.sub("{*}", s).rstrip("/")
    return norm or "/"


def _path_tail_key(norm_path: str) -> str:
    segs = [seg for seg in norm_path.split("/") if seg]
    if len(segs) >= 2:
        return "/" + "/".join(segs[-2:]).lower()
    return ""


def _t_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _t_line(node) -> int:
    return int(node.start_point[0]) + 1


# ── Cross-File Schema Resolution ──────────────────────────────────────────────

def build_resolved_schema_registry(brain: "ProjectBrain") -> dict[tuple[str, str], dict]:
    """Resolve Pydantic / SQLModel / TypedDict / dataclass / TS interface schemas across inheritance."""
    is_partial = bool(getattr(brain, "_is_partial_index", False))
    if not is_partial:
        idx_ver = (len(brain._index), sum(id(v) for v in brain._index.values()))
        if getattr(brain, "_cached_schema_reg_ver", None) == idx_ver and hasattr(brain, "_cached_schema_reg_data"):
            return brain._cached_schema_reg_data

    raw_classes: dict[tuple[str, str], dict] = {}
    by_bare_name: dict[str, list[tuple[str, str]]] = {}
    has_any_direct = False

    for rel, info in brain._index.items():
        for cls in info.get("classes", []):
            c_nm = cls.get("name", "")
            if not c_nm:
                continue
            key = (rel, c_nm)
            raw_classes[key] = cls
            by_bare_name.setdefault(c_nm, []).append(key)
            if not has_any_direct and (cls.get("schema_spec") or {}).get("is_direct_schema"):
                has_any_direct = True

    if is_partial and brain._graph:
        for nd in brain._graph.nodes.values():
            if nd.get("type") == "cls" and nd.get("rel_path") and nd.get("name"):
                key = (nd["rel_path"], nd["name"])
                if key not in raw_classes:
                    raw_classes[key] = nd
                    by_bare_name.setdefault(nd["name"], []).append(key)
                    if not has_any_direct and (nd.get("schema_spec") or {}).get("is_direct_schema"):
                        has_any_direct = True

    if not has_any_direct:
        if not is_partial:
            brain._cached_schema_reg_data = {}
            brain._cached_schema_reg_ver = idx_ver
        return {}

    resolved: dict[tuple[str, str], dict] = {}
    non_schemas: set[tuple[str, str]] = set()
    visiting: set[tuple[str, str]] = set()

    def _find_base_target(src_rel: str, base_name: str) -> tuple[str, str] | None:
        if (src_rel, base_name) in raw_classes:
            return (src_rel, base_name)
        sym_map = brain._symbol_imports.get(src_rel, {})
        imp_rel = sym_map.get(base_name)
        if imp_rel and (imp_rel, base_name) in raw_classes:
            return (imp_rel, base_name)
        info_s = brain._index.get(src_rel, {})
        bind = (info_s.get("import_bindings") or {}).get(base_name)
        if bind:
            orig_sym = bind.get("symbol") or base_name
            if imp_rel and (imp_rel, orig_sym) in raw_classes:
                return (imp_rel, orig_sym)
            cands_o = by_bare_name.get(orig_sym, [])
            if len(cands_o) == 1:
                return cands_o[0]
        cands = by_bare_name.get(base_name, [])
        if len(cands) == 1:
            return cands[0]
        return None

    def _resolve_one(key: tuple[str, str]) -> dict | None:
        if key in resolved:
            return resolved[key]
        if key in non_schemas or key in visiting:
            return None
        cls = raw_classes.get(key)
        if not cls:
            non_schemas.add(key)
            return None
        spec = cls.get("schema_spec")
        if not spec:
            non_schemas.add(key)
            return None

        visiting.add(key)
        src_rel, cls_nm = key
        is_proven = bool(spec.get("is_direct_schema"))
        all_bases_known = True

        req_fields: list[str] = []
        opt_fields: list[str] = []
        comp_fields: list[str] = []
        aliases: dict[str, list[str]] = {}
        pop_by_name = bool(spec.get("populate_by_name"))
        has_alias_gen = bool(spec.get("has_alias_generator"))
        extra_pol = spec.get("extra_policy", "ignore")
        has_custom_ser = bool(spec.get("has_custom_serializer"))

        for b_nm in cls.get("bases") or ():
            b_clean = b_nm.rsplit(".", 1)[-1].split("[", 1)[0].strip()
            if not b_clean or b_clean in _SCHEMA_NON_FIELD_BASES:
                continue
            b_key = _find_base_target(src_rel, b_clean)
            if b_key is None:
                all_bases_known = False
                continue
            b_res = _resolve_one(b_key)
            if b_res is None:
                all_bases_known = False
                continue
            if b_res.get("is_proven_schema"):
                is_proven = True
            if not b_res.get("all_bases_known", True):
                all_bases_known = False
            for rf in b_res["required_fields"]:
                if rf not in req_fields and rf not in opt_fields:
                    req_fields.append(rf)
            for of in b_res["optional_fields"]:
                if of not in opt_fields:
                    if of in req_fields:
                        req_fields.remove(of)
                    opt_fields.append(of)
            for cf in b_res["computed_fields"]:
                if cf not in comp_fields:
                    comp_fields.append(cf)
            for ak, av in b_res["field_aliases"].items():
                aliases.setdefault(ak, []).extend(x for x in av if x not in aliases.get(ak, []))
            pop_by_name = pop_by_name or b_res["populate_by_name"]
            has_alias_gen = has_alias_gen or b_res["has_alias_generator"]
            has_custom_ser = has_custom_ser or b_res["has_custom_serializer"]
            if b_res["extra_policy"] == "allow":
                extra_pol = "allow"

        # Child field overrides take precedence over base definitions
        for rf in spec.get("required_fields", ()):
            if rf in opt_fields:
                opt_fields.remove(rf)
            if rf not in req_fields:
                req_fields.append(rf)
        for of in spec.get("optional_fields", ()):
            if of in req_fields:
                req_fields.remove(of)
            if of not in opt_fields:
                opt_fields.append(of)
        for cf in spec.get("computed_fields", ()):
            if cf not in comp_fields:
                comp_fields.append(cf)
        for ak, av in (spec.get("field_aliases") or {}).items():
            aliases[ak] = list(dict.fromkeys([*(aliases.get(ak) or ()), *av]))

        visiting.discard(key)
        if not is_proven:
            non_schemas.add(key)
            return None

        req_groups: list[tuple[str, set[str]]] = []
        valid_req: set[str] = set()
        valid_res: set[str] = set(comp_fields)
        for f in req_fields:
            als = set(aliases.get(f, ()))
            # In Pydantic v2, when `Field(alias=...)` is declared without `populate_by_name=True`,
            # ONLY the alias is accepted on the request wire (sending the Python field name causes 422!).
            wg_req = ({f} | als) if (not als or pop_by_name) else als
            req_groups.append((f, wg_req))
            valid_req.update(wg_req)
            valid_res.update({f} | als)
        for f in opt_fields:
            als = set(aliases.get(f, ()))
            wg_opt = ({f} | als) if (not als or pop_by_name) else als
            valid_req.update(wg_opt)
            valid_res.update({f} | als)

        is_req_closed = bool(
            all_bases_known
            and extra_pol != "allow"
            and not has_alias_gen
        )
        is_res_closed = bool(
            all_bases_known
            and extra_pol != "allow"
            and not has_alias_gen
            and not has_custom_ser
        )

        out_spec = {
            "rel_path": src_rel,
            "name": cls_nm,
            "line": int(cls.get("line_start") or 1),
            "kind": spec.get("kind", "pydantic"),
            "is_proven_schema": True,
            "all_bases_known": all_bases_known,
            "required_fields": req_fields,
            "optional_fields": opt_fields,
            "computed_fields": comp_fields,
            "field_aliases": aliases,
            "populate_by_name": pop_by_name,
            "has_alias_generator": has_alias_gen,
            "extra_policy": extra_pol,
            "has_custom_serializer": has_custom_ser,
            "required_wire_groups": req_groups,
            "valid_req_keys": valid_req,
            "valid_res_keys": valid_res,
            "is_req_closed": is_req_closed,
            "is_res_closed": is_res_closed,
        }
        resolved[key] = out_spec
        return out_spec

    for k in list(raw_classes.keys()):
        _resolve_one(k)
    if not is_partial:
        brain._cached_schema_reg_data = resolved
        brain._cached_schema_reg_ver = idx_ver
    return resolved


def resolve_schema_for_file(
    brain: "ProjectBrain",
    registry: dict[tuple[str, str], dict],
    src_rel: str,
    schema_name: str,
) -> dict | None:
    if not schema_name:
        return None
    if (src_rel, schema_name) in registry:
        return registry[(src_rel, schema_name)]
    sym_map = brain._symbol_imports.get(src_rel, {})
    imp_rel = sym_map.get(schema_name)
    if imp_rel and (imp_rel, schema_name) in registry:
        return registry[(imp_rel, schema_name)]
    bind = (brain._index.get(src_rel, {}).get("import_bindings") or {}).get(schema_name)
    if bind:
        orig = bind.get("symbol") or schema_name
        if imp_rel and (imp_rel, orig) in registry:
            return registry[(imp_rel, orig)]
        matches_o = [v for (_, nm), v in registry.items() if nm == orig]
        if len(matches_o) == 1:
            return matches_o[0]
    matches = [v for (_, nm), v in registry.items() if nm == schema_name]
    if len(matches) == 1:
        return matches[0]
    return None


# ── Pillar 1: Cross-Stack HTTP Request & Response Schema Solver (PB011) ───────

def _extract_js_object_literal_info(obj_node) -> tuple[list[str], bool] | None:
    """Return `(keys, is_closed)` from a Tree-sitter JS/TS `object` node."""
    if obj_node is None or obj_node.type != "object":
        return None
    keys: list[str] = []
    is_closed = True
    for ch in obj_node.children:
        ct = ch.type
        if ct in ("{", "}", ","):
            continue
        if ct == "spread_element":
            is_closed = False
        elif ct == "shorthand_property_identifier":
            k = _t_text(ch).strip()
            if k and k not in keys:
                keys.append(k)
        elif ct == "pair":
            kn = ch.child_by_field_name("key")
            if kn is None:
                is_closed = False
                continue
            if kn.type == "computed_property_name":
                is_closed = False
            else:
                k = _t_text(kn).strip().strip("'\"`")
                if k and k not in keys:
                    keys.append(k)
        elif ct == "method_definition":
            nm = ch.child_by_field_name("name")
            if nm:
                k = _t_text(nm).strip()
                if k and k not in keys:
                    keys.append(k)
        else:
            is_closed = False
    return keys, is_closed


_GENERIC_URL_PREFIX_TOKENS = frozenset({
    "api", "base", "url", "uri", "host", "origin", "server", "backend", "endpoint",
    "http", "https", "public", "next", "vite", "react", "app", "env", "process",
    "import", "meta", "config", "core", "client", "web", "root", "prefix", "path",
    "window", "location", "const", "var", "let", "service", "gateway",
})


def _extract_js_url_and_query(url_node, const_urls: dict[str, str]) -> tuple[str | None, set[str], bool, str]:
    """Extract `(clean_path, query_keys, query_is_closed, leading_prefix_expr)` from a JS URL node."""
    if url_node is None:
        return None, set(), False, ""
    nt = url_node.type
    raw = ""
    leading_prefix = ""
    if nt == "identifier":
        ident = _t_text(url_node).strip()
        if ident not in const_urls:
            return None, set(), False, ""
        raw = const_urls[ident]
    elif nt in ("string", "template_string"):
        raw = _t_text(url_node).strip("'\"`")
    elif nt == "binary_expression":
        txt = _t_text(url_node)
        m_q = re.search(r"[`'\"](\/[A-Za-z0-9_/\-${}:.?=&]*)[`'\"]", txt)
        if not m_q:
            return None, set(), False, ""
        raw = m_q.group(1)
        leading_prefix = txt[: m_q.start()].strip().rstrip("+").strip()
    else:
        return None, set(), False, ""

    raw_s = raw.strip()
    m_lead = _URL_LEADING_HOST_RE.match(raw_s)
    stripped = _URL_LEADING_HOST_RE.sub("", raw_s)
    if m_lead and m_lead.group(0).startswith("${"):
        leading_prefix = m_lead.group(0)
    if not stripped.startswith("/"):
        return None, set(), False, ""

    q_keys: set[str] = set()
    q_closed = True
    if "?" in stripped:
        path_part, q_part = stripped.split("?", 1)
        if "${" in q_part:
            q_closed = False
        for seg in q_part.split("&"):
            if "=" in seg:
                qk = seg.split("=", 1)[0].strip()
                if qk and "${" not in qk:
                    q_keys.add(qk)
    else:
        path_part = stripped
    # Never treat `${BASE_URL}/` (zero concrete path segments after an unresolved prefix) as backend root `/`
    if leading_prefix and (path_part.rstrip("/") or "/") == "/":
        return None, set(), False, ""
    return path_part, q_keys, q_closed, leading_prefix


def _collect_js_http_callsites(
    rel_path: str,
    src_text: str,
    info: dict,
    brain: "ProjectBrain",
    wrappers_by_key: dict[tuple[str, str], dict],
    wrappers_by_name: dict[str, list[dict]],
) -> list[dict]:
    """Walk JS/TS AST to extract direct `fetch`, `axios`, and 1-hop wrapper HTTP call sites."""
    ext = Path(rel_path).suffix.lower()
    parser = get_parser(ext)
    if parser is None:
        return []
    try:
        tree = parser.parse(src_text.encode("utf-8", errors="replace"))
    except Exception:
        return []
    root = tree.root_node

    # Resolve wrapper functions callable in this file
    sym_map = brain._symbol_imports.get(rel_path, {})
    bindings = info.get("import_bindings") or {}

    def _resolve_wrapper(callee_expr: str) -> dict | None:
        if "." in callee_expr:
            recv, meth = callee_expr.rsplit(".", 1)
            if recv in _AXIOS_LIKE_RECEIVERS:
                return None
            imp_r = sym_map.get(recv)
            if imp_r:
                if (imp_r, meth) in wrappers_by_key:
                    return wrappers_by_key[(imp_r, meth)]
            cands_m = wrappers_by_name.get(meth, [])
            if len(cands_m) == 1 and imp_r == cands_m[0]["rel_path"]:
                return cands_m[0]
            return None
        if (rel_path, callee_expr) in wrappers_by_key:
            return wrappers_by_key[(rel_path, callee_expr)]
        imp_r = sym_map.get(callee_expr)
        orig_nm = (bindings.get(callee_expr) or {}).get("symbol") or callee_expr
        if imp_r and (imp_r, orig_nm) in wrappers_by_key:
            return wrappers_by_key[(imp_r, orig_nm)]
        cands = wrappers_by_name.get(orig_nm, [])
        if len(cands) == 1 and (imp_r == cands[0]["rel_path"] or orig_nm in bindings):
            return cands[0]
        return None

    # Pre-collect per-scope local `const x = { ... }` objects and `const u = \`.../api/...\`` strings
    # plus response variable property reads (`data.prop` / `res.data.prop`)
    callsites: list[dict] = []
    src_lines = src_text.splitlines()

    # Detect custom axios instances (`const backendApi = axios.create(...)` or imported from a file using `axios.create`)
    custom_axios_receivers: set[str] = set(_AXIOS_LIKE_RECEIVERS)
    for m_axc in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*axios\s*\.\s*create\s*\(", src_text):
        custom_axios_receivers.add(m_axc.group(1))
    for loc_nm, imp_r in sym_map.items():
        if imp_r and loc_nm not in custom_axios_receivers:
            imp_src = brain._get_file_text(imp_r) or ""
            if "axios.create" in imp_src:
                custom_axios_receivers.add(loc_nm)

    transport_helper_names: set[str] = {
        fn["name"] for fn in info.get("functions", []) if fn.get("http_transport_spec")
    }
    _DYNAMIC_SUBPATH_PARAMS = frozenset({"path", "endpoint", "route", "url", "uri", "subpath", "suffix"})

    def _scope_param_names(sc_nd) -> set[str]:
        p_out: set[str] = set()
        params_nd = sc_nd.child_by_field_name("parameters")
        if params_nd is None:
            p0 = sc_nd.child_by_field_name("parameter")
            if p0 is not None and p0.type == "identifier":
                p_out.add(_t_text(p0).strip())
            return p_out
        for ch in params_nd.named_children:
            if ch.type == "identifier":
                p_out.add(_t_text(ch).strip())
            elif ch.type in ("required_parameter", "optional_parameter"):
                pat = ch.child_by_field_name("pattern")
                if pat is not None and pat.type == "identifier":
                    p_out.add(_t_text(pat).strip())
            elif ch.type == "assignment_pattern":
                lft = ch.child_by_field_name("left")
                if lft is not None and lft.type == "identifier":
                    p_out.add(_t_text(lft).strip())
        return p_out

    def _has_dynamic_subpath_param(u_path: str, sc_params: set[str]) -> bool:
        dyn_params = sc_params & _DYNAMIC_SUBPATH_PARAMS
        if not dyn_params or "${" not in u_path:
            return False
        for expr in re.findall(r"\$\{([^}]+)\}", u_path):
            if expr.strip() in dyn_params:
                return True
        return False

    def _scan_scope(scope_node, scope_name: str, parent_const_urls: dict[str, str] | None = None) -> None:
        local_objs: dict[str, tuple[list[str], bool]] = {}
        mutated_vars: set[str] = set()
        const_urls: dict[str, str] = dict(parent_const_urls or {})
        call_nodes: list = []

        stack = [scope_node]
        while stack:
            nd = stack.pop()
            nt = nd.type
            if nd is not scope_node and nt in (
                "function_declaration", "function_expression", "arrow_function", "method_definition",
            ):
                nm_n = nd.child_by_field_name("name")
                sub_nm = _t_text(nm_n) if nm_n else scope_name
                if nd.parent and nd.parent.type == "variable_declarator":
                    vn = nd.parent.child_by_field_name("name")
                    if vn and vn.type == "identifier":
                        sub_nm = _t_text(vn)
                _scan_scope(nd, sub_nm, const_urls)
                continue
            if nt == "variable_declarator":
                nm_n = nd.child_by_field_name("name")
                val_n = nd.child_by_field_name("value")
                if nm_n and nm_n.type == "identifier" and val_n is not None:
                    v_nm = _t_text(nm_n)
                    if val_n.type == "object":
                        oi = _extract_js_object_literal_info(val_n)
                        if oi is not None:
                            local_objs[v_nm] = oi
                    elif val_n.type in ("string", "template_string"):
                        const_urls[v_nm] = _t_text(val_n).strip("'\"`")
            elif nt in ("assignment_expression", "augmented_assignment_expression"):
                ln_n = nd.child_by_field_name("left")
                if ln_n is not None:
                    l_txt = _t_text(ln_n).strip()
                    base_v = re.split(r"[.\[]", l_txt, maxsplit=1)[0].strip()
                    if base_v:
                        mutated_vars.add(base_v)
            elif nt == "call_expression":
                fn_c = nd.child_by_field_name("function")
                args_c = nd.child_by_field_name("arguments")
                if fn_c is not None and _t_text(fn_c) == "Object.assign" and args_c is not None:
                    nc = args_c.named_children
                    if nc and nc[0].type == "identifier":
                        mutated_vars.add(_t_text(nc[0]))
                call_nodes.append(nd)
            if nd.named_child_count:
                stack.extend(reversed(nd.named_children))

        scope_txt = _t_text(scope_node)
        scope_start_ln = _t_line(scope_node)
        scope_params = _scope_param_names(scope_node)

        def _resolve_obj_arg(arg_nd) -> tuple[list[str], bool] | None:
            if arg_nd is None:
                return None
            if arg_nd.type == "object":
                return _extract_js_object_literal_info(arg_nd)
            if arg_nd.type == "identifier":
                v_nm = _t_text(arg_nd).strip()
                if v_nm in local_objs:
                    keys, is_cl = local_objs[v_nm]
                    return keys, (is_cl and v_nm not in mutated_vars)
            return None

        def _bound_lhs_var(c_nd) -> tuple[str | None, list[str]]:
            """If `const x = await <c_nd>` or `const { a, b } = await <c_nd>`, return `(var_name, destructured_keys)`."""
            cur = c_nd.parent
            while cur is not None and cur.type in (
                "await_expression",
                "parenthesized_expression",
                "as_expression",
                "satisfies_expression",
                "type_assertion",
                "non_null_expression",
            ):
                cur = cur.parent
            if cur is not None and cur.type == "variable_declarator":
                nn = cur.child_by_field_name("name")
                if nn is not None:
                    if nn.type == "identifier":
                        return _t_text(nn), []
                    if nn.type == "object_pattern":
                        d_keys: list[str] = []
                        has_rest = False
                        for pc in nn.children:
                            if pc.type == "rest_pattern":
                                has_rest = True
                            elif pc.type == "shorthand_property_identifier_pattern":
                                d_keys.append(_t_text(pc))
                            elif pc.type == "pair_pattern":
                                kn = pc.child_by_field_name("key")
                                if kn:
                                    d_keys.append(_t_text(kn).strip("'\""))
                        return (None, [] if has_rest else d_keys)
            return None, []

        def _extract_cb_prop_reads(cb_nd) -> tuple[list[str], list[str]]:
            """CST-exact property-read extraction for callback `(item) => ...` or `({ a, b }) => ...`."""
            if cb_nd is None or cb_nd.type not in ("arrow_function", "function_expression"):
                return [], []
            p0 = cb_nd.child_by_field_name("parameter")
            if p0 is None:
                params_nd = cb_nd.child_by_field_name("parameters")
                if params_nd is not None and params_nd.named_children:
                    p0 = params_nd.named_children[0]
            if p0 is None:
                return [], []
            if p0.type in ("required_parameter", "optional_parameter"):
                pat = p0.child_by_field_name("pattern")
                if pat is not None:
                    p0 = pat

            body_nd = cb_nd.child_by_field_name("body")
            direct_reads: list[str] = []
            nested_item_reads: list[str] = []

            if p0.type == "object_pattern":
                for pc in p0.children:
                    if pc.type == "shorthand_property_identifier_pattern":
                        pk = _t_text(pc).strip()
                        if pk and pk not in _JS_ARRAY_AND_RES_BUILTINS and pk not in direct_reads:
                            direct_reads.append(pk)
                    elif pc.type == "pair_pattern":
                        kn = pc.child_by_field_name("key")
                        if kn:
                            pk = _t_text(kn).strip("'\"`")
                            if pk and pk not in _JS_ARRAY_AND_RES_BUILTINS and pk not in direct_reads:
                                direct_reads.append(pk)
                return direct_reads[:10], nested_item_reads[:10]

            if p0.type != "identifier" or body_nd is None:
                return [], []
            it_var = _t_text(p0).strip()
            if not it_var:
                return [], []

            st_walk = [body_nd]
            while st_walk:
                cur = st_walk.pop()
                ct = cur.type
                if ct == "member_expression":
                    obj_n = cur.child_by_field_name("object")
                    prop_n = cur.child_by_field_name("property")
                    if obj_n is not None and prop_n is not None and obj_n.type == "identifier" and _t_text(obj_n) == it_var:
                        raw_mem = _t_text(cur)
                        pk = _t_text(prop_n)
                        if "?." not in raw_mem and pk not in _JS_ARRAY_AND_RES_BUILTINS:
                            ln_idx = max(0, _t_line(cur) - 1)
                            ln_full = src_lines[ln_idx] if ln_idx < len(src_lines) else ""
                            col_pos = int(cur.start_point[1])
                            if (
                                "||" not in ln_full
                                and "??" not in ln_full
                                and not re.search(rf"\bin\s+{re.escape(it_var)}\b", ln_full)
                                and "?" not in ln_full[: col_pos + len(it_var) + 2]
                            ):
                                if pk not in direct_reads:
                                    direct_reads.append(pk)
                elif ct == "call_expression":
                    fn_sub = cur.child_by_field_name("function")
                    args_sub = cur.child_by_field_name("arguments")
                    if fn_sub is not None and fn_sub.type == "member_expression" and args_sub is not None:
                        o_s = fn_sub.child_by_field_name("object")
                        p_s = fn_sub.child_by_field_name("property")
                        if (
                            o_s is not None
                            and p_s is not None
                            and o_s.type == "identifier"
                            and _t_text(o_s) == it_var
                            and _t_text(p_s) in ("map", "forEach", "filter", "find", "some", "every", "flatMap")
                            and args_sub.named_children
                        ):
                            sub_r, _ = _extract_cb_prop_reads(args_sub.named_children[0])
                            for sr in sub_r:
                                if sr not in nested_item_reads:
                                    nested_item_reads.append(sr)
                if cur.named_child_count:
                    st_walk.extend(reversed(cur.named_children))
            return direct_reads[:10], nested_item_reads[:10]

        def _collect_var_prop_reads(
            var_nm: str,
            after_line: int,
            unwrap_axios_data: bool = False,
        ) -> tuple[list[str], list[str]]:
            if not var_nm:
                return [], []
            root_reads: list[str] = []
            item_reads: list[str] = []
            rel_start = max(0, after_line - scope_start_ln)
            sub_lines = src_lines[scope_start_ln - 1 + rel_start : _t_line(scope_node) + scope_txt.count("\n")]
            sub_txt = "\n".join(sub_lines)
            if unwrap_axios_data:
                rx = re.compile(rf"\b{re.escape(var_nm)}\.data\.([A-Za-z_]\w*)\b")
            else:
                rx = re.compile(rf"\b{re.escape(var_nm)}\.([A-Za-z_]\w*)\b")
            for m_p in rx.finditer(sub_txt):
                pk = m_p.group(1)
                if pk in _JS_ARRAY_AND_RES_BUILTINS:
                    continue
                ln_s = sub_txt.rfind("\n", 0, m_p.start()) + 1
                ln_e = sub_txt.find("\n", m_p.end())
                ln_full = sub_txt[ln_s : (ln_e if ln_e != -1 else len(sub_txt))]
                if (
                    "||" in ln_full
                    or "??" in ln_full
                    or bool(re.search(rf"\bin\s+{re.escape(var_nm)}\b", ln_full))
                    or "?" in ln_full[: m_p.start() - ln_s + len(var_nm) + 2]
                ):
                    continue
                if pk not in root_reads:
                    root_reads.append(pk)

            # CST-exact callback extraction for `.map()` / `.filter()` / `.forEach()` etc.
            # (handles nested calls like `fmt(t.id)`, multi-line bodies, TS types, and destructured params)
            target_recv = f"{var_nm}.data" if unwrap_axios_data else var_nm
            _ARRAY_ITER_METHODS = frozenset({"map", "forEach", "filter", "find", "some", "every", "flatMap"})
            for cn in call_nodes:
                if _t_line(cn) < after_line:
                    continue
                f_n = cn.child_by_field_name("function")
                a_n = cn.child_by_field_name("arguments")
                if f_n is None or f_n.type != "member_expression" or a_n is None or not a_n.named_children:
                    continue
                obj_n = f_n.child_by_field_name("object")
                prop_n = f_n.child_by_field_name("property")
                if obj_n is None or prop_n is None:
                    continue
                if _t_text(prop_n) not in _ARRAY_ITER_METHODS:
                    continue
                if re.sub(r"\s+", "", _t_text(obj_n)) != target_recv:
                    continue
                cb_reads, _ = _extract_cb_prop_reads(a_n.named_children[0])
                for pk in cb_reads:
                    if pk not in item_reads:
                        item_reads.append(pk)
            return root_reads[:10], item_reads[:10]

        def _extract_promise_chain_json_reads(fetch_call_nd) -> tuple[list[str], list[str]]:
            """Walk chained `fetch(...).then(r => r.json()).then(d => d.prop)` CST nodes."""
            saw_json = False
            cur = fetch_call_nd
            for _ in range(6):
                p = cur.parent
                while p is not None and p.type == "parenthesized_expression":
                    p = p.parent
                if p is None or p.type != "member_expression":
                    break
                prop_n = p.child_by_field_name("property")
                gp = p.parent
                if prop_n is None or gp is None or gp.type != "call_expression":
                    break
                m_name = _t_text(prop_n)
                args_n = gp.child_by_field_name("arguments")
                if m_name == "then" and args_n is not None and args_n.named_children:
                    cb0 = args_n.named_children[0]
                    if not saw_json:
                        if ".json(" in _t_text(cb0):
                            saw_json = True
                    else:
                        return _extract_cb_prop_reads(cb0)
                cur = gp
            return [], []

        for c_nd in call_nodes:
            fn_c = c_nd.child_by_field_name("function")
            args_c = c_nd.child_by_field_name("arguments")
            if fn_c is None or args_c is None:
                continue
            callee_txt = re.sub(r"\s+", "", _t_text(fn_c))
            n_args = args_c.named_children
            c_line = _t_line(c_nd)

            # 1. Direct `fetch(url, opts)`
            if callee_txt in ("fetch", "window.fetch", "globalThis.fetch") and n_args:
                u_path, q_keys, q_closed, u_prefix = _extract_js_url_and_query(n_args[0], const_urls)
                if not u_path or _has_dynamic_subpath_param(u_path, scope_params):
                    continue
                verb = "GET"
                verb_dynamic = False
                req_keys: list[str] | None = None
                req_closed = False
                if len(n_args) >= 2:
                    opts_nd = n_args[1]
                    if opts_nd.type == "object":
                        has_spread = False
                        explicit_method = False
                        for ch in opts_nd.children:
                            if ch.type == "spread_element":
                                has_spread = True
                                if explicit_method:
                                    verb_dynamic = True
                            elif ch.type == "pair":
                                kn = ch.child_by_field_name("key")
                                vn = ch.child_by_field_name("value")
                                if not (kn and vn):
                                    continue
                                k_s = _t_text(kn).strip("'\"`")
                                if k_s == "method":
                                    if vn.type in ("string", "template_string") and "${" not in _t_text(vn):
                                        verb = _t_text(vn).strip("'\"`").upper()
                                        explicit_method = True
                                    else:
                                        verb_dynamic = True
                                elif k_s == "body":
                                    if vn.type == "call_expression":
                                        bfn = vn.child_by_field_name("function")
                                        bargs = vn.child_by_field_name("arguments")
                                        if bfn and _t_text(bfn).strip() == "JSON.stringify" and bargs and bargs.named_children:
                                            oi = _resolve_obj_arg(bargs.named_children[0])
                                            if oi is not None:
                                                req_keys, req_closed = oi
                        if has_spread and not explicit_method:
                            verb_dynamic = True
                    else:
                        verb_dynamic = True
                if verb_dynamic:
                    continue

                res_var, _ = _bound_lhs_var(c_nd)
                res_reads: list[str] = []
                res_item_reads: list[str] = []
                if res_var:
                    m_jv = re.search(
                        rf"\b(?:const|let|var)\s+(?:(\w+)(?:\s*:\s*[^=\n]+)?|\{{\s*([^}}]+)\s*\}}(?:\s*:\s*[^=\n]+)?)\s*="
                        rf"\s*(?:\(\s*)?await\s+{re.escape(res_var)}\.json\s*\(\s*\)(?:\s*\))?(?:\s+as\s+[^;\n]+)?",
                        scope_txt,
                    )
                    if m_jv:
                        if m_jv.group(1):
                            res_reads, res_item_reads = _collect_var_prop_reads(
                                m_jv.group(1), c_line, unwrap_axios_data=False
                            )
                        elif m_jv.group(2) and "..." not in m_jv.group(2):
                            for tok in m_jv.group(2).split(","):
                                k_d = tok.split(":", 1)[0].split("=", 1)[0].strip()
                                if k_d.isidentifier() and k_d not in _JS_ARRAY_AND_RES_BUILTINS:
                                    res_reads.append(k_d)
                else:
                    res_reads, res_item_reads = _extract_promise_chain_json_reads(c_nd)

                callsites.append({
                    "rel_path": rel_path,
                    "scope": scope_name,
                    "line": c_line,
                    "method": verb,
                    "url": u_path,
                    "norm_url": _norm_http_path(u_path),
                    "url_prefix_expr": u_prefix,
                    "req_keys": req_keys,
                    "req_closed": req_closed,
                    "query_keys": q_keys,
                    "query_closed": q_closed,
                    "res_keys": res_reads,
                    "res_item_keys": res_item_reads,
                    "via_wrapper": None,
                })
                continue

            # 2. Direct `axios.<verb>(url, bodyOrCfg, cfg)` / `api.<verb>(...)` / custom `axios.create()` instances
            m_ax = re.match(r"^([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete)$", callee_txt, re.IGNORECASE)
            if m_ax and m_ax.group(1) in custom_axios_receivers and n_args:
                verb = m_ax.group(2).upper()
                u_path, q_keys, q_closed, u_prefix = _extract_js_url_and_query(n_args[0], const_urls)
                if not u_path or _has_dynamic_subpath_param(u_path, scope_params):
                    continue
                req_keys = None
                req_closed = False
                cfg_nd = None
                if verb in ("POST", "PUT", "PATCH"):
                    if len(n_args) >= 2:
                        oi = _resolve_obj_arg(n_args[1])
                        if oi is not None:
                            req_keys, req_closed = oi
                    if len(n_args) >= 3:
                        cfg_nd = n_args[2]
                elif len(n_args) >= 2:
                    cfg_nd = n_args[1]

                if cfg_nd is not None:
                    if cfg_nd.type == "object":
                        for ch in cfg_nd.children:
                            if ch.type == "spread_element":
                                q_closed = False
                            elif ch.type == "shorthand_property_identifier" and _t_text(ch).strip() == "params":
                                if "params" in local_objs:
                                    p_keys, p_cl = local_objs["params"]
                                    q_keys.update(p_keys)
                                    q_closed = q_closed and p_cl and ("params" not in mutated_vars)
                                else:
                                    q_closed = False
                            elif ch.type == "pair":
                                kn = ch.child_by_field_name("key")
                                vn = ch.child_by_field_name("value")
                                if kn and vn and _t_text(kn).strip("'\"`") == "params":
                                    pi = _resolve_obj_arg(vn)
                                    if pi is not None:
                                        q_keys.update(pi[0])
                                        q_closed = q_closed and pi[1]
                                    else:
                                        q_closed = False
                    else:
                        # Non-object 2nd/3rd arg (e.g. `http.Get(url, params)` or `axios.get(url, config)`)
                        q_closed = False

                res_var, destr = _bound_lhs_var(c_nd)
                res_reads = []
                res_item_reads = []
                if res_var:
                    res_reads, res_item_reads = _collect_var_prop_reads(res_var, c_line, unwrap_axios_data=True)
                elif "data" in destr:
                    res_reads, res_item_reads = _collect_var_prop_reads("data", c_line, unwrap_axios_data=False)

                callsites.append({
                    "rel_path": rel_path,
                    "scope": scope_name,
                    "line": c_line,
                    "method": verb,
                    "url": u_path,
                    "norm_url": _norm_http_path(u_path),
                    "url_prefix_expr": u_prefix,
                    "req_keys": req_keys,
                    "req_closed": req_closed,
                    "query_keys": q_keys,
                    "query_closed": q_closed,
                    "res_keys": res_reads,
                    "res_item_keys": res_item_reads,
                    "via_wrapper": None,
                })
                continue

            # 3. 1-Hop Wrapper Function Call (`billingApi.createInvoice({ ... })` or `createInvoice({ ... })`)
            w_spec = _resolve_wrapper(callee_txt)
            if w_spec is not None:
                # Avoid checking a wrapper calling itself recursively
                if w_spec["rel_path"] == rel_path and w_spec["fn_name"] == scope_name:
                    continue
                verb = w_spec["method"]
                req_keys = None
                req_closed = False
                b_idx = w_spec.get("body_param_idx")
                if b_idx is not None and b_idx < len(n_args):
                    oi = _resolve_obj_arg(n_args[b_idx])
                    if oi is not None:
                        req_keys, req_closed = oi

                res_reads = []
                res_item_reads = []
                if w_spec.get("returns_json"):
                    res_var, destr = _bound_lhs_var(c_nd)
                    if res_var:
                        res_reads, res_item_reads = _collect_var_prop_reads(res_var, c_line, unwrap_axios_data=False)
                    elif destr:
                        res_reads = [k for k in destr if k not in _JS_ARRAY_AND_RES_BUILTINS]

                if req_keys is not None or res_reads or res_item_reads:
                    callsites.append({
                        "rel_path": rel_path,
                        "scope": scope_name,
                        "line": c_line,
                        "method": verb,
                        "url": w_spec["url"],
                        "norm_url": w_spec["norm_url"],
                        "req_keys": req_keys,
                        "req_closed": req_closed,
                        "query_keys": set(),
                        "query_closed": False,
                        "res_keys": res_reads,
                        "res_item_keys": res_item_reads,
                        "via_wrapper": f"{w_spec['fn_name']} ({w_spec['rel_path']})",
                    })

    _scan_scope(root, Path(rel_path).stem)
    return callsites


def verify_cross_stack_http_schemas(
    brain: "ProjectBrain",
    focus_rels: set[str] | None = None,
    lines_cache: dict[str, list[str]] | None = None,
) -> tuple[list[str], list[dict], dict]:
    """Verify JS/TS HTTP requests & response reads against backend FastAPI/Pydantic/Express route contracts."""
    CodeGraph.propagate_router_mounts(brain._index, brain._import_targets, brain._symbol_imports)
    schema_registry = build_resolved_schema_registry(brain)

    # 1. Index backend routes by `(method, norm_url)` and unambiguous 2-segment suffix
    routes_exact_all: dict[tuple[str, str], list[dict]] = {}
    routes_by_tail: dict[tuple[str, str], list[dict]] = {}
    routes_by_last_seg: dict[tuple[str, str], int] = {}
    verbs_by_norm: dict[str, set[str]] = {}
    partial_route_norms: set[str] = set()

    for r_rel, r_info in brain._index.items():
        if _is_test_file_path(r_rel):
            continue
        is_partial_file = (r_info.get("parse_status") == "partial")
        fn_by_nm = {fn["name"]: fn for fn in r_info.get("functions", []) if not fn.get("parent_class")}
        for r in r_info.get("routes", []):
            verb = (r.get("method") or "GET").upper()
            full_p = r.get("full_path") or r.get("path") or "/"
            norm_p = _norm_http_path(full_p)
            verbs_by_norm.setdefault(norm_p, set()).add(verb)
            if is_partial_file:
                partial_route_norms.add(norm_p)

            b_schema = (
                resolve_schema_for_file(brain, schema_registry, r_rel, r["body_schema"])
                if r.get("body_schema") and not r.get("body_is_dynamic")
                else None
            )
            res_schema = (
                resolve_schema_for_file(brain, schema_registry, r_rel, r["response_schema"])
                if r.get("response_schema") and not r.get("response_is_dynamic")
                else None
            )
            handler_nm = r.get("handler") or ""
            h_fn = fn_by_nm.get(handler_nm)
            closed_ret_keys = set(h_fn.get("return_keys") or ()) if (h_fn and not r.get("response_is_dynamic")) else None

            rc = {
                "rel_path": r_rel,
                "line": int(r.get("line") or 1),
                "method": verb,
                "full_path": full_p,
                "norm_path": norm_p,
                "handler": handler_nm or r_rel,
                "body_schema": b_schema,
                "body_is_dynamic": bool(r.get("body_is_dynamic")),
                "multi_body_keys": r.get("multi_body_keys"),
                "response_schema": res_schema,
                "response_is_list": bool(r.get("response_is_list")),
                "closed_return_keys": closed_ret_keys,
                "required_query_params": list(r.get("required_query_params") or ()),
            }
            bucket = routes_exact_all.setdefault((verb, norm_p), [])
            if not any(x["rel_path"] == rc["rel_path"] and x["line"] == rc["line"] for x in bucket):
                bucket.append(rc)
                n_segs = [s for s in norm_p.split("/") if s]
                if n_segs and n_segs[-1] != "{*}":
                    l_key = (verb, "/" + n_segs[-1].lower())
                    routes_by_last_seg[l_key] = routes_by_last_seg.get(l_key, 0) + 1
            tail = _path_tail_key(norm_p)
            if tail:
                routes_by_tail.setdefault((verb, tail), []).append(rc)

    # Keep only unambiguous `(verb, norm_path)` entries in `routes_exact`; multiple colliding
    # routers sharing the same unmounted path (e.g. `mfa.py`, `openai.py`, `ollama.py` all declaring `/verify`)
    # must never silently overwrite each other.
    routes_exact: dict[tuple[str, str], dict] = {}
    ambiguous_exact: set[tuple[str, str]] = set()
    ambiguous_norms: set[str] = set()
    for ek, cands in routes_exact_all.items():
        if len(cands) == 1:
            routes_exact[ek] = cands[0]
        else:
            ambiguous_exact.add(ek)
            ambiguous_norms.add(ek[1])

    if not routes_exact and not ambiguous_exact:
        return [], [], {"http_routes_indexed": 0, "http_callsites_verified": 0, "schemas_indexed": len(schema_registry)}

    # 2. Index 1-hop JS/TS wrapper functions
    wrappers_by_key: dict[tuple[str, str], dict] = {}
    wrappers_by_name: dict[str, list[dict]] = {}
    for w_rel, w_info in brain._index.items():
        if Path(w_rel).suffix.lower() not in _JS_TS_EXTS:
            continue
        for fn in w_info.get("functions", []):
            ws = fn.get("http_wrapper_spec")
            if not ws and brain._graph is not None:
                ws = (brain._graph.nodes.get(fn_id(w_rel, fn["name"], fn.get("parent_class"))) or {}).get("http_wrapper_spec")
            if ws:
                w_rec = {**ws, "rel_path": w_rel, "fn_name": fn["name"]}
                wrappers_by_key[(w_rel, fn["name"])] = w_rec
                wrappers_by_name.setdefault(fn["name"], []).append(w_rec)

    # 3. Determine JS/TS files to scan
    backend_touched = bool(
        focus_rels
        and any(
            (brain._index.get(fr, {}).get("routes") or any(c.get("schema_spec") for c in brain._index.get(fr, {}).get("classes", [])))
            for fr in focus_rels
        )
    )
    js_Wrapper_touched = bool(
        focus_rels and any( any(fn.get("http_wrapper_spec") for fn in brain._index.get(fr, {}).get("functions", [])) for fr in focus_rels )
    )
    candidate_fe_rels: list[str] = []
    for rel, info in brain._index.items():
        if _is_test_file_path(rel) or Path(rel).suffix.lower() not in _JS_TS_EXTS:
            continue
        if focus_rels is not None and not backend_touched and not js_Wrapper_touched and rel not in focus_rels:
            continue
        candidate_fe_rels.append(rel)

    diags: list[str] = []
    hotspots: list[dict] = []
    seen_keys: set[tuple] = set()
    callsites_checked = 0

    def _url_template_matches(pattern_norm: str, fe_norm: str) -> bool:
        if "{*}" not in pattern_norm:
            return False
        p_segs = [s for s in pattern_norm.split("/") if s]
        f_segs = [s for s in fe_norm.split("/") if s]
        if len(p_segs) < 2 or len(p_segs) != len(f_segs):
            return False
        static_hits = 0
        for ps, fs in zip(p_segs, f_segs):
            if ps == "{*}":
                continue
            if ps.lower() != fs.lower():
                return False
            static_hits += 1
        return static_hits >= 1

    def _resolve_verbs_and_pattern(fe_norm: str) -> tuple[set[str] | None, str]:
        direct = verbs_by_norm.get(fe_norm)
        if direct:
            return direct, fe_norm
        param_hits = [np for np in verbs_by_norm if _url_template_matches(np, fe_norm)]
        if len(param_hits) == 1:
            return verbs_by_norm[param_hits[0]], param_hits[0]
        return None, fe_norm

    def _is_unresolved_prefix_collision(url_prefix_expr: str, fe_norm: str, rc: dict, verb: str) -> bool:
        if not url_prefix_expr:
            return False
        fe_segs = [s for s in fe_norm.split("/") if s]
        if len(fe_segs) <= 1 and routes_by_last_seg.get((verb, fe_norm.lower()), 0) > 1:
            return True
        if len(fe_segs) <= 2:
            pref_toks = _split_ident_tokens(url_prefix_expr) - _GENERIC_URL_PREFIX_TOKENS
            if pref_toks:
                target_hay = f"{rc.get('full_path', '')} {rc.get('rel_path', '')} {rc.get('handler', '')}".lower()
                if not any(tok in target_hay for tok in pref_toks):
                    return True
        return False

    def _match_route(verb: str, fe_norm: str, url_prefix_expr: str = "") -> dict | None:
        if (verb, fe_norm) in ambiguous_exact or ("ANY", fe_norm) in ambiguous_exact:
            return None
        cand: dict | None = None
        if (verb, fe_norm) in routes_exact:
            cand = routes_exact[(verb, fe_norm)]
        elif ("ANY", fe_norm) in routes_exact:
            cand = routes_exact[("ANY", fe_norm)]
        else:
            param_cands = [
                rc for (v0, np0), rc in routes_exact.items()
                if v0 in (verb, "ANY") and _url_template_matches(np0, fe_norm)
            ]
            if len(param_cands) == 1:
                cand = param_cands[0]
            else:
                tail = _path_tail_key(fe_norm)
                if tail:
                    cands = routes_by_tail.get((verb, tail), [])
                    if len(cands) == 1 and (
                        cands[0]["norm_path"].endswith(fe_norm.lower())
                        or fe_norm.lower().endswith(cands[0]["norm_path"].lower())
                    ):
                        cand = cands[0]
        if cand is not None and _is_unresolved_prefix_collision(url_prefix_expr, fe_norm, cand, verb):
            return None
        return cand

    def _emit_http(
        key: tuple,
        msg: str,
        *,
        category: str,
        rel: str,
        line: int,
        peer: str,
        summary: str,
        severity: str = "error",
        confidence: str = "proven",
    ) -> None:
        if key in seen_keys:
            return
        seen_keys.add(key)
        if severity == "error":
            diags.append(msg)
        hotspots.append({
            "severity": severity,
            "confidence": confidence,
            "rule_id": "PB011",
            "category": category,
            "rel_path": rel,
            "line": line,
            "peer_target": peer,
            "summary": summary,
            "recommended_read": f"{rel}:{max(1, line - 8)}-{line + 14}",
            "_diag_msg": msg,
        })

    for fe_rel in sorted(candidate_fe_rels):
        if lines_cache is not None and fe_rel in lines_cache:
            src_txt = "\n".join(lines_cache[fe_rel])
        else:
            src_txt = brain._get_file_text(fe_rel) or ""
        if not src_txt:
            continue
        fe_info = brain._index.get(fe_rel, {})
        cs_list = _collect_js_http_callsites(fe_rel, src_txt, fe_info, brain, wrappers_by_key, wrappers_by_name)
        for cs in cs_list:
            verb = cs["method"]
            fe_norm = cs["norm_url"]
            c_ln = cs["line"]
            scope = cs["scope"]
            via_str = f" (via `{cs['via_wrapper']}`)" if cs.get("via_wrapper") else ""

            url_prefix_expr = cs.get("url_prefix_expr") or ""

            # Check exact or parametric-URL verb mismatch first
            allowed_v, matched_norm = _resolve_verbs_and_pattern(fe_norm)
            if (
                allowed_v
                and matched_norm not in partial_route_norms
                and matched_norm not in ambiguous_norms
                and verb in _HTTP_VERBS
                and "ANY" not in allowed_v
                and "ALL" not in allowed_v
                and verb not in allowed_v
            ):
                peer_rc = next(
                    (rc for (v0, np0), rc in routes_exact.items() if np0 == matched_norm),
                    None,
                )
                if peer_rc is not None and _is_unresolved_prefix_collision(url_prefix_expr, fe_norm, peer_rc, next(iter(allowed_v))):
                    continue
                if focus_rels is None or fe_rel in focus_rels or backend_touched:
                    peer_loc = f"{peer_rc['rel_path']}:L{peer_rc['line']}" if peer_rc else ""
                    _emit_http(
                        ("verb", fe_rel, c_ln, verb, fe_norm),
                        (
                            f"- ⚠️ **[6D HTTP Route Method Mismatch]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                            f"Frontend calls `{verb} {cs['url']}`{via_str}, but backend route only accepts "
                            f"`{', '.join(sorted(allowed_v))}`."
                        ),
                        category="http_verb_mismatch",
                        rel=fe_rel,
                        line=c_ln,
                        peer=peer_loc,
                        summary=f"Calls {verb} {cs['url']}, but backend only accepts {', '.join(sorted(allowed_v))}",
                    )
                continue

            rc = _match_route(verb, fe_norm, url_prefix_expr)
            if rc is None:
                continue
            if focus_rels is not None and fe_rel not in focus_rels and rc["rel_path"] not in focus_rels:
                b_sc_rel = (rc["body_schema"] or {}).get("rel_path")
                r_sc_rel = (rc["response_schema"] or {}).get("rel_path")
                if b_sc_rel not in focus_rels and r_sc_rel not in focus_rels and not js_Wrapper_touched:
                    continue

            callsites_checked += 1
            peer_route_loc = f"{rc['rel_path']}:L{rc['line']}"

            # A. Request Body Schema Verification
            b_schema = rc.get("body_schema")
            sent_keys = cs.get("req_keys")
            if b_schema is not None and sent_keys is not None:
                sent_set = set(sent_keys)
                sc_loc = f"{b_schema['rel_path']}:L{b_schema['line']}"
                sc_nm = b_schema["name"]
                renamed_be_fields: set[str] = set()

                if b_schema["is_req_closed"]:
                    norm_be_map: dict[str, str] = {
                        normalize_ident_key(vk): vk for vk in b_schema["valid_req_keys"] if normalize_ident_key(vk)
                    }
                    req_wire_keys: set[str] = {
                        k for _, wg in b_schema["required_wire_groups"] for k in wg
                    } | {cf for cf, _ in b_schema["required_wire_groups"]}
                    unpopulated_be = [vk for vk in b_schema["valid_req_keys"] if vk not in sent_set]
                    for sk in sent_keys:
                        if sk in b_schema["valid_req_keys"]:
                            continue
                        nk = normalize_ident_key(sk)
                        matched_be = norm_be_map.get(nk) if nk else None
                        matched_by_fuzzy = False
                        if matched_be is None and unpopulated_be:
                            sk_toks = _split_ident_tokens(sk) - _GENERIC_SCHEMA_TOKENS
                            best_uf: str | None = None
                            best_score = 0.0
                            for uf in unpopulated_be:
                                uf_toks = _split_ident_tokens(uf) - _GENERIC_SCHEMA_TOKENS
                                shared = sk_toks & uf_toks
                                ratio = difflib.SequenceMatcher(None, sk.lower(), uf.lower()).ratio()
                                sc_val = (0.65 + 0.1 * len(shared)) if shared else (ratio if ratio >= 0.58 else 0.0)
                                if sc_val > best_score:
                                    best_score = sc_val
                                    best_uf = uf
                            if best_uf is not None:
                                matched_be = best_uf
                                matched_by_fuzzy = True

                        if matched_be:
                            is_soft_optional_fuzzy = (
                                matched_by_fuzzy
                                and b_schema["extra_policy"] != "forbid"
                                and matched_be not in req_wire_keys
                            )
                            renamed_be_fields.add(matched_be)
                            _emit_http(
                                ("req_case", fe_rel, c_ln, sk, matched_be),
                                (
                                    f"- ⚠️ **[Cross-Stack HTTP Payload Key Mismatch]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                    f"Sends JSON key `'{sk}'` to `{verb} {rc['full_path']}`{via_str}, "
                                    f"but schema `{sc_nm}` (`{sc_loc}`) expects `'{matched_be}'`."
                                ),
                                category="http_payload_casing_mismatch",
                                rel=fe_rel,
                                line=c_ln,
                                peer=sc_loc,
                                summary=f"{verb} {rc['full_path']} sends '{sk}', but {sc_nm} expects '{matched_be}'",
                                severity="warning" if is_soft_optional_fuzzy else "error",
                                confidence="high" if is_soft_optional_fuzzy else "proven",
                            )
                        elif b_schema["extra_policy"] == "forbid" or (
                            b_schema["valid_req_keys"] and not (sent_set & b_schema["valid_req_keys"])
                        ):
                            _emit_http(
                                ("req_unk", fe_rel, c_ln, sk, sc_nm),
                                (
                                    f"- ⚠️ **[Cross-Stack HTTP Unknown Payload Key]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                    f"Sends undeclared field `'{sk}'` to `{verb} {rc['full_path']}`{via_str}; "
                                    f"`{sc_nm}` (`{sc_loc}`) only accepts `{sorted(b_schema['valid_req_keys'])[:10]}`."
                                ),
                                category="http_unknown_payload_field",
                                rel=fe_rel,
                                line=c_ln,
                                peer=sc_loc,
                                summary=f"{verb} {rc['full_path']} sends undeclared key '{sk}' not in {sc_nm}",
                                severity="error",
                                confidence="proven",
                            )
                        elif (
                            b_schema["extra_policy"] == "ignore"
                            and any(not (wg & sent_set) for _, wg in b_schema["required_wire_groups"])
                            and len(b_schema["valid_req_keys"]) <= 20
                        ):
                            # Under default Pydantic extra='ignore', silently allow unrelated client keys
                            # whenever all required fields are satisfied; only warn when required fields are missing.
                            _emit_http(
                                ("req_unk", fe_rel, c_ln, sk, sc_nm),
                                (
                                    f"- ⚠️ **[Cross-Stack HTTP Unknown Payload Key]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                    f"Sends undeclared field `'{sk}'` to `{verb} {rc['full_path']}`{via_str}; "
                                    f"`{sc_nm}` (`{sc_loc}`) ignores it while required field(s) remain omitted."
                                ),
                                category="http_unknown_payload_field",
                                rel=fe_rel,
                                line=c_ln,
                                peer=sc_loc,
                                summary=f"{verb} {rc['full_path']} sends ignored key '{sk}' not in {sc_nm}",
                                severity="warning",
                                confidence="high",
                            )

                if cs.get("req_closed"):
                    missing_req: list[str] = []
                    sent_norm_set = (
                        {normalize_ident_key(k) for k in sent_set if normalize_ident_key(k)}
                        if b_schema.get("has_alias_generator")
                        else set()
                    )
                    for canon_f, wire_grp in b_schema["required_wire_groups"]:
                        if (
                            wire_grp & sent_set
                            or canon_f in renamed_be_fields
                            or (wire_grp & renamed_be_fields)
                            or (sent_norm_set and normalize_ident_key(canon_f) in sent_norm_set)
                        ):
                            continue
                        missing_req.append(canon_f)
                    if missing_req:
                        _emit_http(
                            ("req_miss", fe_rel, c_ln, sc_nm, tuple(missing_req)),
                            (
                                f"- ⚠️ **[Cross-Stack HTTP Missing Required Field]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                f"Payload to `{verb} {rc['full_path']}`{via_str} omits required field(s) "
                                f"`{missing_req}` of `{sc_nm}` (`{sc_loc}` → guaranteed `422 Unprocessable Entity`)."
                            ),
                            category="http_missing_required_field",
                            rel=fe_rel,
                            line=c_ln,
                            peer=sc_loc,
                            summary=f"{verb} {rc['full_path']} omits required {sc_nm} field(s) {missing_req} (422)",
                        )

            # B. Required Query Parameter Verification
            if rc["required_query_params"] and cs.get("query_closed") and cs.get("via_wrapper") is None:
                sent_q = cs.get("query_keys") or set()
                miss_q = [qp for qp in rc["required_query_params"] if qp not in sent_q]
                if miss_q:
                    _emit_http(
                        ("query_miss", fe_rel, c_ln, tuple(miss_q)),
                        (
                            f"- ⚠️ **[Cross-Stack HTTP Missing Query Param]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                            f"Calls `{verb} {cs['url']}` without required query parameter(s) `{miss_q}` "
                            f"declared on `{rc['handler']}` (`{peer_route_loc}`)."
                        ),
                        category="http_missing_query_param",
                        rel=fe_rel,
                        line=c_ln,
                        peer=peer_route_loc,
                        summary=f"{verb} {cs['url']} omits required query param(s) {miss_q}",
                    )

            # C. Response Key Verification (Pydantic `response_schema` or closed handler `return_keys`)
            res_reads = cs.get("res_keys") or []
            res_item_reads = cs.get("res_item_keys") or []
            if res_reads or res_item_reads:
                res_sc = rc.get("response_schema")
                if rc.get("response_is_list") and res_sc is not None:
                    sc_loc = f"{res_sc['rel_path']}:L{res_sc['line']}"
                    if res_reads:
                        bad_rk = res_reads[0]
                        _emit_http(
                            ("res_list", fe_rel, c_ln, bad_rk),
                            (
                                f"- ⚠️ **[6D Cross-Stack HTTP Shape Violation]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                f"Reads `.{bad_rk}` on `{verb} {rc['full_path']}`{via_str}, "
                                f"but backend returns a JSON array `list[{res_sc['name']}]` (`{peer_route_loc}`), not an object."
                            ),
                            category="http_response_key_mismatch",
                            rel=fe_rel,
                            line=c_ln,
                            peer=peer_route_loc,
                            summary=f"Reads .{bad_rk} on {verb} {rc['full_path']}, which returns list[{res_sc['name']}]",
                        )
                    elif res_item_reads and res_sc["is_res_closed"]:
                        valid_rk = res_sc["valid_res_keys"]
                        miss_item_rk = [rk for rk in res_item_reads if rk not in valid_rk]
                        if miss_item_rk:
                            _emit_http(
                                ("res_list_item", fe_rel, c_ln, miss_item_rk[0]),
                                (
                                    f"- ⚠️ **[6D Cross-Stack HTTP Shape Violation]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                    f"Reads item field `.{miss_item_rk[0]}` from `list[{res_sc['name']}]` returned by "
                                    f"`{verb} {rc['full_path']}`{via_str}, which is absent from `{res_sc['name']}` "
                                    f"(`{sc_loc}`, fields: `{sorted(valid_rk)[:10]}`)."
                                ),
                                category="http_response_key_mismatch",
                                rel=fe_rel,
                                line=c_ln,
                                peer=sc_loc,
                                summary=(
                                    f"Reads item .{miss_item_rk[0]} from {verb} {rc['full_path']} "
                                    f"(list[{res_sc['name']}]), absent from {res_sc['name']}"
                                ),
                            )
                elif res_reads and res_sc is not None and res_sc["is_res_closed"]:
                    valid_rk = res_sc["valid_res_keys"]
                    miss_rk = [rk for rk in res_reads if rk not in valid_rk]
                    if miss_rk:
                        sc_loc = f"{res_sc['rel_path']}:L{res_sc['line']}"
                        _emit_http(
                            ("res_sc", fe_rel, c_ln, miss_rk[0]),
                            (
                                f"- ⚠️ **[6D Cross-Stack HTTP Shape Violation]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                f"Reads `.{miss_rk[0]}` from `{verb} {rc['full_path']}`{via_str}, "
                                f"which is absent from `{res_sc['name']}` (`{sc_loc}`, fields: `{sorted(valid_rk)[:10]}`)."
                            ),
                            category="http_response_key_mismatch",
                            rel=fe_rel,
                            line=c_ln,
                            peer=sc_loc,
                            summary=f"Reads .{miss_rk[0]} from {verb} {rc['full_path']}, absent from {res_sc['name']}",
                        )
                elif res_reads and res_sc is None and rc.get("closed_return_keys"):
                    be_keys = rc["closed_return_keys"]
                    miss_rk = [rk for rk in res_reads if rk not in be_keys]
                    if miss_rk:
                        _emit_http(
                            ("res_dict", fe_rel, c_ln, miss_rk[0]),
                            (
                                f"- ⚠️ **[6D Cross-Stack HTTP Shape Violation]** `{fe_rel}:L{c_ln}` (`{scope}`) → "
                                f"Reads `.{miss_rk[0]}` from `{verb} {rc['full_path']}`{via_str}, "
                                f"but backend handler `{rc['handler']}` (`{peer_route_loc}`) only returns `{sorted(be_keys)}` (`Post(BE) ⊭ Pre(FE)`)."
                            ),
                            category="http_response_key_mismatch",
                            rel=fe_rel,
                            line=c_ln,
                            peer=peer_route_loc,
                            summary=f"Reads .{miss_rk[0]} from {verb} {rc['full_path']}, handler returns {sorted(be_keys)}",
                        )

    stats = {
        "http_routes_indexed": len(routes_exact),
        "http_callsites_verified": callsites_checked,
        "schemas_indexed": len(schema_registry),
    }
    return diags, hotspots, stats


# ── Pillar 2: React Stateful Lifecycle, Remount & Async Race Verifier (PB012) ─

def analyze_react_lifecycle_and_race_contracts(
    rel_path: str,
    src_text: str,
    brain: "ProjectBrain",
) -> tuple[list[str], list[dict]]:
    """Detect post-mount stateful child unmounts, nested component remounts, and uncancelled async effect races."""
    ext = Path(rel_path).suffix.lower()
    if ext not in _JS_TS_EXTS or ("useState" not in src_text and "useEffect" not in src_text and "<" not in src_text):
        return [], []
    parser = get_parser(ext)
    if parser is None:
        return [], []
    try:
        tree = parser.parse(src_text.encode("utf-8", errors="replace"))
    except Exception:
        return [], []
    root = tree.root_node

    info = brain._index.get(rel_path, {})
    sym_map = brain._symbol_imports.get(rel_path, {})
    bindings = info.get("import_bindings") or {}

    same_file_stateful: dict[str, int] = {
        fn["name"]: int(fn.get("line_start") or 1)
        for fn in info.get("functions", [])
        if fn.get("has_local_state")
    }

    def _resolve_stateful_child(tag_name: str) -> str | None:
        """Return `'rel_path:L<line>'` if `<TagName>` is proven to hold internal state, else None."""
        if tag_name in same_file_stateful:
            return f"{rel_path}:L{same_file_stateful[tag_name]}"
        imp_r = sym_map.get(tag_name)
        orig_nm = (bindings.get(tag_name) or {}).get("symbol") or tag_name
        if imp_r:
            t_info = brain._index.get(imp_r, {})
            for fn in t_info.get("functions", []):
                if (fn["name"] == orig_nm or (orig_nm == "default" and fn["name"][:1].isupper())) and fn.get("has_local_state"):
                    return f"{imp_r}:L{fn.get('line_start', 1)}"
            if brain._graph:
                for nid in brain._graph._nodes_by_file.get(imp_r, ()):
                    nd = brain._graph.nodes.get(nid, {})
                    if nd.get("type") == "fn" and nd.get("has_local_state") and (
                        nd.get("name") == orig_nm or orig_nm == "default"
                    ):
                        return f"{imp_r}:L{nd.get('line_start', 1)}"
        return None

    diags: list[str] = []
    hotspots: list[dict] = []
    seen: set[tuple] = set()

    def _emit_react(
        key: tuple,
        msg: str,
        *,
        category: str,
        line: int,
        peer: str,
        summary: str,
        severity: str = "error",
        confidence: str = "proven",
    ) -> None:
        if key in seen:
            return
        seen.add(key)
        if severity == "error":
            diags.append(msg)
        hotspots.append({
            "severity": severity,
            "confidence": confidence,
            "rule_id": "PB012",
            "category": category,
            "rel_path": rel_path,
            "line": line,
            "peer_target": peer,
            "summary": summary,
            "recommended_read": f"{rel_path}:{max(1, line - 8)}-{line + 16}",
            "_diag_msg": msg,
        })

    # Collect top-level component AST nodes (`function Comp()` or `const Comp = () => ...`)
    comp_entries: list[tuple[str, object]] = []
    for ch in root.children:
        decl = ch.child_by_field_name("declaration") if ch.type == "export_statement" else ch
        if decl is None:
            continue
        if decl.type == "function_declaration":
            nm = decl.child_by_field_name("name")
            if nm and _t_text(nm)[:1].isupper():
                comp_entries.append((_t_text(nm), decl))
        elif decl.type in ("lexical_declaration", "variable_declaration"):
            for sub in decl.children:
                if sub.type == "variable_declarator":
                    nm = sub.child_by_field_name("name")
                    val = sub.child_by_field_name("value")
                    if nm and nm.type == "identifier" and _t_text(nm)[:1].isupper() and val is not None:
                        if val.type in ("arrow_function", "function_expression"):
                            comp_entries.append((_t_text(nm), val))
                        elif val.type == "call_expression":
                            ac = val.child_by_field_name("arguments")
                            if ac and ac.named_children and ac.named_children[0].type in ("arrow_function", "function_expression"):
                                comp_entries.append((_t_text(nm), ac.named_children[0]))

    for comp_name, comp_nd in comp_entries:
        comp_txt = _t_text(comp_nd)
        if "<" not in comp_txt:
            continue

        # Collect direct `useState` pairs, nested uppercase definitions, JSX usages, `useEffect` calls, & early returns
        state_setters: dict[str, tuple[str, int]] = {}  # state_var -> (setter_name, decl_line)
        setter_to_var: dict[str, str] = {}
        nested_upper_defs: dict[str, int] = {}
        jsx_tags_in_comp: dict[str, list[tuple[int, str]]] = {}  # TagName -> [(line, raw_el_txt)]
        effect_calls: list = []
        nested_helper_fns: dict[str, tuple[int, int, str]] = {}
        inline_handler_fns: list[tuple[int, int, str]] = []
        ternary_nodes: list = []
        logical_and_nodes: list = []
        jsx_tags_in_map: set[tuple[str, int]] = set()

        def _inside_map_cb(nd_chk) -> bool:
            anc = nd_chk.parent
            while anc is not None and anc is not comp_nd:
                if anc.type == "call_expression":
                    fn_a = anc.child_by_field_name("function")
                    if fn_a is not None and fn_a.type == "member_expression":
                        p_a = fn_a.child_by_field_name("property")
                        if p_a is not None and _t_text(p_a) == "map":
                            return True
                anc = anc.parent
            return False

        stack = [comp_nd]
        while stack:
            nd = stack.pop()
            nt = nd.type
            if nd is not comp_nd:
                if nt == "function_declaration":
                    nm_n = nd.child_by_field_name("name")
                    if nm_n:
                        fn_nm = _t_text(nm_n)
                        if fn_nm[:1].isupper():
                            nested_upper_defs.setdefault(fn_nm, _t_line(nd))
                        else:
                            nested_helper_fns[fn_nm] = (_t_line(nd), int(nd.end_point[0]) + 1, _t_text(nd))
                elif nt == "variable_declarator":
                    nm_n = nd.child_by_field_name("name")
                    val_n = nd.child_by_field_name("value")
                    if nm_n and val_n:
                        if nm_n.type == "identifier":
                            v_nm = _t_text(nm_n)
                            if val_n.type in ("arrow_function", "function_expression"):
                                if v_nm[:1].isupper():
                                    nested_upper_defs.setdefault(v_nm, _t_line(nd))
                                else:
                                    nested_helper_fns[v_nm] = (_t_line(nd), int(nd.end_point[0]) + 1, _t_text(val_n))
                        elif nm_n.type == "array_pattern" and val_n.type == "call_expression":
                            fn_c = val_n.child_by_field_name("function")
                            if fn_c and _t_text(fn_c).rsplit(".", 1)[-1] == "useState":
                                ids = [c for c in nm_n.named_children if c.type == "identifier"]
                                if len(ids) >= 2:
                                    sv, st_nm = _t_text(ids[0]), _t_text(ids[1])
                                    state_setters[sv] = (st_nm, _t_line(nd))
                                    setter_to_var[st_nm] = sv
                elif nt in ("arrow_function", "function_expression"):
                    # Capture inline JSX/event handlers like `onClick={async () => { setSaving(true); ... setSaving(false); }}`
                    p_nd = nd.parent
                    if p_nd is not None and p_nd.type != "variable_declarator":
                        inline_handler_fns.append((_t_line(nd), int(nd.end_point[0]) + 1, _t_text(nd)))
                elif nt == "ternary_expression":
                    ternary_nodes.append(nd)
                elif nt == "binary_expression":
                    op_n = nd.child_by_field_name("operator")
                    if op_n is not None and _t_text(op_n) == "&&":
                        logical_and_nodes.append(nd)
                elif nt in ("jsx_opening_element", "jsx_self_closing_element"):
                    tn = nd.child_by_field_name("name")
                    if tn and tn.type == "identifier":
                        t_str = _t_text(tn)
                        if t_str[:1].isupper():
                            t_ln = _t_line(nd)
                            jsx_tags_in_comp.setdefault(t_str, []).append((t_ln, _t_text(nd)))
                            if _inside_map_cb(nd):
                                jsx_tags_in_map.add((t_str, t_ln))
                elif nt == "call_expression":
                    fn_c = nd.child_by_field_name("function")
                    if fn_c and _t_text(fn_c).rsplit(".", 1)[-1] == "useEffect":
                        effect_calls.append(nd)
            if nd.named_child_count:
                stack.extend(reversed(nd.named_children))

        def _subtree_jsx_tags(sub_nd) -> dict[str, list[tuple[int, str]]]:
            found_tags: dict[str, list[tuple[int, str]]] = {}
            if sub_nd is None:
                return found_tags
            st_j = [sub_nd]
            while st_j:
                cur_j = st_j.pop()
                if cur_j.type in ("jsx_opening_element", "jsx_self_closing_element"):
                    tn_j = cur_j.child_by_field_name("name")
                    if tn_j and tn_j.type == "identifier":
                        t_s = _t_text(tn_j)
                        if t_s[:1].isupper():
                            found_tags.setdefault(t_s, []).append((_t_line(cur_j), _t_text(cur_j)))
                if cur_j.named_child_count:
                    st_j.extend(reversed(cur_j.named_children))
            return found_tags

        # Law 7b: Nested Component Definition Rendered as `<InnerComp />`
        for inner_nm, def_ln in nested_upper_defs.items():
            if inner_nm in jsx_tags_in_comp:
                use_ln = jsx_tags_in_comp[inner_nm][0][0]
                _emit_react(
                    ("nested_comp", comp_name, inner_nm),
                    (
                        f"- ⚠️ **[React Lifecycle Remount Bug (`nested_component_remount`)]** `{rel_path}:L{def_ln}` (`{comp_name}`) → "
                        f"Declares component `{inner_nm}` inside `{comp_name}()` and renders `<{inner_nm} />` at `L{use_ln}`, "
                        f"creating a new component identity every render and wiping its DOM focus & state."
                    ),
                    category="nested_component_remount",
                    line=def_ln,
                    peer=f"{rel_path}:L{use_ln}",
                    summary=f"{inner_nm} is defined inside {comp_name}() and rendered at L{use_ln}, remounting every render",
                )

        def _reactive_deps(deps_nd) -> list[str]:
            out_d: list[str] = []
            for dc in deps_nd.named_children:
                base_d = _t_text(dc).split(".", 1)[0].strip()
                if (
                    base_d.isidentifier()
                    and base_d not in setter_to_var
                    and base_d not in _STABLE_HOOK_DEPS
                    and not base_d.endswith("Ref")
                ):
                    out_d.append(base_d)
            return out_d

        # Law 7a: Post-Mount Boolean State Guard Unmounting a Stateful Child Component
        if state_setters and jsx_tags_in_comp:
            mount_only_spans: list[tuple[int, int]] = []
            mount_only_bodies: list[str] = []
            retriggered_effect_setters: dict[str, set[str]] = {}  # state_var -> dep_idents

            for eff_nd in effect_calls:
                ac = eff_nd.child_by_field_name("arguments")
                if not ac or len(ac.named_children) < 2:
                    continue
                cb_nd, deps_nd = ac.named_children[0], ac.named_children[1]
                if deps_nd.type != "array":
                    continue
                dep_ids = _reactive_deps(deps_nd)
                cb_txt = _t_text(cb_nd)
                if not dep_ids:
                    mount_only_spans.append((_t_line(eff_nd), int(eff_nd.end_point[0]) + 1))
                    mount_only_bodies.append(cb_txt)
                else:
                    for sv, (st_nm, _) in state_setters.items():
                        set_t = re.search(rf"\b{re.escape(st_nm)}\s*\(\s*true\b", cb_txt)
                        set_f = re.search(rf"\b{re.escape(st_nm)}\s*\(\s*false\b", cb_txt)
                        if set_t and set_f:
                            retriggered_effect_setters.setdefault(sv, set()).update(dep_ids)

            post_mount_flags: dict[str, tuple[int, set[str] | None]] = {}  # sv -> (set_true_line, only_effect_deps_or_None)
            for sv, (st_nm, _) in state_setters.items():
                set_true_rx = re.compile(rf"\b{re.escape(st_nm)}\s*\(\s*true\b")
                set_false_rx = re.compile(rf"\b{re.escape(st_nm)}\s*\(\s*false\b")
                # Require a same-handler transient round-trip (`setSv(true)` AND `setSv(false)` inside the
                # same handler/helper/effect). One-way screens (`setSubmitted(true)` + separate "New Order"
                # button) and synchronous mode toggles (`setIsPreview(!isPreview)`) do NOT trigger false positives.
                found_interactive_line: int | None = None
                for h_nm, (hs, he, h_txt) in nested_helper_fns.items():
                    m_tr = set_true_rx.search(h_txt)
                    if not (m_tr and set_false_rx.search(h_txt)):
                        continue
                    # Ensure helper isn't *exclusively* invoked inside a mount-only `useEffect(..., [])`
                    all_calls_to_h = len(re.findall(rf"\b{re.escape(h_nm)}\b", comp_txt)) - 1  # subtract declaration
                    mount_calls_to_h = sum(len(re.findall(rf"\b{re.escape(h_nm)}\s*\(", mb)) for mb in mount_only_bodies)
                    if all_calls_to_h > mount_calls_to_h:
                        found_interactive_line = hs + h_txt.count("\n", 0, m_tr.start())
                        break
                if found_interactive_line is None:
                    for hs, he, h_txt in inline_handler_fns:
                        if any(ms <= hs and he <= me for ms, me in mount_only_spans):
                            continue
                        m_tr = set_true_rx.search(h_txt)
                        if m_tr and set_false_rx.search(h_txt):
                            found_interactive_line = hs + h_txt.count("\n", 0, m_tr.start())
                            break
                if found_interactive_line is not None:
                    post_mount_flags[sv] = (found_interactive_line, None)
                elif sv in retriggered_effect_setters:
                    post_mount_flags[sv] = (state_setters[sv][1], retriggered_effect_setters[sv])

            if post_mount_flags:
                body_nd = comp_nd.child_by_field_name("body")
                if body_nd is not None:
                    # 1. Early return guards & `if (loading) { return <Spinner/> } else { return <Child/> }`
                    early_guarded_flags: dict[str, tuple[int, dict[str, list[tuple[int, str]]] | None, dict[str, list[tuple[int, str]]]]] = {}
                    for stmt in body_nd.named_children:
                        if stmt.type == "if_statement":
                            cond_n = stmt.child_by_field_name("condition")
                            cons_n = stmt.child_by_field_name("consequence")
                            alt_n = stmt.child_by_field_name("alternative")
                            if cond_n and cons_n and "return" in _t_text(cons_n):
                                c_str = _t_text(cond_n)
                                cons_tags = _subtree_jsx_tags(cons_n)
                                alt_tags = _subtree_jsx_tags(alt_n) if alt_n is not None else None
                                for sv in post_mount_flags:
                                    if re.search(rf"(?<![!\w]){re.escape(sv)}\b", c_str) and not re.search(
                                        rf"!\s*{re.escape(sv)}\b|{re.escape(sv)}\s*===\s*false", c_str
                                    ):
                                        early_guarded_flags.setdefault(sv, (_t_line(stmt), alt_tags, cons_tags))

                    for tag_nm, tag_uses in jsx_tags_in_comp.items():
                        if tag_nm in nested_upper_defs:
                            continue
                        child_peer = _resolve_stateful_child(tag_nm)
                        if not child_peer:
                            continue
                        use_ln, raw_tag = tag_uses[0]
                        # A `key={docId}` on a single child is an intentional remount boundary;
                        # however, a `key={item.id}` inside `.map(...)` is a mandatory React list key
                        # and does NOT protect list items from being unmounted by `if (refreshing) return <Spinner />`.
                        is_explicit_nonlist_key = bool(
                            re.search(r"\bkey\s*=\s*\{", raw_tag) and (tag_nm, use_ln) not in jsx_tags_in_map
                        )
                        if is_explicit_nonlist_key:
                            continue

                        for sv, (set_ln, eff_deps_only) in post_mount_flags.items():
                            # If `sv` only resets on `useEffect([dep])` and `<Child key={dep} />` is explicit, skip!
                            if eff_deps_only and any(
                                re.search(rf"\bkey\s*=\s*\{{[^}}]*\b{re.escape(d)}\b", raw_tag)
                                for d in eff_deps_only
                            ):
                                continue
                            eg_info = early_guarded_flags.get(sv)
                            if eg_info is not None:
                                guard_ln, alt_tags, cons_tags = eg_info
                                if tag_nm not in cons_tags and (
                                    (alt_tags is None and guard_ln < use_ln)
                                    or (alt_tags is not None and tag_nm in alt_tags)
                                ):
                                    _emit_react(
                                        ("unmount_early", comp_name, sv, tag_nm),
                                        (
                                            f"- ⚠️ **[React Stateful Child Unmount Bug (`stateful_child_unmount_guard`)]** "
                                            f"`{rel_path}:L{guard_ln}` (`{comp_name}`) → Early return on `{sv}` (`set` to `true` "
                                            f"post-mount at `L{set_ln}`) unmounts stateful `<{tag_nm}>` (`{child_peer}`), "
                                            f"destroying its internal `useState` state on refresh."
                                        ),
                                        category="stateful_child_unmount_guard",
                                        line=guard_ln,
                                        peer=child_peer,
                                        summary=(
                                            f"Early return on '{sv}' (toggled post-mount at L{set_ln}) unmounts "
                                            f"stateful <{tag_nm}> ({child_peer}), wiping child state"
                                        ),
                                    )
                                    break
                            # 2. CST-exact ternary & logical-AND (`{!saving && <Child />}`) checks
                            matched_tern_ln: int | None = None
                            matched_cond_kind = "ternary"
                            for t_nd in ternary_nodes:
                                t_cond = t_nd.child_by_field_name("condition")
                                t_cons = t_nd.child_by_field_name("consequence")
                                t_alt = t_nd.child_by_field_name("alternative")
                                if not (t_cond and t_cons and t_alt):
                                    continue
                                c_txt = _t_text(t_cond)
                                if not re.search(rf"\b{re.escape(sv)}\b", c_txt):
                                    continue
                                is_neg = bool(re.search(rf"!\s*{re.escape(sv)}\b|{re.escape(sv)}\s*===\s*false", c_txt))
                                cons_t = _subtree_jsx_tags(t_cons)
                                alt_t = _subtree_jsx_tags(t_alt)
                                child_branch = cons_t if is_neg else alt_t
                                other_branch = alt_t if is_neg else cons_t
                                if tag_nm in child_branch and tag_nm not in other_branch:
                                    if not any(
                                        re.search(r"\bkey\s*=\s*\{", rt) and (tag_nm, rl) not in jsx_tags_in_map
                                        for rl, rt in child_branch[tag_nm]
                                    ):
                                        matched_tern_ln = _t_line(t_nd)
                                        break
                            if matched_tern_ln is None:
                                for and_nd in logical_and_nodes:
                                    l_nd = and_nd.child_by_field_name("left")
                                    r_nd = and_nd.child_by_field_name("right")
                                    if not (l_nd and r_nd):
                                        continue
                                    l_txt = _t_text(l_nd)
                                    if not re.search(rf"!\s*{re.escape(sv)}\b|{re.escape(sv)}\s*===\s*false", l_txt):
                                        continue
                                    r_tags = _subtree_jsx_tags(r_nd)
                                    if tag_nm in r_tags:
                                        if not any(
                                            re.search(r"\bkey\s*=\s*\{", rt) and (tag_nm, rl) not in jsx_tags_in_map
                                            for rl, rt in r_tags[tag_nm]
                                        ):
                                            matched_tern_ln = _t_line(and_nd)
                                            matched_cond_kind = "logical_and"
                                            break
                            if matched_tern_ln is not None:
                                expr_lbl = f"!{sv} && <{tag_nm}>" if matched_cond_kind == "logical_and" else f"{sv} ? ... : <{tag_nm}>"
                                _emit_react(
                                    ("unmount_tern", comp_name, sv, tag_nm),
                                    (
                                        f"- ⚠️ **[React Stateful Child Unmount Bug (`stateful_child_unmount_guard`)]** "
                                        f"`{rel_path}:L{matched_tern_ln}` (`{comp_name}`) → Conditional branch `{expr_lbl}` "
                                        f"(`{sv}` set `true` post-mount at `L{set_ln}`) unmounts stateful `<{tag_nm}>` "
                                        f"(`{child_peer}`), wiping its internal state."
                                    ),
                                    category="stateful_child_unmount_guard",
                                    line=matched_tern_ln,
                                    peer=child_peer,
                                    summary=(
                                        f"Conditional '{expr_lbl}' (toggled post-mount at L{set_ln}) "
                                        f"unmounts stateful <{tag_nm}> ({child_peer})"
                                    ),
                                )
                                break

        # Law 8: Uncancelled Async `useEffect([dep])` `setState` Race Condition (CST-Hardened)
        def _has_direct_cleanup_return(cb_node) -> bool:
            """Return True ONLY if `cb_node` itself returns a cleanup function/ref (ignores returns inside `.then()`)."""
            b_nd = cb_node.child_by_field_name("body")
            if b_nd is None:
                return False
            if b_nd.type != "statement_block":
                return False
            st_r = list(reversed(b_nd.named_children))
            while st_r:
                cur_r = st_r.pop()
                ct = cur_r.type
                if ct in ("arrow_function", "function_expression", "function_declaration", "method_definition"):
                    continue
                if ct == "return_statement" and cur_r.named_children:
                    return True
                if cur_r.named_child_count:
                    st_r.extend(reversed(cur_r.named_children))
            return False

        def _has_abort_signal_cst(root_nodes: list, combined_txt: str) -> bool:
            """Detect actual AbortController/AbortSignal usage without false-skipping `{ signal: 'buy' }` or axios body payloads."""
            if "AbortController" in combined_txt or ".abort(" in combined_txt or re.search(r"\bAbortSignal\b", combined_txt):
                return True

            def _is_axios_body_arg(obj_nd) -> bool:
                """Return True if `obj_nd` is arg[1] (the request body, NOT config) of `axios.post/put/patch(url, body, cfg)`."""
                args_p = obj_nd.parent
                if args_p is None or args_p.type != "arguments":
                    return False
                call_p = args_p.parent
                if call_p is None or call_p.type != "call_expression":
                    return False
                fn_p = call_p.child_by_field_name("function")
                if fn_p is None:
                    return False
                callee_p = re.sub(r"\s+", "", _t_text(fn_p))
                m_axp = re.match(r"^[A-Za-z_$][\w$]*\.(post|put|patch)$", callee_p, re.IGNORECASE)
                if not m_axp:
                    return False
                named_a = args_p.named_children
                return len(named_a) >= 2 and named_a[1] == obj_nd

            for rn in root_nodes:
                if rn is None:
                    continue
                st_s = [rn]
                while st_s:
                    cur_s = st_s.pop()
                    ct = cur_s.type
                    # Never treat keys inside `JSON.stringify(...)` or axios POST/PUT/PATCH body arg[1] as AbortSignals!
                    if ct == "call_expression":
                        fn_s = cur_s.child_by_field_name("function")
                        if fn_s is not None and _t_text(fn_s).strip() == "JSON.stringify":
                            continue
                    if ct == "object" and _is_axios_body_arg(cur_s):
                        continue
                    if ct == "member_expression":
                        p_s = cur_s.child_by_field_name("property")
                        if p_s is not None and _t_text(p_s) == "signal":
                            return True
                    elif ct == "shorthand_property_identifier" and _t_text(cur_s) == "signal":
                        return True
                    elif ct == "pair":
                        k_s = cur_s.child_by_field_name("key")
                        v_s = cur_s.child_by_field_name("value")
                        if k_s is not None and v_s is not None and _t_text(k_s).strip("'\"`") == "signal":
                            if v_s.type not in ("string", "template_string", "number", "true", "false", "null", "undefined"):
                                return True
                    if cur_s.named_child_count:
                        st_s.extend(reversed(cur_s.named_children))
            return False

        if setter_to_var and effect_calls:
            # Map helper names to their CST nodes as well for AbortSignal CST inspection
            helper_cst_nodes: dict[str, object] = {}
            st_hc = [comp_nd]
            while st_hc:
                cur_hc = st_hc.pop()
                if cur_hc is not comp_nd:
                    if cur_hc.type == "function_declaration":
                        nm_hc = cur_hc.child_by_field_name("name")
                        if nm_hc:
                            helper_cst_nodes[_t_text(nm_hc)] = cur_hc
                    elif cur_hc.type == "variable_declarator":
                        nm_hc = cur_hc.child_by_field_name("name")
                        val_hc = cur_hc.child_by_field_name("value")
                        if nm_hc and val_hc and nm_hc.type == "identifier" and val_hc.type in ("arrow_function", "function_expression"):
                            helper_cst_nodes[_t_text(nm_hc)] = val_hc
                if cur_hc.named_child_count:
                    st_hc.extend(reversed(cur_hc.named_children))

            for eff_nd in effect_calls:
                ac = eff_nd.child_by_field_name("arguments")
                if not ac or len(ac.named_children) < 2:
                    continue
                cb_nd, deps_nd = ac.named_children[0], ac.named_children[1]
                if deps_nd.type != "array":
                    continue
                dep_ids = _reactive_deps(deps_nd)
                if not dep_ids:
                    continue
                cb_txt = _t_text(cb_nd)
                # Follow 1-hop local async helper functions invoked from inside the effect callback
                eff_s_ln, eff_e_ln = _t_line(eff_nd), int(eff_nd.end_point[0]) + 1
                helper_chunks: list[str] = []
                effect_cst_roots: list = [cb_nd]
                for h_nm, (hs, he, h_txt) in nested_helper_fns.items():
                    if eff_s_ln <= hs and he <= eff_e_ln:
                        continue
                    if re.search(rf"\b{re.escape(h_nm)}\s*\(", cb_txt):
                        helper_chunks.append(h_txt)
                        if h_nm in helper_cst_nodes:
                            effect_cst_roots.append(helper_cst_nodes[h_nm])
                effective_txt = cb_txt + ("\n" + "\n".join(helper_chunks) if helper_chunks else "")

                if _has_direct_cleanup_return(cb_nd) or _has_abort_signal_cst(effect_cst_roots, effective_txt):
                    continue
                if "await " not in effective_txt and ".then(" not in effective_txt:
                    continue
                if not any(re.search(rf"\b{re.escape(d)}\b", effective_txt) for d in dep_ids):
                    continue

                # Confirm a declared `setState` setter is called after `await` or inside `.then(...)` (including point-free `.then(setItems)`)
                m_async_pos = re.search(r"\bawait\s+|\.then\s*\(", effective_txt)
                if not m_async_pos:
                    continue
                tail_after_async = effective_txt[m_async_pos.start() :]

                # A real `.current` ref race guard mutates/compares a sequence ref OR checks `.current`
                # AFTER the async suspension (not a pre-fetch DOM call like `if (inputRef.current) inputRef.current.focus()`).
                has_ref_seq_guard = bool(
                    re.search(r"\.current\s*(?:\+\+|--|\+=|-=|=(?!=))", effective_txt)
                    and re.search(r"\.current\s*(?:===|!==|==|!=)|(?:===|!==|==|!=)\s*[A-Za-z_$][\w$]*\.current\b", tail_after_async)
                )
                has_post_async_ref_guard = bool(re.search(
                    r"\bif\s*\([^)]*\.current\b|&&\s*!?\s*[A-Za-z_$][\w$]*\.current\b",
                    tail_after_async,
                ))
                if has_ref_seq_guard or has_post_async_ref_guard:
                    continue

                called_setters = [
                    st_nm for st_nm in setter_to_var
                    if re.search(rf"\b{re.escape(st_nm)}\s*\(|\.\s*then\s*\(\s*{re.escape(st_nm)}\b", tail_after_async)
                ]
                if called_setters:
                    eff_ln = _t_line(eff_nd)
                    deps_disp = ", ".join(dep_ids[:3])
                    _emit_react(
                        ("effect_race", comp_name, eff_ln),
                        (
                            f"- ⚠️ **[React Async Effect Race (`uncancelled_async_effect_race`)]** "
                            f"`{rel_path}:L{eff_ln}` (`{comp_name}`) → `useEffect(..., [{deps_disp}])` awaits an "
                            f"async call and invokes `{called_setters[0]}(...)` without an `AbortController` or "
                            f"cleanup cancellation guard; out-of-order responses for stale `[{deps_disp}]` can overwrite newer state."
                        ),
                        category="uncancelled_async_effect_race",
                        line=eff_ln,
                        peer="",
                        summary=(
                            f"useEffect([ {deps_disp} ]) awaits async response -> {called_setters[0]}() "
                            f"with no AbortController or cleanup guard"
                        ),
                        severity="warning",
                        confidence="high",
                    )

    return diags, hotspots


# ── Pillar 3: Python Pydantic, ORM & Transactional I/O Hazard Verifier (PB013)

_PY_IO_SUBSTRINGS = (
    "commit(",
    "send_email",
    "send_mail",
    "sendmail",
    "send_message",
    "send_notification",
    "send_webhook",
    "dispatch_webhook",
    "publish_event",
    "charge(",
    "create_payment_intent",
    "refund(",
    ".post(",
    ".put(",
    ".patch(",
    ".delete(",
)
_PY_HAZARD_CALL_TRIGGERS = frozenset({
    "model_dump",
    "dict",
    *_PY_SIDE_EFFECT_CALLS,
    "post",
    "put",
    "patch",
    "delete",
})


_HAS_SILENT_BROAD_EXCEPT_RE = re.compile(
    r"\bexcept(?:\s+(?:Exception|BaseException)(?:\s+as\s+\w+)?)?\s*:\s*"
    r"(?:(?:#[^\n]*)?\r?\n(?:[ \t]*(?:#[^\n]*)?\r?\n)*[ \t]*)?(?:pass|continue|\.\.\.)\b"
)


def _iter_fn_local_nodes(fn_node: ast.AST):
    """Yield AST nodes in `fn_node`'s body without descending into nested defs/classes."""
    stack: list[ast.AST] = list(reversed(getattr(fn_node, "body", ())))
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        children = list(ast.iter_child_nodes(cur))
        if children:
            stack.extend(reversed(children))


def analyze_python_semantic_hazards(
    rel_path: str,
    src_text: str,
    brain: "ProjectBrain",
    schema_registry: dict[tuple[str, str], dict] | None = None,
) -> tuple[list[str], list[dict]]:
    """Detect partial-update `.model_dump()` default clobbers and silent transactional/side-effect drops."""
    if not rel_path.endswith(".py") or _is_test_file_path(rel_path):
        return [], []
    raw_has_dump = "model_dump" in src_text or ".dict(" in src_text
    want_dump = raw_has_dump and (schema_registry is None or bool(schema_registry))
    want_io = (
        "except" in src_text
        and any(k in src_text for k in _PY_IO_SUBSTRINGS)
        and bool(_HAS_SILENT_BROAD_EXCEPT_RE.search(src_text))
    )
    if not want_dump and not want_io:
        return [], []
    if schema_registry is None and raw_has_dump:
        schema_registry = build_resolved_schema_registry(brain)
        want_dump = bool(schema_registry)
        if not want_dump and not want_io:
            return [], []
    try:
        py_tree = ast.parse(src_text)
    except (SyntaxError, ValueError):
        return [], []

    diags: list[str] = []
    hotspots: list[dict] = []
    seen: set[tuple] = set()

    def _emit_py(key: tuple, msg: str, *, category: str, line: int, peer: str, summary: str, severity: str = "error") -> None:
        if key in seen:
            return
        seen.add(key)
        if severity == "error":
            diags.append(msg)
        hotspots.append({
            "severity": severity,
            "confidence": "proven" if category == "pydantic_partial_update_clobber" else "high",
            "rule_id": "PB013",
            "category": category,
            "rel_path": rel_path,
            "line": line,
            "peer_target": peer,
            "summary": summary,
            "recommended_read": f"{rel_path}:{max(1, line - 6)}-{line + 14}",
            "_diag_msg": msg,
        })

    for node in ast.walk(py_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fn_name = node.name

        # 3A. Partial-Update Pydantic `.model_dump()` / `.dict()` Default Clobber
        schema_params: dict[str, dict] = {}
        is_patch_or_update_fn = False
        if want_dump and schema_registry:
            is_patch_or_update_fn = (
                any(
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("patch", "put")
                    for dec in node.decorator_list
                )
                or any(tok in fn_name.lower() for tok in ("update", "patch"))
            )
            all_args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for arg in all_args:
                if arg.annotation is None:
                    continue
                ann_str = ast.unparse(arg.annotation)
                m_ann = re.match(r"^(?:typing\.)?Annotated\[\s*([^,\]]+)", ann_str)
                if m_ann:
                    ann_str = m_ann.group(1).strip()
                cls_nm = ann_str.rsplit(".", 1)[-1].split("[", 1)[0].strip()
                sc = resolve_schema_for_file(brain, schema_registry, rel_path, cls_nm)
                if sc and sc.get("optional_fields"):
                    schema_params[arg.arg] = sc

        if schema_params:
            unset_unsafe_vars: dict[str, tuple[str, dict, int, list[str]]] = {}  # local_var -> (param_nm, sc, line, clobbered_opts)

            def _extract_literal_field_names(expr: ast.AST) -> set[str] | None:
                if isinstance(expr, (ast.Set, ast.List, ast.Tuple)):
                    out: set[str] = set()
                    for elt in expr.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            out.add(elt.value)
                        else:
                            return None
                    return out
                if isinstance(expr, ast.Dict):
                    out = set()
                    for k_nd in expr.keys:
                        if isinstance(k_nd, ast.Constant) and isinstance(k_nd.value, str):
                            out.add(k_nd.value)
                        else:
                            return None
                    return out
                return None

            def _is_unsafe_dump_call(c_nd: ast.AST) -> tuple[str, dict, int, list[str]] | None:
                if not (
                    isinstance(c_nd, ast.Call)
                    and isinstance(c_nd.func, ast.Attribute)
                    and c_nd.func.attr in ("model_dump", "dict")
                    and isinstance(c_nd.func.value, ast.Name)
                    and c_nd.func.value.id in schema_params
                ):
                    return None
                excluded_keys: set[str] = set()
                included_keys: set[str] | None = None
                for kw in c_nd.keywords:
                    if kw.arg in ("exclude_unset", "exclude_none", "exclude_defaults"):
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            return None
                        if not isinstance(kw.value, ast.Constant):
                            return None
                    elif kw.arg == "exclude":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                            continue
                        lit_ex = _extract_literal_field_names(kw.value)
                        if lit_ex is None:
                            return None
                        excluded_keys.update(lit_ex)
                    elif kw.arg == "include":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                            continue
                        lit_in = _extract_literal_field_names(kw.value)
                        if lit_in is None:
                            return None
                        included_keys = lit_in
                p_nm = c_nd.func.value.id
                sc = schema_params[p_nm]
                clobbered = [
                    f for f in sc["optional_fields"]
                    if f not in excluded_keys and (included_keys is None or f in included_keys)
                ]
                if not clobbered:
                    return None
                return p_nm, sc, int(getattr(c_nd, "lineno", node.lineno)), clobbered

            local_nodes = list(_iter_fn_local_nodes(node))
            # Single top-to-bottom flow-sensitive pass: reassigning `data` to a filtered dict/expr
            # clears its unsafe taint before subsequent `.sqlmodel_update(data)` / `setattr` sinks.
            for sub in local_nodes:
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    t_nm = sub.targets[0].id
                    hit = _is_unsafe_dump_call(sub.value)
                    if hit is not None:
                        unset_unsafe_vars[t_nm] = hit
                    elif t_nm in unset_unsafe_vars and not isinstance(sub.value, (ast.Dict, ast.BinOp)):
                        unset_unsafe_vars.pop(t_nm, None)
                    continue
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) and sub.value is not None:
                    t_nm = sub.target.id
                    hit = _is_unsafe_dump_call(sub.value)
                    if hit is not None:
                        unset_unsafe_vars[t_nm] = hit
                    elif t_nm in unset_unsafe_vars and not isinstance(sub.value, (ast.Dict, ast.BinOp)):
                        unset_unsafe_vars.pop(t_nm, None)
                    continue

                # Pattern i: `for k, v in data.items(): setattr(row, k, v)` without `if v is not None`
                if isinstance(sub, ast.For):
                    dump_origin = None
                    if (
                        isinstance(sub.iter, ast.Call)
                        and isinstance(sub.iter.func, ast.Attribute)
                        and sub.iter.func.attr == "items"
                    ):
                        if isinstance(sub.iter.func.value, ast.Name) and sub.iter.func.value.id in unset_unsafe_vars:
                            dump_origin = unset_unsafe_vars[sub.iter.func.value.id]
                        else:
                            dump_origin = _is_unsafe_dump_call(sub.iter.func.value)
                    if dump_origin is not None:
                        for_src = ast.unparse(sub)
                        has_safe_guard = (
                            "is not None" in for_src
                            or "is None" in for_src
                            or "model_fields_set" in for_src
                            or "__fields_set__" in for_src
                        )
                        if "setattr(" in for_src and not has_safe_guard:
                            p_nm, sc, d_ln, clobbered_opts = dump_origin
                            sc_loc = f"{sc['rel_path']}:L{sc['line']}"
                            opt_preview = clobbered_opts[:4]
                            _emit_py(
                                ("clobber_setattr", fn_name, d_ln),
                                (
                                    f"- ⚠️ **[Pydantic Partial-Update Clobber (`pydantic_partial_update_clobber`)]** "
                                    f"`{rel_path}:L{d_ln}` (`{fn_name}`) → Calls `{p_nm}.model_dump()` without "
                                    f"`exclude_unset=True` and writes every key via `setattr(...)` (`L{sub.lineno}`), "
                                    f"overwriting persisted columns `{opt_preview}` of `{sc['name']}` (`{sc_loc}`) with defaults/`None`."
                                ),
                                category="pydantic_partial_update_clobber",
                                line=d_ln,
                                peer=sc_loc,
                                summary=(
                                    f"{p_nm}.model_dump() without exclude_unset=True in setattr() loop "
                                    f"overwrites omitted {sc['name']} fields {opt_preview}"
                                ),
                            )

                # Pattern ii: `row.sqlmodel_update(data)` / `query.update(data)` / `stmt.values(**data)` / `existing.model_copy(update=data)`
                elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr == "sqlmodel_update" or (
                        sub.func.attr in ("update", "values") and is_patch_or_update_fn
                    ):
                        dump_origin = None
                        if sub.args:
                            a0 = sub.args[0]
                            if isinstance(a0, ast.Name) and a0.id in unset_unsafe_vars:
                                dump_origin = unset_unsafe_vars[a0.id]
                            else:
                                dump_origin = _is_unsafe_dump_call(a0)
                        for kw in sub.keywords:
                            if kw.arg is None:  # `**data`
                                if isinstance(kw.value, ast.Name) and kw.value.id in unset_unsafe_vars:
                                    dump_origin = unset_unsafe_vars[kw.value.id]
                                else:
                                    dump_origin = _is_unsafe_dump_call(kw.value)
                        if dump_origin is not None:
                            p_nm, sc, d_ln, clobbered_opts = dump_origin
                            sc_loc = f"{sc['rel_path']}:L{sc['line']}"
                            opt_preview = clobbered_opts[:4]
                            _emit_py(
                                ("clobber_upd", fn_name, d_ln),
                                (
                                    f"- ⚠️ **[Pydantic Partial-Update Clobber (`pydantic_partial_update_clobber`)]** "
                                    f"`{rel_path}:L{d_ln}` (`{fn_name}`) → Passes `{p_nm}.model_dump()` without "
                                    f"`exclude_unset=True` into `.{sub.func.attr}(...)` (`L{sub.lineno}`), "
                                    f"clobbering omitted `{sc['name']}` columns `{opt_preview}` with defaults/`None`."
                                ),
                                category="pydantic_partial_update_clobber",
                                line=d_ln,
                                peer=sc_loc,
                                summary=(
                                    f"{p_nm}.model_dump() without exclude_unset=True passed to .{sub.func.attr}() "
                                    f"clobbers omitted {sc['name']} fields {opt_preview}"
                                ),
                            )
                    elif sub.func.attr in ("model_copy", "copy"):
                        dump_origin = None
                        for kw in sub.keywords:
                            if kw.arg == "update":
                                if isinstance(kw.value, ast.Name) and kw.value.id in unset_unsafe_vars:
                                    dump_origin = unset_unsafe_vars[kw.value.id]
                                else:
                                    dump_origin = _is_unsafe_dump_call(kw.value)
                        if dump_origin is not None:
                            p_nm, sc, d_ln, clobbered_opts = dump_origin
                            sc_loc = f"{sc['rel_path']}:L{sc['line']}"
                            opt_preview = clobbered_opts[:4]
                            _emit_py(
                                ("clobber_copy", fn_name, d_ln),
                                (
                                    f"- ⚠️ **[Pydantic Partial-Update Clobber (`pydantic_partial_update_clobber`)]** "
                                    f"`{rel_path}:L{d_ln}` (`{fn_name}`) → Passes `{p_nm}.model_dump()` without "
                                    f"`exclude_unset=True` into `.{sub.func.attr}(update=...)` (`L{sub.lineno}`), "
                                    f"clobbering omitted `{sc['name']}` fields `{opt_preview}` with defaults/`None`."
                                ),
                                category="pydantic_partial_update_clobber",
                                line=d_ln,
                                peer=sc_loc,
                                summary=(
                                    f"{p_nm}.model_dump() without exclude_unset=True passed to .{sub.func.attr}(update=...) "
                                    f"clobbers omitted {sc['name']} fields {opt_preview}"
                                ),
                            )

                # Pattern iii: Dict merge `{**existing_defaults, **payload.model_dump()}` or Python 3.9+ `existing | payload.model_dump()`
                elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                    dump_origin = (
                        unset_unsafe_vars.get(sub.right.id)
                        if isinstance(sub.right, ast.Name)
                        else _is_unsafe_dump_call(sub.right)
                    )
                    if dump_origin is not None:
                        p_nm, sc, d_ln, clobbered_opts = dump_origin
                        sc_loc = f"{sc['rel_path']}:L{sc['line']}"
                        opt_preview = clobbered_opts[:4]
                        _emit_py(
                            ("clobber_merge", fn_name, d_ln),
                            (
                                f"- ⚠️ **[Pydantic Partial-Update Clobber (`pydantic_partial_update_clobber`)]** "
                                f"`{rel_path}:L{d_ln}` (`{fn_name}`) → Merges `| {p_nm}.model_dump()` without "
                                f"`exclude_unset=True` over an existing dict (`L{sub.lineno}`), "
                                f"overwriting base keys `{opt_preview}` with `{sc['name']}` defaults/`None`."
                            ),
                            category="pydantic_partial_update_clobber",
                            line=d_ln,
                            peer=sc_loc,
                            summary=(
                                f"| {p_nm}.model_dump() without exclude_unset=True overwrites base dict "
                                f"keys {opt_preview} with {sc['name']} defaults"
                            ),
                        )
                elif isinstance(sub, ast.Dict) and len(sub.keys) >= 2 and sub.keys[0] is None:
                    for k_node, v_node in zip(sub.keys[1:], sub.values[1:]):
                        if k_node is not None:
                            continue
                        dump_origin = (
                            unset_unsafe_vars.get(v_node.id)
                            if isinstance(v_node, ast.Name)
                            else _is_unsafe_dump_call(v_node)
                        )
                        if dump_origin is not None:
                            p_nm, sc, d_ln, clobbered_opts = dump_origin
                            sc_loc = f"{sc['rel_path']}:L{sc['line']}"
                            opt_preview = clobbered_opts[:4]
                            _emit_py(
                                ("clobber_merge", fn_name, d_ln),
                                (
                                    f"- ⚠️ **[Pydantic Partial-Update Clobber (`pydantic_partial_update_clobber`)]** "
                                    f"`{rel_path}:L{d_ln}` (`{fn_name}`) → Unpacks `**{p_nm}.model_dump()` without "
                                    f"`exclude_unset=True` over an existing dict (`L{sub.lineno}`), "
                                    f"overwriting base keys `{opt_preview}` with `{sc['name']}` defaults/`None`."
                                ),
                                category="pydantic_partial_update_clobber",
                                line=d_ln,
                                peer=sc_loc,
                                summary=(
                                    f"**{p_nm}.model_dump() without exclude_unset=True overwrites base dict "
                                    f"keys {opt_preview} with {sc['name']} defaults"
                                ),
                            )

        # 3B. Silent `except Exception: pass` Around `.commit()` or Outbound Notification/Webhook/HTTP Calls
        if not want_io or fn_name in ("__del__", "close", "cleanup", "teardown", "dispose"):
            continue
        _HTTP_CLIENT_CTORS = frozenset({"Client", "AsyncClient", "Session", "ClientSession"})
        local_http_clients: set[str] = set()
        fn_local_io = list(_iter_fn_local_nodes(node))
        for ln_nd in fn_local_io:
            if isinstance(ln_nd, (ast.With, ast.AsyncWith)):
                for w_it in ln_nd.items:
                    if isinstance(w_it.optional_vars, ast.Name) and isinstance(w_it.context_expr, ast.Call):
                        cf = w_it.context_expr.func
                        c_nm = cf.attr if isinstance(cf, ast.Attribute) else (cf.id if isinstance(cf, ast.Name) else "")
                        if c_nm in _HTTP_CLIENT_CTORS:
                            local_http_clients.add(w_it.optional_vars.id)
            elif isinstance(ln_nd, ast.Assign) and len(ln_nd.targets) == 1 and isinstance(ln_nd.targets[0], ast.Name) and isinstance(ln_nd.value, ast.Call):
                cf = ln_nd.value.func
                c_nm = cf.attr if isinstance(cf, ast.Attribute) else (cf.id if isinstance(cf, ast.Name) else "")
                if c_nm in _HTTP_CLIENT_CTORS:
                    local_http_clients.add(ln_nd.targets[0].id)

        for sub in fn_local_io:
            if not isinstance(sub, ast.Try) or not sub.handlers:
                continue
            silent_handlers = []
            for h in sub.handlers:
                is_broad = h.type is None or (
                    isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException")
                )
                if not is_broad:
                    continue
                if all(
                    isinstance(st, (ast.Pass, ast.Continue))
                    or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))
                    for st in h.body
                ):
                    silent_handlers.append(h)
            if not silent_handlers:
                continue

            side_effect_desc: str | None = None
            for t_stmt in sub.body:
                for t_call in ast.walk(t_stmt):
                    if not isinstance(t_call, ast.Call):
                        continue
                    func_nd = t_call.func
                    if isinstance(func_nd, ast.Name):
                        bare_c = func_nd.id
                        c_expr = bare_c
                    elif isinstance(func_nd, ast.Attribute):
                        bare_c = func_nd.attr
                        if bare_c not in _PY_HAZARD_CALL_TRIGGERS:
                            continue
                        c_expr = ast.unparse(func_nd)
                    else:
                        continue
                    is_local_http_mut = (
                        isinstance(func_nd, ast.Attribute)
                        and isinstance(func_nd.value, ast.Name)
                        and func_nd.value.id in local_http_clients
                        and bare_c in ("post", "put", "patch", "delete")
                    )
                    if bare_c in _PY_SIDE_EFFECT_CALLS or is_local_http_mut or _PY_HTTP_MUTATION_RE.match(c_expr):
                        side_effect_desc = f"{c_expr}(...)"
                        break
                if side_effect_desc:
                    break
            if not side_effect_desc:
                continue

            for h in silent_handlers:
                h_ln = int(getattr(h, "lineno", sub.lineno))
                _emit_py(
                    ("swallowed_io", fn_name, h_ln),
                    (
                        f"- ⚠️ **[Silent Side-Effect Exception Swallow (`swallowed_io_exception`)]** "
                        f"`{rel_path}:L{h_ln}` (`{fn_name}`) → Catches broad `Exception` and executes `pass` "
                        f"around `{side_effect_desc}` without rollback, logging, or error propagation."
                    ),
                    category="swallowed_io_exception",
                    line=h_ln,
                    peer="",
                    summary=f"Broad 'except: pass' silently drops failures from {side_effect_desc} with no rollback/log",
                    severity="warning",
                )

    return diags, hotspots


# ── Unified Whole-Repo Anomaly Triage Compiler (`brain_check(audit=True)`) ────

_AUDIT_VENDOR_DIRS = frozenset({
    "vendor", "vendors", "vendored", "third_party", "thirdparty",
    "extern", "node_modules", "dist", "build",
})


def run_semantic_repo_audit(
    brain: "ProjectBrain",
    *,
    scope_glob: str | None = None,
    exclude_globs: list[str] | None = None,
) -> dict:
    """Execute <1.5s whole-repo semantic & cross-stack anomaly triage and rank exact hotspot spans."""
    import time as _time
    from .config import path_is_excluded
    from .contracts import verify_cross_file_contracts
    from .reporters import _classify_diag_rule_id

    t0 = _time.perf_counter()
    prod_rels: set[str] = set()
    for rel in brain._index:
        if exclude_globs and path_is_excluded(rel, exclude_globs):
            continue
        if scope_glob:
            if not fnmatch.fnmatch(rel, scope_glob) and scope_glob.strip("*") not in rel:
                continue
        else:
            if _is_test_file_path(rel):
                continue
            if {p.lower() for p in Path(rel).parts[:-1]} & _AUDIT_VENDOR_DIRS:
                continue
        prod_rels.add(rel)

    lines_cache: dict[str, list[str]] = {}
    schema_registry = build_resolved_schema_registry(brain)

    # 1. Cross-stack HTTP schemas (PB011)
    http_diags, http_hotspots, http_stats = verify_cross_stack_http_schemas(
        brain, focus_rels=prod_rels if scope_glob else None, lines_cache=lines_cache,
    )
    if scope_glob:
        http_hotspots = [h for h in http_hotspots if h.get("rel_path") in prod_rels]
        http_diags = [
            d for d in http_diags
            if _extract_location(d, fallback_uri="")[0] in prod_rels
        ]

    # 2. React Lifecycle/Remount/Race (PB012) & Python Pydantic/ORM/IO Hazards (PB013)
    react_diags: list[str] = []
    py_diags: list[str] = []
    semantic_hotspots: list[dict] = list(http_hotspots)

    for rel in sorted(prod_rels):
        ext = Path(rel).suffix.lower()
        info = brain._index.get(rel) or {}
        if ext in _JS_TS_EXTS:
            fns = info.get("functions") or ()
            may_have_react = ext in (".jsx", ".tsx") or any(
                fn.get("name", "")[:1].isupper()
                or "useEffect" in (fn.get("calls") or ())
                or any(c[:1].isupper() for c in (fn.get("calls") or ()))
                for fn in fns
            )
            if not may_have_react:
                continue
            txt = brain._get_file_text(rel) or ""
            if txt:
                r_d, r_h = analyze_react_lifecycle_and_race_contracts(rel, txt, brain)
                react_diags.extend(r_d)
                semantic_hotspots.extend(r_h)
        elif ext == ".py":
            fns = info.get("functions") or ()
            if not any(
                any(c in _PY_HAZARD_CALL_TRIGGERS for c in (fn.get("calls") or ()))
                for fn in fns
            ):
                continue
            txt = brain._get_file_text(rel) or ""
            if txt:
                p_d, p_h = analyze_python_semantic_hazards(rel, txt, brain, schema_registry=schema_registry)
                py_diags.extend(p_d)
                semantic_hotspots.extend(p_h)

    # 3. Existing 6D AST Laws (Laws 1-6, SQL NOT NULL, disjoint types) + 4-Pillar Cross-File Contracts
    static_6d, _ = brain._compile_6d_contract_diagnostics(
        "static contract bug audit", prod_rels, strict_focus=True, skip_http_schemas=True,
    ) if prod_rels else ([], [])
    xf_deadline_state: dict[str, bool] = {"exceeded": False}
    static_xf = (
        verify_cross_file_contracts(
            brain,
            prod_rels,
            lines_cache=lines_cache,
            include_deep_semantic=False,
            deadline=t0 + 4.0,
            deadline_state=xf_deadline_state,
            skip_git_head=True,
        )
        if prod_rels
        else []
    )

    error_diags = list(dict.fromkeys([*http_diags, *react_diags, *py_diags, *static_6d, *static_xf]))
    hotspot_msgs = [h["_diag_msg"] for h in semantic_hotspots if h.get("_diag_msg")]
    combined_diags = list(dict.fromkeys([*error_diags, *hotspot_msgs]))

    # Convert any `static_6d` / `static_xf` diagnostics not yet in `semantic_hotspots` into ranked hotspot records
    covered_loc_rules: set[tuple[str, int]] = {
        (_clean_uri(h["rel_path"]), int(h["line"])) for h in semantic_hotspots
    }
    for msg in [*static_xf, *static_6d]:
        uri, ln, _ = _extract_location(msg, fallback_uri="")
        if not uri or uri == "REPOSITORY" or (uri, ln) in covered_loc_rules:
            continue
        if scope_glob and uri not in prod_rels:
            continue
        covered_loc_rules.add((uri, ln))
        rule_id = _classify_diag_rule_id(msg)
        clean_summary = re.sub(r"^-\s*(?:⚠️|\?\?)\s*\*\*\[[^\]]+\]\*\*\s*`[^`]+`(?:\s*\(`[^`]+`\))?\s*(?:→|—|\?)\s*", "", msg).strip()
        semantic_hotspots.append({
            "severity": "error",
            "confidence": "proven" if rule_id in ("PB003", "PB004", "PB005") else "high",
            "rule_id": rule_id,
            "category": rule_id.lower(),
            "rel_path": uri,
            "line": ln,
            "peer_target": "",
            "summary": clean_summary[:200],
            "recommended_read": f"{uri}:{max(1, ln - 8)}-{ln + 14}",
        })

    # Sort hotspots deterministically: proven errors -> high errors -> warnings, then file + line
    _SEV_RANK = {"error": 0, "warning": 1, "note": 2}
    _CONF_RANK = {"proven": 0, "high": 1}
    semantic_hotspots.sort(
        key=lambda h: (
            _SEV_RANK.get(h.get("severity", "error"), 9),
            _CONF_RANK.get(h.get("confidence", "high"), 9),
            h.get("rel_path", ""),
            int(h.get("line") or 1),
        )
    )
    cleaned_hotspots: list[dict] = []
    for idx, h in enumerate(semantic_hotspots, 1):
        h_clean = {k: v for k, v in h.items() if k != "_diag_msg"}
        h_clean["rank"] = idx
        cleaned_hotspots.append(h_clean)

    flagged_files = {h["rel_path"] for h in cleaned_hotspots if h.get("rel_path") in prod_rels}
    elapsed_ms = round((_time.perf_counter() - t0) * 1000.0, 1)

    return {
        "error_diagnostics": error_diags,
        "combined_diagnostics": combined_diags,
        "ranked_hotspots": cleaned_hotspots[:100],
        "total_hotspots": len(cleaned_hotspots),
        "audit_coverage": {
            "files_scanned": len(prod_rels),
            "clean_files_verified": max(0, len(prod_rels) - len(flagged_files)),
            "flagged_files_count": len(flagged_files),
            "http_routes_indexed": http_stats["http_routes_indexed"],
            "http_callsites_verified": http_stats["http_callsites_verified"],
            "schemas_indexed": len(schema_registry),
            "deadline_exceeded": bool(xf_deadline_state.get("exceeded")),
            "partial_audit": bool(xf_deadline_state.get("exceeded")),
            "elapsed_ms": elapsed_ms,
        },
    }
