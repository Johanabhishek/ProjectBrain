"""Static 6D Coordinate Matrix & Cross-Layer Contract Analyzer for ProjectBrain.

Provides:
  1. Universal Python AST intra-procedural contract & disjoint type analyzer (`_analyze_python_ast_contracts`).
  2. Universal JS/TS/React static AST & lexical-closure contract analyzer (`_analyze_js_ts_ast_contracts`).
  3. 6D Coordinate Matrix extraction & query scoring (`extract_4d_coordinate_matrix`, `query_4d_coordinate_matrix`).
  4. Repository-wide 6D schema, SQL dialect, exception, HTTP route, and AST contract diagnostics (`compile_6d_contract_diagnostics`).
"""
from __future__ import annotations

import ast
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .graph import fn_id
from .parsers import apply_sql_alters_to_schema, get_parser

if TYPE_CHECKING:
    from .brain import ProjectBrain


def _find_balanced_end(lines: list[str], start_idx: int, open_ch: str = "{", close_ch: str = "}", max_scan: int = 2600) -> int:
    n = len(lines)
    depth = 0
    started = False
    limit = min(n, start_idx + max_scan)
    for idx in range(start_idx, limit):
        ln = lines[idx]
        in_str: str | None = None
        escape = False
        for ch in ln:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
                continue
            if ch == open_ch:
                depth += 1
                started = True
            elif ch == close_ch and started:
                depth -= 1
                if depth <= 0:
                    return idx
    return min(n - 1, start_idx + 45)


_NOMINAL_DISJOINT_TYPES = {
    "UUID", "str", "int", "float", "bool", "bytes",
    "datetime", "date", "Decimal", "dict", "list",
}


def _norm_py_type_annotation(ann_node: ast.AST | None) -> str | None:
    if ann_node is None:
        return None
    try:
        raw = ast.unparse(ann_node).strip()
    except Exception:
        return None
    m_opt = re.match(r"^(?:typing\.)?Optional\[\s*(.+)\s*\]$", raw)
    if m_opt:
        raw = m_opt.group(1).strip()
    parts = [p.strip() for p in raw.split("|") if p.strip() != "None"]
    if len(parts) == 1:
        raw = parts[0]
    base = raw.split("[")[0].split(".")[-1].strip()
    return base if base else None


def _infer_py_expr_type(
    expr: ast.AST,
    type_env: dict[str, str],
    row_env: dict[str, str],
) -> tuple[str | None, str | None, str | None]:
    """Return (expr_label, nominal_type, uncoerced_row_col) for a Python AST expression."""
    if isinstance(expr, ast.Name):
        v = expr.id
        if v in row_env and type_env.get(v) in (None, "TEXT"):
            return v, "TEXT", row_env[v]
        return v, type_env.get(v), None
    if isinstance(expr, ast.Constant):
        val = expr.value
        if isinstance(val, bool):
            return repr(val), "bool", None
        if isinstance(val, int):
            return repr(val), "int", None
        if isinstance(val, float):
            return repr(val), "float", None
        if isinstance(val, str):
            return repr(val), "str", None
        if isinstance(val, bytes):
            return repr(val), "bytes", None
    if isinstance(expr, ast.Dict):
        return "{...}", "dict", None
    if isinstance(expr, ast.List):
        return "[...]", "list", None
    if isinstance(expr, ast.Call):
        fn_name = ""
        if isinstance(expr.func, ast.Name):
            fn_name = expr.func.id
        elif isinstance(expr.func, ast.Attribute):
            fn_name = expr.func.attr
        if fn_name in ("str", "int", "float", "bool", "bytes", "UUID", "Decimal", "dict", "list"):
            return f"{fn_name}(...)", fn_name, None
    if isinstance(expr, ast.Subscript):
        sl = expr.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            base_nm = expr.value.id if isinstance(expr.value, ast.Name) else "row"
            if base_nm not in type_env or type_env.get(base_nm) in ("Row", "dict", "Mapping", "Any"):
                return f"{base_nm}[{sl.value!r}]", "TEXT", sl.value
    return None, None, None


def _collect_py_defs(stmts: list[ast.stmt], cls_out: list[ast.ClassDef], fn_out: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> None:
    for st in stmts:
        if isinstance(st, ast.ClassDef):
            cls_out.append(st)
            _collect_py_defs(st.body, cls_out, fn_out)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_out.append(st)
            _collect_py_defs(st.body, cls_out, fn_out)
        elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            _collect_py_defs(st.body, cls_out, fn_out)
            if st.orelse:
                _collect_py_defs(st.orelse, cls_out, fn_out)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            _collect_py_defs(st.body, cls_out, fn_out)
        elif isinstance(st, ast.Try):
            _collect_py_defs(st.body, cls_out, fn_out)
            for h in st.handlers:
                _collect_py_defs(h.body, cls_out, fn_out)
            if st.orelse:
                _collect_py_defs(st.orelse, cls_out, fn_out)
            if st.finalbody:
                _collect_py_defs(st.finalbody, cls_out, fn_out)
        elif hasattr(ast, "Match") and isinstance(st, ast.Match):
            for mc in st.cases:
                _collect_py_defs(mc.body, cls_out, fn_out)


def _analyze_python_ast_contracts(lines: list[str]) -> dict[int, dict]:
    """Universal Python AST intra-procedural dataflow analyzer for:
      1. Parameter & local variable nominal type propagation (`type_env`) and row-column tracking (`row_env`).
      2. Disjoint type equality/inequality detection (`tau(e_1) ∩ tau(e_2) = ∅`).
      3. Arbitrary-depth dictionary return shape & class/model return field extraction (`return_dict_keys`).
    Returns a mapping from 1-based function start line to `{type_comparisons, return_dict_keys}`.
    """
    src = "\n".join(lines)
    if "def " not in src:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}

    cls_nodes: list[ast.ClassDef] = []
    fn_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    _collect_py_defs(tree.body, cls_nodes, fn_nodes)

    cls_fields: dict[str, list[str]] = {}
    for node in cls_nodes:
        f_list: list[str] = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fnm = item.target.id
                if not fnm.startswith("_") and fnm not in f_list:
                    f_list.append(fnm)
            elif isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_") and t.id not in f_list:
                        f_list.append(t.id)
        if f_list:
            cls_fields[node.name] = f_list

    n_lines = len(lines)
    result: dict[int, dict] = {}
    for node in fn_nodes:
        type_env: dict[str, str] = {}
        row_env: dict[str, str] = {}
        all_args = (
            list(getattr(node.args, "posonlyargs", []))
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        for arg in all_args:
            t_norm = _norm_py_type_annotation(arg.annotation)
            if t_norm:
                type_env[arg.arg] = t_norm

        type_comparisons: list[dict] = []
        return_dict_keys: list[str] = []
        has_dynamic_dict_spread = False
        fn_end = getattr(node, "end_lineno", None) or n_lines
        fn_slice = lines[max(0, node.lineno - 1) : fn_end]
        has_fn_eq = any("==" in ln_s or "!=" in ln_s for ln_s in fn_slice)
        has_fn_ret = any("return" in ln_s for ln_s in fn_slice)
        if not has_fn_eq and not has_fn_ret:
            continue

        def _walk_cmp(sub: ast.AST) -> None:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            if isinstance(sub, ast.Compare):
                operands = [sub.left] + list(sub.comparators)
                for op, left_e, right_e in zip(sub.ops, operands, operands[1:]):
                    if not isinstance(op, (ast.Eq, ast.NotEq)):
                        continue
                    l_lbl, l_ty, l_col = _infer_py_expr_type(left_e, type_env, row_env)
                    r_lbl, r_ty, r_col = _infer_py_expr_type(right_e, type_env, row_env)
                    if l_col and r_ty in ("UUID", "datetime", "date", "Decimal", "bytes"):
                        type_comparisons.append({
                            "param": r_lbl or "expr",
                            "param_type": r_ty,
                            "row_key": l_col,
                            "line": getattr(sub, "lineno", node.lineno),
                        })
                    elif r_col and l_ty in ("UUID", "datetime", "date", "Decimal", "bytes"):
                        type_comparisons.append({
                            "param": l_lbl or "expr",
                            "param_type": l_ty,
                            "row_key": r_col,
                            "line": getattr(sub, "lineno", node.lineno),
                        })
                    elif (
                        not l_col
                        and not r_col
                        and l_ty in _NOMINAL_DISJOINT_TYPES
                        and r_ty in _NOMINAL_DISJOINT_TYPES
                        and l_ty != r_ty
                        and {l_ty, r_ty} != {"int", "float"}
                    ):
                        type_comparisons.append({
                            "param": l_lbl or "lhs",
                            "param_type": l_ty,
                            "other_var": r_lbl or "rhs",
                            "other_type": r_ty,
                            "row_key": f"{r_lbl}:{r_ty}",
                            "line": getattr(sub, "lineno", node.lineno),
                        })
            for ch in ast.iter_child_nodes(sub):
                _walk_cmp(ch)

        def _check_expr_comparisons(stmt_node: ast.AST) -> None:
            if not has_fn_eq:
                return
            s_l = getattr(stmt_node, "lineno", 0)
            e_l = getattr(stmt_node, "end_lineno", 0) or s_l
            if s_l > 0 and not any("==" in ln_s or "!=" in ln_s for ln_s in lines[s_l - 1 : e_l]):
                return
            _walk_cmp(stmt_node)

        def _walk_stmt_block(stmts: list[ast.stmt]) -> None:
            nonlocal has_dynamic_dict_spread
            for st in stmts:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(st, ast.Assign):
                    if has_fn_eq:
                        _check_expr_comparisons(st.value)
                        _, rhs_ty, rhs_col = _infer_py_expr_type(st.value, type_env, row_env)
                        for tgt in st.targets:
                            if isinstance(tgt, ast.Name):
                                if rhs_col:
                                    row_env[tgt.id] = rhs_col
                                    type_env[tgt.id] = "TEXT"
                                else:
                                    row_env.pop(tgt.id, None)
                                    if rhs_ty:
                                        type_env[tgt.id] = rhs_ty
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    if has_fn_eq:
                        if st.value:
                            _check_expr_comparisons(st.value)
                        ann_t = _norm_py_type_annotation(st.annotation)
                        _, rhs_ty, rhs_col = (
                            _infer_py_expr_type(st.value, type_env, row_env)
                            if st.value is not None
                            else (None, None, None)
                        )
                        if rhs_col and not ann_t:
                            row_env[st.target.id] = rhs_col
                            type_env[st.target.id] = "TEXT"
                        else:
                            row_env.pop(st.target.id, None)
                            if ann_t or rhs_ty:
                                type_env[st.target.id] = ann_t or rhs_ty  # type: ignore[assignment]
                elif isinstance(st, ast.Return) and st.value is not None:
                    _check_expr_comparisons(st.value)
                    rv = st.value
                    if isinstance(rv, ast.Dict):
                        if any(k is None for k in rv.keys):
                            has_dynamic_dict_spread = True
                        else:
                            for k_node in rv.keys:
                                if isinstance(k_node, ast.Constant) and isinstance(k_node.value, str):
                                    if k_node.value not in return_dict_keys:
                                        return_dict_keys.append(k_node.value)
                    elif isinstance(rv, ast.Call):
                        fn_nm = (
                            rv.func.id
                            if isinstance(rv.func, ast.Name)
                            else (rv.func.attr if isinstance(rv.func, ast.Attribute) else "")
                        )
                        if fn_nm in cls_fields:
                            for cf in cls_fields[fn_nm]:
                                if cf not in return_dict_keys:
                                    return_dict_keys.append(cf)
                        elif fn_nm == "dict":
                            for kw in rv.keywords:
                                if kw.arg and kw.arg not in return_dict_keys:
                                    return_dict_keys.append(kw.arg)
                else:
                    if isinstance(st, (ast.If, ast.While)):
                        _check_expr_comparisons(st.test)
                        _walk_stmt_block(st.body)
                        _walk_stmt_block(st.orelse)
                    elif isinstance(st, (ast.For, ast.AsyncFor)):
                        _check_expr_comparisons(st.iter)
                        _walk_stmt_block(st.body)
                        _walk_stmt_block(st.orelse)
                    elif isinstance(st, (ast.With, ast.AsyncWith)):
                        _walk_stmt_block(st.body)
                    elif isinstance(st, ast.Try):
                        _walk_stmt_block(st.body)
                        for h in st.handlers:
                            _walk_stmt_block(h.body)
                        _walk_stmt_block(st.orelse)
                        _walk_stmt_block(st.finalbody)
                    else:
                        _check_expr_comparisons(st)

        _walk_stmt_block(node.body)
        if has_dynamic_dict_spread:
            return_dict_keys = []
        elif not return_dict_keys and node.returns is not None:
            ret_t = _norm_py_type_annotation(node.returns)
            if ret_t and ret_t in cls_fields:
                return_dict_keys = list(cls_fields[ret_t])

        result[node.lineno] = {
            "type_comparisons": type_comparisons,
            "return_dict_keys": return_dict_keys,
        }
    return result


def _count_call_args(arg_str: str) -> int:
    """Count top-level comma-separated arguments inside a function call's `(...)`."""
    s = arg_str.strip()
    if not s:
        return 0
    depth = 0
    in_str: str | None = None
    escape = False
    commas = 0
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            continue
        if ch in ("(", "[", "{"):
            depth += 1
        elif ch in (")", "]", "}"):
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            commas += 1
    return commas + 1


def _balanced_paren_end(text: str) -> int:
    """Index of the `)` closing the `(` at text[0], skipping string literals; -1 if unbalanced."""
    depth = 0
    in_s: str | None = None
    esc = False
    for ch_i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if in_s:
            if ch == in_s:
                in_s = None
            continue
        if ch in ("'", '"', "`"):
            in_s = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return ch_i
    return -1


def _split_top_level_params(raw: str) -> list[str] | None:
    """Split a parameter list on top-level commas, respecting (), [], {}, <> nesting and strings.
    Returns None when nesting is unbalanced (signature not reliably parseable)."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    in_s: str | None = None
    prev = ""
    for ch in raw:
        if in_s:
            buf.append(ch)
            if ch == in_s and prev != "\\":
                in_s = None
            prev = ch
            continue
        if ch in ("'", '"', "`"):
            in_s = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}" or (ch == ">" and prev != "="):
            depth -= 1
            if depth < 0:
                return None
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            prev = ch
            continue
        buf.append(ch)
        prev = ch
    if depth != 0 or in_s:
        return None
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_JS_REL_IMP_RE = re.compile(r"import\s+\{([^}]+)\}\s+from\s+['\"]\.\.?\/[^'\"]+['\"]")
_JS_LC_IDENT_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*$")
_JS_JSX_HANDLER_RE = re.compile(
    r"\b(on[A-Z]\w*)\s*=\s*\{\s*(?:(?:async\s*)?(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>\s*\{?\s*)?([a-zA-Z_]\w*)\b"
)
_JS_USESTATE_LN_RE = re.compile(r"const\s+\[\s*([a-zA-Z_]\w*)\s*,\s*([a-zA-Z_]\w*)\s*\]\s*=\s*useState\b")
_JS_EFF_LN_RE = re.compile(r"\b(useEffect|useLayoutEffect|useCallback)\s*\(")
_JS_DEPS_TAIL_RE = re.compile(r",\s*\[\s*([^\]]*?)\s*\]\s*\)\s*;?\s*$")
_JS_FN_DECL_LN_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)\s*(?::\s*[^{]+)?\{|"
    r"^(?:export\s+)?const\s+([a-zA-Z_]\w*)\s*=\s*(?:useCallback\s*\(\s*)?(?:async\s*)?\(([^)]*)\)\s*(?::\s*[^=]+)?=>\s*\{"
)
_JS_PARAM_ID_RE = re.compile(r"^(?:\.\.\.)?([a-zA-Z_$][\w$]*)")
_JS_IGNORE_HANDLERS = (
    "reset", "clear", "init", "load", "fetch", "sync", "measure", "resize", "scroll", "noop",
)


_JS_BUILTIN_SETTERS = frozenset({
    "setTimeout", "setInterval", "setImmediate", "setState",
    "setAttribute", "setAttributeNS", "setAttributeNode", "setAttributeNodeNS",
    "setProperty", "setHeader", "setHeaders", "setItem", "setPrototypeOf",
    "setHours", "setMinutes", "setSeconds", "setMilliseconds", "setDate",
    "setMonth", "setFullYear", "setTime", "setUTCDate", "setUTCHours",
    "setUTCMinutes", "setUTCSeconds", "setUTCMilliseconds", "setUTCMonth",
    "setUTCFullYear", "setBigInt64", "setBigUint64", "setFloat32", "setFloat64",
    "setInt8", "setInt16", "setInt32", "setUint8", "setUint16", "setUint32",
    "setRequestHeader", "setCustomValidity", "setSelectionRange", "setRangeText",
    "setPointerCapture", "setDragImage", "setPosition", "setBaseAndExtent",
    "setStart", "setEnd", "setStartBefore", "setStartAfter", "setEndBefore",
    "setEndAfter", "setNamedItem", "setNamedItemNS", "setServerSideProps",
})
_JS_SETTER_IDENT_RE = re.compile(r"^set[A-Z][a-zA-Z0-9_]*$")


def _collect_js_pattern_bindings(pat_node, out: set[str]) -> None:
    """Collect bound identifier names from a JS/TS parameter, declarator LHS, or destructuring pattern."""
    if pat_node is None:
        return
    stack = [pat_node]
    while stack:
        nd = stack.pop()
        nt = nd.type
        if nt in ("type_annotation", "type_arguments", "TSTypeAnnotation"):
            continue
        if nt in ("identifier", "shorthand_property_identifier_pattern", "shorthand_property_identifier"):
            out.add(nd.text.decode("utf-8", errors="replace"))
            continue
        if nt == "assignment_pattern":
            left_n = nd.child_by_field_name("left")
            if left_n is not None:
                stack.append(left_n)
            continue
        if nt == "pair_pattern":
            val_n = nd.child_by_field_name("value")
            if val_n is not None:
                stack.append(val_n)
            continue
        for ch in nd.children:
            stack.append(ch)


def _collect_js_file_bindings_and_bare_setter_calls(root_node) -> tuple[set[str], list[tuple[str, int]]]:
    """Single-pass CST walk returning `(all_bound_identifiers, bare_setX_calls_with_line)`."""
    bound: set[str] = set()
    bare_setter_calls: list[tuple[str, int]] = []
    stack = [root_node]
    while stack:
        nd = stack.pop()
        nt = nd.type
        if nt == "import_specifier":
            alias_n = nd.child_by_field_name("alias") or nd.child_by_field_name("name")
            if alias_n is not None:
                bound.add(alias_n.text.decode("utf-8", errors="replace"))
        elif nt in ("import_clause", "namespace_import"):
            for ch in nd.children:
                if ch.type == "identifier":
                    bound.add(ch.text.decode("utf-8", errors="replace"))
        elif nt == "variable_declarator":
            _collect_js_pattern_bindings(nd.child_by_field_name("name"), bound)
        elif nt in (
            "function_declaration",
            "generator_function_declaration",
            "function_expression",
            "generator_function",
            "arrow_function",
            "method_definition",
        ):
            nm_n = nd.child_by_field_name("name")
            if nm_n is not None and nm_n.type in ("identifier", "property_identifier"):
                bound.add(nm_n.text.decode("utf-8", errors="replace"))
            params_n = nd.child_by_field_name("parameters") or nd.child_by_field_name("parameter")
            if params_n is not None:
                _collect_js_pattern_bindings(params_n, bound)
        elif nt in ("class_declaration", "class", "interface_declaration", "type_alias_declaration", "enum_declaration"):
            nm_n = nd.child_by_field_name("name")
            if nm_n is not None:
                bound.add(nm_n.text.decode("utf-8", errors="replace"))
        elif nt == "catch_clause":
            _collect_js_pattern_bindings(nd.child_by_field_name("parameter"), bound)
        elif nt == "for_in_statement":
            _collect_js_pattern_bindings(nd.child_by_field_name("left"), bound)
        elif nt == "assignment_expression":
            lhs_n = nd.child_by_field_name("left")
            if lhs_n is not None and lhs_n.type in ("identifier", "array_pattern", "object_pattern"):
                _collect_js_pattern_bindings(lhs_n, bound)
        elif nt == "call_expression":
            fn_n = nd.child_by_field_name("function")
            if fn_n is not None and fn_n.type == "identifier":
                c_txt = fn_n.text.decode("utf-8", errors="replace")
                if _JS_SETTER_IDENT_RE.match(c_txt) and c_txt not in _JS_BUILTIN_SETTERS:
                    bare_setter_calls.append((c_txt, fn_n.start_point[0] + 1))

        ch_list = nd.children
        if ch_list:
            stack.extend(reversed(ch_list))
    return bound, bare_setter_calls


def _analyze_js_ts_ast_contracts(lines: list[str], ext: str, tree: object | None = None) -> list[dict]:
    """Universal JS/TS/React static AST & lexical-closure contract analyzer:
      1. `premature_effect_event_handler`: Callback `H` bound to JSX `on[A-Z]*` prop is also invoked
         synchronously inside `useEffect` on mount/state-change.
      2. `stale_hook_closure`: `useEffect(..., [])` / `useCallback(..., [])` with empty deps registers
         `addEventListener` / `setInterval` invoking a local closure that reads mutable `useState` state.
      3. `param_outer_state_conflict`: Function `f(p)` guards on `p === stateVar` (proving `p != stateVar`
         is possible) while unconditionally calling a 0-arg sibling helper `g()` that mutates `setStateVar`
         from `stateVar` instead of `p`.
      4. `missing_required_argument`: A function whose declaration is visible in the same file is called
         with fewer arguments than its REQUIRED parameters (no `?`, default, or rest).
      5. `listener_cleanup_mismatch`: Inside an effect whose cleanup removes listeners, an
         `addEventListener(evt, h)` pair is never removed with the same function identity.
      6. `undeclared_state_setter`: Inside a component with `useState` hooks, a bare `set[A-Z]*` call
         references an identifier with zero lexical declaration/import/prop binding in the file.
    """
    if ext.lower() not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs") or not lines:
        return []

    n = len(lines)
    state_vars: dict[str, tuple[str, int]] = {}
    local_fns: dict[str, dict] = {}
    effect_blocks: list[dict] = []
    rel_imported_syms: set[str] = set()
    jsx_handlers: dict[str, tuple[str, int]] = {}

    full_text = "\n".join(lines)
    if "import " in full_text:
        for m_imp in _JS_REL_IMP_RE.finditer(full_text):
            for spec in m_imp.group(1).split(","):
                tok = spec.strip().split(" as ")[-1].strip()
                if _JS_LC_IDENT_RE.match(tok):
                    rel_imported_syms.add(tok)

    if "={" in full_text or "= {" in full_text:
        for m_jh in _JS_JSX_HANDLER_RE.finditer(full_text):
            prop_nm, h_nm = m_jh.group(1), m_jh.group(2)
            if prop_nm in ("onMouseEnter", "onMouseLeave", "onFocus", "onBlur"):
                continue
            if h_nm in ("true", "false", "null", "undefined", "e", "ev", "event", "if", "const", "let", "return") or (
                h_nm.lower().startswith(_JS_IGNORE_HANDLERS)
            ):
                continue
            jsx_ln = 1 + full_text.count("\n", 0, m_jh.start())
            jsx_handlers.setdefault(h_nm, (prop_nm, jsx_ln))

    for i, ln in enumerate(lines):
        lnum = i + 1
        st = ln.strip()
        if not st or st.startswith(("//", "/*")):
            continue

        if "useState" in ln:
            for m_st in _JS_USESTATE_LN_RE.finditer(ln):
                state_vars[m_st.group(1)] = (m_st.group(2), lnum)
        if "use" in st and "(" in st:
            m_eff = _JS_EFF_LN_RE.search(st)
            if m_eff:
                kind = m_eff.group(1)
                end_i = _find_balanced_end(lines, i, "(", ")", max_scan=300)
                eff_body = "\n".join(lines[i : end_i + 1])
                tail = " ".join(lines[max(i, end_i - 4) : end_i + 1])
                m_deps = _JS_DEPS_TAIL_RE.search(tail)
                deps_str = m_deps.group(1).strip() if m_deps is not None else None
                effect_blocks.append({
                    "kind": kind,
                    "start_line": lnum,
                    "end_line": end_i + 1,
                    "deps": deps_str,
                    "body": eff_body,
                })

        if "{" in st and st.startswith(("export", "async", "function", "const")):
            m_fn = _JS_FN_DECL_LN_RE.match(st)
            if m_fn:
                fn_nm = m_fn.group(1) or m_fn.group(3)
                raw_params = (m_fn.group(2) if m_fn.group(1) else m_fn.group(4)) or ""
                if fn_nm and fn_nm not in ("if", "for", "while", "switch", "catch"):
                    end_i = _find_balanced_end(lines, i, "{", "}", max_scan=2600)
                    span = (end_i + 1) - lnum
                    if span <= 220:
                        parsed_params: list[tuple[str, bool]] = []
                        params_unparsed = False
                        if raw_params.strip():
                            split_params = _split_top_level_params(raw_params)
                            if split_params is None:
                                params_unparsed = True
                                split_params = []
                            for p_clean in split_params:
                                name_part = p_clean.split(":")[0]
                                is_rest = p_clean.startswith("...")
                                has_opt = is_rest or ("=" in p_clean.replace("=>", "")) or ("?" in name_part)
                                m_pid = _JS_PARAM_ID_RE.match(p_clean)
                                if m_pid:
                                    parsed_params.append((m_pid.group(1), has_opt))
                                else:
                                    parsed_params.append(("<destructured>", has_opt))
                        local_fns[fn_nm] = {
                            "name": fn_nm,
                            "params": parsed_params,
                            "params_unparsed": params_unparsed,
                            "start_line": lnum,
                            "end_line": end_i + 1,
                            "span": span,
                            "body": "\n".join(lines[i : end_i + 1]),
                        }

    violations: list[dict] = []

    # Law 1: Premature Effect-Driven Event Handler Invocation
    for eff in effect_blocks:
        if eff["kind"] not in ("useEffect", "useLayoutEffect"):
            continue
        eff_s, eff_e = eff["start_line"], eff["end_line"]
        nested_fn_spans = [
            (fn["start_line"], fn["end_line"])
            for fn in local_fns.values()
            if eff_s < fn["start_line"] and fn["end_line"] <= eff_e
        ]
        for ln_idx in range(eff_s - 1, min(n, eff_e)):
            call_ln = ln_idx + 1
            if any(ns <= call_ln <= ne for ns, ne in nested_fn_spans):
                continue
            ln_txt = lines[ln_idx]
            st_ln = ln_txt.strip()
            if not st_ln or st_ln.startswith("//") or "addEventListener" in st_ln or "removeEventListener" in st_ln or st_ln.startswith("return"):
                continue
            for h_nm, (prop_nm, jsx_ln) in jsx_handlers.items():
                if not (jsx_ln < eff_s or jsx_ln > eff_e):
                    continue
                if re.search(rf"\b{re.escape(h_nm)}\s*\(", st_ln):
                    is_mutator_or_cb = h_nm.startswith("on") and len(h_nm) > 2 and h_nm[2].isupper()
                    if not is_mutator_or_cb and h_nm in local_fns:
                        h_body = local_fns[h_nm]["body"]
                        is_mutator_or_cb = bool(
                            re.search(r"\b(?:on[A-Z]\w*|set[A-Z]\w*|localStorage|sessionStorage)\b", h_body)
                        )
                    if is_mutator_or_cb:
                        violations.append({
                            "kind": "premature_effect_event_handler",
                            "line": call_ln,
                            "end_line": min(eff_e, call_ln + 2),
                            "extra_pin_lines": [eff_s, call_ln, jsx_ln],
                            "inline_msg": (
                                f"# >>> [6D-CONTRACT L{call_ln}] Premature effect invocation: '{h_nm}(...)' is called "
                                f"synchronously inside {eff['kind']} (L{eff_s}-L{eff_e}) on mount/state-change, "
                                f"but is also bound to interactive JSX prop {prop_nm}={{...}} at L{jsx_ln}"
                            ),
                            "summary": (
                                f"Invokes interactive callback `{h_nm}(...)` synchronously inside `{eff['kind']}` "
                                f"(`L{eff_s}-L{eff_e}`) on mount/state-change while also binding it to JSX `{prop_nm}` "
                                f"at `L{jsx_ln}`, firing post-interaction logic prematurely."
                            ),
                        })

    # Law 2: Stale Reactive Closure Capture in useEffect/useCallback with empty `[]` deps
    for eff in effect_blocks:
        if eff["deps"] != "":
            continue
        eff_s, eff_e = eff["start_line"], eff["end_line"]
        eff_body = eff["body"]
        for m_reg in re.finditer(
            r"\b(?:addEventListener\s*\(\s*['\"][^'\"]+['\"]\s*,\s*([a-zA-Z_]\w*)|setInterval\s*\(\s*([a-zA-Z_]\w*))",
            eff_body,
        ):
            cb_nm = m_reg.group(1) or m_reg.group(2)
            if cb_nm and cb_nm in local_fns:
                cb_fn = local_fns[cb_nm]
                captured = [
                    sv for sv in state_vars
                    if re.search(rf"\b{re.escape(sv)}\b", cb_fn["body"])
                ]
                if captured:
                    violations.append({
                        "kind": "stale_hook_closure",
                        "line": eff_s,
                        "end_line": eff_e,
                        "extra_pin_lines": [eff_s, cb_fn["start_line"]],
                        "inline_msg": (
                            f"# >>> [6D-CONTRACT L{eff_s}] Stale closure capture: {eff['kind']}(..., []) with empty deps "
                            f"registers '{cb_nm}' (L{cb_fn['start_line']}-L{cb_fn['end_line']}), which reads mutable "
                            f"useState variable(s) [{', '.join(captured[:4])}] frozen at initial mount"
                        ),
                        "summary": (
                            f"Registers listener/timer `{cb_nm}` inside `{eff['kind']}` with empty dependency array `[]`, "
                            f"capturing stale initial `useState` binding(s) `{', '.join(captured[:4])}` read in "
                            f"`{cb_nm}` (`L{cb_fn['start_line']}-L{cb_fn['end_line']}`)."
                        ),
                    })

    # Law 3: Parameter-vs-Outer-State Guard Divergence
    for fn_a in local_fns.values():
        if not fn_a["params"]:
            continue
        p0 = fn_a["params"][0][0]
        body_a = fn_a["body"]
        for sv, (setter_nm, _) in state_vars.items():
            if sv == p0:
                continue
            has_guard = bool(
                re.search(
                    rf"\bif\s*\([^)]*\b(?:{re.escape(p0)}\s*(?:===|!==|==|!=)\s*{re.escape(sv)}|"
                    rf"{re.escape(sv)}\s*(?:===|!==|==|!=)\s*{re.escape(p0)})\b",
                    body_a,
                )
            )
            if not has_guard:
                continue
            for m_call0 in re.finditer(r"\b([a-zA-Z_]\w*)\s*\(\s*\)", body_a):
                callee_nm = m_call0.group(1)
                if callee_nm not in local_fns or callee_nm == fn_a["name"]:
                    continue
                fn_b = local_fns[callee_nm]
                if (
                    re.search(rf"\b{re.escape(sv)}\b", fn_b["body"])
                    and re.search(rf"\b{re.escape(setter_nm)}\s*\(", fn_b["body"])
                ):
                    call_ln = fn_a["start_line"] + body_a.count("\n", 0, m_call0.start())
                    violations.append({
                        "kind": "param_outer_state_conflict",
                        "line": call_ln,
                        "end_line": fn_a["end_line"],
                        "extra_pin_lines": [fn_a["start_line"], call_ln, fn_b["start_line"]],
                        "inline_msg": (
                            f"# >>> [6D-CONTRACT L{call_ln}] Parameter-vs-State conflict: '{fn_a['name']}({p0})' guards "
                            f"on '{p0} === {sv}' (proving {p0} != {sv} is possible) but calls 0-arg '{callee_nm}()' "
                            f"(L{fn_b['start_line']}), which mutates '{setter_nm}' using outer state '{sv}' instead of '{p0}'"
                        ),
                        "summary": (
                            f"`{fn_a['name']}({p0})` guards on `{p0} === {sv}` (allowing `{p0} !== {sv}`) "
                            f"but invokes 0-arg `{callee_nm}()` (`L{fn_b['start_line']}-L{fn_b['end_line']}`), "
                            f"which computes `{setter_nm}(...)` from outer state `{sv}` instead of parameter `{p0}`."
                        ),
                    })

    # Law 4: Required-Parameter Arity Violation (O(L) single-pass call-line index)
    arity_targets: dict[str, dict] = {}
    for fn_h in local_fns.values():
        if fn_h.get("params_unparsed"):
            continue
        req_cnt = 0
        for _p_nm, p_opt in fn_h["params"]:
            if p_opt:
                break
            req_cnt += 1
        if req_cnt >= 2:
            arity_targets[fn_h["name"]] = {**fn_h, "required": req_cnt}

    if arity_targets:
        cand_lines_by_sym: dict[str, list[int]] = {k: [] for k in arity_targets}
        _CALL_IDENT_PAT = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
        for idx_ln, ln_txt in enumerate(lines):
            if "(" not in ln_txt:
                continue
            st_ln = ln_txt.strip()
            if st_ln.startswith(("import ", "//", "/*", "*")):
                continue
            for m_ci in _CALL_IDENT_PAT.finditer(ln_txt):
                cnm = m_ci.group(1)
                if cnm in cand_lines_by_sym:
                    cand_lines_by_sym[cnm].append(idx_ln)

        for sym_nm, fn_h in arity_targets.items():
            required = fn_h["required"]
            decl_s, decl_e = fn_h["start_line"], fn_h["end_line"]
            short_calls: list[tuple[int, int, str]] = []
            full_call: tuple[int, str] | None = None
            sym_rx = re.compile(rf"(?<![.\w$]){re.escape(sym_nm)}\s*\(")
            for idx_ln in set(cand_lines_by_sym[sym_nm]):
                c_ln = idx_ln + 1
                if decl_s <= c_ln <= decl_e and c_ln == decl_s:
                    continue
                ln_txt = lines[idx_ln]
                for m_c in sym_rx.finditer(ln_txt):
                    if re.search(r"\bfunction\s*$", ln_txt[: m_c.start()]):
                        continue
                    open_pos = ln_txt.find("(", m_c.start())
                    combined_call = "\n".join(lines[idx_ln : min(n, idx_ln + 4)])[open_pos:]
                    end_pos = _balanced_paren_end(combined_call)
                    if end_pos <= 0:
                        continue
                    inner_args = combined_call[1:end_pos]
                    if "..." in inner_args:
                        continue
                    n_args = _count_call_args(inner_args)
                    snip = re.sub(r"\s+", " ", f"{sym_nm}({inner_args.strip()})")[:64]
                    if n_args < required:
                        short_calls.append((c_ln, n_args, snip))
                    elif full_call is None:
                        full_call = (c_ln, snip)
            short_calls.sort(key=lambda x: x[0])
            for c_ln, n_args, snip in short_calls[:2]:
                missing = [p for p, _ in fn_h["params"][n_args:required]]
                violations.append({
                    "kind": "missing_required_argument",
                    "symbol": sym_nm,
                    "line": c_ln,
                    "end_line": c_ln + 1,
                    "extra_pin_lines": [c_ln, decl_s] + ([full_call[0]] if full_call else []),
                    "inline_msg": (
                        f"# >>> [6D-CONTRACT L{c_ln}] Arity: '{sym_nm}' declares {required} required param(s) at L{decl_s} "
                        f"but is called with {n_args}; required param(s) [{', '.join(missing)}] will be undefined"
                    ),
                    "summary": (
                        f"`{snip}` passes {n_args} arg(s) but `{sym_nm}` (`L{decl_s}`) declares {required} required "
                        f"parameter(s); `{', '.join(missing)}` will be `undefined`."
                    ),
                })

    # Law 5: Event-Listener Registration / Cleanup Pairing
    _ADD_PAT = re.compile(r"\.addEventListener\(\s*(['\"])([^'\"]+)\1\s*,\s*([A-Za-z_$][\w$]*)\s*[,)]")
    _REM_PAT = re.compile(r"\.removeEventListener\(\s*(['\"])([^'\"]+)\1\s*,\s*([A-Za-z_$][\w$]*)\s*[,)]")
    for eff in effect_blocks:
        if eff["kind"] not in ("useEffect", "useLayoutEffect"):
            continue
        body = eff["body"]
        adds = [(m.group(2), m.group(3), eff["start_line"] + body.count("\n", 0, m.start())) for m in _ADD_PAT.finditer(body)]
        rems = [(m.group(2), m.group(3), eff["start_line"] + body.count("\n", 0, m.start())) for m in _REM_PAT.finditer(body)]
        if not adds or not rems:
            continue
        removed_pairs = {(ev, h) for ev, h, _ in rems}
        for ev, h, add_ln in adds:
            if (ev, h) in removed_pairs:
                continue
            wrong = [(rh, r_ln) for rev, rh, r_ln in rems if rev == ev and rh != h]
            if wrong:
                rh, r_ln = wrong[0]
                detail = f"cleanup at L{r_ln} removes '{ev}' with a different function '{rh}'"
                pins = [add_ln, r_ln]
            else:
                detail = f"the effect cleanup removes other listeners but never '{ev}'/'{h}'"
                pins = [add_ln, rems[0][2]]
            violations.append({
                "kind": "listener_cleanup_mismatch",
                "symbol": h,
                "line": add_ln,
                "end_line": add_ln + 1,
                "extra_pin_lines": pins,
                "inline_msg": (
                    f"# >>> [6D-CONTRACT L{add_ln}] Listener leak: addEventListener('{ev}', {h}) is never removed with "
                    f"the same function; {detail}"
                ),
                "summary": (
                    f"`addEventListener('{ev}', {h})` at `L{add_ln}` inside {eff['kind']} (`L{eff['start_line']}-L{eff['end_line']}`) "
                    f"is never detached: {detail}. Each effect re-run stacks another live listener."
                ),
            })

    # Law 6: Undeclared React State Setter Call (`setFoo(...)` typo / stale refactor in monolith components)
    if state_vars and "eval(" not in full_text and "with (" not in full_text:
        ts_tree = tree
        if ts_tree is None:
            js_parser = get_parser(ext.lower())
            if js_parser is not None:
                try:
                    ts_tree = js_parser.parse(full_text.encode("utf-8", errors="replace"))
                except Exception:
                    ts_tree = None
        if ts_tree is not None and not getattr(ts_tree.root_node, "has_error", False):
            bound_idents, bare_setter_calls = _collect_js_file_bindings_and_bare_setter_calls(ts_tree.root_node)
            if bare_setter_calls:
                import difflib
                declared_setters = [
                    (sv_var, sv_set, sv_ln)
                    for sv_var, (sv_set, sv_ln) in state_vars.items()
                ]
                emitted_per_sym: dict[str, int] = {}
                for callee_nm, call_ln in bare_setter_calls:
                    if callee_nm in bound_idents:
                        continue
                    if emitted_per_sym.get(callee_nm, 0) >= 2:
                        continue
                    emitted_per_sym[callee_nm] = emitted_per_sym.get(callee_nm, 0) + 1
                    core_low = callee_nm[3:].lower()
                    best_match: tuple[float, str, str, int] | None = None
                    for sv_var, sv_set, sv_ln in declared_setters:
                        cand_core = sv_set[3:].lower() if sv_set.startswith("set") else sv_set.lower()
                        ratio = difflib.SequenceMatcher(None, core_low, cand_core).ratio()
                        if core_low and (cand_core.endswith(core_low) or core_low.endswith(cand_core)):
                            ratio = max(ratio, 0.88)
                        if best_match is None or ratio > best_match[0]:
                            best_match = (ratio, sv_var, sv_set, sv_ln)
                    if best_match and best_match[0] >= 0.45:
                        _, b_var, b_set, b_ln = best_match
                        pins = [call_ln, b_ln]
                        hint_inline = f" (did you mean '{b_set}' from [{b_var}, {b_set}] at L{b_ln}?)"
                        hint_md = f"; closest declared hook setter is `{b_set}` (`[{b_var}, {b_set}] = useState(...)` at `L{b_ln}`)"
                    else:
                        pins = [call_ln]
                        hint_inline = ""
                        hint_md = ""
                    violations.append({
                        "kind": "undeclared_state_setter",
                        "symbol": callee_nm,
                        "line": call_ln,
                        "end_line": call_ln + 1,
                        "extra_pin_lines": pins,
                        "inline_msg": (
                            f"# >>> [6D-CONTRACT L{call_ln}] Undeclared state setter: '{callee_nm}(...)' is never "
                            f"declared, imported, or passed as a prop in this module (ReferenceError at runtime){hint_inline}"
                        ),
                        "summary": (
                            f"Calls undeclared state setter `{callee_nm}(...)` at `L{call_ln}` "
                            f"(throws `ReferenceError` at runtime){hint_md}."
                        ),
                    })

    return violations


_4D_SPAN_NODE_TYPES = frozenset({
    "function_declaration", "arrow_function", "function_definition",
    "method_definition", "call_expression", "if_statement", "elif_clause",
    "else_clause", "try_statement", "except_clause", "catch_clause",
    "with_statement", "for_statement", "for_in_statement", "while_statement",
    "match_statement", "case_clause", "switch_case", "switch_default",
    "jsx_expression", "ternary_expression", "conditional_expression",
})
_4D_ASSIGN_NODE_TYPES = frozenset({
    "assignment_expression", "augmented_assignment_expression",
    "update_expression", "assignment", "augmented_assignment",
})
_4D_JS_RES_BUILTINS = frozenset({
    "ok", "status", "statusText", "headers", "json", "text", "blob", "clone",
    "then", "catch", "finally", "length", "map", "filter", "forEach", "slice",
    "toString", "valueOf", "error", "errors", "detail", "message", "data",
})
_4D_W_CALL_NOISE = frozenset({
    "if", "for", "while", "switch", "catch", "return", "function",
    "useEffect", "useState", "useMemo", "useCallback",
})
_4D_WORD_RE = re.compile(r"\w+")
_4D_IDENT_FULL_RE = re.compile(r"^[a-zA-Z_]\w*$")
_4D_SIMPLE_IDENT_RE = re.compile(r"^\w+$")
_4D_PROP_LHS_RE = re.compile(r"^(?:self|this|state|store|draft|window|globalThis)\.([a-zA-Z_]\w*)")
_4D_USESTATE_RE = re.compile(r"const\s+\[\s*(\w+)\s*,\s*(\w+)\s*\]\s*=\s*useState")
_4D_USEREF_RE = re.compile(r"const\s+(\w+)\s*=\s*useRef\b")
_4D_SELF_ATTR_RE = re.compile(r"\bself\.([a-zA-Z_]\w*)\s*=")
_4D_USEHOOK_RE = re.compile(r"const\s+\{([^}]+)\}\s*=\s*(?:use[A-Z]\w*|useStore)\s*\(")
_4D_HOOK_ACT_RE = re.compile(r"^(?:set|update|add|remove|delete|toggle|clear|reset|mark)([A-Z]\w*)$")
_4D_ZUSTAND_RE = re.compile(r"const\s+(\w+)\s*=\s*use\w*Store\s*\([^)]*=>\s*\w+\.(\w+)")
_4D_ZUSTAND_ACT_RE = re.compile(r"^(?:set|update|add|remove|delete|toggle|clear|reset)([A-Z]\w*)$")

_4D_CREATE_TBL_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[a-zA-Z_]\w*\.)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s*\(",
    re.IGNORECASE,
)
_4D_USESTATE_CALL_RE = re.compile(r"\buse(?:State|Ref)\s*(?:<[^>]*>)?\s*\(")
_4D_USESTATE_NXT_RE = re.compile(r"\buse(?:State|Ref)\b")
_4D_ST_DECL_RE = re.compile(
    r"^([A-Z][A-Z0-9_]{2,}|[a-z_][a-z0-9_]*)\s*(?::\s*[^=]+)?=\s*(?:([A-Z][A-Za-z0-9_]*|[a-z_][a-z0-9_]*)\s*\(|(\{))"
)
_4D_UPPER_CFG_STR_RE = re.compile(r"['\"][A-Z][A-Z0-9_]{2,40}['\"]")
_4D_EFF_RE = re.compile(r"\b(useEffect|useLayoutEffect|useMemo|useCallback)\s*\(")
_4D_DEPS_RE = re.compile(r"\[([^\]]*)\]\s*\)")
_4D_CASE_HDR_RE = re.compile(r"^(?:case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:)+")
_4D_CASE_LBL_RE = re.compile(r"case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:")
_4D_CASE_NXT_RE = re.compile(r"^case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:")
_4D_CASE_BRK_RE = re.compile(r"^(?:case\s+|default\s*:)")
_4D_FN_HDR_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|def)\s+([a-zA-Z_]\w*)"
    r"|^(?:export\s+)?const\s+([a-zA-Z_]\w*)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::\s*[^=]+)?=>\s*\{"
    r"|^(?:public\s+|private\s+|protected\s+)?(?:async\s+)?([a-zA-Z_]\w*)\s*\([^;]*\)\s*(?::\s*[^{]+)?\{"
)
_4D_FN_IO_RE = re.compile(
    r"\b(?:fetch|axios|setItem|getItem|[a-z]+Doc|execute|commit|prisma|supabase|dispatch|requests\.|httpx\.|aiohttp\.|redis\.|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+|\w+\.(?:set|put|write|save|update|upsert)\s*\()\b",
    re.IGNORECASE,
)
_4D_FN_VIEW_RE = re.compile(r"(?:</[A-Za-z]|/>)")
_4D_IF_HDR_RE = re.compile(r"^(?:\}\s*)?(?:else\s+if|elif|if)\s*\(?([^):{]{3,75})\)?\s*[{:]\s*(?:#.*)?$")
_4D_IF_IO_RE = re.compile(
    r"\b(?:fetch|axios|setItem|getItem|execute|commit|prisma|supabase|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+)\b",
    re.IGNORECASE,
)
_4D_WITH_HDR_RE = re.compile(r"^(?:async\s+)?with\s+([^:]{3,65}):\s*(?:#.*)?$")
_4D_WITH_IO_RE = re.compile(
    r"\b(?:tx|db|conn|cursor|open|lock|session|execute|INSERT\s+INTO|UPDATE\s+|SELECT\s+)\b",
    re.IGNORECASE,
)
_4D_TRY_HDR_RE = re.compile(r"^(?:try\s*[:{]|(?:except|catch)\s*\(?([^):{]{0,50})\)?\s*[:{])\s*(?:#.*)?$")
_4D_TRY_IO_RE = re.compile(
    r"\b(?:execute|commit|INSERT\s+INTO|UPDATE\s+|DELETE\s+FROM|SELECT\s+|fetch|axios)\b",
    re.IGNORECASE,
)
_4D_EXEC_HDR_RE = re.compile(r"\b(?:conn|cur|cursor|db|session|pool|client|tx)\.(?:execute|executemany|query)\s*\(")
_4D_EXEC_OP_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|SELECT\b[\s\S]{1,100}?\bFROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([a-zA-Z_][a-zA-Z0-9_]{2,})",
    re.IGNORECASE,
)
_4D_JSX_HDR_RE = re.compile(r"^\{\s*([a-zA-Z0-9_!.?\s&=|]{3,45})\s*(?:&&|\?)\s*\(")
_4D_WS_RE = re.compile(r"\s+")
_4D_MAP_HDR_RE = re.compile(r"\{\s*([a-zA-Z_][\w.]*)\.map\s*\(")
_4D_MAP_VIEW_RE = re.compile(r"(?:</[A-Za-z]|/>|<[A-Z])")

_4D_ORM_RE = re.compile(
    r"\b(?:prisma\.(\w+)\.(?:create|update|upsert|delete)|supabase\.from\(\s*['\"](\w+)['\"]\s*\)\.(?:insert|update|upsert|delete)|(\w+Store)\.setState)\b"
)
_4D_SQL_W_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\b",
    re.IGNORECASE,
)
_4D_SQL_R_RE = re.compile(
    r"\bSELECT\b[\s\S]{1,120}?\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\b",
    re.IGNORECASE,
)
_4D_CFG_KEY_RE = re.compile(r"['\"]([A-Z][A-Z0-9_]{2,40})['\"]")
_4D_KV_RE = re.compile(
    r"(?:getItem|setItem|removeItem|[a-zA-Z_][\w.]*\.(?:get|set|put|write|update|delete|setdefault|pop|setItem|getItem)|os\.getenv|getattr|setattr|[A-Z][A-Za-z0-9_]*(?:Attribute|Property|Descriptor))\(\s*['\"]([a-zA-Z0-9_.\-:]{2,40})['\"]"
)
_4D_DICT_K_RE = re.compile(
    r"\b(?:[a-zA-Z_][\w.]*)\[\s*['\"]([a-zA-Z_][a-zA-Z0-9_.\-:]{2,40})['\"]\s*\](\s*=[^=])?"
)
_4D_EVT_W_RE = re.compile(r"\b(?:emit|publish|trigger|broadcast)\(\s*['\"]([a-zA-Z0-9_.:-]{3,40})['\"]")
_4D_EVT_R_RE = re.compile(r"\b(?:on|once|subscribe|addEventListener|listen)\(\s*['\"]([a-zA-Z0-9_.:-]{3,40})['\"]")
_4D_PY_ATTR_R_RE = re.compile(r"\b(?:self|cls|app|current_app|config|settings|request|session|g)\.([a-zA-Z_]\w*)\b(?!\s*\()")
_4D_RAISE_RE = re.compile(r"\b(?:raise|throw\s+new|throw)\s+([A-Za-z_]\w*)")
_4D_CATCH_RE = re.compile(r"\b(?:except|catch)\s*\(?([A-Za-z_]\w*)")
_4D_STATUS_RE = re.compile(
    r"\b(?:ApiError|HTTPException|status|status_code)\s*[\(=]\s*(?:status_code\s*=\s*)?(4\d\d|5\d\d)\b"
    r"(?:\s*,\s*['\"]([a-zA-Z0-9_\-]+)['\"])?"
)
_4D_GUARD_RET_RE = re.compile(
    r"\bif\b[^{:\n]*[:{]?\s*\n?\s*return\b(?:\s+(?:None|False|null|false|undefined|redirect\b))?"
)
_4D_REACTIVE_RE = re.compile(r"\b(?:useEffect|useLayoutEffect|onSnapshot|addEventListener|subscribe|on_event)\b")
_4D_TIMER_RE = re.compile(r"\b(?:setTimeout|setInterval|requestAnimationFrame|asyncio\.sleep)\b")
_4D_ASYNC_RE = re.compile(r"\b(?:async\s+def|async\s+function|async\s*\(|await\s+|Promise\.|\.then\()\b")
_4D_JSX_COMP_RE = re.compile(r"<([A-Z][a-zA-Z0-9_]+)")
_4D_W_CALLS_RE = re.compile(r"\b([a-z][a-zA-Z0-9_]{3,})\s*\(")
_4D_SQL_INS_RE = re.compile(
    r"\bINSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(([^)]+)\)\s*(?:VALUES|SELECT)\b",
    re.IGNORECASE,
)
_4D_SQL_COL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_4D_DIALECT_INTERVAL_RE = re.compile(r"\binterval\s+'[^']+'", re.IGNORECASE)
_4D_DIALECT_MAKE_INT_RE = re.compile(r"\bmake_interval\s*\(", re.IGNORECASE)
_4D_DIALECT_ANY_RE = re.compile(r"=\s*ANY\s*\(\s*(?:%s|\$|\?)", re.IGNORECASE)
_4D_DIALECT_ILIKE_RE = re.compile(r"\bILIKE\b")
_4D_DIALECT_CAST_RE = re.compile(r"::(?:uuid|jsonb|timestamptz|inet|text\[\]|int\[\])\b", re.IGNORECASE)
_4D_JSON_VAR_RE = re.compile(r"\b(?:const|let|var)\s+([a-zA-Z_]\w*)\s*=\s*await\s+\w+\.json\(\)")
_4D_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*(?:[a-zA-Z_$][\w$.]*\s*\+\s*)?[`'\"](?:\$\{[^{}`]+\})*(\/[a-zA-Z0-9_/\-${}:.]+)[`'\"](?:\s*,\s*\{([\s\S]{0,260}?)\})?"
)
_4D_FETCH_METHOD_RE = re.compile(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]")
_4D_URL_PARAM_RE = re.compile(r"\$\{[^}]+\}|:[a-zA-Z_]\w*")
_4D_HTTP_PY_RE = re.compile(
    r"\b(?:requests|httpx|client)\.(get|post|put|delete|patch)\s*\(\s*(?:[a-zA-Z_]\w*\s*\+\s*)?f?[`'\"](?:\{[^{}]+\}|\$\{[^{}`]+\})*(\/[a-zA-Z0-9_/\-${}:.]+)[`'\"]",
    re.IGNORECASE,
)
_4D_HTTP_JS_RE = re.compile(
    r"\b(?:axios|api|client|http|httpClient|apiClient)\.(get|post|put|delete|patch)\s*\(\s*(?:[a-zA-Z_$][\w$.]*\s*\+\s*)?[`'\"](?:\$\{[^{}`]+\})*(\/[a-zA-Z0-9_/\-${}:.]+)[`'\"]",
    re.IGNORECASE,
)
_4D_HTTP_WRAPPER_RE = re.compile(
    r"\b(?:apiRequest|apiFetch|request|fetchJson|httpRequest|(post|get|put|patch|delete)(?:Json|Api|Request)?)\s*\(\s*(?:[a-zA-Z_$][\w$.]*\s*\+\s*)?[`'\"](?:\$\{[^{}`]+\})*(\/[a-zA-Z0-9_/\-${}:.]+)[`'\"](?:\s*,\s*\{([\s\S]{0,260}?)\})?",
)
_4D_URL_VAR_DECL_RE = re.compile(
    r"\b(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:[a-zA-Z_$][\w$.]*\s*\+\s*)?[`'\"](?:\$\{[^{}`]+\})*(\/[a-zA-Z0-9_/\-${}:.]+)[`'\"]"
)
_4D_FETCHONE_RE = re.compile(
    r"\b([a-zA-Z_]\w*)\s*=\s*[\w.]+\.execute\(\s*(?:f?['\"]{1,3})\s*SELECT\s+([\s\S]+?)\s+FROM\s+([a-zA-Z_]\w*)\b[\s\S]*?\)\.fetchone\(\)",
    re.IGNORECASE,
)
_4D_SQL_AS_RE = re.compile(r"\bAS\s+([a-zA-Z_]\w*)\s*$", re.IGNORECASE)
_4D_RET_DICT_RE = re.compile(r"\breturn\s*\{([^{};]+)\}")
_4D_DICT_KEY_COLON_RE = re.compile(r"['\"]([a-zA-Z_]\w*)['\"]\s*:")


def extract_4d_coordinate_matrix(
    lines: list[str],
    line_start: int = 1,
    line_end: int | None = None,
    ext: str = ".tsx",
    tree: object | None = None,
) -> list[dict]:
    """Decompose a file or giant function into a 4D Coordinate Matrix of atomic sub-blocks:
      - X_scope: structural scope / branch guard (case '...', if (...), useEffect[deps], fn:name, jsx[cond])
      - Y_role: execution phase (STATE | EFFECT | ACTION | IO | VIEW | SCHEMA)
      - Z_write: state variables or storage keys mutated inside the block
      - Z_read: state variables or storage keys read inside the block
      - W_calls: child components rendered (<Comp>) or key functions invoked
    Uses Tree-sitter AST span & assignment extraction combined with universal hook/store/JSX patterns.
    """
    n = len(lines)
    s_idx = max(0, line_start - 1)
    e_idx = min(n, line_end if line_end is not None else n)
    ext_l = ext.lower()

    ast_span_by_start: dict[int, int] = {}
    ast_mutations_by_line: dict[int, list[str]] = {}
    parser = get_parser(ext_l)
    if tree is None and parser is not None and n > 0:
        try:
            src_bytes = "\n".join(lines).encode("utf-8", errors="replace")
            tree = parser.parse(src_bytes)
        except Exception:
            tree = None
    py_ast_contracts: dict[int, dict] = _analyze_python_ast_contracts(lines) if ext_l == ".py" else {}
    js_ast_contracts: list[dict] = (
        _analyze_js_ts_ast_contracts(lines, ext, tree=tree)
        if ext_l in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
        else []
    )
    if tree is not None and n > 0:
        try:
            stack = [tree.root_node]
            while stack:
                nd = stack.pop()
                nd_type = nd.type
                if nd_type in _4D_SPAN_NODE_TYPES:
                    s_ln = nd.start_point[0] + 1
                    e_ln = nd.end_point[0] + 1
                    if e_ln > s_ln:
                        prev_e = ast_span_by_start.get(s_ln, 0)
                        if (e_ln - s_ln) <= 2500 and e_ln > prev_e:
                            ast_span_by_start[s_ln] = e_ln
                elif nd_type in _4D_ASSIGN_NODE_TYPES:
                    s_ln = nd.start_point[0] + 1
                    lhs = nd.child_by_field_name("left") or (nd.children[0] if nd.children else None)
                    if lhs is not None:
                        lhs_txt = lhs.text.decode("utf-8", errors="replace").strip()
                        m_prop = _4D_PROP_LHS_RE.match(lhs_txt)
                        if m_prop:
                            prop_name = m_prop.group(1)
                            if not prop_name.startswith("_"):
                                ast_mutations_by_line.setdefault(s_ln, []).append(prop_name)
                if nd.named_child_count:
                    stack.extend(c for c in nd.named_children if c.named_child_count)
        except Exception:
            pass

    state_vars: dict[str, str] = {}
    self_writes_by_line: dict[int, list[str]] = {}
    file_has_sql = False
    file_has_orm = False
    for ln_idx, ln in enumerate(lines, 1):
        if not file_has_sql and any(kw in ln or kw.lower() in ln for kw in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "INTERVAL ")):
            file_has_sql = True
        if not file_has_orm and ("prisma." in ln or "supabase." in ln or ".setState" in ln):
            file_has_orm = True
        if "useState" in ln:
            for m_st in _4D_USESTATE_RE.finditer(ln):
                state_vars[m_st.group(1)] = m_st.group(2)
        if "useRef" in ln:
            for m_rf in _4D_USEREF_RE.finditer(ln):
                rv = m_rf.group(1)
                state_vars[rv] = f"{rv}\\.current\\s*="
        if "self." in ln and "=" in ln:
            for m_py in _4D_SELF_ATTR_RE.finditer(ln):
                pv = m_py.group(1)
                if not pv.startswith("_"):
                    state_vars.setdefault(pv, "__self_attr__")
                    self_writes_by_line.setdefault(ln_idx, []).append(pv)
        if "const" in ln and "use" in ln:
            m_hk = _4D_USEHOOK_RE.search(ln)
            if m_hk:
                for raw_tok in m_hk.group(1).split(","):
                    tok = raw_tok.split(":")[-1].split("=")[0].strip()
                    if not _4D_IDENT_FULL_RE.match(tok):
                        continue
                    m_act = _4D_HOOK_ACT_RE.match(tok)
                    if m_act:
                        target_k = m_act.group(1)[0].lower() + m_act.group(1)[1:]
                        state_vars[target_k] = tok
                    elif tok == "dispatch":
                        state_vars["dispatch"] = "dispatch"
                    elif tok not in state_vars:
                        state_vars[tok] = f"__readonly_{tok}__"
            m_zs = _4D_ZUSTAND_RE.search(ln)
            if m_zs:
                local_nm = m_zs.group(1)
                m_act = _4D_ZUSTAND_ACT_RE.match(local_nm)
                if m_act:
                    target_k = m_act.group(1)[0].lower() + m_act.group(1)[1:]
                    state_vars[target_k] = local_nm
                elif local_nm not in state_vars:
                    state_vars[local_nm] = f"__readonly_{local_nm}__"

    def _py_block_end(start_i: int, max_scan: int = 400) -> int:
        base_ind = len(lines[start_i]) - len(lines[start_i].lstrip())
        end_k = start_i
        for k in range(start_i + 1, min(e_idx, start_i + max_scan)):
            k_s = lines[k].strip()
            if not k_s or k_s.startswith("#"):
                continue
            if (len(lines[k]) - len(lines[k].lstrip())) <= base_ind:
                break
            end_k = k
        return end_k

    raw_blocks: list[tuple[str, str, int, int]] = []
    i = s_idx
    while i < e_idx:
        ln = lines[i]
        st = ln.strip()
        lnum = i + 1
        if not st or st.startswith(("//", "#", "/*", "--")):
            i += 1
            continue

        if ("CREATE" in st or "create" in st) and ("TABLE" in st or "table" in st):
            m_ct = _4D_CREATE_TBL_RE.search(st)
            if m_ct:
                tbl_nm = m_ct.group(1)
                end_i = _find_balanced_end(lines, i, "(", ")", max_scan=120)
                raw_blocks.append(("SCHEMA", f"table:{tbl_nm}", lnum, end_i + 1))
                i = end_i + 1
                continue
        if "use" in st and "(" in st and _4D_USESTATE_CALL_RE.search(st):
            b_start = lnum
            while i + 1 < e_idx:
                nxt = lines[i + 1].strip()
                if (nxt and "use" in nxt and _4D_USESTATE_NXT_RE.search(nxt)) or not nxt or nxt.startswith("//"):
                    i += 1
                else:
                    break
            raw_blocks.append(("STATE", "state_declarations", b_start, i + 1))
            i += 1
            continue

        if "=" in st and (len(ln) - len(ln.lstrip()) <= 4):
            m_st_decl = _4D_ST_DECL_RE.match(st)
            if m_st_decl:
                attr_nm = m_st_decl.group(1)
                rhs_fn = m_st_decl.group(2) or ""
                has_upper_cfg_str = bool(_4D_UPPER_CFG_STR_RE.search(st))
                is_descriptor_call = bool(
                    rhs_fn
                    and (
                        has_upper_cfg_str
                        or rhs_fn.endswith(("Attribute", "Property", "Descriptor", "Field", "Column", "Dict"))
                        or rhs_fn in ("field", "column", "mapped_column", "property")
                    )
                )
                if attr_nm.isupper() or "config" in attr_nm.lower() or is_descriptor_call:
                    if "{" in st and "}" not in st:
                        end_i = _find_balanced_end(lines, i, "{", "}", max_scan=90)
                    elif "(" in st and ")" not in st:
                        end_i = _find_balanced_end(lines, i, "(", ")", max_scan=90)
                    else:
                        end_i = i
                    raw_blocks.append(("STATE", f"state:{attr_nm}", lnum, end_i + 1))
                    i = end_i + 1
                    continue

        if "use" in st and "(" in st:
            m_eff = _4D_EFF_RE.search(st)
            if m_eff:
                kind = m_eff.group(1)
                end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "(", ")", max_scan=300)
                tail = " ".join(lines[max(i, end_i - 3) : end_i + 1])
                m_deps = _4D_DEPS_RE.search(tail)
                deps = m_deps.group(1).strip() if m_deps else ""
                role = "EFFECT" if "Effect" in kind else "ACTION"
                raw_blocks.append((role, f"{kind}[{deps[:36]}]", lnum, end_i + 1))
                i = (end_i + 1) if (end_i - i) < 12 else (i + 1)
                continue

        if st.startswith("case"):
            m_case = _4D_CASE_HDR_RE.match(st)
            if m_case:
                case_labels = _4D_CASE_LBL_RE.findall(st)
                while i + 1 < e_idx and _4D_CASE_NXT_RE.match(lines[i + 1].strip()):
                    i += 1
                    case_labels.extend(_4D_CASE_LBL_RE.findall(lines[i].strip()))
                lbl_str = "|".join(c.strip("'\"") for c in case_labels[:3])
                b_depth = 0
                p_depth = 0
                end_i = i
                for k in range(i, min(e_idx, i + 2500)):
                    k_st = lines[k].strip()
                    if k > i and b_depth == 0 and p_depth == 0 and (
                        k_st.startswith(("case", "default")) and _4D_CASE_BRK_RE.match(k_st)
                    ):
                        end_i = k - 1
                        break
                    for ch in k_st:
                        if ch == "{":
                            b_depth += 1
                        elif ch == "}":
                            b_depth -= 1
                        elif ch == "(":
                            p_depth += 1
                        elif ch == ")":
                            p_depth -= 1
                    if b_depth < 0:
                        end_i = max(i, k - 1)
                        break
                    end_i = k
                sample = "\n".join(lines[lnum - 1 : min(end_i + 1, lnum + 35)])
                role = "VIEW" if ("<" in sample or "return" in sample) else "ACTION"
                raw_blocks.append((role, f"case:{lbl_str}", lnum, end_i + 1))
                i = max(i + 1, end_i + 1 if (end_i - i) <= 120 else i + 1)
                continue

        m_fn = _4D_FN_HDR_RE.match(st) if ("(" in st or "def " in st) else None
        if m_fn:
            fn_name = m_fn.group(1) or m_fn.group(2) or m_fn.group(3)
            if fn_name not in ("if", "for", "while", "switch", "catch", "constructor"):
                if "def " in st:
                    end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _py_block_end(i, max_scan=800)
                else:
                    end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=3000)
                span = (end_i + 1) - lnum
                if span <= 200:
                    body_txt = "\n".join(lines[i : end_i + 1])
                    if _4D_FN_IO_RE.search(body_txt):
                        role = "IO"
                    elif "<" in body_txt and "return" in body_txt and _4D_FN_VIEW_RE.search(body_txt):
                        role = "VIEW"
                    else:
                        role = "ACTION"
                    raw_blocks.append((role, f"fn:{fn_name}", lnum, end_i + 1))
                    i = (end_i + 1) if span < 8 else (i + 1)
                    continue

        m_if = _4D_IF_HDR_RE.match(st) if ("if" in st and (st.endswith(("{", ":")) or "#" in st)) else None
        if m_if:
            cond = m_if.group(1).strip()
            if st.endswith("{"):
                end_i = (
                    (ast_span_by_start[lnum] - 1)
                    if (lnum in ast_span_by_start and not st.startswith("}"))
                    else _find_balanced_end(lines, i, "{", "}", max_scan=400)
                )
            else:
                end_i = _py_block_end(i, max_scan=250)
            span = (end_i + 1) - lnum
            if 4 <= span <= 220:
                sample = "\n".join(lines[i : min(end_i + 1, i + 30)])
                if _4D_IF_IO_RE.search(sample):
                    role = "IO"
                elif "<" in sample and "return" in sample:
                    role = "VIEW"
                else:
                    role = "ACTION"
                raw_blocks.append((role, f"branch[{cond[:40]}]", lnum, end_i + 1))
                i = (end_i + 1) if span <= 5 else (i + 1)
                continue

        if st.startswith(("with", "async")) and ":" in st:
            m_with = _4D_WITH_HDR_RE.match(st)
            if m_with:
                ctx_expr = m_with.group(1).strip()
                end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _py_block_end(i, max_scan=250)
                span = (end_i + 1) - lnum
                if 3 <= span <= 220:
                    sample = "\n".join(lines[i : min(end_i + 1, i + 30)])
                    role = "IO" if _4D_WITH_IO_RE.search(sample) else "ACTION"
                    raw_blocks.append((role, f"with[{ctx_expr[:36]}]", lnum, end_i + 1))
                    i = i + 1
                    continue

        if st.startswith(("try", "except", "catch")):
            m_try = _4D_TRY_HDR_RE.match(st)
            if m_try:
                is_catch = st.startswith(("except", "catch"))
                end_i = _find_balanced_end(lines, i, "{", "}", max_scan=200) if st.endswith("{") else _py_block_end(i, max_scan=200)
                span = (end_i + 1) - lnum
                if 3 <= span <= 180:
                    sample = "\n".join(lines[i : min(end_i + 1, i + 25)])
                    role = "IO" if _4D_TRY_IO_RE.search(sample) else "ACTION"
                    lbl = f"catch[{(m_try.group(1) or 'all').strip()[:32]}]" if is_catch else "try_block"
                    raw_blocks.append((role, lbl, lnum, end_i + 1))
                    i = (end_i + 1) if (is_catch and span <= 6) else (i + 1)
                    continue

        if "." in st and "(" in st and _4D_EXEC_HDR_RE.search(st):
            end_i = _find_balanced_end(lines, i, "(", ")", max_scan=80)
            span = (end_i + 1) - lnum
            if 2 <= span <= 80:
                sql_sample = "\n".join(lines[i : end_i + 1])
                m_op = _4D_EXEC_OP_RE.search(sql_sample)
                if m_op:
                    raw_verb = m_op.group(1).strip().upper()
                    verb = "SELECT" if raw_verb.startswith("SELECT") else raw_verb.split()[0]
                    tbl_nm = m_op.group(2)
                    raw_blocks.append(("IO", f"sql[{verb} {tbl_nm}]", lnum, end_i + 1))
                    i = end_i + 1
                    continue

        if st.startswith("{") and "(" in st:
            m_jsx = _4D_JSX_HDR_RE.match(st)
            if m_jsx:
                cond = _4D_WS_RE.sub(" ", m_jsx.group(1).strip())
                end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=350)
                if 5 <= (end_i + 1 - lnum) <= 160:
                    raw_blocks.append(("VIEW", f"jsx[{cond[:36]}]", lnum, end_i + 1))
                    i = end_i + 1
                    continue

        if "{" in st and ".map" in st:
            m_map = _4D_MAP_HDR_RE.search(st)
            if m_map:
                arr_nm = m_map.group(1)
                end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=220)
                span_m = (end_i + 1) - lnum
                if 4 <= span_m <= 180:
                    sample_m = "\n".join(lines[i : min(end_i + 1, i + 25)])
                    if _4D_MAP_VIEW_RE.search(sample_m):
                        raw_blocks.append(("VIEW", f"jsx[{arr_nm}.map]", lnum, end_i + 1))
                        i = end_i + 1
                        continue

        i += 1

    # Pre-compile non-self state_vars once per file; use O(1) set intersection per block
    setter_specs: list[tuple[str, str, re.Pattern[str] | None]] = []
    state_var_order: list[str] = list(state_vars.keys())
    state_var_set: set[str] = set(state_var_order)
    for var, setter_pat in state_vars.items():
        if setter_pat in ("__self_attr__", f"__readonly_{var}__") or setter_pat.startswith("__readonly_"):
            continue
        if _4D_SIMPLE_IDENT_RE.match(setter_pat):
            setter_specs.append((var, setter_pat, None))
        else:
            setter_specs.append((var, var, re.compile(rf"\b{setter_pat}\b")))

    http_client_rx = _4D_HTTP_PY_RE if ext_l == ".py" else _4D_HTTP_JS_RE
    cells: list[dict] = []
    for role, scope, s, e in raw_blocks:
        chunk = "\n".join(lines[s - 1 : e])
        chunk_words = set(_4D_WORD_RE.findall(chunk))
        has_quotes = ("'" in chunk) or ('"' in chunk)
        has_paren = "(" in chunk
        up_chunk = chunk.upper() if file_has_sql else ""

        z_write: list[str] = []
        z_write_set: set[str] = set()
        for ln_no in range(s, e + 1):
            for sw in self_writes_by_line.get(ln_no, ()):
                if sw not in z_write_set:
                    z_write_set.add(sw)
                    z_write.append(sw)
        if setter_specs:
            for var, setter_anchor, setter_rx in setter_specs:
                if setter_anchor in chunk_words and (setter_rx is None or setter_rx.search(chunk)):
                    if var not in z_write_set:
                        z_write_set.add(var)
                        z_write.append(var)
        z_read: list[str] = []
        if state_var_set and (chunk_words & state_var_set):
            for var in state_var_order:
                if var in chunk_words and var not in z_write_set:
                    z_read.append(var)
        for ln_no in range(s, e + 1):
            for ast_w in ast_mutations_by_line.get(ln_no, ()):
                if ast_w not in z_write_set:
                    z_write_set.add(ast_w)
                    z_write.append(ast_w)
        if file_has_orm and ("prisma." in chunk or "supabase." in chunk or ".setState" in chunk):
            for m_orm in _4D_ORM_RE.finditer(chunk):
                orm_target = m_orm.group(1) or m_orm.group(2) or m_orm.group(3)
                if orm_target and orm_target not in z_write:
                    z_write.append(orm_target)
        if "INSERT" in up_chunk or "UPDATE" in up_chunk or "DELETE" in up_chunk:
            for m_sql_w in _4D_SQL_W_RE.finditer(chunk):
                tbl = m_sql_w.group(1)
                if tbl.lower() not in ("set", "values", "where", "table") and tbl not in z_write:
                    z_write.append(tbl)
        if "SELECT" in up_chunk and "FROM" in up_chunk:
            for m_sql_r in _4D_SQL_R_RE.finditer(chunk):
                tbl = m_sql_r.group(1)
                if tbl not in z_write and tbl not in z_read:
                    z_read.append(tbl)
        if role == "STATE" and scope.startswith("state:"):
            st_attr = scope.split(":", 1)[1]
            if st_attr not in z_write:
                z_write.append(st_attr)
            if has_quotes:
                for m_cfg_k in _4D_CFG_KEY_RE.finditer(chunk):
                    cfg_k = m_cfg_k.group(1)
                    if cfg_k not in z_write:
                        z_write.append(cfg_k)
        if has_quotes and has_paren:
            for m_kv in _4D_KV_RE.finditer(chunk):
                k_name = m_kv.group(1)
                full_m = m_kv.group(0)
                if any(w in full_m for w in (".set", "setItem", "removeItem", ".put", ".write", ".update", ".delete", "setattr", "Attribute(", "Property(", "Descriptor(")):
                    if k_name not in z_write:
                        z_write.append(k_name)
                elif k_name not in z_read:
                    z_read.append(k_name)
        if has_quotes and "[" in chunk:
            for m_dk in _4D_DICT_K_RE.finditer(chunk):
                k_name = m_dk.group(1)
                is_assign = bool(m_dk.group(2))
                if is_assign:
                    if k_name not in z_write:
                        z_write.append(k_name)
                elif k_name not in z_write and k_name not in z_read:
                    z_read.append(k_name)
        if has_quotes and has_paren:
            if chunk_words & {"emit", "publish", "trigger", "broadcast"}:
                for m_ev_w in _4D_EVT_W_RE.finditer(chunk):
                    ev_key = f"evt:{m_ev_w.group(1)}"
                    if ev_key not in z_write:
                        z_write.append(ev_key)
            if chunk_words & {"on", "once", "subscribe", "addEventListener", "listen"}:
                for m_ev_r in _4D_EVT_R_RE.finditer(chunk):
                    ev_key = f"evt:{m_ev_r.group(1)}"
                    if ev_key not in z_read:
                        z_read.append(ev_key)
        if "." in chunk and (chunk_words & {"self", "cls", "app", "current_app", "config", "settings", "request", "session", "g"}):
            for m_py_r in _4D_PY_ATTR_R_RE.finditer(chunk):
                attr = m_py_r.group(1)
                if not attr.startswith("_") and attr not in z_write and attr not in z_read:
                    z_read.append(attr)

        e_exit: list[str] = []
        if "raise" in chunk_words or "throw" in chunk_words:
            for m_ex in _4D_RAISE_RE.finditer(chunk):
                exc = m_ex.group(1)
                if exc not in ("new", "return", "from") and f"throw:{exc}" not in e_exit:
                    e_exit.append(f"throw:{exc}")
        if "except" in chunk_words or "catch" in chunk_words:
            for m_ct in _4D_CATCH_RE.finditer(chunk):
                exc = m_ct.group(1)
                if exc not in ("as",) and f"catch:{exc}" not in e_exit:
                    e_exit.append(f"catch:{exc}")
        if ("4" in chunk or "5" in chunk) and (chunk_words & {"ApiError", "HTTPException", "status", "status_code"}):
            for m_st in _4D_STATUS_RE.finditer(chunk):
                st_code = f"status:{m_st.group(1)}"
                if st_code not in e_exit:
                    e_exit.append(st_code)
                if m_st.group(2):
                    err_tag = f"err:{m_st.group(2).lower()}"
                    if err_tag not in e_exit:
                        e_exit.append(err_tag)
        if "if" in chunk_words and "return" in chunk_words and _4D_GUARD_RET_RE.search(chunk):
            if "guard_return" not in e_exit:
                e_exit.append("guard_return")

        if role == "EFFECT" or (
            (chunk_words & {"useEffect", "useLayoutEffect", "onSnapshot", "addEventListener", "subscribe", "on_event"})
            and _4D_REACTIVE_RE.search(chunk)
        ):
            t_mode = "REACTIVE"
        elif (
            (chunk_words & {"setTimeout", "setInterval", "requestAnimationFrame", "asyncio"})
            and _4D_TIMER_RE.search(chunk)
        ):
            t_mode = "TIMER"
        elif (
            ("async" in chunk_words or "await" in chunk_words or "Promise" in chunk_words or "then" in chunk_words)
            and _4D_ASYNC_RE.search(chunk)
        ):
            t_mode = "ASYNC"
        else:
            t_mode = "SYNC"

        w_comps = sorted(set(_4D_JSX_COMP_RE.findall(chunk))) if "<" in chunk else []
        w_calls = (
            sorted(
                {
                    m.group(1)
                    for m in _4D_W_CALLS_RE.finditer(chunk)
                    if m.group(1) not in _4D_W_CALL_NOISE
                }
            )
            if has_paren
            else []
        )
        sql_inserts: list[dict] = []
        if "INSERT" in up_chunk:
            for m_ins in _4D_SQL_INS_RE.finditer(chunk):
                ins_tbl = m_ins.group(1)
                ins_cols = [
                    c.strip().strip('"`')
                    for c in m_ins.group(2).split(",")
                    if _4D_SQL_COL_RE.match(c.strip().strip('"`'))
                ]
                if ins_cols:
                    ins_line = s + chunk.count("\n", 0, m_ins.start())
                    sql_inserts.append({"table": ins_tbl, "cols": ins_cols, "line": ins_line})
        sql_dialects: list[str] = []
        if "INTERVAL" in up_chunk:
            if "'" in chunk and _4D_DIALECT_INTERVAL_RE.search(chunk):
                sql_dialects.append("interval_literal")
            if "MAKE_INTERVAL" in up_chunk and _4D_DIALECT_MAKE_INT_RE.search(chunk):
                sql_dialects.append("make_interval")
        if "ANY" in up_chunk and "=" in chunk and _4D_DIALECT_ANY_RE.search(chunk):
            sql_dialects.append("any_array")
        if "ILIKE" in chunk_words and _4D_DIALECT_ILIKE_RE.search(chunk):
            sql_dialects.append("ilike_operator")
        if "::" in chunk and _4D_DIALECT_CAST_RE.search(chunk):
            sql_dialects.append("postgres_typecast")

        expected_res_keys: list[str] = []
        if ".json()" in chunk and "await" in chunk_words:
            for m_jvar in _4D_JSON_VAR_RE.finditer(chunk):
                jvar = m_jvar.group(1)
                for m_prop in re.finditer(rf"\b{re.escape(jvar)}\.([a-zA-Z_]\w*)\b", chunk[m_jvar.end() :]):
                    pk = m_prop.group(1)
                    line_end_pos = chunk.find("\n", m_jvar.end() + m_prop.start())
                    line_start_pos = chunk.rfind("\n", 0, m_jvar.end() + m_prop.start()) + 1
                    prop_line_txt = chunk[line_start_pos : (line_end_pos if line_end_pos != -1 else len(chunk))]
                    if "||" in prop_line_txt or "??" in prop_line_txt:
                        continue
                    if pk not in _4D_JS_RES_BUILTINS and pk not in expected_res_keys:
                        expected_res_keys.append(pk)

        http_calls: list[dict] = []
        seen_hc_keys: set[tuple[str, str, int]] = set()
        if "fetch" in chunk_words:
            for m_fe in _4D_FETCH_RE.finditer(chunk):
                raw_u = m_fe.group(1).split("?")[0]
                opts_u = m_fe.group(2) or ""
                m_verb = _4D_FETCH_METHOD_RE.search(opts_u)
                verb_u = m_verb.group(1).upper() if m_verb else "GET"
                norm_u = _4D_URL_PARAM_RE.sub("{*}", raw_u).rstrip("/") or "/"
                fe_line = s + chunk.count("\n", 0, m_fe.start())
                hc_k = (verb_u, norm_u, fe_line)
                if hc_k not in seen_hc_keys:
                    seen_hc_keys.add(hc_k)
                    hc_entry: dict = {"method": verb_u, "url": raw_u, "norm_url": norm_u, "line": fe_line}
                    if expected_res_keys:
                        hc_entry["expected_keys"] = expected_res_keys[:8]
                    http_calls.append(hc_entry)
            # Also resolve `const url = \`${baseUrl}/api/...\`; fetch(url, { method: ... })`
            if "/" in chunk:
                for m_uv in _4D_URL_VAR_DECL_RE.finditer(chunk):
                    u_var, raw_uv = m_uv.group(1), m_uv.group(2).split("?")[0]
                    m_fvar = re.search(
                        rf"\bfetch\s*\(\s*{re.escape(u_var)}\b(?:\s*,\s*\{{([\s\S]{{0,260}}?)\}})?",
                        chunk[m_uv.end() :],
                    )
                    if m_fvar:
                        opts_uv = m_fvar.group(1) or ""
                        m_vb = _4D_FETCH_METHOD_RE.search(opts_uv)
                        verb_uv = m_vb.group(1).upper() if m_vb else "GET"
                        norm_uv = _4D_URL_PARAM_RE.sub("{*}", raw_uv).rstrip("/") or "/"
                        uv_line = s + chunk.count("\n", 0, m_uv.end() + m_fvar.start())
                        hc_k = (verb_uv, norm_uv, uv_line)
                        if hc_k not in seen_hc_keys:
                            seen_hc_keys.add(hc_k)
                            hc_ev: dict = {"method": verb_uv, "url": raw_uv, "norm_url": norm_uv, "line": uv_line}
                            if expected_res_keys:
                                hc_ev["expected_keys"] = expected_res_keys[:8]
                            http_calls.append(hc_ev)
        has_http_client = (
            ("REQUESTS." in up_chunk or "HTTPX." in up_chunk or "CLIENT." in up_chunk)
            if ext_l == ".py"
            else any(k in up_chunk for k in ("AXIOS.", "API.", "CLIENT.", "HTTP.", "HTTPCLIENT.", "APICLIENT."))
        )
        if has_http_client:
            for m_ax in http_client_rx.finditer(chunk):
                verb_u = m_ax.group(1).upper()
                raw_u = m_ax.group(2).split("?")[0]
                norm_u = _4D_URL_PARAM_RE.sub("{*}", raw_u).rstrip("/") or "/"
                ax_line = s + chunk.count("\n", 0, m_ax.start())
                hc_k = (verb_u, norm_u, ax_line)
                if hc_k not in seen_hc_keys:
                    seen_hc_keys.add(hc_k)
                    hc_entry_ax: dict = {"method": verb_u, "url": raw_u, "norm_url": norm_u, "line": ax_line}
                    if expected_res_keys:
                        hc_entry_ax["expected_keys"] = expected_res_keys[:8]
                    http_calls.append(hc_entry_ax)
        if ext_l != ".py" and "/" in chunk:
            for m_wr in _4D_HTTP_WRAPPER_RE.finditer(chunk):
                fn_vb = m_wr.group(1)
                raw_w = m_wr.group(2).split("?")[0]
                opts_w = m_wr.group(3) or ""
                m_vw = _4D_FETCH_METHOD_RE.search(opts_w)
                verb_w = (m_vw.group(1) if m_vw else (fn_vb or "ANY")).upper()
                norm_w = _4D_URL_PARAM_RE.sub("{*}", raw_w).rstrip("/") or "/"
                wr_line = s + chunk.count("\n", 0, m_wr.start())
                hc_k = (verb_w, norm_w, wr_line)
                if hc_k not in seen_hc_keys:
                    seen_hc_keys.add(hc_k)
                    hc_ew: dict = {"method": verb_w, "url": raw_w, "norm_url": norm_w, "line": wr_line}
                    if expected_res_keys:
                        hc_ew["expected_keys"] = expected_res_keys[:8]
                    http_calls.append(hc_ew)

        type_comparisons: list[dict] = []
        sql_select_violations: list[dict] = []
        return_dict_keys: list[str] = []
        if scope.startswith("fn:"):
            if ext_l == ".py" and s in py_ast_contracts:
                type_comparisons.extend(py_ast_contracts[s].get("type_comparisons", []))
                return_dict_keys.extend(py_ast_contracts[s].get("return_dict_keys", []))
            if "FETCHONE" in up_chunk:
                for m_sel in _4D_FETCHONE_RE.finditer(chunk):
                    r_var = m_sel.group(1)
                    raw_c_str = m_sel.group(2)
                    sel_tbl = m_sel.group(3)
                    if "*" in raw_c_str or "(" in raw_c_str:
                        continue
                    proj_cols: list[str] = []
                    for c_part in raw_c_str.split(","):
                        cp = c_part.strip()
                        m_as = _4D_SQL_AS_RE.search(cp)
                        if m_as:
                            proj_cols.append(m_as.group(1))
                        else:
                            base_c = cp.split("::")[0].strip().split(".")[-1].strip().strip('"`')
                            if _4D_IDENT_FULL_RE.match(base_c):
                                proj_cols.append(base_c)
                    if not proj_cols:
                        continue
                    proj_low = {c.lower() for c in proj_cols}
                    tail_chunk = chunk[m_sel.end() :]
                    m_reassign = re.search(rf"\b{re.escape(r_var)}\s*=\s*", tail_chunk)
                    if m_reassign:
                        tail_chunk = tail_chunk[: m_reassign.start()]
                    for m_rk in re.finditer(rf"\b{re.escape(r_var)}\[\s*['\"]([a-zA-Z_]\w*)['\"]\s*\]", tail_chunk):
                        rk = m_rk.group(1)
                        if rk.lower() not in proj_low:
                            rk_line = s + chunk.count("\n", 0, m_sel.end() + m_rk.start())
                            sql_select_violations.append({
                                "var": r_var,
                                "read_key": rk,
                                "projected_cols": proj_cols,
                                "table": sel_tbl,
                                "line": rk_line,
                            })

            if ext_l == ".py" and not return_dict_keys and s not in py_ast_contracts and "return" in chunk_words and "{" in chunk:
                ret_blocks = _4D_RET_DICT_RE.findall(chunk)
                if ret_blocks and not any("**" in rb for rb in ret_blocks):
                    for rb in ret_blocks:
                        for mk in _4D_DICT_KEY_COLON_RE.finditer(rb):
                            k_str = mk.group(1)
                            if k_str not in return_dict_keys:
                                return_dict_keys.append(k_str)
        cell_dict: dict = {
            "X_scope": scope,
            "Y_role": role,
            "Z_write": z_write[:10],
            "Z_read": z_read[:14],
            "W_calls": (w_comps + w_calls)[:10],
            "E_exit": e_exit[:10],
            "T_mode": t_mode,
            "line_start": s,
            "line_end": e,
            "span": e - s + 1,
        }
        if sql_inserts:
            cell_dict["sql_inserts"] = sql_inserts
        if sql_dialects:
            cell_dict["sql_dialects"] = sql_dialects
        if http_calls:
            cell_dict["http_calls"] = http_calls
        if type_comparisons:
            cell_dict["type_comparisons"] = type_comparisons
        if sql_select_violations:
            cell_dict["sql_select_violations"] = sql_select_violations
        if return_dict_keys:
            cell_dict["return_dict_keys"] = return_dict_keys[:16]
        cells.append(cell_dict)
    if js_ast_contracts:
        for jc in js_ast_contracts:
            j_ln = jc["line"]
            if not (s_idx + 1 <= j_ln <= e_idx):
                continue
            enclosing = [
                c for c in cells
                if c["line_start"] <= j_ln <= c["line_end"]
            ]
            if enclosing:
                finest = min(enclosing, key=lambda c: c["span"])
                finest.setdefault("js_ast_contracts", []).append(jc)
            elif cells:
                cells[0].setdefault("js_ast_contracts", []).append(jc)
            else:
                cells.append({
                    "X_scope": f"contract:{jc['kind']}",
                    "Y_role": "ACTION",
                    "Z_write": [],
                    "Z_read": [],
                    "W_calls": [],
                    "E_exit": [],
                    "T_mode": "SYNC",
                    "line_start": j_ln,
                    "line_end": jc.get("end_line", j_ln),
                    "span": max(1, jc.get("end_line", j_ln) - j_ln + 1),
                    "js_ast_contracts": [jc],
                })
    return cells


def query_4d_coordinate_matrix(
    cells: list[dict],
    focus_terms: list[str] | None = None,
    raw_query: str = "",
    max_table_rows: int = 22,
) -> tuple[list[dict], str]:
    """Intersect query terms against the Universal 6D Coordinate Matrix (X_scope, Y_role, Z_write, Z_read, W_calls, E_exit, T_mode).
    Returns (top_matched_cells, formatted_4d_matrix_table).
    """
    _STOP = {
        "the", "and", "for", "with", "from", "into", "that", "this", "where", "when", "which",
        "what", "how", "does", "are", "was", "were", "wired", "rendered", "updated", "changes",
        "change", "study", "table", "studytable", "file", "function", "code",
    }
    raw_terms = [t.lower() for t in (focus_terms or []) if len(t) >= 3 and t.lower() not in _STOP]
    terms: list[str] = []
    for rt in raw_terms:
        if rt not in terms:
            terms.append(rt)
    q_low = raw_query.lower()

    wants_view = any(w in q_low for w in ("render", "ui", "view", "jsx", "button", "modal", "header", "layout", "display", "screen"))
    wants_effect = any(w in q_low for w in ("effect", "mount", "reset", "hydrate", "watch", "change", "init", "sync", "countdown", "timer"))
    wants_io = any(
        w in q_low
        for w in (
            "save", "persist", "storage", "db", "database", "sql", "sqlite", "postgres",
            "firestore", "cloud", "fetch", "api", "kv", "pay", "submit", "connect",
            "connection", "query", "insert", "table", "upload", "worker", "backend",
        )
    )
    wants_write = any(
        w in q_low
        for w in (
            "set", "update", "mark", "reset", "write", "save", "mutate", "toggle",
            "clear", "override", "insert", "create", "delete", "upload", "persist",
        )
    )
    wants_error = any(
        w in q_low
        for w in (
            "error", "throw", "raise", "fail", "exception", "guard", "abort", "reject",
            "catch", "bug", "bugs", "cant", "cannot", "broken", "crash", "500", "409",
            "400", "constraint",
        )
    )

    def _stem(w: str) -> str:
        st = re.sub(r"(?:ation|ment|ing|ed|es|s)$", "", w) if len(w) >= 5 else w
        return st if len(st) >= 3 else w

    term_idf: dict[str, float] = {}
    n_cells = max(1, len(cells))
    for t in terms:
        ts = _stem(t)
        df = 0
        for c in cells:
            ex_str = " ".join(c.get("E_exit", []))
            blob = f"{c['X_scope']} {' '.join(c['Z_write'])} {' '.join(c['Z_read'])} {' '.join(c['W_calls'])} {ex_str}".lower()
            if t in blob or (len(ts) >= 3 and ts in blob):
                df += 1
        term_idf[t] = 1.0 + math.log((n_cells + 1.0) / (df + 1.0))

    scored: list[tuple[float, dict]] = []
    for c in cells:
        sc = 0.0
        x_low = c["X_scope"].lower()
        y_role = c["Y_role"]
        zw_low = " ".join(c["Z_write"]).lower()
        zr_low = " ".join(c["Z_read"]).lower()
        w_low = " ".join(c["W_calls"]).lower()
        ex_low = " ".join(c.get("E_exit", [])).lower()

        for t in terms:
            ts = _stem(t)
            idf_w = term_idf.get(t, 1.0)
            axis_hits: list[float] = []
            if t in x_low or (len(ts) >= 3 and ts in x_low):
                axis_hits.append(5.0)
            if t in zw_low or (len(ts) >= 3 and ts in zw_low):
                axis_hits.append(4.5)
            if t in w_low or (len(ts) >= 3 and ts in w_low):
                axis_hits.append(4.0)
            if t in ex_low or (len(ts) >= 3 and ts in ex_low):
                axis_hits.append(4.2)
            if t in zr_low or (len(ts) >= 3 and ts in zr_low):
                axis_hits.append(2.5)
            if axis_hits:
                sc += (max(axis_hits) + 0.35 * (len(axis_hits) - 1)) * idf_w

        if sc > 0:
            if wants_view and y_role == "VIEW":
                sc += 2.5
            elif not wants_view and y_role == "VIEW":
                sc *= 0.75
            if wants_effect and y_role == "EFFECT":
                sc += 2.5
            if wants_io and y_role in ("IO", "ACTION"):
                sc += 2.5
            if wants_write and c["Z_write"]:
                sc += 2.0
            if wants_error and c.get("E_exit"):
                sc += 2.5
            if y_role == "STATE":
                sc *= 0.30
            if c["span"] > 400:
                sc *= 0.6
        else:
            beh_sc = 0.0
            if wants_io and y_role in ("IO", "SCHEMA"):
                beh_sc += 2.0
            if (wants_write or wants_io) and c["Z_write"]:
                beh_sc += 1.8
            if wants_error and c.get("E_exit"):
                beh_sc += 2.0
            if wants_view and y_role == "VIEW":
                beh_sc += 1.5
            if wants_effect and y_role == "EFFECT":
                beh_sc += 1.5
            if c.get("sql_inserts") or c.get("sql_dialects"):
                beh_sc += 1.5
            if beh_sc > 0:
                if c["span"] <= 90 and not x_low.startswith("fn:"):
                    beh_sc *= 1.15
                elif c["span"] > 400:
                    beh_sc *= 0.5
                sc = beh_sc
        scored.append((sc, c))
    scored_pos = [item for item in scored if item[0] > 0]
    scored_pos.sort(key=lambda item: (item[0], -item[1]["span"]), reverse=True)
    top_cells = [item[1] for item in scored_pos[:4]]

    display_cells: list[dict] = list(top_cells)
    seen_spans = {(c["line_start"], c["line_end"]) for c in display_cells}
    step = max(1, len(cells) // max(1, max_table_rows - len(display_cells)))
    for idx in range(0, len(cells), step):
        cand = cells[idx]
        key = (cand["line_start"], cand["line_end"])
        if key not in seen_spans:
            display_cells.append(cand)
            seen_spans.add(key)
            if len(display_cells) >= max_table_rows:
                break
    display_cells.sort(key=lambda c: c["line_start"])

    if max_table_rows <= 2 and display_cells:
        c0 = display_cells[0]
        zw0 = f" ΔZ:[{','.join(c0['Z_write'][:2])}]" if c0["Z_write"] else ""
        zr0 = f" ?Z:[{','.join(c0['Z_read'][:2])}]" if c0.get("Z_read") and not zw0 else ""
        wc0 = f" W:[{','.join(c0['W_calls'][:3])}]" if c0["W_calls"] else ""
        ex0 = f" E:[{','.join(c0.get('E_exit', [])[:2])}]" if c0.get("E_exit") else ""
        return top_cells, f"# [6D Coordinate Matrix] L{c0['line_start']}-L{c0['line_end']} {c0['Y_role']} {c0['X_scope'][:32]}{zw0}{zr0}{wc0}{ex0}"

    table_lines = [
        "# [6D Coordinate Matrix] Lstart-Lend | Y_Role | X_Scope | ?Z_Write -> W_Calls | E_Exit | T_Mode"
    ]
    for c in display_cells:
        zw = f" ?Z:[{','.join(c['Z_write'][:3])}]" if c["Z_write"] else ""
        wc = f" W:[{','.join(c['W_calls'][:3])}]" if c["W_calls"] else ""
        ex = f" E:[{','.join(c.get('E_exit', [])[:2])}]" if c.get("E_exit") else ""
        tm = f" T:{c.get('T_mode')}" if c.get("T_mode") and c.get("T_mode") != "SYNC" else ""
        table_lines.append(
            f"#   L{c['line_start']:4d}-L{c['line_end']:4d} | {c['Y_role']:6s} | {c['X_scope'][:34]:34s} |{zw}{wc}{ex}{tm}"
        )
    return top_cells, "\n".join(table_lines)


def contract_in_query_scope(
    brain: "ProjectBrain",
    rel: str,
    scope_text: str,
    summary: str,
    terms: list[str],
) -> bool:
    """Is a static-contract finding relevant to the current query?"""
    terms = list(dict.fromkeys(t.lower() for t in terms if len(t) >= 3))
    if not terms:
        return True
    stem = Path(rel).stem.lower()
    if any(len(t) >= 4 and t in stem for t in terms):
        return True
    groups: list[list[str]] = []
    seen_t: set[str] = set()
    for t in terms:
        if t in seen_t:
            continue
        members = [t] + [a for a in brain._expand_keywords_with_aliases([t])[1:] if a in terms and a not in seen_t]
        seen_t.update(members)
        groups.append(members)
    file_toks = brain._token_sets.get(rel, set())
    file_blob = brain._blobs.get(rel, "")
    region = f"{scope_text} {summary}".lower()
    present_w = 0.0
    matched_w = 0.0
    for members in groups:
        in_file = any(m in file_toks or (len(m) >= 4 and m in file_blob) for m in members)
        if not in_file:
            continue
        w = max(brain._idf.get(m, 1.0) for m in members)
        present_w += w
        if any(m in region for m in members):
            matched_w += w
    if present_w <= 0:
        return False
    return matched_w / present_w >= 0.5


def compile_6d_contract_diagnostics(
    brain: "ProjectBrain",
    task: str,
    focus_files: set[str],
    strict_focus: bool = False,
    *,
    skip_http_schemas: bool = False,
) -> tuple[list[str], list[str]]:
    """Cross-check 6D IO cells (SQL INSERT column sets, SQL dialects, exception contracts)
    against indexed CREATE TABLE schemas and database adapter capabilities across the repository.
    Returns (diagnostic_markdown_lines, offending_node_ids).
    """
    from .brain import DEPRIORITY_DIRS, _TEST_QUERY_TERMS, _extract_keywords, _is_test_file

    t_low = task.lower()
    is_explanatory_q = bool(re.match(r"^\s*(?:how|where|what|which)\b", t_low)) and not any(
        w in t_low for w in ("bug", "bugs", "broken", "mismatch", "violation", "failing", "crash", "error", "fails", "fail")
    )
    if is_explanatory_q and not strict_focus:
        brain._last_line_annotations = {}
        brain._last_pin_lines = {}
        return [], []

    idx_ver = (len(brain._index), sum(id(v) for v in brain._index.values()))
    if getattr(brain, "_cached_6d_schema_ver", None) == idx_ver and hasattr(brain, "_cached_6d_schema_data"):
        schemas_by_table, sqlite_adapters = brain._cached_6d_schema_data
    else:
        schemas_by_table = {}
        all_sql_alters: dict[str, list[dict]] = {}
        sqlite_adapters = []
        for rel in sorted(brain._index.keys()):
            info = brain._index[rel]
            for tbl, sc in (info.get("sql_schemas") or {}).items():
                sc_copy = {
                    "name": sc.get("name", tbl),
                    "line_start": sc.get("line_start", 1),
                    "line_end": sc.get("line_end", 1),
                    "all_cols": list(sc.get("all_cols", [])),
                    "required_cols": list(sc.get("required_cols", [])),
                    "defaulted_cols": list(sc.get("defaulted_cols", [])),
                    "fields": list(sc.get("fields", [])),
                }
                schemas_by_table.setdefault(tbl.lower(), []).append((rel, sc_copy))
            sql_alters_map = info.get("sql_alters") or {}
            if isinstance(sql_alters_map, dict):
                for alt_tbl, ops in sql_alters_map.items():
                    all_sql_alters.setdefault(alt_tbl.lower(), []).extend(ops)
            if rel.endswith(".py") and "sqlite3" in info.get("imports", []):
                src_txt = brain._get_file_text(rel)
                if src_txt and ("def execute" in src_txt or "_translate_sql" in src_txt):
                    sqlite_adapters.append((rel, src_txt))
        if all_sql_alters and schemas_by_table:
            for alt_tbl_low, ops in all_sql_alters.items():
                for _, sc_copy in schemas_by_table.get(alt_tbl_low, []):
                    apply_sql_alters_to_schema(sc_copy, ops)
        brain._cached_6d_schema_data = (schemas_by_table, sqlite_adapters)
        brain._cached_6d_schema_ver = idx_ver

    q_terms = [t for t in brain._expand_keywords_with_aliases(_extract_keywords(task)) if len(t) >= 4]
    q_wants_tests_diag = bool(set(q_terms) & _TEST_QUERY_TERMS)
    wants_all_repo_diag = (not strict_focus) and (not is_explanatory_q) and any(
        w in t_low
        for w in ("bug", "bugs", "broken", "fail", "failing", "db", "database", "sql", "sqlite", "postgres", "connect", "insert", "schema", "mismatch", "violation")
    )
    ordered_files = sorted(
        brain._index.keys(),
        key=lambda r: (0 if r in focus_files else 1, r),
    )
    warnings: list[str] = []
    offending_nodes: list[str] = []
    seen_warn_keys: set[tuple[str, int, str]] = set()
    covered_dialect_spans: list[tuple[str, int, int, str]] = []
    line_ann: dict[str, dict[int, list[str]]] = {}
    pin_map: dict[str, set[int]] = {}

    def _record_offending_fn(f_rel: str, ln_no: int, span_end: int = 0, inline_msg: str = "") -> None:
        end_p = max(ln_no, span_end)
        for p_ln in range(max(1, ln_no - 1), min(end_p + 1, ln_no + 6) + 1):
            pin_map.setdefault(f_rel, set()).add(p_ln)
        if inline_msg:
            ann_list = line_ann.setdefault(f_rel, {}).setdefault(ln_no, [])
            if inline_msg not in ann_list:
                ann_list.append(inline_msg)
        for fn in brain._index.get(f_rel, {}).get("functions", []):
            if fn.get("line_start", 0) <= ln_no <= fn.get("line_end", 0):
                nid = fn_id(f_rel, fn["name"], fn.get("parent_class"))
                if nid not in offending_nodes:
                    offending_nodes.append(nid)

    for rel in ordered_files:
        if not wants_all_repo_diag and rel not in focus_files:
            continue
        if not q_wants_tests_diag and (
            _is_test_file(rel)
            or (rel not in focus_files and bool({p.lower() for p in Path(rel).parts[:-1]} & DEPRIORITY_DIRS))
        ):
            continue
        raw_cells = brain._index[rel].get("cells_4d")
        if not raw_cells:
            continue
        relevant_cells = [
            c for c in raw_cells
            if c.get("sql_inserts")
            or (sqlite_adapters and (c.get("sql_dialects") or c.get("E_exit")))
            or c.get("type_comparisons")
            or c.get("sql_select_violations")
            or c.get("http_calls")
            or c.get("js_ast_contracts")
        ]
        if not relevant_cells:
            continue
        cells_sorted = sorted(relevant_cells, key=lambda c: c.get("span", 9999))
        for cell in cells_sorted:
            scope = cell.get("X_scope", "")
            c_line = cell.get("line_start", 1)
            c_end = cell.get("line_end", c_line)
            for ins in cell.get("sql_inserts", []):
                tbl_raw = ins["table"]
                tbl_key = tbl_raw.lower()
                ins_cols_low = {c.lower() for c in ins["cols"]}
                ins_line = ins.get("line", c_line)
                cand_schemas = schemas_by_table.get(tbl_key, [])
                local_schemas = [(s_rel, sc) for s_rel, sc in cand_schemas if s_rel == rel]
                for schema_rel, sc in (local_schemas or cand_schemas):
                    missing = [
                        req for req in sc.get("required_cols", [])
                        if req.lower() not in ins_cols_low
                    ]
                    if missing:
                        w_key = (rel, ins_line, f"null:{tbl_key}:{','.join(missing)}")
                        if w_key not in seen_warn_keys:
                            seen_warn_keys.add(w_key)
                            _record_offending_fn(
                                rel,
                                ins_line,
                                span_end=min(c_end, ins_line + 5),
                                inline_msg=(
                                    f"# >>> [6D-CONTRACT L{ins_line}] INSERT INTO {tbl_raw} omits required NOT NULL "
                                    f"column(s): {', '.join(missing)} (schema: {schema_rel}:L{sc['line_start']})"
                                ),
                            )
                            warnings.append(
                                f"- ⚠️ **[6D Schema Contract Violation]** `{rel}:L{ins_line}` (`{scope}`) → "
                                f"`INSERT INTO {tbl_raw}({', '.join(ins['cols'][:5])}{'...' if len(ins['cols']) > 5 else ''})` "
                                f"omits required `NOT NULL` column(s) `{', '.join(missing)}` "
                                f"(declared without `DEFAULT` in `{schema_rel}:L{sc['line_start']}`)."
                            )
            if sqlite_adapters and not any(
                db_tok in rel.lower()
                for db_tok in ("postgres", "psycopg", "mysql", "oracle", "mssql")
            ):
                ad_rel, ad_src = sqlite_adapters[0]
                for feat in cell.get("sql_dialects", []):
                    if any(
                        sr == rel and c_line <= ss and se <= c_end and sf == feat
                        for sr, ss, se, sf in covered_dialect_spans
                    ):
                        continue
                    handled = False
                    if feat == "interval_literal":
                        handled = bool(re.search(r"interval\\s\+|interval\s+'", ad_src, re.IGNORECASE))
                    elif feat == "make_interval":
                        handled = "make_interval" in ad_src
                    elif feat == "any_array":
                        handled = bool(re.search(r"ANY\\s\*\\\(|ANY\s*\(", ad_src, re.IGNORECASE))
                    elif feat == "ilike_operator":
                        handled = bool(re.search(r"\bILIKE\b", ad_src, re.IGNORECASE))
                    elif feat == "postgres_typecast":
                        handled = "::" in ad_src
                    if not handled:
                        covered_dialect_spans.append((rel, c_line, c_end, feat))
                        w_key = (rel, c_line, f"dialect:{feat}")
                        if w_key not in seen_warn_keys:
                            seen_warn_keys.add(w_key)
                            _record_offending_fn(
                                rel,
                                c_line,
                                span_end=min(c_end, c_line + 3),
                                inline_msg=(
                                    f"# >>> [6D-CONTRACT L{c_line}] PostgreSQL construct '{feat}' is not translated "
                                    f"by SQLite adapter ({ad_rel})"
                                ),
                            )
                            warnings.append(
                                f"- ⚠️ **[6D SQL Dialect Mismatch]** `{rel}:L{c_line}` (`{scope}`) → "
                                f"Query uses PostgreSQL construct `{feat}` not translated by SQLite adapter `{ad_rel}`."
                            )
                exits = cell.get("E_exit", [])
                if (
                    cell.get("Z_write")
                    and "catch:UniqueViolation" in exits
                    and "catch:IntegrityError" not in exits
                    and "UniqueViolation" not in ad_src
                ):
                    if not any(
                        sr == rel and c_line <= ss and se <= c_end and sf == "exc:UniqueViolation"
                        for sr, ss, se, sf in covered_dialect_spans
                    ):
                        covered_dialect_spans.append((rel, c_line, c_end, "exc:UniqueViolation"))
                        w_key = (rel, c_line, "exc:UniqueViolation")
                        if w_key not in seen_warn_keys:
                            seen_warn_keys.add(w_key)
                            _record_offending_fn(
                                rel,
                                c_line,
                                span_end=min(c_end, c_line + 2),
                                inline_msg=(
                                    f"# >>> [6D-CONTRACT L{c_line}] Catches UniqueViolation on DB write without "
                                    f"catching sqlite3.IntegrityError ({ad_rel})"
                                ),
                            )
                            warnings.append(
                                f"- ⚠️ **[6D Exception Contract Mismatch]** `{rel}:L{c_line}` (`{scope}`) → "
                                f"Catches `UniqueViolation` on DB write (`ΔZ:{','.join(cell['Z_write'][:2])}`) "
                                f"without catching `IntegrityError` raised by SQLite adapter `{ad_rel}`."
                            )
            for tc_cmp in cell.get("type_comparisons", []):
                cmp_line = tc_cmp["line"]
                w_key = (rel, cmp_line, f"type:{tc_cmp['param']}:{tc_cmp['row_key']}")
                if w_key not in seen_warn_keys:
                    seen_warn_keys.add(w_key)
                    if tc_cmp.get("other_var"):
                        _record_offending_fn(
                            rel,
                            cmp_line,
                            span_end=cmp_line + 1,
                            inline_msg=(
                                f"# >>> [6D-CONTRACT L{cmp_line}] Disjoint type comparison: {tc_cmp['param']} "
                                f"({tc_cmp['param_type']}) vs {tc_cmp['other_var']} ({tc_cmp['other_type']}) — "
                                f"τ({tc_cmp['param_type']}) ∩ τ({tc_cmp['other_type']}) = ∅"
                            ),
                        )
                        warnings.append(
                            f"- ⚠️ **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) — "
                            f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                            f"`{tc_cmp['other_type']}` expression `{tc_cmp['other_var']}` without type coercion "
                            f"(`τ({tc_cmp['param_type']}) ∩ τ({tc_cmp['other_type']}) = ∅`)."
                        )
                    else:
                        _record_offending_fn(
                            rel,
                            cmp_line,
                            span_end=cmp_line + 1,
                            inline_msg=(
                                f"# >>> [6D-CONTRACT L{cmp_line}] Disjoint type comparison: {tc_cmp['param']} "
                                f"({tc_cmp['param_type']}) vs SQLite TEXT row['{tc_cmp['row_key']}'] without "
                                f"str()/{tc_cmp['param_type']}() coercion"
                            ),
                        )
                        warnings.append(
                            f"- ⚠️ **[6D Type Disjointness Violation]** `{rel}:L{cmp_line}` (`{scope}`) — "
                            f"Compares `{tc_cmp['param_type']}` expression `{tc_cmp['param']}` directly against "
                            f"SQLite `TEXT` row key `['{tc_cmp['row_key']}']` without `str()`/`{tc_cmp['param_type']}()` coercion "
                            f"(`τ({tc_cmp['param_type']}) ∩ τ(str) = ∅`)."
                        )

            for sv in cell.get("sql_select_violations", []):
                sv_line = sv["line"]
                w_key = (rel, sv_line, f"sql_proj:{sv['var']}:{sv['read_key']}")
                if w_key not in seen_warn_keys:
                    seen_warn_keys.add(w_key)
                    _record_offending_fn(
                        rel,
                        sv_line,
                        span_end=sv_line + 1,
                        inline_msg=(
                            f"# >>> [6D-CONTRACT L{sv_line}] Row key {sv['var']}['{sv['read_key']}'] is absent "
                            f"from SELECT {', '.join(sv['projected_cols'])} on {sv['table']}"
                        ),
                    )
                    warnings.append(
                        f"- ⚠️ **[6D Producer-Consumer SQL Shape Violation]** `{rel}:L{sv_line}` (`{scope}`) → "
                        f"Reads row key `{sv['var']}['{sv['read_key']}']`, which is absent from `SELECT {', '.join(sv['projected_cols'])}` "
                        f"on `{sv['table']}` (`Post(SELECT) ⊭ Pre(row['{sv['read_key']}'])`)."
                    )

            for jc in (
                [
                    jc0 for jc0 in cell.get("js_ast_contracts", [])
                    if strict_focus or contract_in_query_scope(brain, rel, scope, jc0.get("summary", ""), q_terms)
                ]
                if (not focus_files or rel in focus_files) else []
            ):
                j_line = jc["line"]
                j_kind = jc["kind"]
                w_key = (rel, j_line, f"js_ast:{j_kind}:{jc.get('symbol', '')}")
                if w_key not in seen_warn_keys:
                    seen_warn_keys.add(w_key)
                    for ex_ln in jc.get("extra_pin_lines", []):
                        pin_map.setdefault(rel, set()).add(ex_ln)
                    _record_offending_fn(
                        rel,
                        j_line,
                        span_end=jc.get("end_line", j_line + 1),
                        inline_msg=jc["inline_msg"],
                    )
                    warnings.append(
                        f"- ⚠️ **[6D AST Contract Violation (`{j_kind}`)]** `{rel}:L{j_line}` (`{scope}`) → {jc['summary']}"
                    )

    brain._last_http_warnings = []
    if not skip_http_schemas:
        from .semantic_contracts import verify_cross_stack_http_schemas

        http_focus = focus_files if (strict_focus and focus_files) else None
        _, http_hotspots, _ = verify_cross_stack_http_schemas(brain, focus_rels=http_focus)
        for hh in http_hotspots:
            h_rel = hh["rel_path"]
            h_line = int(hh.get("line") or 1)
            h_msg = hh.get("_diag_msg") or ""
            if not q_wants_tests_diag and _is_test_file(h_rel):
                continue
            if not strict_focus and focus_files and h_rel not in focus_files and not wants_all_repo_diag:
                if not contract_in_query_scope(brain, h_rel, "", hh.get("summary", ""), q_terms):
                    continue
            if hh.get("severity") != "error":
                if h_msg and h_msg not in brain._last_http_warnings:
                    brain._last_http_warnings.append(h_msg)
                continue
            w_key = (h_rel, h_line, f"http_sc:{hh.get('category')}:{hh.get('summary', '')[:48]}")
            if w_key not in seen_warn_keys and h_msg:
                seen_warn_keys.add(w_key)
                _record_offending_fn(
                    h_rel,
                    h_line,
                    span_end=h_line + 2,
                    inline_msg=f"# >>> [6D-CONTRACT L{h_line}] {hh['summary']}",
                )
                warnings.append(h_msg)

    brain._last_line_annotations = line_ann
    brain._last_pin_lines = pin_map
    return warnings, offending_nodes


def _extract_balanced_call_args(
    snippet: str,
    fn_name: str,
    is_method: bool = False,
    mod_stem: str = "",
    qual_expr: str = "",
) -> list[str] | None:
    """Extract top-level argument strings for `fn_name(...)` respecting nested `()`, `[]`, `{}`, and quotes."""
    m = None
    if qual_expr:
        m = re.search(rf"(?<!def\s)(?<!function\s)\b{re.escape(qual_expr)}\s*\(", snippet)
    elif not is_method:
        if mod_stem:
            m = re.search(rf"(?<!def\s)(?<!function\s)(?:(?<!\.)|\b{re.escape(mod_stem)}\.)\b{re.escape(fn_name)}\s*\(", snippet)
        else:
            m = re.search(rf"(?<!def\s)(?<!function\s)(?<!\.)\b{re.escape(fn_name)}\s*\(", snippet)
    else:
        m = re.search(rf"(?<!def\s)(?<!function\s)\b{re.escape(fn_name)}\s*\(", snippet)
    if not m:
        return None
    i = m.end()
    n = len(snippet)
    depth = 1
    in_str: str | None = None
    cur: list[str] = []
    args: list[str] = []
    while i < n and depth > 0:
        ch = snippet[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                cur.append(snippet[i])
            elif ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"', "`"):
                in_str = ch
                cur.append(ch)
            elif ch in ("(", "[", "{"):
                depth += 1
                cur.append(ch)
            elif ch in (")", "]", "}"):
                depth -= 1
                if depth == 0:
                    tok = "".join(cur).strip()
                    if tok:
                        args.append(tok)
                    return args
                cur.append(ch)
            elif ch == "," and depth == 1:
                tok = "".join(cur).strip()
                if tok:
                    args.append(tok)
                cur = []
            else:
                cur.append(ch)
        i += 1
    return None


def _py_scope_binds_name(
    rel: str,
    src: str,
    fn_name: str,
    fn_line_lo: int,
    call_line: int,
    name: str,
    cache: dict,
) -> bool:
    """Return True if `name` is a parameter or local binding (assignment / def / class / loop / with target)
    of the Python function `fn_name` defined at or after `fn_line_lo` and enclosing `call_line`.

    Uses CPython's own `symtable` scope analysis, so names that are only *referenced* (globals, imports
    at module level) return False. Local `import` bindings are excluded on purpose; they are handled by
    the explicit import-path check in `_verify_call_site`.
    """
    import symtable

    if not fn_name:
        return False
    fn_by_name = cache.get(rel, False)
    if fn_by_name is False:
        try:
            top = symtable.symtable(src, rel, "exec")
        except (SyntaxError, ValueError):
            top = None
        if top is None:
            fn_by_name = None
        else:
            fn_map: dict[str, list[tuple[int, object]]] = {}
            stack = [top]
            while stack:
                tbl = stack.pop()
                try:
                    children = tbl.get_children()
                except Exception:
                    children = []
                stack.extend(children)
                if tbl.get_type() == "function":
                    fn_map.setdefault(tbl.get_name(), []).append((tbl.get_lineno(), tbl))
            for lst in fn_map.values():
                lst.sort(key=lambda x: x[0])
            fn_by_name = fn_map
        cache[rel] = fn_by_name
    if not fn_by_name:
        return False

    best = None
    for ln, tbl in fn_by_name.get(fn_name, ()):
        if fn_line_lo and ln < fn_line_lo:
            continue
        if ln > call_line:
            break
        best = tbl
        break
    if best is None:
        return False
    try:
        sym = best.lookup(name)
    except KeyError:
        return False
    if sym.is_parameter():
        return True
    if sym.is_local() and not sym.is_imported() and (sym.is_assigned() or sym.is_namespace()):
        return True
    return False


def _resolve_receiver_class(scope_txt: str, recv: str) -> str | None:
    """Resolve the class of local receiver `recv` from the most recent `recv = ClassName(...)` or
    `recv: ClassName` binding in `scope_txt`. Returns the bare class name, or None if unresolved.

    Only constructor-looking callees (identifier starting with an uppercase letter) are accepted, so factory
    calls such as `recv = make_session()` remain unresolved (UNKNOWN) instead of being guessed.
    """
    best_pos = -1
    best_cls: str | None = None
    r = re.escape(recv)
    for m in re.finditer(rf"(?<![\w.]){r}\s*(?::\s*[\w.\[\], |]+?)?\s*=\s*([A-Za-z_][\w.]*)\s*\(", scope_txt):
        cand = m.group(1).rsplit(".", 1)[-1]
        if m.start() > best_pos:
            best_pos = m.start()
            best_cls = cand if cand[:1].isupper() else None
    for m in re.finditer(rf"(?<![\w.]){r}\s*:\s*(?:\"|')?([A-Za-z_][\w.]*)", scope_txt):
        if m.start() > best_pos:
            cand = m.group(1).rsplit(".", 1)[-1]
            best_pos = m.start()
            best_cls = cand if cand[:1].isupper() else None
    return best_cls


# Optional diagnostics sink: set to a list to record why call sites were treated as UNKNOWN.
SKIP_LOG: list | None = None


def _log_skip(rule: str, caller_rel: str, call_line: int) -> None:
    if SKIP_LOG is not None:
        SKIP_LOG.append((rule, caller_rel, call_line))


def _extract_py_import_statements_fast(src: str) -> str:
    """Extract top-level and nested `import` / `from ... import` statements (0-indented) for fast AST parsing."""
    imp_lines: list[str] = []
    in_cont = False
    paren_balance = 0
    in_triple: str | None = None
    for raw_ln in src.splitlines():
        st = raw_ln.strip()
        if in_triple is not None:
            if raw_ln.count(in_triple) % 2 == 1:
                in_triple = None
            continue
        if not in_cont:
            for q3 in ('"""', "'''"):
                if q3 in st and st.count(q3) % 2 == 1:
                    in_triple = q3
                    break
            if in_triple is not None:
                continue
            if st.startswith(("import ", "from ")) and not st.startswith("from __future__"):
                clean = raw_ln.lstrip()
                imp_lines.append(clean)
                no_comment = clean.split("#", 1)[0].rstrip()
                paren_balance = no_comment.count("(") - no_comment.count(")")
                in_cont = paren_balance > 0 or no_comment.endswith("\\")
        else:
            imp_lines.append(raw_ln)
            no_comment = raw_ln.split("#", 1)[0].rstrip()
            paren_balance += no_comment.count("(") - no_comment.count(")")
            in_cont = paren_balance > 0 or no_comment.endswith("\\")
    return "\n".join(imp_lines)


def _py_import_bindings(rel: str, src: str, cache: dict) -> dict[str, tuple[str, int, str | None]]:
    """Map each name bound by an import in `src` to `(module_dotted, level, imported_attr)`.

    Follows Python semantics: `import a.b.c` binds `a` (to package `a`); `import a.b as x` binds `x` to `a.b`;
    `from P import n as m` binds `m` to attribute `n` of `P` (relative `level` preserved).
    """
    key = ("bindings", rel)
    if key in cache:
        return cache[key]
    out: dict[str, tuple[str, int, str | None]] = {}
    imp_src = _extract_py_import_statements_fast(src)
    if not imp_src:
        cache[key] = out
        return out
    tree = None
    try:
        tree = ast.parse(imp_src)
    except (SyntaxError, ValueError):
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            cache[key] = out
            return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = (a.name, 0, None)
                else:
                    top = a.name.split(".", 1)[0]
                    out.setdefault(top, (top, 0, None))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                out[a.asname or a.name] = (node.module or "", node.level or 0, a.name)
    cache[key] = out
    return out


def _py_module_file(brain: "ProjectBrain", dotted: str, level: int, caller_rel: str, cache: dict) -> str | None:
    """Resolve a (possibly relative) dotted module path to an indexed `.py` / `__init__.py` file."""
    key = ("mod", dotted, level, str(Path(caller_rel).parent) if level else "")
    if key in cache:
        return cache[key]
    keys = getattr(brain, "_all_indexed_files", None) or brain._index
    res: str | None = None
    if level:
        base = Path(caller_rel).parent
        for _ in range(level - 1):
            base = base.parent
        parts = [p for p in dotted.split(".") if p]
        stem = "/".join([*base.parts, *parts])
        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
            cand = cand.lstrip("/")
            if cand in keys:
                res = cand
                break
    elif dotted:
        tail = dotted.replace(".", "/")
        for k in keys:
            if k in (f"{tail}.py", f"{tail}/__init__.py") or k.endswith((f"/{tail}.py", f"/{tail}/__init__.py")):
                if res is None or len(k) < len(res):
                    res = k
    cache[key] = res
    return res


def _py_follow_defn(brain: "ProjectBrain", rel: str | None, name: str, max_hops: int = 6) -> str | None:
    """Follow `from X import name` re-export chains starting at module `rel` until a file that defines a
    top-level function `name` is reached. Returns that file, or None if the chain cannot be resolved."""
    seen: set[str] = set()
    get_info = getattr(brain, "_get_file_info", None)
    for _ in range(max_hops):
        if not rel or rel in seen:
            return None
        seen.add(rel)
        info = (get_info(rel) if get_info is not None else brain._index.get(rel)) or {}
        if any(
            f.get("name") == name and not f.get("parent_class") and not f.get("is_nested")
            for f in info.get("functions", [])
        ):
            return rel
        sym_map = (getattr(brain, "_symbol_imports", {}) or {}).get(rel)
        if sym_map is None:
            sym_map = info.get("_resolved_sym_map") or {}
        rel = sym_map.get(name)
    return None


def _resolve_bare_name_defn(brain: "ProjectBrain", caller_rel: str, caller_src: str, name: str, cache: dict) -> str | None:
    if not caller_rel.endswith(".py"):
        return None
    b = _py_import_bindings(caller_rel, caller_src, cache).get(name)
    if b is None:
        info = brain._index.get(caller_rel) or {}
        if any(f.get("name") == name and not f.get("parent_class") and not f.get("is_nested") for f in info.get("functions", [])):
            return caller_rel
        return None
    mod, level, attr = b
    if attr is None:
        return None
    return _py_follow_defn(brain, _py_module_file(brain, mod, level, caller_rel, cache), attr)


def _resolve_qualified_defn(
    brain: "ProjectBrain", caller_rel: str, caller_src: str, prefix: str, name: str, cache: dict
) -> str | None:
    if not caller_rel.endswith(".py"):
        return None
    segs = prefix.split(".")
    b = _py_import_bindings(caller_rel, caller_src, cache).get(segs[0])
    if b is None:
        return None
    mod, level, attr = b
    if attr is None:
        # Unaliased `import a.b.c` binds `a`, so the access path is the prefix itself;
        # aliased `import a.b as x` binds `x` to module `a.b`.
        dotted = prefix if mod == segs[0] else ".".join([mod, *segs[1:]])
    else:
        dotted = ".".join([p for p in (mod, attr, *segs[1:]) if p])
    mod_file = _py_module_file(brain, dotted, level, caller_rel, cache)
    if not mod_file:
        return None
    return _py_follow_defn(brain, mod_file, name)


def verify_cross_file_contracts(
    brain: "ProjectBrain",
    focus_rels: set[str],
    patched_texts: dict[str, str] | None = None,
    pre_edit_infos: dict[str, dict] | None = None,
    *,
    lines_cache: dict[str, list[str]] | None = None,
    include_deep_semantic: bool = True,
    deadline: float | None = None,
    skip_git_head: bool = False,
) -> list[str]:
    """Universal Cross-File & Deep Semantic Contract Verifier for `brain.patch()` and `brain.check()`.

    Checks across `focus_rels` and their 1-hop graph neighbors:
      1. Call-site arity, required keyword-only args, unknown keyword args, and duplicate pos+kw args.
      2. Broken explicit symbol imports (`from X import Y` / `import { Y } from './X'`) and deleted/renamed callables.
      3. Closed return-dict / return-object key contract drift (`res['missing_key']` / `const { missingKey } = fn()`).
      4. Deep React stateful lifecycle/remount/race & Python Pydantic partial-update/IO hazards (when `include_deep_semantic=True`).
    """
    import difflib
    import time as _time
    from . import resolve
    from .parsers import parse

    diag_lines: list[str] = []
    seen_diags: set[str] = set()

    def _emit(msg: str) -> None:
        if msg not in seen_diags:
            seen_diags.add(msg)
            diag_lines.append(msg)

    info_cache: dict[str, dict] = {}
    text_cache: dict[str, str] = {}
    lines_cache = lines_cache if lines_cache is not None else {}
    _symtable_cache: dict[str, object] = {}
    _resolve_cache: dict = {}

    def _get_text(rel: str) -> str:
        if rel in text_cache:
            return text_cache[rel]
        txt = (patched_texts or {}).get(rel)
        if txt is None:
            txt = brain._get_file_text(rel) or ""
        text_cache[rel] = txt
        return txt

    def _get_lines(rel: str) -> list[str]:
        if rel in lines_cache:
            return lines_cache[rel]
        lns = _get_text(rel).splitlines()
        lines_cache[rel] = lns
        return lns

    def _get_info(rel: str) -> dict:
        if rel in info_cache:
            return info_cache[rel]
        if patched_texts and rel in patched_texts:
            inf = parse(patched_texts[rel], Path(rel).suffix.lower())
        elif hasattr(brain, "_get_file_info"):
            inf = brain._get_file_info(rel) or {}
        else:
            inf = brain._index.get(rel, {})
        info_cache[rel] = inf
        return inf

    resolved_pre: dict[str, dict] = dict(pre_edit_infos or {})
    for f_rel in focus_rels:
        if f_rel not in resolved_pre:
            pre_mem = getattr(brain, "_pre_edit_index", {}).get(f_rel)
            if pre_mem:
                resolved_pre[f_rel] = pre_mem
            elif not skip_git_head and hasattr(brain, "_get_git_head_info"):
                git_inf = brain._get_git_head_info(f_rel)
                if git_inf:
                    resolved_pre[f_rel] = git_inf

    _FRAMEWORK_DECORATOR_RE = re.compile(
        r"\b(?:property|cached_property|fixture|model_validator|field_validator|validator|root_validator|contextmanager|asynccontextmanager)\b"
    )
    _KW_ARG_EQ_RE = re.compile(r"^[A-Za-z_]\w*\s*=(?!=)")
    _LOCAL_FROM_IMPORT_RE = re.compile(r"(?m)^\s*from\s+([A-Za-z0-9_.]+)\s+import\s+([^\n#]+)")
    _SIMPLE_IDENT_FULL_RE = re.compile(r"[A-Za-z_]\w*")
    _STRIP_PY_COMMENT_RE = re.compile(r"\s+#.*$")
    _LAMBDA_WORD_RE = re.compile(r"\blambda\b")
    _QUAL_EDGE_REASON_RE = re.compile(r"^'([^']+?)\(\)'")

    def _check_args_against_fn(arg_items: list[str], callee_rel: str, cand_fn: dict, is_meth: bool) -> list[str]:
        arity = cand_fn.get("arity_spec")
        if not arity:
            return []
        pos_args = [a for a in arg_items if not _KW_ARG_EQ_RE.match(a)]
        kw_names = {
            a.split("=", 1)[0].strip()
            for a in arg_items
            if _KW_ARG_EQ_RE.match(a)
        }
        min_a = arity.get("min_args", 0)
        max_a = arity.get("max_args")
        kw_req = set(arity.get("kwonly_required", []))
        param_names = list(arity.get("param_names") or []) if arity.get("param_names") is not None else None
        pos_param_names = list(arity.get("pos_param_names") or [])
        req_pos_names = list(arity.get("required_pos_names") or [])

        # Top-level function whose 1st parameter happens to be named `cls` or `self` (e.g. `def itermro(cls, stop)`)
        sig_str = cand_fn.get("signature", "")
        if not is_meth and re.search(r"\(\s*(cls|self)\b", sig_str):
            m_first = re.search(r"\(\s*(cls|self)\b(\s*=)?", sig_str)
            first_nm = m_first.group(1) if m_first else "cls"
            has_def = bool(m_first and m_first.group(2))
            if param_names is not None and first_nm not in param_names:
                param_names.insert(0, first_nm)
            if first_nm not in pos_param_names:
                pos_param_names.insert(0, first_nm)
            if not has_def and first_nm not in req_pos_names:
                req_pos_names.insert(0, first_nm)
                min_a += 1
            if max_a is not None:
                max_a += 1

        errs: list[str] = []
        # 1A. Unknown keyword arguments (Python functions without **kwargs)
        if callee_rel.endswith(".py") and kw_names and not arity.get("has_kwargs") and param_names is not None:
            valid_params = set(param_names) | kw_req
            unknown_kws = sorted(kw_names - valid_params)
            if unknown_kws:
                hints = []
                for ukw in unknown_kws:
                    close = difflib.get_close_matches(ukw, sorted(valid_params), n=1, cutoff=0.45)
                    if close:
                        hints.append(f"`{ukw}` -> `{close[0]}`")
                hint_str = f" (did you mean {', '.join(hints)}?)" if hints else ""
                errs.append(
                    f"__KW__Call to `{cand_fn['name']}(...)` passes unexpected keyword arg(s) `{', '.join(unknown_kws)}`{hint_str} "
                    f"for signature `{cand_fn.get('signature', cand_fn['name'])}`."
                )

        # 1B. Duplicate positional + keyword arguments
        if callee_rel.endswith(".py") and kw_names and pos_param_names:
            already_pos = set(pos_param_names[: len(pos_args)])
            dup_kws = sorted(kw_names & already_pos)
            if dup_kws:
                errs.append(
                    f"__ARITY__Call to `{cand_fn['name']}(...)` passes multiple values for parameter(s) `{', '.join(dup_kws)}`."
                )

        # 1C. Arity & required keyword-only / positional obligations
        missing_kw = sorted(kw_req - kw_names)
        if callee_rel.endswith(".py") and req_pos_names:
            missing_pos = [
                p for idx, p in enumerate(req_pos_names)
                if idx >= len(pos_args) and p not in kw_names
            ]
            too_few = len(missing_pos) > 0
        else:
            valid_kw_set = (kw_names & set(param_names)) - kw_req if param_names is not None else (kw_names - kw_req)
            total_supplied = len(pos_args) + len(valid_kw_set)
            too_few = total_supplied < min_a

        too_many = max_a is not None and len(pos_args) > max_a
        if too_few or too_many or missing_kw:
            if missing_kw:
                reason_str = f"missing required keyword-only arg(s) `{', '.join(missing_kw)}`"
            else:
                reason_str = (
                    f"passes {len(arg_items)} arg(s), but updated signature `{cand_fn.get('signature', cand_fn['name'])}` "
                    f"requires {min_a}..{max_a if max_a is not None else '∞'}"
                )
            errs.append(f"__ARITY__Call to `{cand_fn['name']}(...)` {reason_str}.")
        return errs

    def _verify_call_site(
        caller_rel: str,
        caller_name: str,
        call_line: int,
        callee_rel: str,
        callee_fn: dict,
        edge_reason: str = "",
        caller_l_start: int = 0,
        caller_l_end: int = 0,
    ) -> None:
        arity = callee_fn.get("arity_spec")
        if not arity or not caller_rel or not call_line:
            return
        # Lexical scoping: a nested function is only callable from inside its enclosing function.
        callee_nested_in_caller = False
        if callee_fn.get("is_nested"):
            if caller_rel != callee_rel:
                return
            fn_l_start = callee_fn.get("line_start", 0)
            if caller_l_start and caller_l_end and not (caller_l_start < fn_l_start <= caller_l_end):
                return
            callee_nested_in_caller = True
        decs = callee_fn.get("decorators") or []
        if any(
            _FRAMEWORK_DECORATOR_RE.search(d) or d.endswith((".setter", ".deleter", ".getter"))
            for d in decs
        ):
            return
        is_meth = bool(callee_fn.get("parent_class") or callee_fn.get("is_method"))
        if is_meth and callee_rel.endswith(".py"):
            callee_l_s = callee_fn.get("line_start", 0)
            callee_l_e = callee_fn.get("line_end", 0)
            if callee_l_s and callee_l_e and (callee_l_e - callee_l_s) <= 8:
                callee_lines = _get_lines(callee_rel)
                if callee_lines:
                    callee_body_txt = "\n".join(callee_lines[callee_l_s - 1 : callee_l_e])
                    if "NotImplementedError" in callee_body_txt and "return " not in callee_body_txt:
                        return
        m_qual = _QUAL_EDGE_REASON_RE.match(edge_reason or "")
        qual_expr = m_qual.group(1) if m_qual else ""
        if is_meth and not qual_expr:
            return
        c_lines = _get_lines(caller_rel)
        if not c_lines or not (1 <= call_line <= len(c_lines)):
            return

        def _passes_disambiguation() -> bool:
            caller_src = _get_text(caller_rel)
            if not caller_src:
                return False
            if caller_l_start and 1 <= caller_l_start <= call_line:
                mod_stem_chk = Path(callee_rel).stem if caller_rel != callee_rel else ""
                fn_head_lines = "\n".join(c_lines[caller_l_start - 1 : call_line])
                if "import " in fn_head_lines:
                    for m_loc_imp in _LOCAL_FROM_IMPORT_RE.finditer(fn_head_lines):
                        imp_mod, imp_names = m_loc_imp.group(1), m_loc_imp.group(2)
                        if re.search(rf"\b{re.escape(callee_fn['name'])}\b", imp_names):
                            mod_tail = imp_mod.lstrip(".").replace(".", "/")
                            if mod_tail and mod_tail not in callee_rel:
                                return False
                        if mod_stem_chk and re.search(rf"\b{re.escape(mod_stem_chk)}\b", imp_names) and imp_mod.endswith("." + mod_stem_chk):
                            return False

            caller_simple = (caller_name or "").rsplit(".", 1)[-1]

            # (a) Bare call `name(...)`: local binding via CPython `symtable` or import to another file -> UNKNOWN.
            if not is_meth and "." not in qual_expr and not callee_nested_in_caller and caller_rel.endswith(".py"):
                if _py_scope_binds_name(
                    caller_rel, caller_src, caller_simple, caller_l_start, call_line, callee_fn["name"], _symtable_cache
                ):
                    _log_skip("a_local_binding", caller_rel, call_line)
                    return False
                if caller_rel != callee_rel:
                    final_rel = _resolve_bare_name_defn(brain, caller_rel, caller_src, callee_fn["name"], _resolve_cache)
                    if final_rel and final_rel != callee_rel:
                        _log_skip("a_import_elsewhere", caller_rel, call_line)
                        return False

            # (b) Module-qualified call `pkg.name(...)` resolving to a different defining file -> UNKNOWN.
            if not is_meth and "." in qual_expr and caller_rel != callee_rel:
                q_prefix = qual_expr.rsplit(".", 1)[0]
                final_rel = _resolve_qualified_defn(brain, caller_rel, caller_src, q_prefix, callee_fn["name"], _resolve_cache)
                if final_rel and final_rel != callee_rel:
                    _log_skip("b_qualifier_elsewhere", caller_rel, call_line)
                    return False

            # (c) Method call on a non-`self` receiver `recv.method(...)`: verify receiver class -> UNKNOWN if unresolved/mismatch.
            if is_meth and "." in qual_expr:
                recv_expr = qual_expr.rsplit(".", 1)[0]
                owner_cls = callee_fn.get("parent_class") or ""
                if recv_expr not in ("self", "cls", owner_cls) and not recv_expr.startswith("super("):
                    if not _SIMPLE_IDENT_FULL_RE.fullmatch(recv_expr):
                        _log_skip("c_receiver_complex", caller_rel, call_line)
                        return False
                    lo = max(1, caller_l_start or 1)
                    scope_txt = "\n".join(c_lines[lo - 1 : call_line])
                    resolved_cls = _resolve_receiver_class(scope_txt, recv_expr)
                    if not resolved_cls or resolved_cls != owner_cls:
                        _log_skip("c_receiver_unresolved" if not resolved_cls else "c_receiver_other_class", caller_rel, call_line)
                        return False
            return True

        if SKIP_LOG is not None and not _passes_disambiguation():
            return

        end_idx = min(len(c_lines), call_line + 25)
        if caller_l_end and caller_l_end >= call_line:
            end_idx = min(end_idx, caller_l_end)
        snippet = " ".join(
            _STRIP_PY_COMMENT_RE.sub("", ln) if "#" in ln else ln
            for ln in c_lines[call_line - 1 : end_idx]
        )
        if ".delay(" in snippet or ".apply_async(" in snippet or ".s(" in snippet or ".si(" in snippet:
            if re.search(rf"\b{re.escape(callee_fn['name'])}\.(?:delay|apply_async|s|si)\s*\(", snippet):
                return
        mod_stem = Path(callee_rel).stem if caller_rel != callee_rel else ""
        arg_items = _extract_balanced_call_args(
            snippet, callee_fn["name"], is_method=is_meth, mod_stem=mod_stem, qual_expr=qual_expr
        )
        if arg_items is None or any(a.startswith("*") or ("lambda" in a and _LAMBDA_WORD_RE.search(a)) for a in arg_items):
            return
        if (
            is_meth
            and "." in qual_expr
            and not any("classmethod" in d or "staticmethod" in d for d in decs)
            and re.match(r"^\(\s*self\b", callee_fn.get("params", ""))
            and qual_expr.rsplit(".", 1)[0].rsplit(".", 1)[-1] == (callee_fn.get("parent_class") or "")
            and arg_items
            and arg_items[0].strip() == "self"
        ):
            arg_items = arg_items[1:]

        errs = _check_args_against_fn(arg_items, callee_rel, callee_fn, is_meth)
        if not errs:
            return

        # Support overloaded / conditional definitions of the same callable in `callee_rel`
        callee_info = _get_info(callee_rel)
        overloads = [
            f for f in callee_info.get("functions", [])
            if f["name"] == callee_fn["name"] and f.get("parent_class") == callee_fn.get("parent_class")
        ] or [callee_fn]
        for alt_fn in overloads:
            if alt_fn is not callee_fn and not _check_args_against_fn(arg_items, callee_rel, alt_fn, is_meth):
                return

        # If a compatibility branch in the same snippet calls the same function again with valid args, accept it
        if qual_expr and snippet.count(f"{qual_expr}(") > 1:
            second_idx = snippet.find(f"{qual_expr}(")
            rest_snip = snippet[second_idx + len(qual_expr) + 1 :]
            alt_args = _extract_balanced_call_args(
                rest_snip, callee_fn["name"], is_method=is_meth, mod_stem=mod_stem, qual_expr=qual_expr
            )
            if alt_args is not None and not _check_args_against_fn(alt_args, callee_rel, callee_fn, is_meth):
                return

        if SKIP_LOG is None and not _passes_disambiguation():
            return

        for e_msg in errs:
            if e_msg.startswith("__KW__"):
                _emit(f"- ⚠️ **[6D Caller Keyword Contract Violation]** `{caller_rel}:L{call_line}` (`{caller_name}`) → {e_msg[6:]}")
            else:
                _emit(f"- ⚠️ **[6D Caller Arity Contract Violation]** `{caller_rel}:L{call_line}` (`{caller_name}`) → {e_msg[9:]}")

    def _verify_return_keys_for_caller(
        caller_rel: str,
        caller_name: str,
        caller_l_start: int,
        caller_l_end: int,
        callee_rel: str,
        callee_fn: dict,
        removed_keys: set[str],
    ) -> None:
        ret_keys = callee_fn.get("return_keys")
        if not ret_keys or not caller_rel:
            return
        valid_ret_keys = set(ret_keys)
        caller_src = _get_text(caller_rel)
        if not caller_src:
            return
        c_lines = _get_lines(caller_rel)
        l0 = max(1, caller_l_start)
        l1 = min(len(c_lines), max(l0, caller_l_end))
        body_slice = "\n".join(c_lines[l0 - 1 : l1])
        fn_nm = callee_fn["name"]

        # Python: `res = fn(...)` followed by `res["key"]` or `res.get("key")`
        for m_asgn in re.finditer(
            rf"(?m)^[ \t]*([A-Za-z_]\w*)\s*(?::\s*[^=]+)?=\s*(?:await\s+)?(?:\w+\.)?{re.escape(fn_nm)}\s*\(",
            body_slice,
        ):
            var_nm = m_asgn.group(1)
            tail = body_slice[m_asgn.end() :]
            if re.search(rf"\b{re.escape(var_nm)}\s*(?:\[[^\]]+\]\s*=(?!=)|\.update\s*\(|(?<![=!<>])=(?!=))", tail):
                continue
            for m_sub in re.finditer(
                rf"\b{re.escape(var_nm)}\s*(?:\[\s*['\"]([A-Za-z0-9_:-]+)['\"]\s*\]|\.get\s*\(\s*['\"]([A-Za-z0-9_:-]+)['\"]\s*\))",
                tail,
            ):
                acc_k = m_sub.group(1) or m_sub.group(2)
                if acc_k and acc_k not in valid_ret_keys:
                    line_off = body_slice[: m_asgn.end() + m_sub.start()].count("\n")
                    sub_line = l0 + line_off
                    close_k = difflib.get_close_matches(acc_k, sorted(valid_ret_keys), n=1, cutoff=0.4)
                    k_hint = f" (did you mean `{close_k[0]}`?)" if close_k else (
                        f" (`{acc_k}` was removed from `{fn_nm}`)" if acc_k in removed_keys else ""
                    )
                    _emit(
                        f"- ⚠️ **[6D Return-Shape Key Contract Violation]** `{caller_rel}:L{sub_line}` (`{caller_name}`) → "
                        f"Accesses key `{acc_k}` on result of `{fn_nm}(...)`, but `{callee_rel}:{fn_nm}` only returns keys `{sorted(valid_ret_keys)}`{k_hint}."
                    )

        # JS/TS: `const { a, b: alias } = fn(...)`
        for m_destr in re.finditer(
            rf"(?:const|let|var)\s*\{{([^}}]+)\}}\s*=\s*(?:await\s+)?(?:\w+\.)?{re.escape(fn_nm)}\s*\(",
            body_slice,
        ):
            raw_props = m_destr.group(1)
            if "..." in raw_props:
                continue
            line_off = body_slice[: m_destr.start()].count("\n")
            destr_line = l0 + line_off
            for part in raw_props.split(","):
                p_clean = part.split("=")[0].split(":")[0].strip()
                if p_clean and re.match(r"^[A-Za-z_$]\w*$", p_clean) and p_clean not in valid_ret_keys:
                    close_k = difflib.get_close_matches(p_clean, sorted(valid_ret_keys), n=1, cutoff=0.4)
                    k_hint = f" (did you mean `{close_k[0]}`?)" if close_k else ""
                    _emit(
                        f"- ⚠️ **[6D Return-Shape Key Contract Violation]** `{caller_rel}:L{destr_line}` (`{caller_name}`) → "
                        f"Destructures `{p_clean}` from `{fn_nm}(...)`, but `{callee_rel}:{fn_nm}` only returns keys `{sorted(valid_ret_keys)}`{k_hint}."
                    )

    if brain._graph:
        for f_rel in sorted(focus_rels):
            if deadline is not None and _time.perf_counter() > deadline:
                break
            new_info = _get_info(f_rel)
            old_info = resolved_pre.get(f_rel) or {}
            old_if_map = {}
            old_ret_map: dict[str, set[str]] = {}
            for ofn in old_info.get("functions", []):
                oq = f"{ofn.get('parent_class')}.{ofn['name']}" if ofn.get("parent_class") else ofn["name"]
                old_if_map[oq] = ofn.get("interface_hash", "")
                if ofn.get("return_keys"):
                    old_ret_map[oq] = set(ofn["return_keys"])

            file_was_edited = bool(patched_texts and f_rel in patched_texts) or bool(
                old_info
                and (
                    old_info.get("interface_hash") != new_info.get("interface_hash")
                    or [x.get("ast_hash") for x in old_info.get("functions", [])]
                    != [x.get("ast_hash") for x in new_info.get("functions", [])]
                )
            )

            new_fn_map = {
                (fn.get("parent_class"), fn["name"]): fn
                for fn in new_info.get("functions", [])
            }

            # Check incoming callers to functions in `f_rel`
            for fn in new_info.get("functions", []):
                q_nm = f"{fn.get('parent_class')}.{fn['name']}" if fn.get("parent_class") else fn["name"]
                nid = fn_id(f_rel, fn["name"], fn.get("parent_class"))
                iface_unchanged = bool(old_if_map and q_nm in old_if_map and old_if_map[q_nm] == fn.get("interface_hash", ""))
                removed_ret_keys = old_ret_map.get(q_nm, set()) - set(fn.get("return_keys") or [])

                for ei in brain._graph._radj.get(nid, []):
                    e_in = brain._graph.edges[ei]
                    if e_in["type"] != "calls" or e_in.get("confidence") not in ("exact", "import_resolved"):
                        continue
                    if e_in.get("reason") == "same-file function call" and fn.get("parent_class"):
                        if sum(
                            1 for o_fn in new_info.get("functions", ())
                            if o_fn["name"] == fn["name"] and o_fn.get("parent_class")
                        ) > 1:
                            continue
                    caller_nd = brain._graph.nodes.get(e_in["src"], {})
                    caller_rel = caller_nd.get("rel_path", "")
                    if skip_git_head and caller_rel not in focus_rels:
                        continue
                    call_line = e_in.get("line", 0)
                    if not (file_was_edited and iface_unchanged and caller_rel not in focus_rels):
                        _verify_call_site(
                            caller_rel,
                            caller_nd.get("name", ""),
                            call_line,
                            f_rel,
                            fn,
                            edge_reason=e_in.get("reason", ""),
                            caller_l_start=caller_nd.get("line_start", 0),
                            caller_l_end=caller_nd.get("line_end", 0),
                        )
                    if fn.get("return_keys") and not (
                        file_was_edited and iface_unchanged and not removed_ret_keys and caller_rel not in focus_rels
                    ):
                        _verify_return_keys_for_caller(
                            caller_rel,
                            caller_nd.get("name", ""),
                            caller_nd.get("line_start", call_line),
                            caller_nd.get("line_end", call_line + 25),
                            f_rel,
                            fn,
                            removed_ret_keys,
                        )

                # Same-file callers of `fn` (including when `fn` is called within `f_rel`)
                if fn.get("return_keys"):
                    fn_pcls = fn.get("parent_class")
                    for s_fn in new_info.get("functions", []):
                        if s_fn["name"] == fn["name"] and s_fn.get("parent_class") == fn_pcls:
                            continue
                        if fn["name"] not in s_fn.get("calls", []):
                            continue
                        if fn_pcls:
                            s_pcls = s_fn.get("parent_class")
                            q_calls = s_fn.get("qualified_calls") or {}
                            if s_pcls != fn_pcls and fn["name"] not in q_calls.get(fn_pcls, ()):
                                continue
                        elif any(
                            o_fn is not fn and o_fn["name"] == fn["name"]
                            for o_fn in new_info.get("functions", [])
                        ):
                            continue
                        _verify_return_keys_for_caller(
                            f_rel,
                            s_fn["name"],
                            s_fn.get("line_start", 1),
                            s_fn.get("line_end", 1),
                            f_rel,
                            fn,
                            removed_ret_keys,
                        )

            # Check outgoing calls from functions in `f_rel` to resolved callees in the repo
            for fn in new_info.get("functions", []):
                src_nid = fn_id(f_rel, fn["name"], fn.get("parent_class"))
                for ei in brain._graph._adj.get(src_nid, []):
                    e_out = brain._graph.edges[ei]
                    if e_out["type"] != "calls" or e_out.get("confidence") not in ("exact", "import_resolved"):
                        continue
                    dst_nd = brain._graph.nodes.get(e_out["dst"], {})
                    dst_rel = dst_nd.get("rel_path", "")
                    if not dst_rel or dst_rel in focus_rels:
                        continue
                    dst_info = _get_info(dst_rel)
                    dst_fn_name = dst_nd.get("name", "")
                    dst_fn = next(
                        (
                            df for df in dst_info.get("functions", [])
                            if df["name"] == dst_fn_name
                            or f"{df.get('parent_class')}.{df['name']}" == dst_fn_name
                        ),
                        None,
                    )
                    if dst_fn:
                        call_line = e_out.get("line") or fn.get("call_sites", {}).get(dst_fn["name"], 0)
                        _verify_call_site(
                            f_rel,
                            fn["name"],
                            call_line,
                            dst_rel,
                            dst_fn,
                            edge_reason=e_out.get("reason", ""),
                            caller_l_start=fn.get("line_start", 0),
                            caller_l_end=fn.get("line_end", 0),
                        )
                        if dst_fn.get("return_keys"):
                            _verify_return_keys_for_caller(
                                f_rel,
                                fn["name"],
                                fn.get("line_start", 1),
                                fn.get("line_end", 1),
                                dst_rel,
                                dst_fn,
                                set(),
                            )

            # 2A. Deleted / Renamed functions or methods compared to `old_info`
            if old_info:
                old_fn_map = {
                    (ofn.get("parent_class"), ofn["name"]): ofn
                    for ofn in old_info.get("functions", [])
                    if not ofn.get("is_nested")
                }
                added_fns = [fn for k, fn in new_fn_map.items() if k not in old_fn_map]
                for (p_cls, old_nm), old_fn in old_fn_map.items():
                    if (p_cls, old_nm) in new_fn_map:
                        continue
                    rename_cand = None
                    for afn in added_fns:
                        if afn.get("parent_class") == p_cls and (
                            afn.get("ast_hash") == old_fn.get("ast_hash")
                            or afn.get("line_start") == old_fn.get("line_start")
                        ):
                            rename_cand = f"{p_cls}.{afn['name']}" if p_cls else afn["name"]
                            break
                    if not rename_cand:
                        same_cls_names = [fn["name"] for (pc, _), fn in new_fn_map.items() if pc == p_cls]
                        close = difflib.get_close_matches(old_nm, same_cls_names, n=1, cutoff=0.45)
                        if close:
                            rename_cand = f"{p_cls}.{close[0]}" if p_cls else close[0]
                    hint = f" (renamed to `{rename_cand}`?)" if rename_cand else ""
                    dep_files = [f_rel] + list(brain._imported_by.get(f_rel, []))
                    for dep_rel in dep_files:
                        dep_info = _get_info(dep_rel)
                        if dep_rel != f_rel and not p_cls and any(df["name"] == old_nm for df in dep_info.get("functions", [])):
                            continue
                        for dep_fn in dep_info.get("functions", []):
                            if old_nm in dep_fn.get("calls", []):
                                c_line = dep_fn.get("call_sites", {}).get(old_nm, dep_fn.get("line_start", 1))
                                q_old = f"{p_cls}.{old_nm}" if p_cls else old_nm
                                _emit(
                                    f"- ⚠️ **[6D Deleted/Renamed Callable Violation]** `{dep_rel}:L{c_line}` (`{dep_fn['name']}`) → "
                                    f"Calls `{q_old}(...)` which was removed from `{f_rel}`{hint}."
                                )

    # 2B. Broken Explicit Symbol Imports (`from X import Y` / `import { Y } from './X'`)
    base_file_set = getattr(brain, "_all_indexed_files", None)
    file_set = (set(base_file_set) if base_file_set is not None else set(brain._index.keys())) | set(focus_rels)
    module_index = getattr(brain, "_python_module_index", None) or resolve.build_python_module_index(file_set)

    class _LazyIndexView:
        def get(self, k: str, default: object = None) -> object:
            inf = _get_info(k)
            return inf if inf else default

    combined_index = _LazyIndexView() if getattr(brain, "_is_partial_index", False) else {
        **brain._index,
        **{r: _get_info(r) for r in focus_rels},
    }
    def _top_defined_syms(inf: dict) -> set[str]:
        return (
            {f["name"] for f in inf.get("functions", []) if not f.get("parent_class")}
            | {c["name"] for c in inf.get("classes", [])}
            | set(inf.get("exports", []))
            | {k for k in (inf.get("import_bindings") or {}) if not k.startswith("__star__")}
        )

    files_to_check_imports: set[str] = set(focus_rels)
    for r in focus_rels:
        old_r = resolved_pre.get(r, {})
        new_r = _get_info(r)
        r_edited = bool(patched_texts and r in patched_texts) or bool(
            old_r
            and (
                old_r.get("interface_hash") != new_r.get("interface_hash")
                or [x.get("ast_hash") for x in old_r.get("functions", [])]
                != [x.get("ast_hash") for x in new_r.get("functions", [])]
                or [x.get("ast_hash") for x in old_r.get("classes", [])]
                != [x.get("ast_hash") for x in new_r.get("classes", [])]
            )
        )
        if not skip_git_head and (not r_edited or not old_r or bool(_top_defined_syms(old_r) - _top_defined_syms(new_r))):
            files_to_check_imports.update(brain._imported_by.get(r, []))

    for imp_rel in sorted(files_to_check_imports):
        if deadline is not None and _time.perf_counter() > deadline:
            break
        imp_info = _get_info(imp_rel)
        for local_name, bind in (imp_info.get("import_bindings") or {}).items():
            if local_name.startswith("__star__"):
                continue
            sym = bind.get("symbol", "")
            if not sym or sym in ("*", "default") or "." in sym:
                continue
            mod = bind.get("module", "")
            qual_mod = bind.get("qualified_module", "")
            if qual_mod and imp_rel.endswith(".py"):
                if resolve.resolve_python_import(imp_rel, qual_mod, file_set, module_index) is not None:
                    continue
            target_rel = resolve.resolve_import(imp_rel, mod, file_set, module_index)
            if not target_rel or target_rel == imp_rel:
                continue
            if (
                imp_rel.endswith(".py")
                and "." not in mod
                and not mod.startswith(".")
                and target_rel.count("/") >= 2
                and Path(imp_rel).parent != Path(target_rel).parent
            ):
                continue
            unwrapped_rel = resolve.unwrap_barrel_reexport(target_rel, sym, combined_index, file_set, module_index)
            if unwrapped_rel != target_rel:
                re_bind = (_get_info(target_rel).get("import_bindings") or {}).get(sym)
                if re_bind and re_bind.get("qualified_module"):
                    if resolve.resolve_python_import(target_rel, re_bind["qualified_module"], file_set, module_index) == unwrapped_rel:
                        continue
            if unwrapped_rel not in focus_rels and target_rel not in focus_rels and imp_rel not in focus_rels:
                continue
            if unwrapped_rel.endswith("__init__.py") and unwrapped_rel not in focus_rels:
                continue
            target_info = _get_info(unwrapped_rel)
            if target_info.get("parse_status") not in (None, "ok"):
                continue
            defined_syms = (
                {f["name"] for f in target_info.get("functions", []) if not f.get("parent_class")}
                | {c["name"] for c in target_info.get("classes", [])}
                | set(target_info.get("exports", []))
                | {k for k in (target_info.get("import_bindings") or {}) if not k.startswith("__star__")}
            )
            if sym in defined_syms or any(
                k.startswith("__star__") for k in (target_info.get("import_bindings") or {})
            ):
                continue
            target_txt = _get_text(unwrapped_rel)
            if target_txt and re.search(rf"\bfrom\s+\S+\s+import\s+\*|\bexport\s+\*\s+from\b|\bdef\s+__getattr__\s*\(", target_txt):
                continue
            if target_txt and re.search(rf"\b{re.escape(sym)}\b", target_txt):
                if (
                    re.search(
                        rf"(?m)^[ \t]*(?:(?:module\.)?exports\.|export\s+)?(?:const|let|var|type|interface|enum|function|class|def|async\s+def)?[ \t]*\b{re.escape(sym)}\b\s*(?:[:=(]|$)",
                        target_txt,
                    )
                    or re.search(
                        rf"(?m)^[ \t]*(?:[A-Za-z_]\w*[ \t]*(?:,[ \t]*|=(?!=)[ \t]*))*\b{re.escape(sym)}\b[\w., \t]*=(?!=)",
                        target_txt,
                    )
                    or re.search(rf"\bimport\s+\(?[^;\n#)]*\b{re.escape(sym)}\b", target_txt)
                    or (unwrapped_rel.endswith("__init__.py") and re.search(rf"['\"]{re.escape(sym)}['\"]", target_txt))
                ):
                    continue
            close = difflib.get_close_matches(sym, sorted(defined_syms), n=1, cutoff=0.45)
            hint = f" (did you mean `{close[0]}`?)" if close else ""
            imp_line = bind.get("line", 1)
            _emit(
                f"- ⚠️ **[6D Broken Import / Deleted Symbol Violation]** `{imp_rel}:L{imp_line}` → "
                f"Cannot import `{sym}` from `{unwrapped_rel}` (symbol does not exist in `{unwrapped_rel}`{hint})."
            )

    if include_deep_semantic and focus_rels:
        from .reporters import _is_test_file_path
        from .semantic_contracts import (
            _JS_TS_EXTS,
            analyze_python_semantic_hazards,
            analyze_react_lifecycle_and_race_contracts,
            build_resolved_schema_registry,
        )

        sem_warnings: list[str] = list(getattr(brain, "_last_http_warnings", None) or ())
        schema_reg: dict | None = None
        for s_rel in sorted(focus_rels):
            if _is_test_file_path(s_rel):
                continue
            ext = Path(s_rel).suffix.lower()
            if ext in _JS_TS_EXTS:
                s_txt = _get_text(s_rel)
                if s_txt:
                    r_errs, r_hots = analyze_react_lifecycle_and_race_contracts(s_rel, s_txt, brain)
                    for e_msg in r_errs:
                        _emit(e_msg)
                    for rh in r_hots:
                        if rh.get("severity") != "error" and rh.get("_diag_msg"):
                            if rh["_diag_msg"] not in sem_warnings:
                                sem_warnings.append(rh["_diag_msg"])
            elif ext == ".py":
                s_txt = _get_text(s_rel)
                if s_txt:
                    if schema_reg is None:
                        schema_reg = build_resolved_schema_registry(brain)
                    p_errs, p_hots = analyze_python_semantic_hazards(s_rel, s_txt, brain, schema_registry=schema_reg)
                    for e_msg in p_errs:
                        _emit(e_msg)
                    for ph in p_hots:
                        if ph.get("severity") != "error" and ph.get("_diag_msg"):
                            if ph["_diag_msg"] not in sem_warnings:
                                sem_warnings.append(ph["_diag_msg"])
        brain._last_semantic_warnings = sem_warnings
    else:
        brain._last_semantic_warnings = list(getattr(brain, "_last_http_warnings", None) or ())

    return diag_lines

