"""Tree-sitter based parsers for Python and JS/TS/TSX."""
from __future__ import annotations

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_python as _tspy
import tree_sitter_javascript as _tsjs
import tree_sitter_typescript as _tsts

_PY  = Language(_tspy.language())
_JS  = Language(_tsjs.language())
_TS  = Language(_tsts.language_typescript())
_TSX = Language(_tsts.language_tsx())

_LANG_MAP: dict[str, Language] = {
    ".py":  _PY,
    ".js":  _JS,
    ".jsx": _JS,
    ".mjs": _JS,
    ".cjs": _JS,
    ".ts":  _TS,
    ".tsx": _TSX,
}

_PARSERS: dict[str, Parser] = {ext: Parser(lang) for ext, lang in _LANG_MAP.items()}

_HTTP = {"get", "post", "put", "delete", "patch", "all"}

# Receivers that genuinely mount express/koa/fastify routes. Anything else
# calling .get()/.delete() is almost certainly a Map, Set, URLSearchParams,
# Headers or cache — see _is_express_route.
_ROUTER_RECEIVERS = {"app", "router", "server", "api", "express", "fastify", "koa"}


def _is_express_route(receiver: str, path: str) -> bool:
    """Distinguish `app.get('/users')` from `searchParams.get('className')`.

    The route query matches any `<obj>.<http-verb>('<str>')`, which in a React
    or TS codebase fires constantly on Map/URLSearchParams/Headers lookups and
    invents dozens of phantom routes. Two cheap signals separate them: a real
    route path is a URL path, and it is mounted on an app/router-ish receiver.
    """
    if not path.startswith("/"):
        return False
    # Tolerate app.route(...).get(...) chains and this.app / v1.router members
    # by matching on the final segment of the receiver expression.
    tail = receiver.split(".")[-1].split("(")[0].strip().lower()
    return tail in _ROUTER_RECEIVERS or tail.endswith("router") or tail.endswith("app")


# ── shared helpers ──────────────────────────────────────────────────────────

def _captures(lang: Language, pattern: str, root) -> dict:
    return QueryCursor(Query(lang, pattern)).captures(root)

def _matches(lang: Language, pattern: str, root):
    return QueryCursor(Query(lang, pattern)).matches(root)

def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")

def _line(node) -> int:
    return node.start_point[0] + 1

def _end_line(node) -> int:
    return node.end_point[0] + 1

def _call_sites(callee_nodes) -> dict:
    """Map each callee name to the earliest line it is called on.

    Lets an edge cite "calls X at line N" and a caller slice a source snippet —
    the citable evidence verify_claim needs. First occurrence wins so the line
    is stable regardless of capture order.
    """
    sites: dict[str, int] = {}
    for n in callee_nodes:
        name = _text(n)
        line = _line(n)
        if name not in sites or line < sites[name]:
            sites[name] = line
    return sites


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
      function: (attribute attribute: (identifier) @method)
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


def _py_bases(cls_node) -> list[str]:
    """Extract base class names from a class_definition node."""
    # tree-sitter-python uses 'superclasses' as the field name
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
    """Extract methods=[...] from a Flask @app.route(...) argument_list.

    Defaults to ["GET"] per Flask semantics when no methods kwarg is present.
    """
    for child in arg_list_node.children:
        if child.type != "keyword_argument":
            continue
        name_node = child.child_by_field_name("name")
        if not name_node or _text(name_node) != "methods":
            continue
        value_node = child.child_by_field_name("value")
        if not value_node or value_node.type != "list":
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
    """Return only direct function_definition children of a class body block.

    A recursive captures() call would also pick up nested functions defined
    inside method bodies (e.g. helper closures). Walking direct children avoids
    that — a direct method is either a function_definition or a
    decorated_definition whose inner definition is a function_definition.
    """
    methods = []
    for child in body_node.children:
        if child.type == "function_definition":
            methods.append(child)
        elif child.type == "decorated_definition":
            inner = child.child_by_field_name("definition")
            if inner and inner.type == "function_definition":
                methods.append(inner)
    return methods


def _parse_python(root) -> dict:
    functions, classes, imports, routes = [], [], [], []
    method_to_class: dict[tuple, str] = {}

    # Phase 1 — classes first: collect bases, methods, populate method_to_class.
    # Walk direct children of the body block (not a recursive query) to avoid
    # treating nested helper functions inside method bodies as class methods.
    # Use start_point as key — stable across separate captures() calls.
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

        classes.append({
            "name": cls_name,
            "line_start": _line(cls_node),
            "line_end": _end_line(cls_node),
            "docstring": _py_docstring(cls_node),
            "bases": bases,
            "methods": method_names,
        })

    # Phase 2 — all functions: extract calls and mark methods.
    # Use child_by_field_name("name") instead of zip — tree-sitter does not
    # guarantee that @func and @name captures interleave in the expected order.
    caps = _captures(_PY, _PY_FUNC, root)
    for func_node in caps.get("func", []):
        name_node = func_node.child_by_field_name("name")
        if not name_node:
            continue
        callee_matches = _matches(_PY, _PY_CALLS, func_node)
        calls = []
        call_sites = {}
        qualified_calls = {}
        for _, mc in callee_matches:
            callee_nodes = mc.get("callee", [])
            receiver_nodes = mc.get("receiver", [])
            if callee_nodes:
                callee_node = callee_nodes[0]
                callee_text = _text(callee_node)
                line = _line(callee_node)
                if callee_text not in calls:
                    calls.append(callee_text)
                if callee_text not in call_sites or line < call_sites[callee_text]:
                    call_sites[callee_text] = line
                if receiver_nodes:
                    receiver_text = _text(receiver_nodes[0])
                    qual = f"{receiver_text}.{callee_text}"
                    if qual not in qualified_calls or line < qualified_calls[qual]:
                        qualified_calls[qual] = line

        entry = {
            "name": _text(name_node),
            "line_start": _line(func_node),
            "line_end": _end_line(func_node),
            "docstring": _py_docstring(func_node),
            "calls": calls,
            "call_sites": call_sites,
            "qualified_calls": qualified_calls,
        }
        parent_cls = method_to_class.get(func_node.start_point)
        if parent_cls is not None:
            entry["is_method"] = True
            entry["parent_class"] = parent_cls
        functions.append(entry)

    caps = _captures(_PY, _PY_IMPORT, root)
    imports_set = set()
    for n in caps.get("mod", []):
        mod_text = _text(n)
        if mod_text.replace(".", "") == "":
            parent = n.parent
            if parent and parent.type == "import_from_statement":
                for child in parent.children:
                    if child.type == "dotted_name":
                        if child != n:
                            imports_set.add(mod_text + _text(child))
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            imports_set.add(mod_text + _text(name_node))
        else:
            imports_set.add(mod_text)
    imports = list(imports_set)

    for _, mc in _matches(_PY, _PY_ROUTE, root):
        m_nodes = mc.get("method", [])
        p_nodes = mc.get("path", [])
        h_nodes = mc.get("handler", [])
        a_nodes = mc.get("args", [])
        if not (m_nodes and p_nodes and h_nodes):
            continue
        method = _text(m_nodes[0])
        path = _text(p_nodes[0])
        handler = _text(h_nodes[0])
        line = _line(h_nodes[0])

        if method in _HTTP:
            routes.append({
                "method": method.upper(), "path": path, "handler": handler,
                "line": line, "framework": "fastapi",
            })
        elif method == "route" and a_nodes:
            for verb in _flask_methods(a_nodes[0]):
                routes.append({
                    "method": verb, "path": path, "handler": handler,
                    "line": line, "framework": "flask",
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

    return {"functions": functions, "classes": classes, "imports": imports, "routes": routes}


# ── JavaScript / TypeScript ─────────────────────────────────────────────────

_JS_FUNC = """
[
  (function_declaration name: (identifier) @name) @func
  (variable_declarator
    name: (identifier) @name
    value: [(arrow_function) (function_expression)]) @func
  (method_definition name: (property_identifier) @name) @func
]
"""
# name: (_) instead of (identifier) — TS/TSX grammars use type_identifier for
# class names, which makes (identifier) an impossible pattern in those grammars.
_JS_CLASS = """
(class_declaration name: (_) @name) @class
"""
_JS_IMPORT = """
[
  (import_statement source: (string (string_fragment) @mod))
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
  arguments: (arguments (string (string_fragment) @path)))
"""
_JS_CALLS = """
(call_expression function:
  [(identifier) @callee
   (member_expression object: (_) @receiver property: (property_identifier) @callee)])
"""


def _js_bases(cls_node) -> list[str]:
    """Extract base class name from a JS/TS class_declaration node."""
    for child in cls_node.children:
        if child.type == "class_heritage":
            for sub in child.children:
                if sub.type == "extends_clause":
                    value = sub.child_by_field_name("value")
                    if value:
                        if value.type == "identifier":
                            return [_text(value)]
                        if value.type == "member_expression":
                            prop = value.child_by_field_name("property")
                            if prop:
                                return [_text(prop)]
                elif sub.type == "identifier":
                    return [_text(sub)]
                elif sub.type == "member_expression":
                    prop = sub.child_by_field_name("property")
                    if prop:
                        return [_text(prop)]
    return []


def _js_direct_methods(body_node, lang: Language) -> list:
    """Return only direct method_definition / function children of a JS class body.

    In JS, a class_body directly contains method_definition nodes.
    Walking direct children avoids picking up nested arrow functions or
    function expressions inside method bodies.
    """
    methods = []
    for child in body_node.children:
        # method_definition is always a direct class member
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


def _parse_js(root, lang: Language) -> dict:
    functions, classes, imports, exports, routes = [], [], [], [], []
    method_to_class: dict[tuple, str] = {}

    # Phase 1 — classes first: walk direct class body children to avoid
    # treating nested functions as methods.
    caps = _captures(lang, _JS_CLASS, root)
    for cls_node in caps.get("class", []):
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

        classes.append({
            "name": cls_name,
            "line_start": _line(cls_node),
            "line_end": _end_line(cls_node),
            "bases": bases,
            "methods": method_names,
        })

    # Phase 2 — all functions (use child_by_field_name, not zip)
    caps = _captures(lang, _JS_FUNC, root)
    for func_node in caps.get("func", []):
        name_node = func_node.child_by_field_name("name")
        if not name_node:
            continue
        callee_matches = _matches(lang, _JS_CALLS, func_node)
        calls = []
        call_sites = {}
        qualified_calls = {}
        for _, mc in callee_matches:
            callee_nodes = mc.get("callee", [])
            receiver_nodes = mc.get("receiver", [])
            if callee_nodes:
                callee_node = callee_nodes[0]
                callee_text = _text(callee_node)
                line = _line(callee_node)
                if callee_text not in calls:
                    calls.append(callee_text)
                if callee_text not in call_sites or line < call_sites[callee_text]:
                    call_sites[callee_text] = line
                if receiver_nodes:
                    receiver_text = _text(receiver_nodes[0])
                    qual = f"{receiver_text}.{callee_text}"
                    if qual not in qualified_calls or line < qualified_calls[qual]:
                        qualified_calls[qual] = line

        entry = {
            "name": _text(name_node),
            "line_start": _line(func_node),
            "line_end": _end_line(func_node),
            "calls": calls,
            "call_sites": call_sites,
            "qualified_calls": qualified_calls,
        }
        parent_cls = method_to_class.get(func_node.start_point)
        if parent_cls is not None:
            entry["is_method"] = True
            entry["parent_class"] = parent_cls
        functions.append(entry)

    caps = _captures(lang, _JS_IMPORT, root)
    imports = list({_text(n) for n in caps.get("mod", [])})

    caps = _captures(lang, _JS_EXPORT, root)
    exports = list({_text(n) for n in caps.get("name", [])})
    exports.extend(_js_named_exports(root, lang))
    exports = list(set(exports))

    for _, mc in _matches(lang, _JS_ROUTE, root):
        m_nodes = mc.get("method", [])
        p_nodes = mc.get("path", [])
        o_nodes = mc.get("obj", [])
        if not (m_nodes and p_nodes):
            continue
        method = _text(m_nodes[0])
        if method not in _HTTP:
            continue
        path = _text(p_nodes[0])
        receiver = _text(o_nodes[0]) if o_nodes else ""
        if not _is_express_route(receiver, path):
            continue
        routes.append({
            "method": method.upper(),
            "path": path,
            "line": _line(m_nodes[0]),
            "framework": "express",
        })

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "exports": exports,
        "routes": routes,
    }


# ── Public entry point ──────────────────────────────────────────────────────

def parse(source: str, ext: str) -> dict:
    """Parse source by file extension. Returns functions/classes/imports/routes/exports.

    Every result carries a ``parse_status`` so callers can tell three genuinely
    different situations apart — the honesty the graph is built on:
      * ``"ok"``          — parsed cleanly; the extracted symbols are authoritative.
      * ``"partial"``     — tree-sitter hit ERROR/MISSING nodes (broken/unsupported
                            syntax); extraction is incomplete, so absence proves nothing.
      * ``"unsupported"`` — the extension is walked/indexed but has no grammar
                            (e.g. .go/.rs/.java); zero symbols means "we didn't
                            look", not "there's nothing here".
    """
    lang = _LANG_MAP.get(ext)
    if lang is None:
        return {
            "functions": [], "classes": [], "imports": [], "routes": [],
            "parse_status": "unsupported",
        }

    tree = _PARSERS[ext].parse(source.encode("utf-8", errors="replace"))
    root = tree.root_node

    result = _parse_python(root) if ext == ".py" else _parse_js(root, lang)
    # tree-sitter never raises on invalid syntax — it yields a tree containing
    # ERROR/MISSING nodes. Surface that so a partial parse can never silently
    # ground a confident "no such call" refutation.
    result["parse_status"] = "partial" if root.has_error else "ok"
    return result


# ── Legacy shims (brain.py calls these directly) ────────────────────────────

def parse_python(source: str) -> dict:
    return parse(source, ".py")


def parse_js(source: str) -> dict:
    return parse(source, ".js")
