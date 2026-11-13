"""Tree-sitter based parsers for Python and JS/TS/TSX.

Extracts functions (with exact AST signatures, parameters, return types,
arity_spec, interface_hash, calls, qualified_calls, and ast_hash), classes
(with bases, methods, fields/schema, signature, interface_hash, and ast_hash),
imports, import_bindings, exports, and routes.
"""
from __future__ import annotations

import bisect
import hashlib
import re
import threading
from tree_sitter import Language, Parser, Query, QueryCursor

_PY: Language | None = None
_JS: Language | None = None
_TS: Language | None = None
_TSX: Language | None = None
_GO: Language | None = None
_RS: Language | None = None
_JAVA: Language | None = None

_LANG_MAP: dict[str, Language] = {}
_UNAVAILABLE_EXTS: set[str] = set()
_LANG_LOCK = threading.RLock()
_THREAD_LOCAL = threading.local()


def get_language(ext: str) -> Language | None:
    """Lazily load and cache the Tree-sitter Language for `ext` on first use."""
    global _PY, _JS, _TS, _TSX, _GO, _RS, _JAVA
    ext_l = ext.lower()
    if ext_l in _LANG_MAP:
        return _LANG_MAP[ext_l]
    if ext_l in _UNAVAILABLE_EXTS:
        return None
    with _LANG_LOCK:
        if ext_l in _LANG_MAP:
            return _LANG_MAP[ext_l]
        if ext_l in _UNAVAILABLE_EXTS:
            return None
        try:
            if ext_l == ".py":
                import tree_sitter_python as _tspy
                _PY = Language(_tspy.language())
                _LANG_MAP[".py"] = _PY
            elif ext_l in (".js", ".mjs", ".cjs"):
                import tree_sitter_javascript as _tsjs
                _JS = Language(_tsjs.language())
                for k in (".js", ".mjs", ".cjs"):
                    _LANG_MAP[k] = _JS
            elif ext_l in (".ts", ".tsx", ".jsx"):
                try:
                    import tree_sitter_typescript as _tsts
                    _TS = Language(_tsts.language_typescript())
                    _TSX = Language(_tsts.language_tsx())
                    _LANG_MAP[".ts"] = _TS
                    _LANG_MAP[".tsx"] = _TSX
                    # Official JSX spec (`JSXAttributeName := JSXIdentifier`) permits JS reserved words
                    # like `class` and `for` as attribute names; `language_tsx` parses JSX spec-compliantly.
                    _LANG_MAP[".jsx"] = _TSX
                except ImportError:
                    if ext_l == ".jsx":
                        import tree_sitter_javascript as _tsjs
                        _JS = Language(_tsjs.language())
                        _LANG_MAP[".jsx"] = _JS
                    else:
                        raise
            elif ext_l == ".go":
                import tree_sitter_go as _tsgo
                _GO = Language(_tsgo.language())
                _LANG_MAP[".go"] = _GO
            elif ext_l == ".rs":
                import tree_sitter_rust as _tsrs
                _RS = Language(_tsrs.language())
                _LANG_MAP[".rs"] = _RS
            elif ext_l == ".java":
                import tree_sitter_java as _tsjava
                _JAVA = Language(_tsjava.language())
                _LANG_MAP[".java"] = _JAVA
            else:
                _UNAVAILABLE_EXTS.add(ext_l)
                return None
        except ImportError:
            _UNAVAILABLE_EXTS.add(ext_l)
            return None
        return _LANG_MAP.get(ext_l)


def get_parser(ext: str) -> Parser | None:
    """Return a thread-local Tree-sitter Parser for `ext` (or None if unsupported)."""
    ext_l = ext.lower()
    lang = get_language(ext_l)
    if lang is None:
        return None
    parsers_map: dict[str, Parser] | None = getattr(_THREAD_LOCAL, "parsers", None)
    if parsers_map is None:
        parsers_map = {}
        _THREAD_LOCAL.parsers = parsers_map
    parser = parsers_map.get(ext_l)
    if parser is None:
        parser = Parser(lang)
        parsers_map[ext_l] = parser
    return parser


def _split_top_level_params(inner: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str: str | None = None
    for ch in inner:
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            cur.append(ch)
        elif ch in "([{<":
            depth += 1
            cur.append(ch)
        elif ch in ")]}>":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        tail = "".join(cur).strip()
        if tail:
            parts.append(tail)
    return parts


def extract_arity_spec(params_str: str, is_method: bool = False, is_python: bool = True) -> dict:
    """Compute formal parameter arity bounds (min_args, max_args, kwonly_required, param_names, required_pos_names)."""
    s = params_str.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    if not s:
        return {
            "min_args": 0,
            "max_args": 0,
            "has_varargs": False,
            "has_kwargs": False,
            "kwonly_required": [],
            "param_names": [],
            "pos_param_names": [],
            "required_pos_names": [],
        }
    raw_parts = _split_top_level_params(s)
    min_pos = 0
    max_pos = 0
    has_varargs = False
    has_kwargs = False
    in_kwonly = False
    kwonly_req: list[str] = []
    all_param_names: list[str] = []
    pos_param_names: list[str] = []
    required_pos_names: list[str] = []
    posonly_set: set[str] = set()

    for idx, p in enumerate(raw_parts):
        if not p:
            continue
        if p == "/":
            posonly_set.update(pos_param_names)
            continue
        first_tok = p.split(":")[0].split("=")[0].strip()
        if is_python and idx == 0 and (is_method or first_tok in ("self", "cls")):
            if first_tok in ("self", "cls"):
                continue
        if not is_python and idx == 0 and first_tok == "this":
            continue
        if p == "*":
            in_kwonly = True
            continue
        if p.startswith("**"):
            has_kwargs = True
            continue
        if p.startswith("*") or p.startswith("..."):
            has_varargs = True
            if is_python:
                in_kwonly = True
            continue

        top_eq = False
        top_opt = False
        d = 0
        q_ch: str | None = None
        for ch_i, ch in enumerate(p):
            if q_ch:
                if ch == q_ch:
                    q_ch = None
                continue
            if ch in ("'", '"', "`"):
                q_ch = ch
            elif ch in "([{<":
                d += 1
            elif ch in ")]}>":
                d = max(0, d - 1)
            elif d == 0 and ch == "=" and (ch_i + 1 >= len(p) or p[ch_i + 1] != ">"):
                top_eq = True
                break
            elif d == 0 and ch == ":" and ch_i > 0 and p[ch_i - 1] == "?":
                top_opt = True
        head = p.split(":", 1)[0].split("=", 1)[0].strip()
        if head.endswith("?"):
            top_opt = True
            head = head[:-1].strip()

        is_optional = top_eq or top_opt
        is_valid_ident = bool(re.match(r"^[A-Za-z_]\w*$", head))
        if is_valid_ident:
            all_param_names.append(head)
        if in_kwonly:
            if not is_optional and is_valid_ident:
                kwonly_req.append(head)
        else:
            max_pos += 1
            if is_valid_ident:
                pos_param_names.append(head)
            if not is_optional:
                min_pos += 1
                if is_valid_ident:
                    required_pos_names.append(head)

    return {
        "min_args": min_pos,
        "max_args": None if has_varargs else max_pos,
        "has_varargs": has_varargs,
        "has_kwargs": has_kwargs,
        "kwonly_required": kwonly_req,
        "param_names": [nm for nm in all_param_names if nm not in posonly_set],
        "pos_param_names": pos_param_names,
        "required_pos_names": required_pos_names,
    }


_SCOPE_BOUNDARY_TYPES = {
    "function_definition", "function_declaration", "function_expression",
    "arrow_function", "method_definition", "generator_function_declaration",
    "class_definition", "class_declaration",
}


def _extract_dict_or_obj_keys(expr_node, is_python: bool) -> list[str] | None:
    while expr_node is not None and expr_node.type == "parenthesized_expression":
        named = [c for c in expr_node.children if c.is_named and c.type != "comment"]
        expr_node = named[0] if len(named) == 1 else None
    if expr_node is None:
        return None
    target_type = "dictionary" if is_python else "object"
    if expr_node.type != target_type:
        return None
    keys: list[str] = []
    for child in expr_node.children:
        if not child.is_named or child.type == "comment":
            continue
        if child.type in ("dictionary_splat", "spread_element"):
            return None
        if child.type == "pair":
            k_node = child.child_by_field_name("key")
            if k_node is None:
                return None
            if k_node.type == "string":
                raw_k = _text(k_node).strip("'\"`")
                if raw_k and "{" not in raw_k:
                    keys.append(raw_k)
                else:
                    return None
            elif not is_python and k_node.type in ("property_identifier", "identifier"):
                keys.append(_text(k_node))
            else:
                return None
        elif not is_python and child.type in ("shorthand_property_identifier", "shorthand_property_identifier_pattern"):
            keys.append(_text(child))
        else:
            return None
    return sorted(set(keys)) if keys else None


def _extract_closed_return_keys(func_node, is_python: bool = True) -> list[str] | None:
    """If every value-returning branch of `func_node` returns a closed dict/object literal, return its sorted keys."""
    if not is_python and func_node.type == "variable_declarator":
        val_n = func_node.child_by_field_name("value")
        if val_n is not None:
            func_node = val_n
    if not is_python and func_node.type == "arrow_function":
        body = func_node.child_by_field_name("body")
        if body is not None and body.type != "statement_block":
            return _extract_dict_or_obj_keys(body, is_python=False)

    ret_nodes = []
    has_yield = False

    def _walk(node):
        nonlocal has_yield
        for ch in node.children:
            if ch.type in _SCOPE_BOUNDARY_TYPES:
                continue
            if ch.type in ("yield", "yield_expression"):
                has_yield = True
                return
            if ch.type == "return_statement":
                ret_nodes.append(ch)
            else:
                _walk(ch)

    _walk(func_node)
    if has_yield or not ret_nodes:
        return None

    all_keys: set[str] = set()
    saw_dict = False
    for r_node in ret_nodes:
        exprs = [c for c in r_node.children if c.is_named and c.type != "comment"]
        if not exprs:
            continue
        expr = exprs[0]
        if _text(expr).strip() in ("None", "null", "undefined"):
            continue
        ks = _extract_dict_or_obj_keys(expr, is_python=is_python)
        if ks is None:
            return None
        saw_dict = True
        all_keys.update(ks)

    return sorted(all_keys) if saw_dict else None


_PRIMITIVE_TYPE_NAMES = {
    "str", "int", "float", "bool", "bytes", "dict", "list", "set", "tuple",
    "any", "none", "object", "callable", "iterable", "iterator", "sequence",
    "mapping", "optional", "union", "literal", "type", "string", "number",
    "boolean", "void", "unknown", "never", "record", "array", "promise",
    "valueerror", "typeerror", "runtimeerror", "keyerror", "indexerror",
    "attributeerror", "oserror", "exception", "path",
}


_PARAM_TYPE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\??\s*:\s*([A-Za-z_][A-Za-z0-9_.]*)")
_LOCAL_CTOR_RE = re.compile(
    r"\b((?:self\.|this\.)?[a-z_][A-Za-z0-9_]*)\s*(?::\s*[A-Za-z0-9_.\[\]| ]+\s*)?=\s*(?:new\s+)?([A-Z][A-Za-z0-9_]*)\s*\("
)


def _extract_param_and_local_types(params_str: str | None, body_text: str) -> dict[str, str]:
    """Extract static receiver type bindings (`var -> ClassName`) from parameter annotations and constructor assignments."""
    types_map: dict[str, str] = {}
    if params_str and ":" in params_str:
        inner = params_str.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        for m_p in _PARAM_TYPE_RE.finditer(inner):
            p_nm, t_raw = m_p.group(1), m_p.group(2).split(".")[-1]
            if p_nm not in ("self", "cls", "this") and t_raw.lower() not in _PRIMITIVE_TYPE_NAMES:
                types_map[p_nm] = t_raw
    if body_text and "=" in body_text and "(" in body_text and len(body_text) <= 16000:
        for m_c in _LOCAL_CTOR_RE.finditer(body_text):
            v_nm, cls_nm = m_c.group(1), m_c.group(2)
            if cls_nm.lower() not in _PRIMITIVE_TYPE_NAMES:
                types_map.setdefault(v_nm, cls_nm)
    return types_map


_HTTP = {"get", "post", "put", "delete", "patch", "all"}

# Receivers that genuinely mount express/koa/fastify routes. Anything else
# calling .get()/.delete() is almost certainly a Map, Set, URLSearchParams,
# Headers or cache — see _is_express_route.
_ROUTER_RECEIVERS = {"app", "router", "server", "api", "express", "fastify", "koa"}


def _is_express_route(receiver: str, path: str) -> bool:
    """Distinguish `app.get('/users')` from `searchParams.get('className')`."""
    if not path.startswith("/"):
        return False
    tail = receiver.split(".")[-1].split("(")[0].strip().lower()
    return tail in _ROUTER_RECEIVERS or tail.endswith("router") or tail.endswith("app")


# ── shared helpers ──────────────────────────────────────────────────────────

_QUERY_CACHE: dict[tuple[int, str], Query] = {}


def _get_compiled_query(lang: Language, pattern: str) -> Query:
    key = (id(lang), pattern)
    q = _QUERY_CACHE.get(key)
    if q is None:
        q = Query(lang, pattern)
        _QUERY_CACHE[key] = q
    return q


def _captures(lang: Language, pattern: str, root) -> dict:
    raw = QueryCursor(_get_compiled_query(lang, pattern)).captures(root)
    return {k: sorted(v, key=lambda n: n.start_byte) for k, v in raw.items()}


def _matches(lang: Language, pattern: str, root):
    return QueryCursor(_get_compiled_query(lang, pattern)).matches(root)

def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")

def _line(node) -> int:
    return node.start_point[0] + 1

def _end_line(node) -> int:
    return node.end_point[0] + 1

def _ast_hash(text: str) -> str:
    """Fast 6-byte hex blake2b hash of normalized AST node text for staleness detection."""
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.blake2b(normalized.encode("utf-8", errors="replace"), digest_size=6).hexdigest()

def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


# ── Python ──────────────────────────────────────────────────────────────────

_PY_FUNC = """
(function_definition name: (identifier) @name) @func
"""
_PY_CLASS = """
(class_definition name: (identifier) @name) @class
"""
_PY_IMPORT = """
[
  (import_statement name:
    [(dotted_name) @mod
     (aliased_import name: (dotted_name) @mod)])
  (import_from_statement module_name:
    [(dotted_name) @mod
     (relative_import) @mod])
]
"""
_PY_ROUTE = """
(decorated_definition
  (decorator
    (call
      function: (attribute object: (_) @router_obj attribute: (identifier) @method)
      arguments: (argument_list (string (string_content) @path)) @args))
  definition: (function_definition name: (identifier) @handler))
"""
_PY_DJANGO_ROUTE = """
(call
  function: (identifier) @func
  arguments: (argument_list
    (string (string_content) @path)
    [(identifier) @handler
     (attribute attribute: (identifier) @handler)]))
"""
_PY_CALLS = """
(call function:
  [(identifier) @callee
   (attribute object: (_) @receiver attribute: (identifier) @callee)])
"""


def _py_docstring(node) -> str:
    """Extract the docstring from a function or class node, capped at 200 chars."""
    body = next((c for c in node.children if c.type == "block"), None)
    if not body:
        return ""
    first_expr = next((c for c in body.children if c.type == "expression_statement"), None)
    if not first_expr:
        return ""
    string_node = next((c for c in first_expr.children if c.type == "string"), None)
    if not string_node:
        return ""
    content = next((c for c in string_node.children if c.type == "string_content"), None)
    return _text(content)[:200] if content else ""


def _py_func_signature(func_node) -> tuple[str, str, str]:
    """Extract (signature_str, params_str, return_type_str) from a Python function_definition."""
    name_node = func_node.child_by_field_name("name")
    params_node = func_node.child_by_field_name("parameters")
    ret_node = func_node.child_by_field_name("return_type")

    name = _text(name_node) if name_node else "anonymous"
    params = _collapse_ws(_text(params_node)) if params_node else "()"
    ret = _collapse_ws(_text(ret_node)) if ret_node else ""

    is_async = any(c.type == "async" for c in func_node.children)
    prefix = "async def" if is_async else "def"
    sig = f"{prefix} {name}{params}"
    if ret:
        sig += f" -> {ret}"
    return sig, params, ret


def _py_class_fields(body_node) -> list[str]:
    """Extract class-level field/attribute declarations (e.g. Pydantic, dataclass, SQLAlchemy, attrs)."""
    if not body_node:
        return []
    fields: list[str] = []
    for child in body_node.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "assignment":
                    left = sub.child_by_field_name("left")
                    typ = sub.child_by_field_name("type")
                    right = sub.child_by_field_name("right")
                    if left and left.type == "identifier":
                        lhs = _text(left)
                        if typ:
                            fields.append(f"{lhs}: {_collapse_ws(_text(typ))}")
                        elif right:
                            rhs = _collapse_ws(_text(right))
                            if len(rhs) <= 95:
                                fields.append(f"{lhs} = {rhs}")
                            else:
                                fields.append(lhs)
                    elif not left and typ:
                        ann_text = _collapse_ws(_text(sub))
                        if len(ann_text) <= 80:
                            fields.append(ann_text)
                elif sub.type == "type":
                    ann_text = _collapse_ws(_text(sub))
                    if len(ann_text) <= 80:
                        fields.append(ann_text)
        elif child.type == "typed_parameter" or child.type == "assignment":
            ann_text = _collapse_ws(_text(child))
            if len(ann_text) <= 80:
                fields.append(ann_text)
    return fields[:60]


def _py_bases(cls_node) -> list[str]:
    """Extract base class names from a class_definition node."""
    bases_node = cls_node.child_by_field_name("superclasses") or cls_node.child_by_field_name("bases")
    if not bases_node:
        return []
    result = []
    for child in bases_node.children:
        if child.type == "identifier":
            result.append(_text(child))
        elif child.type == "attribute":
            attr = child.child_by_field_name("attribute")
            if attr:
                result.append(_text(attr))
    return result


def _flask_methods(arg_list_node) -> list[str]:
    """Extract methods=[...] or methods=(...) from a Flask @app.route(...) argument_list."""
    for child in arg_list_node.children:
        if child.type != "keyword_argument":
            continue
        name_node = child.child_by_field_name("name")
        if not name_node or _text(name_node) != "methods":
            continue
        value_node = child.child_by_field_name("value")
        if not value_node or value_node.type not in ("list", "tuple", "set"):
            continue
        methods = []
        for item in value_node.children:
            if item.type != "string":
                continue
            content = next((c for c in item.children if c.type == "string_content"), None)
            if content:
                methods.append(_text(content).upper())
        return methods or ["GET"]
    return ["GET"]


def _py_direct_methods(body_node) -> list:
    """Return only direct function_definition children of a class body block."""
    methods = []
    for child in body_node.children:
        if child.type == "function_definition":
            methods.append(child)
        elif child.type == "decorated_definition":
            inner = child.child_by_field_name("definition")
            if inner and inner.type == "function_definition":
                methods.append(inner)
    return methods


def _py_extract_imports_and_bindings(root) -> tuple[list[str], dict[str, dict]]:
    """Extract both backward-compatible `imports` list AND symbol-level `import_bindings`."""
    caps = _captures(_PY, _PY_IMPORT, root)
    imports_set: set[str] = set()
    bindings: dict[str, dict] = {}

    for n in caps.get("mod", []):
        mod_text = _text(n)
        if mod_text.replace(".", "") == "":
            parent = n.parent
            if parent and parent.type == "import_from_statement":
                for child in parent.children:
                    if child.type == "dotted_name" and child != n:
                        sym = _text(child)
                        imports_set.add(mod_text + sym)
                        bindings[sym] = {"module": mod_text + sym, "symbol": sym}
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if name_node:
                            sym = _text(name_node)
                            local = _text(alias_node) if alias_node else sym
                            imports_set.add(mod_text + sym)
                            bindings[local] = {"module": mod_text + sym, "symbol": sym}
        else:
            imports_set.add(mod_text)

    def _walk_imports(node):
        for child in node.children:
            if child.type == "import_statement":
                for sub in child.children:
                    if sub.type == "dotted_name":
                        mod_name = _text(sub)
                        bindings[mod_name.split(".")[0]] = {"module": mod_name, "symbol": "*"}
                    elif sub.type == "aliased_import":
                        name_node = sub.child_by_field_name("name")
                        alias_node = sub.child_by_field_name("alias")
                        if name_node and alias_node:
                            bindings[_text(alias_node)] = {
                                "module": _text(name_node),
                                "symbol": "*",
                            }
            elif child.type == "import_from_statement":
                mod_node = child.child_by_field_name("module_name")
                mod_name = _text(mod_node) if mod_node else ""
                imp_line = _line(child)
                for sub in child.children:
                    if sub == mod_node:
                        continue
                    if sub.type == "dotted_name":
                        sym = _text(sub)
                        full_mod = f"{mod_name}.{sym}" if mod_name and not mod_name.endswith(".") else f"{mod_name}{sym}"
                        bindings[sym] = {"module": mod_name or ".", "symbol": sym, "qualified_module": full_mod, "line": imp_line}
                    elif sub.type == "aliased_import":
                        name_node = sub.child_by_field_name("name")
                        alias_node = sub.child_by_field_name("alias")
                        if name_node:
                            sym = _text(name_node)
                            local = _text(alias_node) if alias_node else sym
                            full_mod = f"{mod_name}.{sym}" if mod_name and not mod_name.endswith(".") else f"{mod_name}{sym}"
                            bindings[local] = {"module": mod_name or ".", "symbol": sym, "qualified_module": full_mod, "line": imp_line}
                    elif sub.type == "wildcard_import":
                        bindings[f"__star__{mod_name or '.'}"] = {
                            "module": mod_name or ".",
                            "symbol": "*",
                            "is_reexport": True,
                            "line": imp_line,
                        }
            elif child.type in ("block", "if_statement", "try_statement", "function_definition"):
                _walk_imports(child)

    _walk_imports(root)
    return list(imports_set), bindings


_BODY_NOISE = {
    "return", "const", "let", "var", "function", "async", "await", "true", "false",
    "null", "undefined", "self", "this", "cls", "def", "class", "import", "from",
    "export", "string", "number", "boolean", "any", "void", "none", "dict", "list",
    "set", "tuple", "int", "float", "str", "bool", "else", "elif", "for", "while",
    "try", "except", "catch", "finally", "new", "typeof", "instanceof", "Promise",
}


_BT_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_BT_STR_KEY_RE = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_.\-]{2,40})['\"]")
_BT_CODE_ID_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_BT_WORD_RE = re.compile(r"[a-zA-Z]{3,}")
_BT_CAMEL_RE = re.compile(r"([a-z])([A-Z])")


def _collect_call_events(
    lang: Language,
    call_query: str,
    root,
    jsx_query: str | None = None,
) -> tuple[list[int], list[tuple[int, str, int, str | None]]]:
    """Run call query once at file `root` and return sorted `(starts, events)` for O(log M + k) per-function slicing."""
    events: list[tuple[int, str, int, str | None]] = []
    for _, mc in _matches(lang, call_query, root):
        callee_nodes = mc.get("callee")
        if not callee_nodes:
            continue
        c_nd = callee_nodes[0]
        r_nodes = mc.get("receiver")
        r_txt = _text(r_nodes[0]) if r_nodes else None
        events.append((c_nd.start_byte, _text(c_nd), _line(c_nd), r_txt))
    if jsx_query is not None:
        for _, mc_j in _matches(lang, jsx_query, root):
            callee_nodes = mc_j.get("callee")
            if callee_nodes:
                c_nd = callee_nodes[0]
                c_txt = _text(c_nd)
                if c_txt[:1].isupper():
                    events.append((c_nd.start_byte, c_txt, _line(c_nd), None))
    events.sort(key=lambda x: x[0])
    return [ev[0] for ev in events], events


def _slice_call_events(
    starts: list[int],
    events: list[tuple[int, str, int, str | None]],
    fn_node,
) -> tuple[list[str], dict[str, int], dict[str, list[int]], dict[str, int]]:
    lo = bisect.bisect_left(starts, fn_node.start_byte)
    hi = bisect.bisect_right(starts, fn_node.end_byte)
    calls: list[str] = []
    call_sites: dict[str, int] = {}
    call_sites_all: dict[str, list[int]] = {}
    qualified_calls: dict[str, int] = {}
    for idx in range(lo, hi):
        _, callee_text, line, receiver_text = events[idx]
        if callee_text not in call_sites:
            calls.append(callee_text)
            call_sites[callee_text] = line
        elif line < call_sites[callee_text]:
            call_sites[callee_text] = line
        s_list = call_sites_all.setdefault(callee_text, [])
        if line not in s_list and len(s_list) < 8:
            s_list.append(line)
            s_list.sort()
        if receiver_text is not None:
            qual = f"{receiver_text}.{callee_text}"
            if qual not in qualified_calls or line < qualified_calls[qual]:
                qualified_calls[qual] = line
    return calls, call_sites, call_sites_all, qualified_calls


_IDENT_TOKS_CACHE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}


def _split_ident_cached(ident: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    hit = _IDENT_TOKS_CACHE.get(ident)
    if hit is not None:
        return hit
    if len(_IDENT_TOKS_CACHE) > 65536:
        _IDENT_TOKS_CACHE.clear()
    is_flat = ident.islower() or ident.isupper()
    if "_" not in ident and is_flat:
        w_low = ident.lower()
        w_tup = (w_low,) if (len(w_low) >= 3 and w_low not in _BODY_NOISE) else ()
        res = ((), w_tup)
        _IDENT_TOKS_CACHE[ident] = res
        return res
    raw_low = ident.lower().strip("_")
    clean_id = ident.replace("_", "").lower()
    split_id = ident if is_flat else _BT_CAMEL_RE.sub(r"\1 \2", ident)
    s_list: list[str] = []
    for p in (raw_low, clean_id, *(x.lower() for x in split_id.replace("_", " ").split())):
        if len(p) >= 3 and p not in _BODY_NOISE and p not in s_list:
            s_list.append(p)
    res = (tuple(s_list), tuple(s_list))
    _IDENT_TOKS_CACHE[ident] = res
    return res


def _extract_body_tokens(
    func_node,
    const_map: dict[str, str] | None = None,
    raw_full_text: str | None = None,
) -> list[str]:
    """Extract distinct domain words from a function's body/comments plus any
    same-file module-level constants it references (e.g. BLOCKED_PATTERNS)."""
    raw_text = (raw_full_text if raw_full_text is not None else _text(func_node))[:6000]
    all_idents = _BT_IDENT_RE.findall(raw_text)
    uniq_idents = list(dict.fromkeys(all_idents))
    extra_words: list[str] = []
    if const_map:
        for ref in uniq_idents:
            if ref in const_map:
                extra_words.extend(_BT_WORD_RE.findall(const_map[ref]))
    out: list[str] = []
    seen: set[str] = set()
    # Pass 1: prioritize PascalCase / camelCase / snake_case code identifiers and string config keys
    if "'" in raw_text or '"' in raw_text:
        for s_key in _BT_STR_KEY_RE.findall(raw_text):
            if "_" in s_key or not s_key.islower():
                sk_low = s_key.lower()
                split_src = s_key if (s_key.islower() or s_key.isupper()) else _BT_CAMEL_RE.sub(r"\1 \2", s_key)
                for p in (
                    sk_low,
                    sk_low.replace("_", ""),
                    *(x.lower() for x in split_src.replace("_", " ").replace("-", " ").replace(".", " ").split()),
                ):
                    if len(p) >= 3 and p not in _BODY_NOISE and p not in seen:
                        seen.add(p)
                        out.append(p)
    for ident in uniq_idents:
        if len(ident) < 3:
            continue
        s_parts, _ = _split_ident_cached(ident)
        if s_parts:
            for p in s_parts:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
                    if len(out) >= 100:
                        break
            if len(out) >= 100:
                break
    # Pass 2: general lexical words up to 140 total tokens
    for w in uniq_idents:
        if len(w) < 3:
            continue
        _, w_parts = _split_ident_cached(w)
        for p in w_parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
                if len(out) >= 140:
                    return out
    for w in extra_words:
        _, w_parts = _split_ident_cached(w)
        for p in w_parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
                if len(out) >= 140:
                    return out
    return out


def _py_const_map(root) -> dict[str, str]:
    """Map top-level Python module constants to their assignment text + nearby comments."""
    cmap: dict[str, str] = {}
    children = list(root.children)
    for idx, child in enumerate(children):
        node = child
        if child.type == "expression_statement" and child.children:
            node = child.children[0]
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left and left.type == "identifier":
                cname = _text(left)
                ctx_parts = [_text(node)[:1500]]
                for back in range(max(0, idx - 6), idx):
                    if children[back].type == "comment":
                        ctx_parts.append(_text(children[back]))
                cmap[cname] = " ".join(ctx_parts)
    return cmap


def _js_const_map_and_leading_docs(root) -> tuple[dict[str, str], dict[int, str]]:
    """Map top-level JS/TS constants to their text+comments, and map declaration start lines to leading JSDoc."""
    cmap: dict[str, str] = {}
    leading_docs: dict[int, str] = {}
    children = list(root.children)
    for idx, child in enumerate(children):
        if idx > 0 and children[idx - 1].type == "comment":
            comment_txt = _text(children[idx - 1]).strip("/* \t\r\n")
            comment_txt = re.sub(r"(?m)^\s*\*\s?", " ", comment_txt)
            leading_docs[_line(child)] = _collapse_ws(comment_txt)[:200]
        decl = child
        if child.type == "export_statement":
            d = child.child_by_field_name("declaration")
            if d:
                decl = d
                if _line(child) in leading_docs:
                    leading_docs[_line(decl)] = leading_docs[_line(child)]
        if decl.type in ("lexical_declaration", "variable_declaration"):
            for sub in decl.children:
                if sub.type == "variable_declarator":
                    name_n = sub.child_by_field_name("name")
                    val_n = sub.child_by_field_name("value")
                    if name_n and val_n and val_n.type not in ("arrow_function", "function_expression"):
                        cname = _text(name_n)
                        ctx_parts = [_text(sub)[:1500]]
                        for back in range(max(0, idx - 6), idx):
                            if children[back].type == "comment":
                                ctx_parts.append(_text(children[back]))
                        cmap[cname] = " ".join(ctx_parts)
    return cmap, leading_docs


def _extract_const_aliases(const_map: dict[str, str]) -> dict[str, list[str]]:
    """Extract bijective symbolic token mappings (e.g. `videos: 'watch'`) from top-level lookup dicts/objects."""
    aliases: dict[str, list[str]] = {}
    if not const_map:
        return aliases
    _PAIR_PAT = re.compile(
        r"""(?:['"]([a-z][a-z0-9_-]{2,20})['"]|\b([a-z][a-z0-9_-]{2,20}))\s*:\s*['"]([a-z][a-z0-9_-]{2,20})['"]"""
    )
    for c_text in const_map.values():
        if "{" not in c_text or "}" not in c_text:
            continue
        pairs = _PAIR_PAT.findall(c_text)
        if not (1 <= len(pairs) <= 25):
            continue
        for k1, k2, val in pairs:
            key = (k1 or k2).strip()
            val = val.strip()
            if key == val or key in _BODY_NOISE or val in _BODY_NOISE:
                continue
            if val not in aliases.setdefault(key, []):
                aliases[key].append(val)
            if key not in aliases.setdefault(val, []):
                aliases[val].append(key)
    return aliases


def _parse_python(root) -> dict:
    functions, classes, routes = [], [], []
    method_to_class: dict[tuple, str] = {}
    py_consts = _py_const_map(root)

    # Phase 1 — classes first
    caps = _captures(_PY, _PY_CLASS, root)
    for cls_node in caps.get("class", []):
        name_node = cls_node.child_by_field_name("name")
        if not name_node:
            continue
        cls_name = _text(name_node)
        bases = _py_bases(cls_node)

        body_node = cls_node.child_by_field_name("body")
        method_names = []
        if body_node:
            for meth_node in _py_direct_methods(body_node):
                meth_name_node = meth_node.child_by_field_name("name")
                if meth_name_node:
                    method_names.append(_text(meth_name_node))
                method_to_class[meth_node.start_point] = cls_name

        fields = _py_class_fields(body_node)
        base_str = f"({', '.join(bases)})" if bases else ""
        cls_sig = f"class {cls_name}{base_str}:"

        classes.append({
            "name": cls_name,
            "line_start": _line(cls_node),
            "line_end": _end_line(cls_node),
            "signature": cls_sig,
            "docstring": _py_docstring(cls_node),
            "bases": bases,
            "methods": method_names,
            "fields": fields,
            "interface_hash": _ast_hash(f"{cls_sig}|{'|'.join(method_names)}|{'|'.join(fields)}"),
            "ast_hash": _ast_hash(_text(cls_node)),
        })

    # Phase 2 — all functions
    root_span = max(1, _end_line(root) - _line(root))
    caps = _captures(_PY, _PY_FUNC, root)
    raw_py_funcs = caps.get("func", [])
    # Identify non-overload definitions so @typing.overload stubs don't duplicate real methods
    non_overload_keys: set[tuple[str | None, str]] = set()
    for fn_n in raw_py_funcs:
        nm_n = fn_n.child_by_field_name("name")
        if not nm_n:
            continue
        p_cls = method_to_class.get(fn_n.start_point)
        is_ovl = False
        if fn_n.parent and fn_n.parent.type == "decorated_definition":
            for dec_c in fn_n.parent.children:
                if dec_c.type == "decorator" and re.search(r"\boverload\b", _text(dec_c)):
                    is_ovl = True
                    break
        if not is_ovl:
            non_overload_keys.add((p_cls, _text(nm_n)))

    py_call_starts, py_call_events = _collect_call_events(_PY, _PY_CALLS, root) if raw_py_funcs else ([], [])

    for func_node in raw_py_funcs:
        name_node = func_node.child_by_field_name("name")
        if not name_node:
            continue
        fn_nm_str = _text(name_node)
        parent_cls = method_to_class.get(func_node.start_point)
        decorators: list[str] = []
        if func_node.parent and func_node.parent.type == "decorated_definition":
            for dec_c in func_node.parent.children:
                if dec_c.type == "decorator":
                    decorators.append(_collapse_ws(_text(dec_c)).lstrip("@").strip())
            if any(re.search(r"\boverload\b", d) for d in decorators) and (parent_cls, fn_nm_str) in non_overload_keys:
                continue
        is_nested_fn = False
        anc = func_node.parent
        while anc is not None and anc != root:
            if anc.type == "function_definition" and (_end_line(anc) - _line(anc)) <= root_span * 0.8:
                is_nested_fn = True
                break
            anc = anc.parent
        calls, call_sites, call_sites_all, qualified_calls = _slice_call_events(
            py_call_starts, py_call_events, func_node,
        )
        sig, params, ret_type = _py_func_signature(func_node)
        is_meth = parent_cls is not None
        fn_body_raw = _text(func_node)
        param_types = _extract_param_and_local_types(params, fn_body_raw) if qualified_calls else {}
        ret_keys = (
            _extract_closed_return_keys(func_node, is_python=True)
            if ("{" in fn_body_raw and "return" in fn_body_raw)
            else None
        )
        iface_str = f"{sig}|ret:{','.join(ret_keys)}" if ret_keys else sig
        entry = {
            "name": fn_nm_str,
            "line_start": _line(func_node),
            "line_end": _end_line(func_node),
            "signature": sig,
            "params": params,
            "return_type": ret_type,
            "arity_spec": extract_arity_spec(params, is_method=is_meth, is_python=True),
            "param_types": param_types,
            "docstring": _py_docstring(func_node),
            "body_tokens": _extract_body_tokens(func_node, py_consts, raw_full_text=fn_body_raw),
            "calls": calls,
            "call_sites": call_sites,
            "call_sites_all": call_sites_all,
            "qualified_calls": qualified_calls,
            "interface_hash": _ast_hash(iface_str),
            "ast_hash": _ast_hash(fn_body_raw),
        }
        if ret_keys:
            entry["return_keys"] = ret_keys
        if decorators:
            entry["decorators"] = decorators
        if is_meth:
            entry["is_method"] = True
            entry["parent_class"] = parent_cls
        if is_nested_fn:
            entry["is_nested"] = True
        functions.append(entry)

    imports, import_bindings = _py_extract_imports_and_bindings(root)
    raw_root_text = _text(root)
    router_prefixes: dict[str, str] = {}
    default_router_prefix = ""
    for m_rp in re.finditer(
        r"(?:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)?\b(?:APIRouter|Blueprint)\s*\([^)]*?\b(?:url_)?prefix\s*=\s*['\"]([^'\"]+)['\"]",
        raw_root_text,
    ):
        r_var, r_pref = m_rp.group(1), m_rp.group(2).rstrip("/")
        if not default_router_prefix:
            default_router_prefix = r_pref
        if r_var:
            router_prefixes[r_var] = r_pref

    seen_routes: set[tuple[str, str, str]] = set()
    for _, mc in _matches(_PY, _PY_ROUTE, root):
        m_nodes = mc.get("method", [])
        p_nodes = mc.get("path", [])
        h_nodes = mc.get("handler", [])
        a_nodes = mc.get("args", [])
        r_nodes = mc.get("router_obj", [])
        if not (m_nodes and p_nodes and h_nodes):
            continue
        method = _text(m_nodes[0])
        path = _text(p_nodes[0])
        handler = _text(h_nodes[0])
        line = _line(h_nodes[0])
        r_obj = _text(r_nodes[0]).strip() if r_nodes else ""
        active_prefix = router_prefixes.get(r_obj, default_router_prefix if r_obj in ("router", "bp", "api") or not router_prefixes else "")
        full_path = (f"{active_prefix}/{path.lstrip('/')}".rstrip("/") or "/") if active_prefix else path

        if method in _HTTP:
            rk = (method.upper(), full_path, handler)
            if rk not in seen_routes:
                seen_routes.add(rk)
                routes.append({
                    "method": method.upper(), "path": path, "full_path": full_path,
                    "handler": handler, "line": line, "framework": "fastapi",
                })
        elif method == "route" and a_nodes:
            for verb in _flask_methods(a_nodes[0]):
                rk = (verb, full_path, handler)
                if rk not in seen_routes:
                    seen_routes.add(rk)
                    routes.append({
                        "method": verb, "path": path, "full_path": full_path,
                        "handler": handler, "line": line, "framework": "flask",
                    })
    for _, mc in _matches(_PY, _PY_DJANGO_ROUTE, root):
        f_nodes = mc.get("func", [])
        p_nodes = mc.get("path", [])
        h_nodes = mc.get("handler", [])
        if not (f_nodes and p_nodes and h_nodes):
            continue
        func_name = _text(f_nodes[0])
        if func_name not in ("path", "re_path"):
            continue
        routes.append({
            "method": "ANY",
            "path": _text(p_nodes[0]),
            "handler": _text(h_nodes[0]),
            "line": _line(f_nodes[0]),
            "framework": "django",
        })

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": import_bindings,
        "exports": list(py_consts.keys()),
        "routes": routes,
        "const_aliases": _extract_const_aliases(py_consts),
    }


# ── JavaScript / TypeScript ─────────────────────────────────────────────────

_JS_FUNC = """
[
  (function_declaration name: (identifier) @name) @func
  (variable_declarator
    name: (identifier) @name
    value: [(arrow_function) (function_expression)]) @func
  (variable_declarator
    name: (identifier) @name
    value: (call_expression
      function: [(identifier) (member_expression)]
      arguments: (arguments . [(arrow_function) (function_expression)]))) @func
  (method_definition name: (property_identifier) @name) @func
]
"""
_JS_CLASS = """
(class_declaration name: (_) @name) @class
"""
_JS_IMPORT = """
[
  (import_statement source: (string (string_fragment) @mod))
  (export_statement source: (string (string_fragment) @mod))
  (call_expression
    function: (import)
    arguments: (arguments (string (string_fragment) @mod)))
  (call_expression
    function: (identifier) @_req
    arguments: (arguments (string (string_fragment) @mod)))
]
"""
_JS_EXPORT = """
[
  (export_statement
    declaration: (function_declaration name: (identifier) @name))
  (export_statement
    declaration: (class_declaration name: (_) @name))
  (export_statement
    declaration: (lexical_declaration
      (variable_declarator name: (identifier) @name)))
]
"""
_JS_ROUTE = """
(call_expression
  function: (member_expression
    object: (_) @obj
    property: (property_identifier) @method)
  arguments: (arguments (string (string_fragment) @path)) @args) @route_call
"""
_JS_CALLS = """
(call_expression function:
  [(identifier) @callee
   (member_expression object: (_) @receiver property: (property_identifier) @callee)])
"""
_JSX_CALLS = """
[
  (jsx_self_closing_element name: (identifier) @callee)
  (jsx_opening_element name: (identifier) @callee)
]
"""


def _js_func_signature(func_node, name: str) -> tuple[str, str, str]:
    """Extract (signature_str, params_str, return_type_str) from a JS/TS function node."""
    target = func_node
    if func_node.type == "variable_declarator":
        val = func_node.child_by_field_name("value")
        if val:
            if val.type == "call_expression":
                args_n = val.child_by_field_name("arguments")
                if args_n:
                    for ac in args_n.children:
                        if ac.type in ("arrow_function", "function_expression"):
                            target = ac
                            break
            else:
                target = val

    params_node = target.child_by_field_name("parameters") or target.child_by_field_name("parameter")
    ret_node = target.child_by_field_name("return_type")

    params = _collapse_ws(_text(params_node)) if params_node else "()"
    if not params.startswith("("):
        params = f"({params})"
    ret = _collapse_ws(_text(ret_node)).lstrip(":").strip() if ret_node else ""

    is_async = any(c.type == "async" for c in target.children) or any(c.type == "async" for c in func_node.children)
    prefix = "async function" if is_async else "function"
    if func_node.type == "method_definition":
        prefix = "async " if is_async else ""
        sig = f"{prefix}{name}{params}"
    else:
        sig = f"{prefix} {name}{params}"
    if ret:
        sig += f": {ret}"
    return sig, params, ret


def _js_bases(cls_node) -> list[str]:
    """Extract base class name from a JS/TS class_declaration node."""
    for child in cls_node.children:
        if child.type == "class_heritage":
            for sub in child.children:
                if sub.type == "extends_clause":
                    value = sub.child_by_field_name("value")
                    if value:
                        if value.type in ("identifier", "type_identifier"):
                            return [_text(value)]
                        if value.type == "member_expression":
                            prop = value.child_by_field_name("property")
                            if prop:
                                return [_text(prop)]
                    for ext_child in sub.children:
                        if ext_child.type in ("identifier", "type_identifier"):
                            return [_text(ext_child)]
                        if ext_child.type == "member_expression":
                            prop = ext_child.child_by_field_name("property")
                            if prop:
                                return [_text(prop)]
                elif sub.type in ("identifier", "type_identifier"):
                    return [_text(sub)]
                elif sub.type == "member_expression":
                    prop = sub.child_by_field_name("property")
                    if prop:
                        return [_text(prop)]
    return []


def _js_class_fields(body_node) -> list[str]:
    """Extract field definitions from JS/TS class body."""
    if not body_node:
        return []
    fields: list[str] = []
    for child in body_node.children:
        if child.type in ("field_definition", "public_field_definition", "property_signature"):
            txt = _collapse_ws(_text(child)).rstrip(";")
            if len(txt) <= 80:
                fields.append(txt)
    return fields[:25]


def _js_extract_ts_interfaces_and_types(root) -> list[dict]:
    """Extract TypeScript interface_declaration and type_alias_declaration as schema classes."""
    schemas: list[dict] = []
    for child in root.children:
        node = child
        if child.type == "export_statement":
            decl = child.child_by_field_name("declaration")
            if decl:
                node = decl
        if node.type == "interface_declaration":
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if name_node:
                name = _text(name_node)
                fields = []
                if body_node:
                    for prop in body_node.children:
                        if prop.type in ("property_signature", "method_signature"):
                            fields.append(_collapse_ws(_text(prop)).rstrip(";,").strip())
                f_list = fields[:25]
                schemas.append({
                    "name": name,
                    "line_start": _line(node),
                    "line_end": _end_line(node),
                    "signature": f"interface {name}",
                    "bases": [],
                    "methods": [],
                    "fields": f_list,
                    "is_interface": True,
                    "interface_hash": _ast_hash(f"interface {name}|{'|'.join(f_list)}"),
                    "ast_hash": _ast_hash(_text(node)),
                })
    return schemas


def _js_direct_methods(body_node, lang: Language) -> list:
    """Return only direct method_definition / function children of a JS class body."""
    methods = []
    for child in body_node.children:
        if child.type == "method_definition":
            methods.append(child)
    return methods


def _js_named_exports(root, lang: Language) -> list[str]:
    exports = []
    for node in root.children:
        if node.type == "export_statement":
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name_node = spec.child_by_field_name("name") or spec.child_by_field_name("alias")
                            if not name_node:
                                for c in spec.children:
                                    if c.type == "identifier":
                                        name_node = c
                                        break
                            if name_node:
                                exports.append(_text(name_node))
                elif child.type == "identifier":
                    exports.append(_text(child))
    return exports


def _js_import_bindings(root) -> dict[str, dict]:
    """Extract JS/TS symbol-level import bindings (`import { a } from './mod'` and CommonJS `require('./mod')`)."""
    bindings: dict[str, dict] = {}
    for node in root.children:
        if node.type == "import_statement":
            src_node = node.child_by_field_name("source")
            if not src_node:
                continue
            mod = _text(src_node).strip("'\"")
            imp_line = _line(node)
            for child in node.children:
                if child.type == "import_clause":
                    for sub in child.children:
                        if sub.type == "identifier":
                            bindings[_text(sub)] = {"module": mod, "symbol": "default", "line": imp_line}
                        elif sub.type == "named_imports":
                            for spec in sub.children:
                                if spec.type == "import_specifier":
                                    name_n = spec.child_by_field_name("name")
                                    alias_n = spec.child_by_field_name("alias")
                                    if name_n:
                                        orig = _text(name_n)
                                        local = _text(alias_n) if alias_n else orig
                                        bindings[local] = {"module": mod, "symbol": orig, "line": _line(spec)}
                                    else:
                                        ids = [c for c in spec.children if c.type == "identifier"]
                                        if len(ids) == 1:
                                            sym = _text(ids[0])
                                            bindings[sym] = {"module": mod, "symbol": sym, "line": _line(spec)}
                                        elif len(ids) >= 2:
                                            bindings[_text(ids[-1])] = {"module": mod, "symbol": _text(ids[0]), "line": _line(spec)}
                        elif sub.type == "namespace_import":
                            for ns_c in sub.children:
                                if ns_c.type == "identifier":
                                    bindings[_text(ns_c)] = {"module": mod, "symbol": "*", "line": imp_line}
        elif node.type == "export_statement":
            src_node = node.child_by_field_name("source")
            if not src_node:
                continue
            mod = _text(src_node).strip("'\"")
            imp_line = _line(node)
            has_clause = False
            for child in node.children:
                if child.type == "export_clause":
                    has_clause = True
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name_n = spec.child_by_field_name("name")
                            alias_n = spec.child_by_field_name("alias")
                            if name_n:
                                orig = _text(name_n)
                                exported = _text(alias_n) if alias_n else orig
                                bindings[exported] = {"module": mod, "symbol": orig, "is_reexport": True, "line": _line(spec)}
                            else:
                                ids = [c for c in spec.children if c.type == "identifier"]
                                if len(ids) == 1:
                                    sym = _text(ids[0])
                                    bindings[sym] = {"module": mod, "symbol": sym, "is_reexport": True, "line": _line(spec)}
                                elif len(ids) >= 2:
                                    bindings[_text(ids[-1])] = {"module": mod, "symbol": _text(ids[0]), "is_reexport": True, "line": _line(spec)}
                elif child.type == "namespace_export":
                    has_clause = True
                    for ns_c in child.children:
                        if ns_c.type == "identifier":
                            bindings[_text(ns_c)] = {"module": mod, "symbol": "*", "is_reexport": True, "line": imp_line}
            if not has_clause and "*" in _text(node):
                bindings[f"__star__{mod}"] = {"module": mod, "symbol": "*", "is_reexport": True, "line": imp_line}
        elif node.type in ("lexical_declaration", "variable_declaration"):
            imp_line = _line(node)
            for sub in node.children:
                if sub.type != "variable_declarator":
                    continue
                name_n = sub.child_by_field_name("name")
                val_n = sub.child_by_field_name("value")
                if not (name_n and val_n):
                    continue
                val_txt = _collapse_ws(_text(val_n))
                m_req = re.match(r"^require\(\s*['\"]([^'\"]+)['\"]\s*\)(?:\.([A-Za-z_$][A-Za-z0-9_$]*))?$", val_txt)
                if not m_req:
                    continue
                mod, prop = m_req.group(1), m_req.group(2)
                if name_n.type == "identifier":
                    local = _text(name_n)
                    bindings[local] = {"module": mod, "symbol": prop or "*", "line": imp_line}
                elif name_n.type == "object_pattern":
                    for pat_c in name_n.children:
                        if pat_c.type == "shorthand_property_identifier_pattern":
                            sym = _text(pat_c)
                            bindings[sym] = {"module": mod, "symbol": sym, "line": _line(pat_c)}
                        elif pat_c.type == "pair_pattern":
                            k_n = pat_c.child_by_field_name("key")
                            v_n = pat_c.child_by_field_name("value")
                            if k_n and v_n and v_n.type == "identifier":
                                bindings[_text(v_n)] = {"module": mod, "symbol": _text(k_n), "line": _line(pat_c)}
    return bindings


def _parse_js_lhs_target(lhs_text: str, rhs_node) -> tuple[str | None, str | None, bool]:
    """Resolve a JS assignment LHS (e.g. `res.send`, `View.prototype.lookup`, `exports.foo`)
    into `(parent_class, fn_name, is_export)`."""
    t = lhs_text.strip()
    if not t or "[" in t or "(" in t:
        return None, None, False
    if t in ("module.exports", "exports"):
        if rhs_node.type in ("function_expression", "generator_function"):
            nm_n = rhs_node.child_by_field_name("name")
            if nm_n:
                return None, _text(nm_n), True
        return None, None, False
    if t.startswith("module.exports."):
        rem = t[len("module.exports."):]
        if "." not in rem:
            return None, rem, True
        obj_p, meth_p = rem.rsplit(".", 1)
        return obj_p.rsplit(".", 1)[-1], meth_p, True
    if t.startswith("exports."):
        rem = t[len("exports."):]
        if "." not in rem:
            return None, rem, True
        obj_p, meth_p = rem.rsplit(".", 1)
        return obj_p.rsplit(".", 1)[-1], meth_p, True
    if ".prototype." in t:
        cls_p, meth_p = t.rsplit(".prototype.", 1)
        if "." not in meth_p:
            return cls_p.rsplit(".", 1)[-1], meth_p, False
        return None, None, False
    if "." in t:
        obj_p, meth_p = t.rsplit(".", 1)
        if obj_p not in ("this", "self", "window", "global", "globalThis") and meth_p:
            return obj_p.rsplit(".", 1)[-1], meth_p, False
    return None, None, False


def _walk_js_ast(
    root,
    is_jsx: bool,
) -> tuple[
    list,
    list,
    set[str],
    set[str],
    list[tuple],
    list[int],
    list[tuple[int, str, int, str | None]],
]:
    """Single-pass AST walk for JS/TS/TSX avoiding 6 cold `tree_sitter.Query` compilations."""
    cls_nodes: list = []
    func_nodes: list = []
    import_mods: set[str] = set()
    export_names: set[str] = set()
    route_matches: list[tuple] = []
    call_events: list[tuple[int, str, int, str | None]] = []

    stack = [root]
    while stack:
        nd = stack.pop()
        nt = nd.type
        if nt == "class_declaration":
            if nd.child_by_field_name("name") is not None:
                cls_nodes.append(nd)
        elif nt == "function_declaration":
            nm = nd.child_by_field_name("name")
            if nm is not None and nm.type == "identifier":
                func_nodes.append(nd)
        elif nt == "method_definition":
            nm = nd.child_by_field_name("name")
            if nm is not None and nm.type == "property_identifier":
                func_nodes.append(nd)
        elif nt == "variable_declarator":
            nm = nd.child_by_field_name("name")
            val = nd.child_by_field_name("value")
            if nm is not None and nm.type == "identifier" and val is not None:
                vt = val.type
                if vt in ("arrow_function", "function_expression"):
                    func_nodes.append(nd)
                elif vt == "call_expression":
                    fn_c = val.child_by_field_name("function")
                    args_c = val.child_by_field_name("arguments")
                    if (
                        fn_c is not None
                        and fn_c.type in ("identifier", "member_expression")
                        and args_c is not None
                    ):
                        nc = args_c.named_children
                        if nc and nc[0].type in ("arrow_function", "function_expression"):
                            func_nodes.append(nd)
        elif nt == "import_statement":
            src_n = nd.child_by_field_name("source")
            if src_n is not None and src_n.type == "string":
                for sc in src_n.children:
                    if sc.type == "string_fragment":
                        import_mods.add(_text(sc))
        elif nt == "export_statement":
            src_n = nd.child_by_field_name("source")
            if src_n is not None and src_n.type == "string":
                for sc in src_n.children:
                    if sc.type == "string_fragment":
                        import_mods.add(_text(sc))
            decl = nd.child_by_field_name("declaration")
            if decl is not None:
                dt = decl.type
                if dt == "function_declaration":
                    nm = decl.child_by_field_name("name")
                    if nm is not None and nm.type == "identifier":
                        export_names.add(_text(nm))
                elif dt == "class_declaration":
                    nm = decl.child_by_field_name("name")
                    if nm is not None:
                        export_names.add(_text(nm))
                elif dt == "lexical_declaration":
                    for vd in decl.children:
                        if vd.type == "variable_declarator":
                            nm = vd.child_by_field_name("name")
                            if nm is not None and nm.type == "identifier":
                                export_names.add(_text(nm))
        elif nt == "call_expression":
            fn_c = nd.child_by_field_name("function")
            args_c = nd.child_by_field_name("arguments")
            if fn_c is not None:
                ft = fn_c.type
                if ft == "identifier":
                    call_events.append((fn_c.start_byte, _text(fn_c), _line(fn_c), None))
                    if args_c is not None:
                        for ac in args_c.children:
                            if ac.type == "string":
                                for sf in ac.children:
                                    if sf.type == "string_fragment":
                                        import_mods.add(_text(sf))
                elif ft == "import":
                    if args_c is not None:
                        for ac in args_c.children:
                            if ac.type == "string":
                                for sf in ac.children:
                                    if sf.type == "string_fragment":
                                        import_mods.add(_text(sf))
                elif ft == "member_expression":
                    obj_n = fn_c.child_by_field_name("object")
                    prop_n = fn_c.child_by_field_name("property")
                    if prop_n is not None and prop_n.type == "property_identifier":
                        p_txt = _text(prop_n)
                        o_txt = _text(obj_n) if obj_n is not None else None
                        call_events.append((prop_n.start_byte, p_txt, _line(prop_n), o_txt))
                        if p_txt in _HTTP and args_c is not None and obj_n is not None:
                            for ac in args_c.children:
                                if ac.type == "string":
                                    for sf in ac.children:
                                        if sf.type == "string_fragment":
                                            route_matches.append((prop_n, sf, obj_n, args_c, nd))
                                            break
                                    break
        elif is_jsx and nt in ("jsx_self_closing_element", "jsx_opening_element"):
            nm = nd.child_by_field_name("name")
            if nm is not None and nm.type == "identifier":
                c_txt = _text(nm)
                if c_txt[:1].isupper():
                    call_events.append((nm.start_byte, c_txt, _line(nm), None))

        ch = nd.children
        if ch:
            stack.extend(reversed(ch))

    call_events.sort(key=lambda x: x[0])
    return (
        cls_nodes,
        func_nodes,
        import_mods,
        export_names,
        route_matches,
        [ev[0] for ev in call_events],
        call_events,
    )


def _parse_js(root, lang: Language) -> dict:
    functions, classes, imports, exports, routes = [], [], [], [], []
    method_to_class: dict[tuple, str] = {}
    js_consts, leading_docs = _js_const_map_and_leading_docs(root)
    root_span = max(1, _end_line(root) - _line(root))

    (
        cls_nodes,
        func_nodes,
        import_mods,
        export_names,
        route_matches,
        js_call_starts,
        js_call_events,
    ) = _walk_js_ast(root, is_jsx=(lang in (_TSX, _JS)))

    def _extract_js_callees(fn_node) -> tuple[list[str], dict[str, int], dict[str, list[int]], dict[str, int]]:
        return _slice_call_events(js_call_starts, js_call_events, fn_node)

    # Phase 1 — classes first
    for cls_node in cls_nodes:
        name_node = cls_node.child_by_field_name("name")
        if not name_node:
            continue
        cls_name = _text(name_node)
        bases = _js_bases(cls_node)

        body_node = cls_node.child_by_field_name("body")
        method_names = []
        if body_node:
            for meth_node in _js_direct_methods(body_node, lang):
                meth_name_node = meth_node.child_by_field_name("name")
                if meth_name_node:
                    method_names.append(_text(meth_name_node))
                method_to_class[meth_node.start_point] = cls_name

        fields = _js_class_fields(body_node)
        ext_str = f" extends {', '.join(bases)}" if bases else ""
        cls_sig = f"class {cls_name}{ext_str}"
        classes.append({
            "name": cls_name,
            "line_start": _line(cls_node),
            "line_end": _end_line(cls_node),
            "signature": cls_sig,
            "bases": bases,
            "methods": method_names,
            "fields": fields,
            "interface_hash": _ast_hash(f"{cls_sig}|{'|'.join(method_names)}|{'|'.join(fields)}"),
            "ast_hash": _ast_hash(_text(cls_node)),
        })

    existing_cls_names = {c["name"] for c in classes}
    for iface in _js_extract_ts_interfaces_and_types(root):
        if iface["name"] not in existing_cls_names:
            classes.append(iface)

    # Phase 2 — standard declarations, arrow declarators, and class methods
    seen_fn_keys: set[tuple[str | None, str, int]] = set()
    for func_node in func_nodes:
        name_node = func_node.child_by_field_name("name")
        if not name_node:
            continue
        fn_name = _text(name_node)
        is_nested_fn = False
        anc = func_node.parent
        while anc is not None and anc != root:
            if anc.type in ("function_declaration", "function_expression", "arrow_function", "method_definition"):
                if (_end_line(anc) - _line(anc)) <= root_span * 0.8:
                    is_nested_fn = True
                    break
            anc = anc.parent
        calls, call_sites, call_sites_all, qualified_calls = _extract_js_callees(func_node)
        sig, params, ret_type = _js_func_signature(func_node, fn_name)
        l_start = _line(func_node)
        doc = leading_docs.get(l_start)
        parent_cls = method_to_class.get(func_node.start_point)
        is_meth = parent_cls is not None
        seen_fn_keys.add((parent_cls, fn_name, l_start))
        fn_body_raw = _text(func_node)
        param_types = _extract_param_and_local_types(params, fn_body_raw) if qualified_calls else {}
        ret_keys = (
            _extract_closed_return_keys(func_node, is_python=False)
            if ("{" in fn_body_raw and ("return" in fn_body_raw or "=>" in fn_body_raw))
            else None
        )
        iface_str = f"{sig}|ret:{','.join(ret_keys)}" if ret_keys else sig
        entry = {
            "name": fn_name,
            "line_start": l_start,
            "line_end": _end_line(func_node),
            "signature": sig,
            "params": params,
            "return_type": ret_type,
            "arity_spec": extract_arity_spec(params, is_method=is_meth, is_python=False),
            "param_types": param_types,
            "docstring": doc,
            "body_tokens": _extract_body_tokens(func_node, js_consts, raw_full_text=fn_body_raw),
            "calls": calls,
            "call_sites": call_sites,
            "call_sites_all": call_sites_all,
            "qualified_calls": qualified_calls,
            "interface_hash": _ast_hash(iface_str),
            "ast_hash": _ast_hash(fn_body_raw),
        }
        if ret_keys:
            entry["return_keys"] = ret_keys
        if is_meth:
            entry["is_method"] = True
            entry["parent_class"] = parent_cls
        if is_nested_fn:
            entry["is_nested"] = True
        functions.append(entry)

    # Phase 2b — CommonJS / prototype / object method assignments (`res.send = function...`,
    # `exports.foo = ...`, `View.prototype.lookup = ...`) and `defineGetter(obj, 'prop', fn)`
    assigned_exports: list[str] = []
    proto_methods_by_cls: dict[str, list[str]] = {}
    for stmt in root.children:
        expr = stmt.children[0] if (stmt.type == "expression_statement" and stmt.children) else stmt
        if expr.type == "assignment_expression":
            lhs_list = []
            curr = expr
            while curr is not None and curr.type == "assignment_expression":
                l_n = curr.child_by_field_name("left")
                if l_n is not None:
                    lhs_list.append(l_n)
                curr = curr.child_by_field_name("right")
            rhs_n = curr
            if rhs_n is None:
                continue
            if rhs_n.type == "identifier":
                for l_n in lhs_list:
                    lt = _collapse_ws(_text(l_n))
                    if lt in ("module.exports", "exports"):
                        assigned_exports.append(_text(rhs_n))
                    elif lt.startswith(("module.exports.", "exports.")):
                        assigned_exports.append(lt.split(".", 2)[-1])
                continue
            if rhs_n.type not in ("function_expression", "arrow_function", "generator_function"):
                continue
            l_start = _line(stmt)
            l_end = _end_line(stmt)
            doc = leading_docs.get(l_start)
            calls, call_sites, call_sites_all, qualified_calls = _extract_js_callees(rhs_n)
            body_toks = _extract_body_tokens(rhs_n, js_consts)
            ast_h = _ast_hash(_text(rhs_n))
            for l_n in lhs_list:
                p_cls, f_nm, is_exp = _parse_js_lhs_target(_collapse_ws(_text(l_n)), rhs_n)
                if not f_nm:
                    continue
                if is_exp:
                    assigned_exports.append(f_nm)
                if (p_cls, f_nm, l_start) in seen_fn_keys:
                    continue
                seen_fn_keys.add((p_cls, f_nm, l_start))
                q_display = f"{p_cls}.{f_nm}" if p_cls else f_nm
                sig, params, ret_type = _js_func_signature(rhs_n, q_display)
                is_meth = p_cls is not None
                if p_cls:
                    proto_methods_by_cls.setdefault(p_cls, []).append(f_nm)
                entry = {
                    "name": f_nm,
                    "line_start": l_start,
                    "line_end": l_end,
                    "signature": sig,
                    "params": params,
                    "return_type": ret_type,
                    "arity_spec": extract_arity_spec(params, is_method=is_meth, is_python=False),
                    "docstring": doc,
                    "body_tokens": body_toks,
                    "calls": calls,
                    "call_sites": call_sites,
                    "call_sites_all": call_sites_all,
                    "qualified_calls": qualified_calls,
                    "interface_hash": _ast_hash(sig),
                    "ast_hash": ast_h,
                }
                if is_meth:
                    entry["is_method"] = True
                    entry["parent_class"] = p_cls
                functions.append(entry)
        elif expr.type == "call_expression":
            fn_n = expr.child_by_field_name("function")
            args_n = expr.child_by_field_name("arguments")
            if not (fn_n and args_n):
                continue
            fn_callee = _collapse_ws(_text(fn_n))
            if fn_callee not in ("defineGetter", "Object.defineProperty"):
                continue
            arg_items = [c for c in args_n.children if c.type not in ("(", ")", ",")]
            if len(arg_items) < 3:
                continue
            obj_arg, prop_arg, val_arg = arg_items[0], arg_items[1], arg_items[2]
            if obj_arg.type not in ("identifier", "member_expression") or prop_arg.type != "string":
                continue
            p_cls = _text(obj_arg).rsplit(".", 1)[-1]
            f_nm = _text(prop_arg).strip("'\"`")
            target_fn = val_arg if val_arg.type in ("function_expression", "arrow_function") else None
            if target_fn is None and val_arg.type == "object":
                for sub_c in val_arg.children:
                    if sub_c.type == "pair":
                        v_c = sub_c.child_by_field_name("value")
                        if v_c and v_c.type in ("function_expression", "arrow_function"):
                            target_fn = v_c
                            break
                    elif sub_c.type == "method_definition":
                        target_fn = sub_c
                        break
            if target_fn is None or not f_nm:
                continue
            l_start = _line(stmt)
            l_end = _end_line(stmt)
            if (p_cls, f_nm, l_start) in seen_fn_keys:
                continue
            seen_fn_keys.add((p_cls, f_nm, l_start))
            doc = leading_docs.get(l_start)
            calls, call_sites, call_sites_all, qualified_calls = _extract_js_callees(target_fn)
            sig, params, ret_type = _js_func_signature(target_fn, f"{p_cls}.{f_nm}")
            proto_methods_by_cls.setdefault(p_cls, []).append(f_nm)
            functions.append({
                "name": f_nm,
                "line_start": l_start,
                "line_end": l_end,
                "signature": sig,
                "params": params,
                "return_type": ret_type,
                "arity_spec": extract_arity_spec(params, is_method=True, is_python=False),
                "docstring": doc,
                "body_tokens": _extract_body_tokens(target_fn, js_consts),
                "calls": calls,
                "call_sites": call_sites,
                "call_sites_all": call_sites_all,
                "qualified_calls": qualified_calls,
                "interface_hash": _ast_hash(sig),
                "ast_hash": _ast_hash(_text(target_fn)),
                "is_method": True,
                "parent_class": p_cls,
            })

    for cls_obj in classes:
        extra_m = proto_methods_by_cls.get(cls_obj["name"], [])
        for em in extra_m:
            if em not in cls_obj["methods"]:
                cls_obj["methods"].append(em)

    imports = list(import_mods)
    import_bindings = _js_import_bindings(root)

    exports = list(export_names)
    exports.extend(_js_named_exports(root, lang))
    exports.extend(assigned_exports)
    exports = list(set(exports))

    for m_node, p_node, o_node, args_node, rc_node in route_matches:
        method = _text(m_node)
        if method not in _HTTP:
            continue
        path = _text(p_node)
        receiver = _text(o_node) if o_node else ""
        if not _is_express_route(receiver, path):
            continue

        handler_name = None
        if args_node is not None:
            arg_children = [
                c for c in args_node.children
                if c.type not in ("(", ")", ",", "string")
            ]
            if arg_children:
                last_arg = arg_children[-1]
                if last_arg.type == "identifier":
                    handler_name = _text(last_arg)
                elif last_arg.type in ("arrow_function", "function_expression"):
                    r_calls, r_sites, _, r_qual = _extract_js_callees(last_arg)
                    if r_calls:
                        clean_p = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_") or "root"
                        syn_name = f"route_{method.upper()}_{clean_p}"
                        handler_name = syn_name
                        syn_sig = f"{receiver}.{method}('{path}', (req, res) => ...)"
                        functions.append({
                            "name": syn_name,
                            "line_start": _line(rc_node),
                            "line_end": _end_line(rc_node),
                            "signature": syn_sig,
                            "params": "(req, res)",
                            "return_type": "",
                            "arity_spec": {"min_args": 0, "max_args": 3, "has_varargs": False, "has_kwargs": False, "kwonly_required": []},
                            "docstring": f"Express {method.upper()} {path} route handler",
                            "body_tokens": _extract_body_tokens(last_arg, js_consts),
                            "calls": r_calls,
                            "call_sites": r_sites,
                            "qualified_calls": r_qual,
                            "interface_hash": _ast_hash(syn_sig),
                            "ast_hash": _ast_hash(_text(last_arg)),
                        })

        route_dict = {
            "method": method.upper(),
            "path": path,
            "line": _line(m_node),
            "framework": "express",
        }
        if handler_name:
            route_dict["handler"] = handler_name
        routes.append(route_dict)

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": import_bindings,
        "exports": exports,
        "routes": routes,
        "const_aliases": _extract_const_aliases(js_consts),
    }


# ── SQL schema & function parser ────────────────────────────────────────────

def extract_sql_table_schemas(source: str) -> dict[str, dict]:
    """Extract CREATE TABLE definitions (columns, NOT NULL without DEFAULT) from SQL or inline strings."""
    schemas: dict[str, dict] = {}
    if "CREATE" not in source.upper():
        return schemas
    for m in re.finditer(
        r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[a-zA-Z_]\w*\.)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s*\(",
        source,
        re.IGNORECASE,
    ):
        tbl_name = m.group(1)
        l_start = source.count("\n", 0, m.start()) + 1
        open_pos = m.end() - 1
        depth = 0
        close_pos = len(source)
        in_str: str | None = None
        for idx in range(open_pos, min(len(source), open_pos + 15000)):
            ch = source[idx]
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in ("'", '"'):
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_pos = idx
                    break
        l_end = source.count("\n", 0, close_pos) + 1
        body = source[open_pos + 1 : close_pos]
        clean_lines = [re.sub(r"--.*$", "", ln) for ln in body.splitlines()]
        clean_body = "\n".join(clean_lines)
        clauses: list[str] = []
        cur: list[str] = []
        p_depth = 0
        q_char: str | None = None
        for ch in clean_body:
            if q_char:
                cur.append(ch)
                if ch == q_char:
                    q_char = None
                continue
            if ch in ("'", '"'):
                q_char = ch
                cur.append(ch)
            elif ch == "(":
                p_depth += 1
                cur.append(ch)
            elif ch == ")":
                p_depth = max(0, p_depth - 1)
                cur.append(ch)
            elif ch == "," and p_depth == 0:
                clauses.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            clauses.append("".join(cur).strip())

        all_cols: list[str] = []
        required_cols: list[str] = []
        defaulted_cols: list[str] = []
        fields: list[str] = []
        for cl in clauses:
            if not cl or re.match(
                r"^(?:PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE\b|CONSTRAINT\b|CHECK\b|INDEX\b|KEY\b)",
                cl,
                re.IGNORECASE,
            ):
                continue
            m_col = re.match(r"^[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+([A-Za-z][A-Za-z0-9_()]*)", cl)
            if not m_col:
                continue
            c_name = m_col.group(1)
            c_type = m_col.group(2)
            if c_name.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"):
                continue
            is_not_null = bool(re.search(r"\bNOT\s+NULL\b", cl, re.IGNORECASE))
            has_default = bool(
                re.search(
                    r"\b(?:DEFAULT\b|SERIAL\b|BIGSERIAL\b|SMALLSERIAL\b|AUTOINCREMENT\b|AUTO_INCREMENT\b|GENERATED\b)",
                    cl,
                    re.IGNORECASE,
                )
            )
            is_int_pk = bool(re.search(r"\bINTEGER\s+PRIMARY\s+KEY\b", cl, re.IGNORECASE))
            all_cols.append(c_name)
            suffix = (" NOT NULL" if is_not_null else "") + (" DEFAULT" if has_default else "")
            fields.append(f"{c_name}: {c_type}{suffix}")
            if is_not_null and not has_default and not is_int_pk:
                required_cols.append(c_name)
            elif is_not_null and has_default:
                defaulted_cols.append(c_name)

        if all_cols:
            schemas[tbl_name] = {
                "name": tbl_name,
                "line_start": l_start,
                "line_end": l_end,
                "all_cols": all_cols,
                "required_cols": required_cols,
                "defaulted_cols": defaulted_cols,
                "fields": fields[:25],
            }
    return schemas


def extract_sql_schema_alters(source: str) -> dict[str, list[dict]]:
    """Extract standard SQL ALTER TABLE schema evolution operations (DROP/SET NOT NULL, SET DEFAULT, ADD/DROP COLUMN)."""
    alters: dict[str, list[dict]] = {}
    if "ALTER" not in source.upper():
        return alters
    clean_src = "\n".join(re.sub(r"--.*$", "", ln) for ln in source.splitlines())
    for m in re.finditer(
        r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:[a-zA-Z_]\w*\.)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+([\s\S]+?);",
        clean_src,
        re.IGNORECASE,
    ):
        tbl = m.group(1)
        body = m.group(2)
        ops: list[dict] = []
        for m_dn in re.finditer(
            r"\bALTER\s+(?:COLUMN\s+)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+DROP\s+NOT\s+NULL\b",
            body,
            re.IGNORECASE,
        ):
            ops.append({"op": "drop_not_null", "col": m_dn.group(1)})
        for m_sn in re.finditer(
            r"\bALTER\s+(?:COLUMN\s+)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+SET\s+NOT\s+NULL\b",
            body,
            re.IGNORECASE,
        ):
            ops.append({"op": "set_not_null", "col": m_sn.group(1)})
        for m_sd in re.finditer(
            r"\bALTER\s+(?:COLUMN\s+)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+SET\s+DEFAULT\b",
            body,
            re.IGNORECASE,
        ):
            ops.append({"op": "set_default", "col": m_sd.group(1)})
        for m_dc in re.finditer(
            r"\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\b",
            body,
            re.IGNORECASE,
        ):
            col_d = m_dc.group(1)
            if col_d.upper() not in ("NOT", "DEFAULT", "CONSTRAINT", "INDEX", "KEY"):
                ops.append({"op": "drop_col", "col": col_d})
        for m_ac in re.finditer(
            r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s+([A-Za-z][A-Za-z0-9_()]*)(.*?)(?:,|$)",
            body,
            re.IGNORECASE | re.DOTALL,
        ):
            col_a = m_ac.group(1)
            if col_a.upper() in ("CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "INDEX"):
                continue
            rest_a = m_ac.group(3) or ""
            is_nn = bool(re.search(r"\bNOT\s+NULL\b", rest_a, re.IGNORECASE))
            has_def = bool(re.search(r"\b(?:DEFAULT\b|SERIAL\b|GENERATED\b|AUTOINCREMENT\b)", rest_a, re.IGNORECASE))
            ops.append({"op": "add_col", "col": col_a, "required": is_nn and not has_def})
        if ops:
            alters.setdefault(tbl, []).extend(ops)
    return alters


def apply_sql_alters_to_schema(sc: dict, ops: list[dict]) -> None:
    """Mutate a table schema dict in-place according to chronological ALTER TABLE operations."""
    all_c: list[str] = list(sc.get("all_cols", []))
    req_c: list[str] = list(sc.get("required_cols", []))
    def_c: list[str] = list(sc.get("defaulted_cols", []))
    for item in ops:
        op = item.get("op")
        col = item.get("col", "")
        col_low = col.lower()
        if op in ("drop_not_null", "set_default"):
            req_c = [c for c in req_c if c.lower() != col_low]
            if op == "set_default" and all(c.lower() != col_low for c in def_c):
                def_c.append(col)
        elif op == "set_not_null":
            if all(c.lower() != col_low for c in def_c) and all(c.lower() != col_low for c in req_c):
                req_c.append(col)
        elif op == "drop_col":
            all_c = [c for c in all_c if c.lower() != col_low]
            req_c = [c for c in req_c if c.lower() != col_low]
            def_c = [c for c in def_c if c.lower() != col_low]
        elif op == "add_col":
            if all(c.lower() != col_low for c in all_c):
                all_c.append(col)
            if item.get("required") and all(c.lower() != col_low for c in req_c):
                req_c.append(col)
    sc["all_cols"] = all_c
    sc["required_cols"] = req_c
    sc["defaulted_cols"] = def_c


def _parse_sql(source: str) -> dict:
    schemas = extract_sql_table_schemas(source)
    alters = extract_sql_schema_alters(source)
    for tbl_k, ops in alters.items():
        for sc_k, sc_obj in schemas.items():
            if sc_k.lower() == tbl_k.lower():
                apply_sql_alters_to_schema(sc_obj, ops)
    classes: list[dict] = []
    functions: list[dict] = []
    for tbl_name, sc in schemas.items():
        f_hash = _ast_hash(",".join(sc["fields"]))
        classes.append({
            "name": tbl_name,
            "line_start": sc["line_start"],
            "line_end": sc["line_end"],
            "signature": f"CREATE TABLE {tbl_name} ({', '.join(sc['all_cols'][:8])})",
            "bases": [],
            "methods": [],
            "fields": sc["fields"],
            "required_cols": sc["required_cols"],
            "defaulted_cols": sc["defaulted_cols"],
            "is_sql_table": True,
            "interface_hash": f_hash,
            "ast_hash": f_hash,
        })
    for m_fn in re.finditer(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:[a-zA-Z_]\w*\.)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)",
        source,
        re.IGNORECASE,
    ):
        fn_nm = m_fn.group(1)
        params = _collapse_ws(m_fn.group(2))
        l_start = source.count("\n", 0, m_fn.start()) + 1
        end_m = re.search(r"\$\$\s*;|END\s*;", source[m_fn.end() :], re.IGNORECASE)
        end_pos = (m_fn.end() + end_m.end()) if end_m else min(len(source), m_fn.end() + 1500)
        l_end = source.count("\n", 0, end_pos) + 1
        body_slice = source[m_fn.start() : end_pos]
        ref_tables = [t for t in schemas if re.search(rf"\b{re.escape(t)}\b", body_slice, re.IGNORECASE)]
        sig_sql = f"CREATE FUNCTION {fn_nm}({params})"
        functions.append({
            "name": fn_nm,
            "line_start": l_start,
            "line_end": l_end,
            "signature": sig_sql,
            "params": f"({params})",
            "return_type": "",
            "arity_spec": extract_arity_spec(f"({params})", is_method=False, is_python=False),
            "docstring": "SQL stored function",
            "body_tokens": [w.lower() for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", body_slice)][:60],
            "calls": ref_tables,
            "call_sites": {},
            "qualified_calls": {},
            "interface_hash": _ast_hash(sig_sql),
            "ast_hash": _ast_hash(body_slice),
        })
    res = {
        "functions": functions,
        "classes": classes,
        "imports": [],
        "import_bindings": {},
        "exports": list(schemas.keys()),
        "routes": [],
        "sql_schemas": schemas,
        "sql_alters": alters,
        "parse_status": "ok",
    }
    res["interface_hash"] = compute_file_interface_hash(res)
    return res


def compute_file_interface_hash(info: dict) -> str:
    """Compute a Salsa-style interface hash over exported signatures, classes, imports, routes, and schemas.

    When a file's body edits do not alter `interface_hash` (nor its outgoing cross-file calls/state channels),
    incremental indexing can short-circuit global import and graph resolution.
    """
    parts: list[str] = []
    for fn_part in sorted(
        f"fn:{fn.get('parent_class', '')}.{fn.get('name', '')}:{fn.get('interface_hash', fn.get('signature', ''))}"
        for fn in info.get("functions", [])
    ):
        parts.append(fn_part)
    for cls_part in sorted(
        f"cls:{cls.get('name', '')}:{cls.get('interface_hash', cls.get('signature', ''))}"
        for cls in info.get("classes", [])
    ):
        parts.append(cls_part)
    for imp in sorted(info.get("imports", [])):
        parts.append(f"imp:{imp}")
    for exp in sorted(info.get("exports", [])):
        parts.append(f"exp:{exp}")
    for rt_part in sorted(
        f"rt:{rt.get('method', '')}:{rt.get('full_path', rt.get('path', ''))}:{rt.get('handler', '')}"
        for rt in info.get("routes", [])
    ):
        parts.append(rt_part)
    for tbl in sorted((info.get("sql_schemas") or {}).keys()):
        sc = info["sql_schemas"][tbl]
        parts.append(f"sql:{tbl}:{','.join(sc.get('required_cols', []))}:{','.join(sc.get('all_cols', []))}")
    for alt_t in sorted((info.get("sql_alters") or {}).keys()):
        parts.append(f"alt:{alt_t}:{len(info['sql_alters'][alt_t])}")
    for ak in sorted((info.get("const_aliases") or {}).keys()):
        parts.append(f"al:{ak}:{','.join(info['const_aliases'][ak])}")
    return _ast_hash("\n".join(parts))



# ── Polyglot Tree-sitter CST Codecs (Go, Rust, Java) ────────────────────────

def _walk_cst(node):
    """Iterative pre-order CST generator."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        for ch in reversed(cur.children):
            stack.append(ch)


def _extract_polyglot_calls(body_node) -> tuple[list[str], dict[str, int], dict[str, list[int]], list[dict], list[str]]:
    """Extract calls, call_sites, call_sites_all, qualified_calls, and body_tokens from a Go/Rust/Java body."""
    calls_seen: list[str] = []
    calls_set: set[str] = set()
    call_sites: dict[str, int] = {}
    call_sites_all: dict[str, list[int]] = {}
    qualified_calls: dict[str, int] = {}
    body_tokens: set[str] = set()

    if body_node is None:
        return [], {}, {}, {}, []

    for nd in _walk_cst(body_node):
        nt = nd.type
        if nt in ("identifier", "field_identifier", "type_identifier", "property_identifier"):
            tok = _text(nd).strip().lower()
            if len(tok) >= 3 and tok.isidentifier():
                body_tokens.add(tok)
        elif nt in ("call_expression", "method_invocation", "macro_invocation"):
            ln = _line(nd)
            callee_txt = ""
            if nt == "macro_invocation":
                m_nd = nd.child_by_field_name("macro") or (nd.children[0] if nd.children else None)
                callee_txt = _text(m_nd).strip() if m_nd else ""
            elif nt == "method_invocation":
                obj_nd = nd.child_by_field_name("object")
                name_nd = nd.child_by_field_name("name")
                if name_nd:
                    m_name = _text(name_nd).strip()
                    callee_txt = f"{_text(obj_nd).strip()}.{m_name}" if obj_nd else m_name
            else:
                fn_nd = nd.child_by_field_name("function") or (nd.children[0] if nd.children else None)
                callee_txt = _text(fn_nd).strip() if fn_nd else ""

            if not callee_txt:
                continue
            callee_clean = re.sub(r"::<.*>$", "", callee_txt).replace("::", ".")
            parts = [p.strip() for p in callee_clean.split(".") if p.strip()]
            if not parts:
                continue
            bare = parts[-1]
            if not re.match(r"^[A-Za-z_]\w*$", bare):
                continue
            if bare not in calls_set:
                calls_set.add(bare)
                calls_seen.append(bare)
            if bare not in call_sites or ln < call_sites[bare]:
                call_sites[bare] = ln
            call_sites_all.setdefault(bare, []).append(ln)
            if len(parts) >= 2:
                recv = parts[-2]
                if re.match(r"^[A-Za-z_]\w*$", recv):
                    q_key = f"{recv}.{bare}"
                    if q_key not in qualified_calls or ln < qualified_calls[q_key]:
                        qualified_calls[q_key] = ln

    return calls_seen, call_sites, call_sites_all, qualified_calls, _extract_body_tokens(body_node)


def _leading_comment_doc(lines: list[str], start_line: int) -> str | None:
    """Extract GoDoc (`//`), RustDoc (`///`), or Javadoc (`/** ... */`) immediately preceding `start_line`."""
    doc_lines: list[str] = []
    idx = start_line - 2
    in_block = False
    while idx >= 0 and len(doc_lines) < 8:
        s = lines[idx].strip()
        if not s:
            break
        if s.startswith(("#[", "@")):
            idx -= 1
            continue
        if s.endswith("*/"):
            in_block = True
            cleaned = s.removesuffix("*/").lstrip("/* \t").strip()
            if cleaned:
                doc_lines.append(cleaned)
            if s.startswith("/*"):
                in_block = False
            idx -= 1
            continue
        if in_block:
            if s.startswith("/*"):
                in_block = False
            cleaned = s.lstrip("/* \t").strip()
            if cleaned:
                doc_lines.append(cleaned)
            idx -= 1
            continue
        if s.startswith("//"):
            cleaned = s.lstrip("/ \t").strip()
            if cleaned:
                doc_lines.append(cleaned)
            idx -= 1
            continue
        break
    if not doc_lines:
        return None
    doc_lines.reverse()
    return _collapse_ws(" ".join(doc_lines))[:240]


def _parse_go(root, source: str) -> dict:
    functions: list[dict] = []
    classes: list[dict] = []
    imports: list[str] = []
    routes: list[dict] = []
    methods_by_struct: dict[str, list[str]] = {}
    lines = source.splitlines()
    for nd in _walk_cst(root):
        nt = nd.type
        if nt == "import_spec":
            p_nd = nd.child_by_field_name("path") or nd
            imp_str = _text(p_nd).strip().strip('"').strip("`")
            if imp_str and imp_str not in imports:
                imports.append(imp_str)
        elif nt == "type_spec":
            name_nd = nd.child_by_field_name("name")
            type_nd = nd.child_by_field_name("type")
            if name_nd and type_nd and type_nd.type in ("struct_type", "interface_type"):
                c_name = _text(name_nd).strip()
                kind_lbl = "interface" if type_nd.type == "interface_type" else "struct"
                sig = f"type {c_name} {kind_lbl}"
                c_txt = _text(nd)
                classes.append({
                    "name": c_name,
                    "bases": [],
                    "methods": [],
                    "fields": [],
                    "signature": sig,
                    "docstring": _leading_comment_doc(lines, _line(nd.parent) if nd.parent and nd.parent.type == "type_declaration" else _line(nd)),
                    "line_start": _line(nd),
                    "line_end": _end_line(nd),
                    "ast_hash": _ast_hash(c_txt),
                    "interface_hash": _ast_hash(sig),
                })
        elif nt in ("function_declaration", "method_declaration"):
            name_nd = nd.child_by_field_name("name")
            if not name_nd:
                continue
            fn_name = _text(name_nd).strip()
            params_nd = nd.child_by_field_name("parameters")
            res_nd = nd.child_by_field_name("result")
            body_nd = nd.child_by_field_name("body")
            params_str = _collapse_ws(_text(params_nd)) if params_nd else "()"
            ret_str = _collapse_ws(_text(res_nd)) if res_nd else ""
            parent_cls: str | None = None
            if nt == "method_declaration":
                recv_nd = nd.child_by_field_name("receiver")
                if recv_nd:
                    recv_txt = _text(recv_nd)
                    m_recv = re.search(r"\*?\s*([A-Za-z_]\w*)\s*\)", recv_txt)
                    if m_recv:
                        parent_cls = m_recv.group(1)
                        methods_by_struct.setdefault(parent_cls, []).append(fn_name)
            sig = f"func ({parent_cls}) {fn_name}{params_str}" if parent_cls else f"func {fn_name}{params_str}"
            if ret_str:
                sig = f"{sig} {ret_str}"
            calls, c_sites, c_sites_all, q_calls, b_toks = _extract_polyglot_calls(body_nd)
            arity = extract_arity_spec(params_str, is_method=False, is_python=False)
            functions.append({
                "name": fn_name,
                "parent_class": parent_cls,
                "line_start": _line(nd),
                "line_end": _end_line(nd),
                "signature": sig,
                "params": params_str,
                "return_type": ret_str or None,
                "decorators": [],
                "docstring": _leading_comment_doc(lines, _line(nd)),
                "calls": calls,
                "call_sites": c_sites,
                "call_sites_all": c_sites_all,
                "qualified_calls": q_calls,
                "body_tokens": b_toks,
                "arity_spec": arity,
                "ast_hash": _ast_hash(_text(nd)),
                "interface_hash": _ast_hash(sig),
            })

    for cls in classes:
        if cls["name"] in methods_by_struct:
            cls["methods"] = methods_by_struct[cls["name"]]
            cls["interface_hash"] = _ast_hash(f"{cls['signature']}|{','.join(sorted(cls['methods']))}")

    # Extract Go Gin / Echo / Chi / Fiber / net/http routes
    for idx, line in enumerate(source.splitlines(), start=1):
        m_rt = re.search(
            r'\b(?:GET|POST|PUT|DELETE|PATCH|HandleFunc|Get|Post|Put|Delete)\s*\(\s*"([^"]+)"\s*,\s*([A-Za-z_][\w.]*)',
            line,
        )
        if m_rt:
            verb_m = re.search(r"\.(GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|HandleFunc)\s*\(", line)
            verb = verb_m.group(1).upper() if verb_m else "ANY"
            if verb == "HANDLEFUNC":
                verb = "ANY"
            path_str = m_rt.group(1)
            handler_str = m_rt.group(2).split(".")[-1]
            routes.append({"method": verb, "path": path_str, "handler": handler_str, "line": idx})

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": {},
        "exports": [f["name"] for f in functions if f["name"][:1].isupper()],
        "routes": routes,
    }


def _parse_rust(root, source: str) -> dict:
    functions: list[dict] = []
    classes: list[dict] = []
    imports: list[str] = []
    routes: list[dict] = []
    methods_by_type: dict[str, list[str]] = {}
    lines = source.splitlines()

    for nd in _walk_cst(root):
        nt = nd.type
        if nt == "use_declaration":
            u_txt = _collapse_ws(_text(nd)).removeprefix("pub ").removeprefix("use ").rstrip(";").strip()
            if u_txt and u_txt not in imports:
                imports.append(u_txt)
        elif nt in ("struct_item", "enum_item", "trait_item"):
            name_nd = nd.child_by_field_name("name")
            if not name_nd:
                continue
            c_name = _text(name_nd).strip()
            kind_lbl = nt.split("_")[0]
            sig = f"{kind_lbl} {c_name}"
            classes.append({
                "name": c_name,
                "bases": [],
                "methods": [],
                "fields": [],
                "signature": sig,
                "docstring": _leading_comment_doc(lines, _line(nd)),
                "line_start": _line(nd),
                "line_end": _end_line(nd),
                "ast_hash": _ast_hash(_text(nd)),
                "interface_hash": _ast_hash(sig),
            })
        elif nt == "function_item":
            name_nd = nd.child_by_field_name("name")
            if not name_nd:
                continue
            fn_name = _text(name_nd).strip()
            params_nd = nd.child_by_field_name("parameters")
            ret_nd = nd.child_by_field_name("return_type")
            body_nd = nd.child_by_field_name("body")
            params_str = _collapse_ws(_text(params_nd)) if params_nd else "()"
            ret_str = _collapse_ws(_text(ret_nd)).removeprefix("->").strip() if ret_nd else ""
            parent_cls: str | None = None
            p_cur = nd.parent
            while p_cur is not None:
                if p_cur.type == "impl_item":
                    t_nd = p_cur.child_by_field_name("type")
                    if t_nd:
                        parent_cls = re.sub(r"<.*>$", "", _text(t_nd).strip())
                        methods_by_type.setdefault(parent_cls, []).append(fn_name)
                    break
                p_cur = p_cur.parent
            sig = f"fn {parent_cls}::{fn_name}{params_str}" if parent_cls else f"fn {fn_name}{params_str}"
            if ret_str:
                sig = f"{sig} -> {ret_str}"
            calls, c_sites, c_sites_all, q_calls, b_toks = _extract_polyglot_calls(body_nd)
            is_meth = bool(parent_cls and re.search(r"\bself\b", params_str))
            arity = extract_arity_spec(params_str, is_method=is_meth, is_python=False)
            l_start = _line(nd)
            # Check preceding lines for Actix #[get("/path")] / #[post("/path")] attributes
            for attr_ln in range(max(1, l_start - 3), l_start):
                attr_txt = lines[attr_ln - 1].strip()
                m_actix = re.match(r'^#\[(get|post|put|delete|patch)\(\s*"([^"]+)"', attr_txt, re.IGNORECASE)
                if m_actix:
                    routes.append({
                        "method": m_actix.group(1).upper(),
                        "path": m_actix.group(2),
                        "handler": fn_name,
                        "line": l_start,
                    })
            functions.append({
                "name": fn_name,
                "parent_class": parent_cls,
                "line_start": l_start,
                "line_end": _end_line(nd),
                "signature": sig,
                "params": params_str,
                "return_type": ret_str or None,
                "decorators": [],
                "docstring": _leading_comment_doc(lines, l_start),
                "calls": calls,
                "call_sites": c_sites,
                "call_sites_all": c_sites_all,
                "qualified_calls": q_calls,
                "body_tokens": b_toks,
                "arity_spec": arity,
                "ast_hash": _ast_hash(_text(nd)),
                "interface_hash": _ast_hash(sig),
            })

    for cls in classes:
        if cls["name"] in methods_by_type:
            cls["methods"] = methods_by_type[cls["name"]]
            cls["interface_hash"] = _ast_hash(f"{cls['signature']}|{','.join(sorted(cls['methods']))}")

    # Check Axum `.route("/path", get(handler))`
    for idx, line in enumerate(lines, start=1):
        m_axum = re.search(
            r'\.route\(\s*"([^"]+)"\s*,\s*(get|post|put|delete|patch)\(\s*([A-Za-z_][\w:]*)',
            line,
            re.IGNORECASE,
        )
        if m_axum:
            routes.append({
                "method": m_axum.group(2).upper(),
                "path": m_axum.group(1),
                "handler": m_axum.group(3).split("::")[-1],
                "line": idx,
            })

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": {},
        "exports": [f["name"] for f in functions],
        "routes": routes,
    }


def _parse_java(root, source: str) -> dict:
    functions: list[dict] = []
    classes: list[dict] = []
    imports: list[str] = []
    routes: list[dict] = []
    methods_by_cls: dict[str, list[str]] = {}
    cls_route_prefix: dict[str, str] = {}
    lines = source.splitlines()
    for nd in _walk_cst(root):
        nt = nd.type
        if nt == "import_declaration":
            imp_txt = _collapse_ws(_text(nd)).removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if imp_txt and imp_txt not in imports:
                imports.append(imp_txt)
        elif nt in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
            name_nd = nd.child_by_field_name("name")
            if not name_nd:
                continue
            c_name = _text(name_nd).strip()
            kind_lbl = nt.split("_")[0]
            sig = f"{kind_lbl} {c_name}"
            # Check class-level @RequestMapping("/prefix")
            c_head = _text(nd).split("{", 1)[0]
            m_pref = re.search(r'@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]+)"', c_head)
            if m_pref:
                cls_route_prefix[c_name] = m_pref.group(1).rstrip("/")
            classes.append({
                "name": c_name,
                "bases": [],
                "methods": [],
                "fields": [],
                "signature": sig,
                "docstring": _leading_comment_doc(lines, _line(nd)),
                "line_start": _line(nd),
                "line_end": _end_line(nd),
                "ast_hash": _ast_hash(_text(nd)),
                "interface_hash": _ast_hash(sig),
            })
        elif nt in ("method_declaration", "constructor_declaration"):
            name_nd = nd.child_by_field_name("name")
            if not name_nd:
                continue
            fn_name = _text(name_nd).strip()
            params_nd = nd.child_by_field_name("parameters")
            type_nd = nd.child_by_field_name("type")
            body_nd = nd.child_by_field_name("body")
            params_str = _collapse_ws(_text(params_nd)) if params_nd else "()"
            ret_str = _collapse_ws(_text(type_nd)) if type_nd else "void"
            parent_cls: str | None = None
            p_cur = nd.parent
            while p_cur is not None:
                if p_cur.type in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                    cn_nd = p_cur.child_by_field_name("name")
                    if cn_nd:
                        parent_cls = _text(cn_nd).strip()
                        methods_by_cls.setdefault(parent_cls, []).append(fn_name)
                    break
                p_cur = p_cur.parent
            sig = f"{ret_str} {parent_cls}.{fn_name}{params_str}" if parent_cls else f"{ret_str} {fn_name}{params_str}"
            calls, c_sites, c_sites_all, q_calls, b_toks = _extract_polyglot_calls(body_nd)
            arity = extract_arity_spec(params_str, is_method=False, is_python=False)
            l_start = _line(nd)
            m_head = _text(nd).split("{", 1)[0]
            m_spring = re.search(
                r'@(Get|Post|Put|Delete|Patch|Request)Mapping(?:\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]*)")?',
                m_head,
            )
            if m_spring:
                verb = m_spring.group(1).upper()
                if verb == "REQUEST":
                    verb = "ANY"
                sub_path = m_spring.group(2) or ""
                if sub_path and not sub_path.startswith("/"):
                    sub_path = "/" + sub_path
                pref = cls_route_prefix.get(parent_cls or "", "")
                full_path = f"{pref}{sub_path}" or "/"
                routes.append({
                    "method": verb,
                    "path": full_path,
                    "handler": fn_name,
                    "line": l_start,
                })
            functions.append({
                "name": fn_name,
                "parent_class": parent_cls,
                "line_start": l_start,
                "line_end": _end_line(nd),
                "signature": sig,
                "params": params_str,
                "return_type": ret_str,
                "decorators": [],
                "docstring": _leading_comment_doc(lines, l_start),
                "calls": calls,
                "call_sites": c_sites,
                "call_sites_all": c_sites_all,
                "qualified_calls": q_calls,
                "body_tokens": b_toks,
                "arity_spec": arity,
                "ast_hash": _ast_hash(_text(nd)),
                "interface_hash": _ast_hash(sig),
            })

    for cls in classes:
        if cls["name"] in methods_by_cls:
            cls["methods"] = methods_by_cls[cls["name"]]
            cls["interface_hash"] = _ast_hash(f"{cls['signature']}|{','.join(sorted(cls['methods']))}")

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": {},
        "exports": [c["name"] for c in classes] + [f["name"] for f in functions],
        "routes": routes,
    }



# ── Public entry points ─────────────────────────────────────────────────────

_STRUCTURAL_POLYGLOT_EXTS = {
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".cs", ".kt", ".kts", ".scala", ".rb", ".php", ".swift",
    ".graphql", ".gql", ".prisma", ".proto",
}


_SFC_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SFC_SCRIPT_OPEN_RE = re.compile(r'<script\b((?:[^>"\']|"[^"]*"|\'[^\']*\')*)>', re.IGNORECASE | re.DOTALL)
_SFC_SCRIPT_CLOSE_RE = re.compile(r"</script\s*>", re.IGNORECASE)


def _extract_sfc_script_preserving_lines(source: str) -> tuple[str, str]:
    """Extract `<script>` block(s) from `.vue` / `.svelte` files while preserving 1-based line numbers."""
    masked = _SFC_HTML_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    chunks: list[str] = []
    pos = 0
    script_ext = ".js"
    while pos < len(masked):
        m_open = _SFC_SCRIPT_OPEN_RE.search(masked, pos)
        if not m_open:
            chunks.append("\n" * masked[pos:].count("\n"))
            break
        chunks.append("\n" * masked[pos:m_open.end()].count("\n"))
        attrs = m_open.group(1) or ""
        m_lang = re.search(r'\blang\s*=\s*["\']?(tsx|jsx|ts|typescript)\b', attrs, re.IGNORECASE)
        if m_lang:
            lv = m_lang.group(1).lower()
            if lv == "tsx":
                script_ext = ".tsx"
            elif lv == "jsx" and script_ext != ".tsx":
                script_ext = ".jsx"
            elif script_ext not in (".tsx",):
                script_ext = ".ts"
        m_close = _SFC_SCRIPT_CLOSE_RE.search(masked, m_open.end())
        if not m_close:
            chunks.append(masked[m_open.end():])
            break
        chunks.append(masked[m_open.end():m_close.start()])
        chunks.append("\n" * m_close.group(0).count("\n"))
        pos = m_close.end()
    return "".join(chunks), script_ext


def _find_matching_brace_end(lines: list[str], start_idx: int, max_scan: int = 400) -> int:
    """Return 1-based end line of a brace-delimited block starting near `start_idx` (0-based)."""
    depth = 0
    opened = False
    limit = min(len(lines), start_idx + max_scan)
    for i in range(start_idx, limit):
        ln = lines[i]
        for ch in ln:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return i + 1
        if not opened and ln.rstrip().endswith(";"):
            return i + 1
    return min(len(lines), start_idx + 1)


def _parse_polyglot_structural(source: str, ext: str) -> dict:
    """Structural parser for C/C++, C#, Kotlin, Scala, Ruby, PHP, Swift, GraphQL, Prisma, and Protobuf."""
    lines = source.splitlines()
    functions: list[dict] = []
    classes: list[dict] = []
    imports: list[str] = []
    routes: list[dict] = []
    current_cls: str | None = None
    cls_end_line = 0
    methods_by_cls: dict[str, list[str]] = {}

    for i, raw_line in enumerate(lines):
        lno = i + 1
        if current_cls and lno > cls_end_line:
            current_cls = None
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue

        m_imp = (
            re.match(r'^#\s*include\s+[<"]([^>"]+)[>"]', stripped)
            or re.match(r'^(?:using|import|package)\s+([A-Za-z0-9_.*:]+)', stripped)
            or re.match(r'^(?:require|require_relative|include|use)\s+["\']?([A-Za-z0-9_./:\\-]+)', stripped)
        )
        if m_imp:
            imp_val = m_imp.group(1).strip().rstrip(";")
            if imp_val and imp_val not in imports:
                imports.append(imp_val)
            continue

        m_rt = (
            re.search(r'\[Http(Get|Post|Put|Delete|Patch)\(\s*"([^"]*)"\s*\)\]', stripped)
            or re.search(r'\bRoute::(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']', stripped, re.IGNORECASE)
            or re.search(r'^(get|post|put|delete|patch)\s+["\']([^"\']+)["\']', stripped)
        )
        if m_rt:
            routes.append({
                "method": m_rt.group(1).upper(),
                "path": m_rt.group(2) or "/",
                "handler": current_cls or "route_handler",
                "line": lno,
            })

        m_cls = re.match(
            r'^(?:(?:public|private|protected|internal|abstract|sealed|final|open|data|case|partial|static|export)\s+)*'
            r'(class|struct|interface|trait|object|enum|protocol|extension|model|type|input|service|message)\s+'
            r'([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?:<[^>]+>)?\s*(?::|extends|implements|<)\s*([A-Za-z0-9_.,\s<>]+))?',
            stripped,
        )
        if m_cls:
            kind, cname, bases_raw = m_cls.group(1), m_cls.group(2), m_cls.group(3) or ""
            end_l = (
                _find_matching_brace_end(lines, i, max_scan=800)
                if ext != ".rb"
                else min(len(lines), lno + 60)
            )
            bases = [b.strip() for b in re.split(r"[,&\s]+", bases_raw) if b.strip() and b.strip()[0].isupper()][:4]
            sig = f"{kind} {cname}" + (f"({', '.join(bases)})" if bases else "")
            classes.append({
                "name": cname,
                "line_start": lno,
                "line_end": end_l,
                "bases": bases,
                "methods": [],
                "fields": [],
                "signature": sig,
                "docstring": _leading_comment_doc(lines, lno),
                "ast_hash": _ast_hash("\n".join(lines[i:end_l])),
                "interface_hash": _ast_hash(sig),
            })
            current_cls = cname
            cls_end_line = end_l
            continue

        m_fn = (
            re.match(r'^(?:(?:public|private|protected|static|async|virtual|override|suspend|inline|fun|func|function|def)\s+)+([A-Za-z_][A-Za-z0-9_?!]*)\s*(\([^)]*\))?', stripped)
            or re.match(r'^rpc\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))', stripped)
            or (
                ext in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs", ".java", ".kt", ".scala")
                and re.match(
                    r'^(?:(?:public|private|protected|internal|static|virtual|override|inline|const|constexpr|async|extern)\s+)*'
                    r'[A-Za-z_][A-Za-z0-9_:<>,*\s&]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^;{}]*\))\s*(?:const\s*)?(?:\{|$)',
                    stripped,
                )
            )
        )
        if m_fn:
            fn_name = m_fn.group(1)
            if fn_name in ("if", "for", "while", "switch", "catch", "return", "sizeof", "typeof", "new", "delete"):
                continue
            params_str = (m_fn.group(2) or "()").strip()
            end_l = (
                _find_matching_brace_end(lines, i, max_scan=300)
                if ext != ".rb"
                else min(len(lines), lno + 30)
            )
            body_text = "\n".join(lines[i:end_l])
            raw_calls = [
                m.group(1)
                for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", body_text)
                if m.group(1) not in (fn_name, "if", "for", "while", "switch", "catch", "return", "sizeof")
            ]
            seen_c: set[str] = set()
            dedup_calls: list[str] = []
            for c_nm in raw_calls:
                if c_nm not in seen_c:
                    seen_c.add(c_nm)
                    dedup_calls.append(c_nm)
            b_toks = [
                w.lower()
                for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", body_text)
                if w.lower() not in _BODY_NOISE
            ][:100]
            sig = f"{current_cls}.{fn_name}{params_str}" if current_cls else f"{fn_name}{params_str}"
            if current_cls:
                methods_by_cls.setdefault(current_cls, []).append(fn_name)
            functions.append({
                "name": fn_name,
                "parent_class": current_cls,
                "line_start": lno,
                "line_end": end_l,
                "signature": sig,
                "params": params_str,
                "return_type": "",
                "decorators": [],
                "docstring": _leading_comment_doc(lines, lno),
                "calls": dedup_calls[:40],
                "call_sites": {},
                "call_sites_all": {},
                "qualified_calls": {},
                "body_tokens": b_toks,
                "arity_spec": extract_arity_spec(params_str, is_method=bool(current_cls), is_python=False),
                "ast_hash": _ast_hash(body_text),
                "interface_hash": _ast_hash(sig),
            })

    for cls in classes:
        if cls["name"] in methods_by_cls:
            cls["methods"] = methods_by_cls[cls["name"]]
            cls["interface_hash"] = _ast_hash(f"{cls['signature']}|{','.join(sorted(cls['methods']))}")

    res = {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "import_bindings": {},
        "exports": [c["name"] for c in classes] + [f["name"] for f in functions],
        "routes": routes,
        "parse_status": "ok",
    }
    res["interface_hash"] = compute_file_interface_hash(res)
    return res


def _has_structural_error(node, ext_l: str) -> bool:
    """Return True if the CST contains a real syntax error (ignoring benign unescaped `& ` in JSX text)."""
    if not node.has_error:
        return False
    if ext_l not in (".tsx", ".jsx"):
        return True
    stack = [node]
    while stack:
        cur = stack.pop()
        if not cur.has_error:
            continue
        if cur.type == "ERROR" or cur.is_missing:
            parent_t = cur.parent.type if cur.parent else ""
            txt = (cur.text or b"").decode("utf-8", errors="ignore")
            if parent_t in ("jsx_element", "jsx_fragment") and "&" in txt and "<" not in txt and "{" not in txt:
                continue
            return True
        stack.extend(cur.children)
    return False


def parse_with_tree(source: str, ext: str) -> tuple[dict, object | None]:
    """Parse source by file extension and return `(result_dict, tree_sitter_tree)` for single-pass reuse."""
    ext_l = ext.lower()
    if ext_l == ".sql":
        return _parse_sql(source), None
    if ext_l in (".vue", ".svelte"):
        script_src, script_ext = _extract_sfc_script_preserving_lines(source)
        return parse_with_tree(script_src, script_ext)
    if ext_l in _STRUCTURAL_POLYGLOT_EXTS and get_language(ext_l) is None:
        return _parse_polyglot_structural(source, ext_l), None
    parser = get_parser(ext_l)
    lang = get_language(ext_l)
    if parser is None or lang is None:
        return {
            "functions": [], "classes": [], "imports": [], "routes": [],
            "parse_status": "unsupported",
        }, None


    tree = parser.parse(source.encode("utf-8", errors="replace"))
    root = tree.root_node

    if ext_l == ".py":
        result = _parse_python(root)
    elif ext_l == ".go":
        result = _parse_go(root, source)
    elif ext_l == ".rs":
        result = _parse_rust(root, source)
    elif ext_l == ".java":
        result = _parse_java(root, source)
    else:
        result = _parse_js(root, lang)
    up_src = source.upper()
    if "CREATE TABLE" in up_src:
        sql_sc = extract_sql_table_schemas(source)
        if sql_sc:
            result["sql_schemas"] = sql_sc
    if "ALTER TABLE" in up_src:
        sql_alt = extract_sql_schema_alters(source)
        if sql_alt:
            result["sql_alters"] = sql_alt
            for tbl_k, ops in sql_alt.items():
                for sc_k, sc_obj in (result.get("sql_schemas") or {}).items():
                    if sc_k.lower() == tbl_k.lower():
                        apply_sql_alters_to_schema(sc_obj, ops)
    result["parse_status"] = "partial" if _has_structural_error(root, ext_l) else "ok"
    result["interface_hash"] = compute_file_interface_hash(result)
    return result, tree


def parse(source: str, ext: str) -> dict:
    """Parse source by file extension. Returns functions/classes/imports/routes/exports."""
    res, _ = parse_with_tree(source, ext)
    return res


def _is_unexpanded_symlink_src(path, source: str) -> bool:
    if not source or len(source) > 255 or "\n" in source or "\r" in source:
        return False
    target = source.strip()
    if not target.startswith(("./", "../")):
        return False
    try:
        return (path.parent / target).exists()
    except OSError:
        return False


def _is_minified_src(source: str, min_line_threshold: int = 500) -> bool:
    lines = source.splitlines()
    if not lines:
        return False
    non_empty = [len(line) for line in lines if line.strip()]
    if not non_empty:
        return False
    max_len = max(non_empty)
    if max_len <= min_line_threshold:
        return False
    if max_len > 5000 or len(non_empty) <= 5:
        return True
    long_count = sum(1 for ln in non_empty if ln > min_line_threshold)
    avg_len = sum(non_empty) / len(non_empty)
    return (long_count / len(non_empty)) > 0.25 or avg_len > 250


def _parse_worker_batch(
    batch: list[tuple[str, str, str, float, int]],
) -> list[tuple[str, dict, int]]:
    """Process-pool worker: parse a batch of `(rel, ext, source, mtime, size)` items."""
    from . import frameworks as _fw
    from .contracts import extract_4d_coordinate_matrix as _extract_4d

    out: list[tuple[str, dict, int]] = []
    for rel, ext, source, mtime, size in batch:
        info, ts_tree = parse_with_tree(source, ext)
        info.setdefault("routes", []).extend(_fw.infer_file_based_routes(rel, info))
        info["cells_4d"] = _extract_4d(source.splitlines(), ext=ext, tree=ts_tree)
        info["mtime"] = mtime
        out.append((rel, info, size))
    return out


def parse_files_parallel(
    dirty_items: list[tuple[str, object, str, float, int]],
    min_line_threshold: int = 500,
) -> tuple[dict[str, dict], dict[str, int], dict[str, str]]:
    """Read and parse `(rel, path, ext, mtime, size)` concurrently while preserving walk order.

    Uses a 16-thread I/O reader pool (overlapping cold OS disk reads) followed by a
    multi-core `ProcessPoolExecutor` for >=48 files (falling back gracefully in-process).
    """
    import os
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    if not dirty_items:
        return {}, {}, {}

    def _read_one(item: tuple[str, object, str, float, int]):
        rel, path, ext, mtime, size = item
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")  # type: ignore[attr-defined]
        except OSError as e:
            return (rel, None, f"read_error:{e.__class__.__name__}", ext, mtime, size)
        if _is_minified_src(source, min_line_threshold):
            return (rel, None, "minified_or_generated", ext, mtime, size)
        if _is_unexpanded_symlink_src(path, source):
            return (rel, None, "symlink_stub", ext, mtime, size)
        return (rel, source, None, ext, mtime, size)

    if len(dirty_items) >= 16:
        with ThreadPoolExecutor(max_workers=16) as io_pool:
            read_results = list(io_pool.map(_read_one, dirty_items))
    else:
        read_results = [_read_one(it) for it in dirty_items]

    skipped_map: dict[str, str] = {}
    to_parse: list[tuple[str, str, str, float, int]] = []
    for rel, source, skip_reason, ext, mtime, size in read_results:
        if skip_reason is not None or source is None:
            skipped_map[rel] = skip_reason or "read_error"
        else:
            to_parse.append((rel, ext, source, mtime, size))

    parsed_by_rel: dict[str, tuple[dict, int]] = {}
    cpu_cnt = min(os.cpu_count() or 4, 8)
    use_multiproc = (
        len(to_parse) >= 48
        and cpu_cnt >= 2
        and os.environ.get("PROJECTBRAIN_PARALLEL_INDEX", "1") != "0"
    )
    if use_multiproc:
        # Sort descending by source length and deal round-robin across chunks so no worker straggles
        sorted_items = sorted(to_parse, key=lambda x: len(x[2]), reverse=True)
        num_batches = cpu_cnt * 2
        batches: list[list[tuple[str, str, str, float, int]]] = [[] for _ in range(num_batches)]
        for idx, it in enumerate(sorted_items):
            batches[idx % num_batches].append(it)
        batches = [b for b in batches if b]
        try:
            with ProcessPoolExecutor(max_workers=cpu_cnt) as cpu_pool:
                for batch_res in cpu_pool.map(_parse_worker_batch, batches):
                    for rel, info, size in batch_res:
                        parsed_by_rel[rel] = (info, size)
        except Exception:
            for batch in batches:
                for rel, info, size in _parse_worker_batch(batch):
                    parsed_by_rel[rel] = (info, size)
    elif to_parse:
        for rel, info, size in _parse_worker_batch(to_parse):
            parsed_by_rel[rel] = (info, size)

    # Reconstruct maps in deterministic original walk order
    parsed_index: dict[str, dict] = {}
    parsed_sizes: dict[str, int] = {}
    parsed_skipped: dict[str, str] = {}
    for rel, _, _, _, _ in dirty_items:
        if rel in skipped_map:
            parsed_skipped[rel] = skipped_map[rel]
        elif rel in parsed_by_rel:
            info, sz = parsed_by_rel[rel]
            parsed_index[rel] = info
            parsed_sizes[rel] = sz
    return parsed_index, parsed_sizes, parsed_skipped


