"""Surgical AST Slicer and Skeleton Compiler for ProjectBrain.

Instead of returning bare file paths (which forces the LLM agent to call
`view_file` on entire 1,000-line files), this module slices:
  1. Exact AST source code (with line numbers) for target functions/classes,
     using smart focus-window folding on monolith functions instead of blind
     head truncation.
  2. Module-level constants, configuration objects, and bootstrap blocks so
     function slices are 100% self-contained.
  3. Signature-only skeletons (parameters, return types, docstrings, class
     schemas — bodies stripped!) for 1-hop dependencies and callers.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from .parsers import _PARSERS, get_parser

_IMPORTANT_CODE_PAT = re.compile(
    r"\b(setInterval|setTimeout|clearTimeout|clearInterval|addEventListener|removeEventListener|fetch|axios|Promise\.race|AbortController|AbortSignal|"
    r"catch|throw|finally|document\.cookie|localStorage|sessionStorage|"
    r"(?:set|update|get|add|delete|write|run|on)[A-Z][a-zA-Z0-9_]*|hydrate|flush|persist|dispatch|commit|execute|"
    r"[a-zA-Z0-9_$]+(?:\.current)?\.(?:get|set|has|add|delete|put|write|save|update|upsert|setItem|getItem)|"
    r"[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b"
)
_UPPER_CONST_PAT = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_CODE_STMT_PAT = re.compile(
    r"^\s*(?:const\s|let\s|var\s|if\s*\(|}\s*else\b|else\b|for\s*\(|while\s*\(|try\s*\{|catch\s*\(|return\b|await\s|throw\s|"
    r"def\s|async\s|elif\s|except\b|with\s|raise\s|"
    r"[a-zA-Z_$][a-zA-Z0-9_$]*\s*\(|"
    r"[a-zA-Z0-9_$.]+\.[a-zA-Z0-9_$]+\s*[=(])"
)
_STYLE_PROP_PAT = re.compile(
    r"^\s*(?:display|flex|flexDirection|flexWrap|flexShrink|alignItems|justifyContent|gap|padding\w*|margin\w*|"
    r"width|minWidth|maxWidth|height|minHeight|maxHeight|border\w*|borderRadius|background\w*|color|fontSize|"
    r"fontWeight|fontFamily|lineHeight|letterSpacing|textAlign|textTransform|textDecoration|cursor|transition|"
    r"transform|opacity|overflow\w*|position|top|right|bottom|left|zIndex|boxShadow|outline|whiteSpace|"
    r"grid\w*|objectFit|pointerEvents|userSelect|aspectRatio|filter|backdropFilter)\s*:"
)
_KV_PROP_PAT = re.compile(
    r"^\s*['\"]?[a-zA-Z_$][a-zA-Z0-9_$]*['\"]?\s*:\s*[^/\s]"
)
_JSX_NOISE_PAT = re.compile(
    r"^\s*(?:</?(?:div|span|p|svg|path|circle|rect|line|polyline|polygon|g|h[1-6]|ul|li|label|section|header|footer|main|aside|nav)[\s>]|"
    r"style=\{\{|className=|d=\"M\d|stroke=|fill=|strokeWidth=|strokeLinecap=|strokeLinejoin=|viewBox=|xmlns=)",
    re.IGNORECASE,
)

_PDG_NOISE_VARS = {
    "self", "cls", "this", "true", "false", "none", "null", "undefined",
    "err", "error", "e", "ex", "exc", "i", "j", "k", "x", "v", "fn",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "len",
    "range", "enumerate", "isinstance", "getattr", "setattr", "hasattr",
    "type", "super", "print", "return", "await", "async", "const", "let", "var",
}
_TS_CTRL_NODE_TYPES = {
    "if_statement", "for_statement", "for_in_statement", "for_of_statement",
    "while_statement", "do_statement", "switch_statement", "switch_case",
    "switch_default", "try_statement", "catch_clause", "finally_clause",
    "with_statement", "match_expression", "match_arm", "enhanced_for_statement",
}
_TS_FUNC_NODE_TYPES = {
    "function_declaration", "function_expression", "arrow_function",
    "method_definition", "generator_function_declaration", "function_item",
    "method_declaration", "Constructor_declaration",
}
_TS_SINK_NODE_TYPES = {
    "return_statement", "throw_statement", "yield_expression",
}
_TS_ID_NODE_TYPES = {
    "identifier", "property_identifier", "shorthand_property_identifier",
    "shorthand_property_identifier_pattern", "field_identifier",
}


def _analyze_treesitter_cst_pdg(
    selected_lines: list[str],
    ext: str,
    docstring_lines: set[int],
    defs: list[set[str]],
    uses: list[set[str]],
    control_parent: dict[int, int],
    sig_lines: set[int],
    sink_lines: set[int],
    stmt_spans: dict[int, range],
) -> bool:
    """Populate `defs`, `uses`, `control_parent`, `sig_lines`, and `sink_lines` using Tree-sitter CST."""
    parser = get_parser(ext)
    if parser is None:
        return False
    n = len(selected_lines)
    if n == 0:
        return False
    raw_src = "\n".join(selected_lines)
    try:
        tree = parser.parse(raw_src.encode("utf-8", errors="replace"))
    except Exception:
        return False
    root = tree.root_node
    if root is None:
        return False

    lhs_def_node_ids: set[int] = set()

    def _collect_lhs_identifiers(lhs_node, span_lines: range) -> None:
        if lhs_node is None:
            return
        stack = [lhs_node]
        while stack:
            cur = stack.pop()
            ntype = cur.type
            if ntype in ("object_assignment_pattern", "assignment_pattern", "required_parameter", "optional_parameter"):
                left_ch = cur.child_by_field_name("left") or cur.child_by_field_name("pattern") or cur.child_by_field_name("name")
                if left_ch is not None:
                    stack.append(left_ch)
                continue
            if ntype == "pair_pattern":
                val_ch = cur.child_by_field_name("value")
                if val_ch is not None:
                    stack.append(val_ch)
                continue
            if ntype in ("subscript_expression", "index_expression"):
                obj_ch = cur.child_by_field_name("object")
                if obj_ch is not None:
                    stack.append(obj_ch)
                continue
            if ntype in _TS_ID_NODE_TYPES:
                lhs_def_node_ids.add(cur.id)
                nm = cur.text.decode("utf-8", errors="replace").strip().lower()
                if len(nm) >= 2 and nm not in _PDG_NOISE_VARS:
                    ln0 = cur.start_point[0]
                    if 0 <= ln0 < n and ln0 not in docstring_lines:
                        defs[ln0].add(nm)
                    for sp_ln in span_lines:
                        if 0 <= sp_ln < n and sp_ln not in docstring_lines:
                            defs[sp_ln].add(nm)
            for ch in cur.children:
                stack.append(ch)

    # First pass: walk CST for declarations, parameters, sinks, and control parents
    stack = [root]
    while stack:
        nd = stack.pop()
        ntype = nd.type
        s_ln = nd.start_point[0]
        e_ln = nd.end_point[0]

        if ntype in _TS_FUNC_NODE_TYPES:
            if s_ln <= 4 and 0 <= s_ln < n:
                sig_lines.add(s_ln)
            params_nd = nd.child_by_field_name("parameters") or nd.child_by_field_name("type_parameters")
            if params_nd is not None:
                p_start = max(0, params_nd.start_point[0])
                p_end = min(n - 1, params_nd.end_point[0])
                for p_ln in range(p_start, p_end + 1):
                    if p_ln <= 8:
                        sig_lines.add(p_ln)
                _collect_lhs_identifiers(params_nd, range(p_start, p_start + 1))

        elif ntype in ("formal_parameters", "parameters") and s_ln <= 6:
            p_start = max(0, s_ln)
            p_end = min(n - 1, e_ln)
            for p_ln in range(p_start, p_end + 1):
                if p_ln <= 8:
                    sig_lines.add(p_ln)
            _collect_lhs_identifiers(nd, range(p_start, p_start + 1))

        elif ntype == "variable_declarator":
            decl_parent = nd.parent
            stmt_s = decl_parent.start_point[0] if decl_parent is not None else s_ln
            stmt_e = decl_parent.end_point[0] if decl_parent is not None else e_ln
            span = range(stmt_s, stmt_e + 1) if (0 <= stmt_e - stmt_s <= 6) else range(s_ln, s_ln + 1)
            for sp_ln in span:
                stmt_spans[sp_ln] = span
            _collect_lhs_identifiers(nd.child_by_field_name("name"), span)

        elif ntype in ("assignment_expression", "augmented_assignment_expression", "short_var_declaration", "assignment_statement"):
            span = range(s_ln, e_ln + 1) if (0 <= e_ln - s_ln <= 6) else range(s_ln, s_ln + 1)
            for sp_ln in span:
                stmt_spans[sp_ln] = span
            _collect_lhs_identifiers(nd.child_by_field_name("left"), span)

        elif ntype == "let_declaration":
            span = range(s_ln, e_ln + 1) if (0 <= e_ln - s_ln <= 6) else range(s_ln, s_ln + 1)
            for sp_ln in span:
                stmt_spans[sp_ln] = span
            _collect_lhs_identifiers(nd.child_by_field_name("pattern"), span)

        elif ntype in ("for_in_statement", "for_of_statement", "enhanced_for_statement"):
            _collect_lhs_identifiers(nd.child_by_field_name("left") or nd.child_by_field_name("name"), range(s_ln, s_ln + 1))

        elif ntype == "catch_clause":
            _collect_lhs_identifiers(nd.child_by_field_name("parameter") or nd.child_by_field_name("body"), range(s_ln, s_ln + 1))

        if ntype in _TS_SINK_NODE_TYPES:
            for k_ln in range(max(0, s_ln), min(n, e_ln + 1)):
                if k_ln not in docstring_lines:
                    sink_lines.add(k_ln)

        # Control parent mapping for every line inside a CST node
        if 0 <= s_ln < n and s_ln not in control_parent and s_ln not in docstring_lines:
            curr = nd.parent
            while curr is not None:
                if curr.type in _TS_CTRL_NODE_TYPES:
                    c_ln = curr.start_point[0]
                    if 0 <= c_ln < s_ln:
                        control_parent[s_ln] = c_ln
                        break
                curr = curr.parent

        for ch in reversed(nd.children):
            stack.append(ch)

    # Second pass: collect all identifier and string-literal uses, propagating multi-line declaration RHS uses
    stack = [root]
    while stack:
        nd = stack.pop()
        ntype = nd.type
        ln0 = nd.start_point[0]
        if 0 <= ln0 < n and ln0 not in docstring_lines:
            if ntype in _TS_ID_NODE_TYPES and nd.id not in lhs_def_node_ids:
                nm = nd.text.decode("utf-8", errors="replace").strip().lower()
                if len(nm) >= 2 and nm not in _PDG_NOISE_VARS:
                    uses[ln0].add(nm)
                    # If inside a multi-line variable_declarator / assignment, also credit the declaration header lines
                    curr_p = nd.parent
                    while curr_p is not None:
                        if curr_p.type in ("lexical_declaration", "variable_declaration", "variable_declarator", "assignment_expression", "short_var_declaration", "let_declaration"):
                            ps = curr_p.start_point[0]
                            pe = curr_p.end_point[0]
                            if 0 <= pe - ps <= 6:
                                for sp_ln in range(max(0, ps), min(n, pe + 1)):
                                    if sp_ln not in docstring_lines and nm not in defs[sp_ln]:
                                        uses[sp_ln].add(nm)
                            break
                        curr_p = curr_p.parent
            elif ntype in ("string_fragment", "string_literal", "interpreted_string_literal"):
                s_val = nd.text.decode("utf-8", errors="replace").strip("\"'` ")
                if 3 <= len(s_val) <= 40 and re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", s_val):
                    uses[ln0].add(s_val.lower())
        for ch in reversed(nd.children):
            stack.append(ch)

    return any(defs) or any(uses)



def _detect_leading_docstring(selected_lines: list[str]) -> tuple[int, int, int] | None:
    """Detect a leading multi-line docstring or JSDoc comment near the top of `selected_lines`.

    Returns `(doc_start_idx0, fold_start_idx0, doc_end_idx0)` (0-based inclusive indices)
    where `fold_start_idx0 .. doc_end_idx0` can be folded while keeping the opening
    1-line summary at `doc_start_idx0` (and `doc_start_idx0 + 1` if opening line is bare `\"\"\"`).
    """
    n = len(selected_lines)
    if n < 4:
        return None
    idx = 0
    in_sig = False
    paren_depth = 0
    while idx < min(n, 18):
        st = selected_lines[idx].strip()
        if not st or st.startswith(("#", "//")):
            idx += 1
            continue
        if st.startswith("@") and not in_sig:
            idx += 1
            continue
        if not in_sig and re.match(r"^(?:async\s+def|def|class|export\s+(?:default\s+)?(?:async\s+)?function|function)\b", st):
            in_sig = True
        if in_sig:
            paren_depth += st.count("(") - st.count(")")
            idx += 1
            if paren_depth <= 0 and (st.endswith(":") or st.endswith("{")):
                in_sig = False
                break
            continue
        break

    while idx < min(n, 20) and not selected_lines[idx].strip():
        idx += 1
    if idx >= n:
        return None

    first_st = selected_lines[idx].strip()
    # Python triple-quoted docstring
    m_py = re.match(r"^[rRuUfFbB]{0,2}(\"\"\"|\'\'\')", first_st)
    if m_py:
        q = m_py.group(1)
        after_open = first_st[m_py.end():]
        if q in after_open:
            return None  # Single-line docstring
        doc_start = idx
        for j in range(idx + 1, n):
            if q in selected_lines[j]:
                doc_end = j
                # Keep the first summary line of the docstring
                fold_start = doc_start + 1 if after_open.strip() else doc_start + 2
                if doc_end - fold_start + 1 >= 2:
                    return (doc_start, fold_start, doc_end)
                return None
        return None

    # JSDoc / C-style block comment
    if first_st.startswith("/*") and "*/" not in first_st:
        doc_start = idx
        after_open = first_st[2:].lstrip("*").strip()
        for j in range(idx + 1, n):
            if "*/" in selected_lines[j]:
                doc_end = j
                fold_start = doc_start + 1 if after_open else doc_start + 2
                if doc_end - fold_start + 1 >= 2:
                    return (doc_start, fold_start, doc_end)
                return None
    return None


def compute_multidim_program_slice(
    selected_lines: list[str],
    line_start: int,
    focus_terms: list[str] | None = None,
    ext: str = ".py",
) -> dict:
    """Compute a Multi-Dimensional Program Dependence Graph (PDG) slice over `selected_lines`.

    Traces three orthogonal program-slicing dimensions around statements matching `focus_terms`:
      1. Dimension Z_bwd (Backward Data-Dependence Slice): `def -> use` chains locating prior
         statements in the function that define/mutate variables consumed at criterion lines.
      2. Dimension Z_fwd (Forward Data-Dependence Slice): `def -> use` chains locating downstream
         statements that read, validate, branch on, or return variables defined at criterion lines.
      3. Dimension C_guard (Upward Control-Dependence Slice): Enclosing `if`/`elif`/`else`,
         `try`/`except`/`finally`, `for`/`while`, `with`, or `switch`/`case` guard headers
         governing the criterion and data-dependent statements.
    """
    n = len(selected_lines)
    terms = [t.lower() for t in (focus_terms or []) if len(t) >= 3]
    if n == 0:
        return {
            "seed_lines": set(),
            "backward_def_lines": set(),
            "forward_use_lines": set(),
            "control_guard_lines": set(),
            "traced_vars": [],
        }

    defs: list[set[str]] = [set() for _ in range(n)]
    uses: list[set[str]] = [set() for _ in range(n)]
    control_parent: dict[int, int] = {}
    docstring_lines: set[int] = set()
    sig_lines: set[int] = set()
    sink_lines: set[int] = set()

    doc_bounds = _detect_leading_docstring(selected_lines)
    if doc_bounds is not None:
        for d_i in range(doc_bounds[0], doc_bounds[2] + 1):
            docstring_lines.add(d_i)

    parsed_py_ast = False
    if ext == ".py":
        import textwrap
        try:
            dedented = textwrap.dedent("\n".join(selected_lines))
            tree = ast.parse(dedented)
            parent_map: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for ch in ast.iter_child_nodes(node):
                    parent_map[ch] = node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    fn_ln = getattr(node, "lineno", 1) - 1
                    if fn_ln <= 4:
                        sig_lines.add(fn_ln)
                    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                        ds_s = getattr(node.body[0], "lineno", 1) - 1
                        ds_e = getattr(node.body[0], "end_lineno", ds_s + 1) - 1
                        for d_i in range(max(0, ds_s), min(n, ds_e + 1)):
                            docstring_lines.add(d_i)
            _CTRL_TYPES = (
                ast.If, ast.For, ast.AsyncFor, ast.While,
                ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler,
            )
            for node in ast.walk(tree):
                lineno = getattr(node, "lineno", None)
                if lineno is None or not (1 <= lineno <= n):
                    continue
                idx0 = lineno - 1
                if idx0 in docstring_lines:
                    continue
                if isinstance(node, ast.arg):
                    a_low = node.arg.lower()
                    if len(a_low) >= 2 and a_low not in _PDG_NOISE_VARS:
                        defs[idx0].add(a_low)
                        sig_lines.add(idx0)
                elif isinstance(node, ast.Name):
                    v_low = node.id.lower()
                    if len(v_low) >= 2 and v_low not in _PDG_NOISE_VARS:
                        if isinstance(node.ctx, ast.Store):
                            defs[idx0].add(v_low)
                        elif isinstance(node.ctx, ast.Load):
                            uses[idx0].add(v_low)
                elif isinstance(node, ast.Attribute):
                    attr_low = node.attr.lower()
                    if len(attr_low) >= 3 and attr_low not in _PDG_NOISE_VARS:
                        if isinstance(node.ctx, ast.Store):
                            defs[idx0].add(attr_low)
                        elif isinstance(node.ctx, ast.Load):
                            uses[idx0].add(attr_low)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    eh_low = node.name.lower()
                    if len(eh_low) >= 2 and eh_low not in _PDG_NOISE_VARS:
                        defs[idx0].add(eh_low)
                elif isinstance(node, (ast.Return, ast.Raise, ast.Yield, ast.YieldFrom)):
                    sink_lines.add(idx0)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    s_val = node.value.strip()
                    if 3 <= len(s_val) <= 40 and re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", s_val):
                        uses[idx0].add(s_val.lower())
                if isinstance(node, ast.stmt) and idx0 not in control_parent:
                    curr = parent_map.get(node)
                    while curr is not None:
                        if isinstance(curr, _CTRL_TYPES):
                            p_ln = getattr(curr, "lineno", 0) - 1
                            if 0 <= p_ln < idx0:
                                control_parent[idx0] = p_ln
                                break
                        curr = parent_map.get(curr)
            parsed_py_ast = True
        except SyntaxError:
            parsed_py_ast = False

    stmt_spans: dict[int, range] = {}
    parsed_cst = False
    if not parsed_py_ast and ext.lower() != ".py":
        parsed_cst = _analyze_treesitter_cst_pdg(
            selected_lines,
            ext.lower(),
            docstring_lines,
            defs,
            uses,
            control_parent,
            sig_lines,
            sink_lines,
            stmt_spans,
        )

    if not parsed_py_ast and not parsed_cst:
        # Polyglot statement-level def/use & indentation/brace control-dependence analyzer
        ctrl_stack: list[tuple[int, int]] = []  # (indent_len, line_idx0)
        _CTRL_HEADER_PAT = re.compile(
            r"^\s*(?:\}\s*)?(?:if|elif|else\s+if|else|for|while|try|except|catch|finally|with|switch|case)\b"
        )
        for i, raw in enumerate(selected_lines):
            if i in docstring_lines:
                continue
            st = raw.strip()
            if not st or st.startswith(("#", "//", "/*")):
                continue
            if i == 0 and re.match(r"^(?:async\s+def|def|class|export|function)\b", st):
                sig_lines.add(i)
            if re.match(r"^(?:return|raise|throw|yield)\b", st):
                sink_lines.add(i)
            ind = len(raw) - len(raw.lstrip())
            while ctrl_stack and ctrl_stack[-1][0] >= ind and not st.startswith(("elif", "else", "except", "catch", "finally", "} else", "} catch")):
                ctrl_stack.pop()
            if ctrl_stack and ctrl_stack[-1][1] < i:
                control_parent[i] = ctrl_stack[-1][1]
            if _CTRL_HEADER_PAT.match(raw) and (st.endswith((":", "{")) or "case " in st):
                ctrl_stack.append((ind, i))

            # Extract LHS defs
            m_js_decl = re.match(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", raw)
            if m_js_decl:
                v_nm = m_js_decl.group(1).lower()
                if len(v_nm) >= 2 and v_nm not in _PDG_NOISE_VARS:
                    defs[i].add(v_nm)
            m_js_destr = re.match(r"^\s*(?:const|let|var)\s+[\{\[]([^\}\]]+)[\}\]]\s*=", raw)
            if m_js_destr:
                for part in m_js_destr.group(1).split(","):
                    v_nm = part.split(":")[-1].split("=")[0].strip().lower()
                    if re.match(r"^[a-z_$][\w$]*$", v_nm) and len(v_nm) >= 2 and v_nm not in _PDG_NOISE_VARS:
                        defs[i].add(v_nm)
            m_py_lhs = re.match(r"^\s*([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)\s*(?::\s*[^=]+)?=\s*[^=]", raw)
            if m_py_lhs:
                for part in m_py_lhs.group(1).split(","):
                    v_nm = part.strip().lower()
                    if len(v_nm) >= 2 and v_nm not in _PDG_NOISE_VARS:
                        defs[i].add(v_nm)
                rhs_part = raw.split("=", 1)[1]
            else:
                rhs_part = raw
            for id_m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", rhs_part):
                id_low = id_m.lower()
                if id_low not in _PDG_NOISE_VARS and id_low not in defs[i]:
                    uses[i].add(id_low)
            for sk_m in re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_.\-]{2,40})['\"]", rhs_part):
                uses[i].add(sk_m.lower())

    # Identify slicing criterion seed lines (excluding docstring prose and bare signature headers)
    seed_lines: set[int] = set()
    if terms:
        for i, raw in enumerate(selected_lines):
            if i in docstring_lines or i in sig_lines:
                continue
            st = raw.strip()
            if not st or st.startswith(("#", "//")):
                continue
            low = st.lower()
            row_syms = defs[i] | uses[i]
            if any(t in row_syms or t in low for t in terms):
                seed_lines.add(i)
    if not seed_lines and sink_lines:
        seed_lines = set(sink_lines)

    backward_def_lines: set[int] = set()
    forward_use_lines: set[int] = set()
    control_guard_lines: set[int] = set()
    traced_vars: list[str] = []

    # 1. Backward Data-Dependence Slicing (use -> preceding def, up to 2 hops)
    frontier_bwd = set(seed_lines)
    for _hop in range(2):
        next_bwd: set[int] = set()
        for idx in sorted(frontier_bwd):
            for v in uses[idx]:
                for prev in range(idx - 1, -1, -1):
                    if v in defs[prev]:
                        for sp_ln in stmt_spans.get(prev, (prev,)):
                            if sp_ln not in seed_lines and sp_ln not in backward_def_lines and sp_ln not in sig_lines:
                                backward_def_lines.add(sp_ln)
                                next_bwd.add(sp_ln)
                        if v not in traced_vars:
                            traced_vars.append(v)
                        break
        frontier_bwd = next_bwd
        if not frontier_bwd:
            break

    # 2. Forward Data-Dependence Slicing (def -> downstream use/return/branch, up to 2 hops)
    frontier_fwd = set(seed_lines) | backward_def_lines
    for _hop in range(2):
        next_fwd: set[int] = set()
        for idx in sorted(frontier_fwd):
            for v in defs[idx]:
                hits_for_v = 0
                for nxt in range(idx + 1, n):
                    if v in uses[nxt]:
                        for sp_ln in stmt_spans.get(nxt, (nxt,)):
                            if sp_ln not in seed_lines and sp_ln not in forward_use_lines:
                                forward_use_lines.add(sp_ln)
                                next_fwd.add(sp_ln)
                        if v not in traced_vars:
                            traced_vars.append(v)
                        hits_for_v += 1
                        if hits_for_v >= 4:
                            break
                    if v in defs[nxt] and v not in uses[nxt]:
                        break
        frontier_fwd = next_fwd
        if not frontier_fwd:
            break

    # 3. Upward Control-Dependence Slicing (include enclosing if/try/except/for/with headers)
    active_lines = seed_lines | backward_def_lines | forward_use_lines
    for idx in list(active_lines):
        curr = control_parent.get(idx)
        depth = 0
        while curr is not None and depth < 3:
            if curr not in seed_lines:
                control_guard_lines.add(curr)
            curr = control_parent.get(curr)
            depth += 1

    return {
        "seed_lines": seed_lines,
        "backward_def_lines": backward_def_lines,
        "forward_use_lines": forward_use_lines,
        "control_guard_lines": control_guard_lines,
        "traced_vars": traced_vars[:12],
        "docstring_bounds": doc_bounds,
    }


def _format_pdg_slice_banner(
    pdg_slice: dict,
    start_idx: int,
    keep: list[bool] | None = None,
) -> str:
    """Format a single-line provenance header showing PDG backward/forward/guard lines."""
    def _to_lns(idx_set: set[int]) -> list[int]:
        return [
            start_idx + i + 1
            for i in sorted(idx_set)
            if keep is None or (0 <= i < len(keep) and keep[i])
        ]

    bwd_lns = _to_lns(pdg_slice.get("backward_def_lines", set()))
    fwd_lns = _to_lns(pdg_slice.get("forward_use_lines", set()))
    grd_lns = _to_lns(pdg_slice.get("control_guard_lines", set()))
    if not (bwd_lns or fwd_lns or grd_lns):
        return ""
    seed_lns = _to_lns(pdg_slice.get("seed_lines", set()))
    parts: list[str] = []
    if seed_lns:
        parts.append("seeds=" + ",".join(f"L{x}" for x in seed_lns[:4]))
    if bwd_lns:
        parts.append("bwd_defs=" + ",".join(f"L{x}" for x in bwd_lns[:4]))
    if fwd_lns:
        parts.append("fwd_uses=" + ",".join(f"L{x}" for x in fwd_lns[:4]))
    if grd_lns:
        parts.append("guards=" + ",".join(f"L{x}" for x in grd_lns[:4]))
    tvars = pdg_slice.get("traced_vars", [])
    if tvars:
        parts.append("vars=" + ",".join(tvars[:5]))
    return f"# [PDG Slice: {' | '.join(parts)}]"


def slice_lines(
    root: Path,
    rel_path: str,
    line_start: int,
    line_end: int,
    max_lines: int = 80,
    focus_terms: list[str] | None = None,
    pin_lines: set[int] | list[int] | None = None,
    inline_annotations: dict[int, list[str]] | None = None,
    fold_docstrings: bool = True,
) -> str:
    """Read [line_start..line_end] (1-indexed, inclusive) from root/rel_path,
    prefixed with line numbers.

    Performs Multi-Dimensional Program Slicing (tracing AST backward `def -> use`
    data dependencies, forward `def -> use` consumers, and upward control-flow
    guards), automatic multi-line docstring folding, and smart focus-window folding.
    """
    full_path = root / rel_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        from .store import SQLiteIndexStore
        blob_txt = SQLiteIndexStore(root / ".projectbrain.db").get_source_blob(rel_path)
        if not blob_txt:
            return ""
        lines = blob_txt.splitlines()
    start_idx = max(line_start - 1, 0)
    end_idx = min(line_end, len(lines))
    selected = lines[start_idx:end_idx]
    total_len = len(selected)
    if total_len == 0:
        return ""

    def _emit_kept_line(idx_0: int, out_list: list[str]) -> None:
        actual_ln = start_idx + idx_0 + 1
        raw_line = selected[idx_0]
        if inline_annotations and actual_ln in inline_annotations:
            indent_m = re.match(r"^(\s*)", raw_line)
            ind = indent_m.group(1) if indent_m else ""
            for ann_msg in inline_annotations[actual_ln]:
                out_list.append(f"   ! | {ind}{ann_msg}")
        out_list.append(f"{actual_ln:4d} | {raw_line}")

    terms = list(dict.fromkeys(t.lower() for t in (focus_terms or []) if len(t) >= 3))
    doc_bounds = _detect_leading_docstring(selected) if (fold_docstrings and (focus_terms is not None or total_len > max_lines)) else None
    doc_fold_set: set[int] = set(range(doc_bounds[1], doc_bounds[2] + 1)) if doc_bounds is not None else set()
    exec_len = total_len - len(doc_fold_set)

    pdg_slice = (
        compute_multidim_program_slice(
            selected,
            line_start,
            terms,
            ext=Path(rel_path).suffix.lower(),
        )
        if (terms or exec_len > max_lines) and exec_len >= 6
        else {
            "seed_lines": set(),
            "backward_def_lines": set(),
            "forward_use_lines": set(),
            "control_guard_lines": set(),
            "traced_vars": [],
        }
    )

    if exec_len <= max_lines:
        out_short: list[str] = []
        pdg_banner = _format_pdg_slice_banner(pdg_slice, start_idx) if (focus_terms is not None and exec_len >= 8) else ""
        if pdg_banner:
            out_short.append(pdg_banner)
        i = 0
        while i < total_len:
            if i in doc_fold_set:
                f_s = start_idx + doc_bounds[1] + 1  # type: ignore[index]
                f_e = start_idx + doc_bounds[2] + 1  # type: ignore[index]
                out_short.append(f" ... | [folded {len(doc_fold_set)} docstring lines L{f_s}-L{f_e}]")
                i = doc_bounds[2] + 1  # type: ignore[index]
            else:
                _emit_kept_line(i, out_short)
                i += 1
        return "\n".join(out_short)

    bwd_def_set = pdg_slice["backward_def_lines"]
    fwd_use_set = pdg_slice["forward_use_lines"]
    ctrl_guard_set = pdg_slice["control_guard_lines"]
    keep = [False] * total_len

    # Always keep the first head_exec non-docstring lines (signature + 1st docstring summary line + initial guardrails) and last tail_n lines
    pinned_in_range = [
        pln for pln in sorted(set(pin_lines or ()))
        if line_start <= pln <= line_end
    ]
    head_exec_target = min(5 if len(pinned_in_range) >= 4 else 6, max(3, max_lines // 5))
    tail_n = min(3, max(2, max_lines // 12))
    kept_head_exec = 0
    head_end_idx = 0
    for i in range(total_len):
        if i in doc_fold_set:
            continue
        keep[i] = True
        kept_head_exec += 1
        head_end_idx = i + 1
        if kept_head_exec >= head_exec_target:
            break
    for i in range(max(0, total_len - tail_n), total_len):
        if i not in doc_fold_set:
            keep[i] = True
    # Causal-Spine & Violation-Site Unfolding: pin exact AST violation / bridge lines first
    for pln in pinned_in_range:
        p_idx = pln - start_idx - 1
        if 0 <= p_idx < total_len and p_idx not in doc_fold_set:
            keep[p_idx] = True
            for nb in (p_idx - 1, p_idx + 1):
                if 0 <= nb < total_len and nb not in doc_fold_set and sum(1 for k in keep if k) < max_lines - 2:
                    keep[nb] = True
    # Pin primary seed lines first, then enclosing AST control-dependence guards and backward/forward data-dependence lines
    pdg_budget = max(5, max_lines // 2)
    pdg_pinned = 0
    for g_idx in sorted(pdg_slice["seed_lines"]) + sorted(bwd_def_set) + sorted(ctrl_guard_set) + sorted(fwd_use_set):
        if pdg_pinned >= pdg_budget or sum(1 for k in keep if k) >= max_lines - 3:
            break
        if 0 <= g_idx < total_len and g_idx not in doc_fold_set and not keep[g_idx]:
            keep[g_idx] = True
            pdg_pinned += 1
    # Compute term frequency inside the function to discount ubiquitous local variable names
    low_lines = [ln.lower() for ln in selected]
    term_weights: dict[str, float] = {}
    freq_cap = max(6, exec_len // 6)
    for t in terms:
        df = sum(1 for idx_l, low in enumerate(low_lines) if idx_l not in doc_fold_set and t in low)
        term_weights[t] = 0.6 if df > freq_cap else 2.5

    # Score remaining lines by keyword matches, PDG data/control dependence, and control/timing patterns
    _CONSOLE_PAT = re.compile(r"^\s*console\.(log|warn|error|info|debug)\b")
    line_scores = [0.0] * total_len
    for i, line in enumerate(selected):
        if i in doc_fold_set:
            line_scores[i] = -99.0
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#"):
            continue
        if _CONSOLE_PAT.match(line):
            line_scores[i] = -2.0
            continue
        low = low_lines[i]
        sc = 0.0
        is_style_noise = bool(_STYLE_PROP_PAT.match(line)) or bool(_JSX_NOISE_PAT.match(line))
        is_jsx_handler = bool(re.search(r"\bon[A-Z]\w*\s*=\s*\{", line)) and not bool(
            re.search(r"\bonMouse(?:Enter|Leave)\s*=", line)
        )
        is_code_stmt = bool(_CODE_STMT_PAT.match(line)) and not is_style_noise
        is_kv_prop = bool(_KV_PROP_PAT.match(line)) and not is_style_noise
        is_code_like = is_code_stmt or is_kv_prop or is_jsx_handler or any(ch in stripped for ch in ("=", "{", "}", ";", ":"))
        for t in terms:
            if t in low:
                # Discount matches on pure English prose lines inside template strings or CSS noise
                sc += (term_weights[t] if not is_style_noise else 0.4) if is_code_like else 0.2
        if i in pdg_slice["seed_lines"]:
            sc += 8.0
        if i in bwd_def_set:
            sc += 6.5
        if i in fwd_use_set:
            sc += 6.0
        if i in ctrl_guard_set:
            sc += 5.5
        if _IMPORTANT_CODE_PAT.search(line):
            sc += 5.0
        if _UPPER_CONST_PAT.search(line) and is_code_like:
            sc += 0.8
        if re.match(r"^\s*for\s*\(", line):
            sc += 3.0
        if is_jsx_handler:
            sc += 3.2
        if is_code_stmt:
            sc += 1.8
        elif is_kv_prop:
            sc += 0.6
        if is_style_noise and not is_jsx_handler and sc <= 2.0:
            sc = -1.5
        line_scores[i] = sc
    # Smooth scores over a +/- 2 line window
    window_scores: list[tuple[float, int]] = []
    for i in range(head_end_idx, total_len - tail_n):
        if i not in doc_fold_set and line_scores[i] >= 1.0:
            neighbors = sum(
                max(0.0, line_scores[j])
                for j in range(max(0, i - 2), min(total_len, i + 3))
                if j != i and j not in doc_fold_set
            )
            w_sc = line_scores[i] + 0.35 * neighbors
            window_scores.append((w_sc, i))
    window_scores.sort(key=lambda x: x[0], reverse=True)
    kept_count = sum(1 for k in keep if k)

    for _, center in window_scores:
        if kept_count >= max_lines:
            break
        radius = 1 if max_lines <= 32 else (2 if total_len < 500 else 3)
        for j in range(max(0, center - radius), min(total_len, center + radius + 1)):
            if j not in doc_fold_set and not keep[j] and line_scores[j] >= -0.5:
                keep[j] = True
                kept_count += 1
                if kept_count >= max_lines:
                    break

    # If few focus windows matched inside the function, fill remaining budget with non-noise code lines
    if kept_count < max_lines // 2:
        for i in range(total_len - tail_n):
            if kept_count >= max_lines:
                break
            if i not in doc_fold_set and not keep[i] and line_scores[i] >= 0.0:
                keep[i] = True
                kept_count += 1
    # Merge small 1-2 line micro-gaps between kept code blocks (never unfolding docstring lines)
    last_kept = -1
    for i in range(total_len):
        if keep[i]:
            gap_sz = i - last_kept - 1
            if (
                last_kept >= 0
                and 0 < gap_sz <= 2
                and kept_count + gap_sz <= max_lines + 3
                and all(g not in doc_fold_set for g in range(last_kept + 1, i))
            ):
                for g in range(last_kept + 1, i):
                    keep[g] = True
                    kept_count += 1
            last_kept = i

    formatted: list[str] = []
    pdg_banner = _format_pdg_slice_banner(pdg_slice, start_idx, keep=keep)
    if pdg_banner:
        formatted.append(pdg_banner)
    i = 0
    while i < total_len:
        if keep[i]:
            _emit_kept_line(i, formatted)
            i += 1
        elif doc_bounds is not None and i == doc_bounds[1]:
            f_s = start_idx + doc_bounds[1] + 1
            f_e = start_idx + doc_bounds[2] + 1
            formatted.append(f" ... | [folded {len(doc_fold_set)} docstring lines L{f_s}-L{f_e}]")
            i = doc_bounds[2] + 1
        else:
            gap_start = i
            while i < total_len and not keep[i]:
                i += 1
            gap_lines = i - gap_start
            landmarks: list[str] = []
            if gap_lines >= 12:
                for k in range(gap_start, i):
                    st = selected[k].strip()
                    if len(st) > 6 and (
                        "onClick=" in st
                        or "className=" in st
                        or "&& (" in st
                        or st.startswith(("/* ", "{/*", "case "))
                    ):
                        snippet = re.sub(r"\s+", " ", st)[:38]
                        landmarks.append(f"L{start_idx + k + 1}:{snippet}")
                        if len(landmarks) >= 3:
                            break
            lm_suffix = f" | landmarks: {', '.join(landmarks)}" if landmarks else ""
            f_s = start_idx + gap_start + 1
            f_e = start_idx + i
            exp_suffix = f" -> expand: brain_slice {rel_path}:{f_s}-{f_e}" if gap_lines >= 15 else ""
            formatted.append(
                f" ... | [folded {gap_lines} lines L{f_s}-L{f_e}{lm_suffix}{exp_suffix}]"
            )
    return "\n".join(formatted)


_EMITTED_LINE_PAT = re.compile(r"^\s*(\d+)\s*\|")


def extract_emitted_intervals(formatted_code: str) -> list[tuple[int, int]]:
    """Extract contiguous (start_line, end_line) intervals of actually emitted
    (unfolded) code lines from a formatted slice string.
    """
    line_nums: list[int] = []
    for raw_ln in formatted_code.splitlines():
        m = _EMITTED_LINE_PAT.match(raw_ln)
        if m:
            line_nums.append(int(m.group(1)))
    if not line_nums:
        return []
    line_nums.sort()
    intervals: list[tuple[int, int]] = []
    cur_s = line_nums[0]
    cur_e = line_nums[0]
    for ln in line_nums[1:]:
        if ln <= cur_e + 1:
            cur_e = max(cur_e, ln)
        else:
            intervals.append((cur_s, cur_e))
            cur_s = ln
            cur_e = ln
    intervals.append((cur_s, cur_e))
    return intervals


def extract_file_globals_slice(
    root: Path,
    rel_path: str,
    info: dict,
    max_lines: int = 50,
) -> str:
    """Extract top-level module constants, regex tables, configuration state,
    and module-load IIFEs that live outside indexed functions/classes.
    """
    full_path = root / rel_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    n = len(lines)
    if n == 0:
        return ""

    covered = [False] * (n + 1)
    for fn in info.get("functions", []):
        s, e = fn.get("line_start", 1), fn.get("line_end", 1)
        for ln in range(max(1, s), min(n, e) + 1):
            covered[ln] = True
    for cls in info.get("classes", []):
        s, e = cls.get("line_start", 1), cls.get("line_end", 1)
        first_ln = lines[max(0, s - 1)].strip() if 1 <= s <= n else ""
        if re.match(r"^(export\s+)?(interface|type)\s+\w+", first_ln):
            continue
        for ln in range(max(1, s), min(n, e) + 1):
            covered[ln] = True

    kept_lines: list[tuple[int, str]] = []
    in_block_comment = False
    in_import = False
    in_type_block = False
    type_brace_depth = 0
    type_block_lines = 0
    literal_item_run = 0

    for idx, raw in enumerate(lines):
        ln = idx + 1
        if covered[ln]:
            literal_item_run = 0
            continue
        stripped = raw.strip()
        if not stripped:
            continue

        # Skip block comments
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("//") or stripped.startswith("#"):
            continue

        # Skip multi-line imports
        if in_import:
            if ";" in stripped or stripped.endswith(("'", '"')) or " from " in stripped:
                in_import = False
            continue
        if re.match(r"^(import\s|from\s+\S+\s+import\s)", stripped):
            if "{" in stripped and "}" not in stripped:
                in_import = True
            continue

        # Keep TypeScript interface / type blocks compactly (up to 8 field lines + closing brace)
        if in_type_block:
            type_brace_depth += stripped.count("{") - stripped.count("}")
            type_block_lines += 1
            is_type_end = type_brace_depth <= 0 and (stripped.endswith("}") or stripped.endswith("};") or ";" in stripped)
            if is_type_end:
                in_type_block = False
                kept_lines.append((ln, raw))
                if len(kept_lines) >= max_lines:
                    break
            elif type_block_lines <= 8:
                kept_lines.append((ln, raw))
                if len(kept_lines) >= max_lines:
                    break
            continue
        if re.match(r"^(export\s+)?(interface|type)\s+\w+", stripped):
            if "{" in stripped and "}" not in stripped:
                in_type_block = True
                type_brace_depth = stripped.count("{") - stripped.count("}")
                type_block_lines = 0
            kept_lines.append((ln, raw))
            if len(kept_lines) >= max_lines:
                break
            continue
        # Collapse long static string/label arrays/maps unless they are regex patterns (/.../)
        is_regex_entry = stripped.startswith("/") and ("/i" in stripped or "/g" in stripped or stripped.endswith("/,"))
        is_simple_item = (
            not is_regex_entry
            and not stripped.startswith(("const ", "let ", "var ", "export ", "function ", "(()", "if ", "for "))
            and stripped.endswith(",")
            and len(stripped) < 60
        )
        if is_simple_item:
            literal_item_run += 1
            max_run = 1 if ":" in stripped else 5
            if literal_item_run > max_run:
                continue
        else:
            literal_item_run = 0

        kept_lines.append((ln, raw))
        if len(kept_lines) >= max_lines:
            break

    if not kept_lines:
        return ""

    out: list[str] = []
    prev_ln = -10
    for ln, raw in kept_lines:
        if prev_ln > 0 and ln - prev_ln > 3:
            out.append(f" ... | [L{prev_ln + 1}-L{ln - 1} omitted]")
        out.append(f"{ln:4d} | {raw}")
        prev_ln = ln
    return "\n".join(out)



_LANDMARK_DECL_PAT = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|function|async\s+function|def|async\s+def|class)\s+([a-zA-Z0-9_$]+)"
)
_LANDMARK_HOOK_PAT = re.compile(r"^\s*(?:const\s+\[([^\]]+)\]\s*=\s*useState|useEffect\s*\(|useMemo\s*\(|useCallback\s*\()")
_LANDMARK_BRANCH_PAT = re.compile(
    r"^\s*(?:\{\s*/\*\s*(.+?)\s*\*/\s*\}|/\*\s*===\s*(.+?)\s*===|\{?\s*([a-zA-Z0-9_$.]+\s*===\s*['\"][^'\"]+['\"])|case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:)"
)


def extract_monolith_landmarks(
    lines: list[str],
    line_start: int = 1,
    line_end: int | None = None,
    max_landmarks: int = 24,
) -> list[dict]:
    """Scan a monolith file/function for high-signal inner landmarks (handlers, state, tabs, JSX sections)."""
    n = len(lines)
    s_idx = max(0, line_start - 1)
    e_idx = min(n, line_end if line_end is not None else n)
    raw_lms: list[dict] = []

    for idx in range(s_idx, e_idx):
        ln_num = idx + 1
        line = lines[idx]
        st = line.strip()
        if not st or len(st) < 4:
            continue
        label = None
        m_br = _LANDMARK_BRANCH_PAT.search(st)
        if m_br:
            raw_lbl = next((g for g in m_br.groups() if g), st[:36])
            label = re.sub(r"\s+", " ", raw_lbl.strip())[:36]
        else:
            m_hk = _LANDMARK_HOOK_PAT.search(st)
            if m_hk:
                label = f"state[{m_hk.group(1)[:24]}]" if m_hk.group(1) else st.split("(", 1)[0].strip()
            else:
                m_dc = _LANDMARK_DECL_PAT.match(line)
                if m_dc:
                    indent = len(line) - len(line.lstrip())
                    if indent <= 12:
                        label = m_dc.group(1)
        if label:
            # Skip if too close (<8 lines) to previous landmark
            if raw_lms and (ln_num - raw_lms[-1]["line_start"]) < 8:
                continue
            raw_lms.append({"line_start": ln_num, "label": label})

    for i, lm in enumerate(raw_lms):
        lm["line_end"] = (raw_lms[i + 1]["line_start"] - 1) if (i + 1 < len(raw_lms)) else e_idx

    if len(raw_lms) <= max_landmarks:
        return raw_lms
    step = max(1, len(raw_lms) // max_landmarks)
    return [raw_lms[i] for i in range(0, len(raw_lms), step)][:max_landmarks]


def snap_to_block_boundaries(
    lines: list[str],
    line_start: int,
    line_end: int,
    max_snap: int = 8,
) -> tuple[int, int]:
    """Snap arbitrary line range [line_start, line_end] up/down by up to `max_snap` lines
    so statements or block headers/footers aren't cut in half.
    """
    n = len(lines)
    s = max(1, min(n, line_start))
    e = max(s, min(n, line_end))

    # Snap start upward if a comment header or declaration starts within max_snap lines above
    for k in range(1, min(max_snap + 1, s)):
        cand = lines[s - 1 - k].strip()
        if not cand:
            s = s - k + 1
            break
        if _LANDMARK_DECL_PAT.match(lines[s - 1 - k]) or _LANDMARK_BRANCH_PAT.search(cand):
            s = s - k
            break

    # Snap end downward if a closing brace/tag is immediately followed by a blank line within max_snap lines
    for k in range(1, min(max_snap + 1, n - e + 1)):
        cand = lines[e - 1 + k].strip()
        if not cand:
            e = e + k - 1
            break
        if cand in ("}", "};", ")};", "});", "</div>", "</>") and (
            (e - 1 + k + 1 < n and not lines[e - 1 + k + 1].strip())
        ):
            e = e + k
            break

    return s, e


def format_boundary_header(
    lines: list[str],
    line_start: int,
    line_end: int,
) -> str:
    """Return a 1-line structural navigation bar showing adjacent landmarks in monolith files."""
    if len(lines) < 180:
        return ""
    lms = extract_monolith_landmarks(lines, 1, len(lines), max_landmarks=60)
    if not lms:
        return f"# [File Bounds: L1-L{len(lines)} | Current Slice: L{line_start}-L{line_end}]"

    prev_lm = None
    curr_lms: list[dict] = []
    next_lm = None
    for lm in lms:
        if lm["line_end"] < line_start:
            prev_lm = lm
        elif lm["line_start"] > line_end:
            if next_lm is None:
                next_lm = lm
        else:
            curr_lms.append(lm)

    parts = [f"File L1-L{len(lines)}"]
    if prev_lm:
        parts.append(f"Prev: L{prev_lm['line_start']}-L{prev_lm['line_end']} ({prev_lm['label']})")
    if curr_lms:
        c_str = ", ".join(f"L{c['line_start']}:{c['label']}" for c in curr_lms[:3])
        parts.append(f"In-slice: {c_str}")
    if next_lm:
        parts.append(f"Next: L{next_lm['line_start']}-L{next_lm['line_end']} ({next_lm['label']})")
    return "# [Boundary Map] " + " | ".join(parts)

def build_file_skeleton(
    rel_path: str,
    info: dict,
    behaviors: dict[str, str] | None = None,
    focus_terms: list[str] | None = None,
    root: Path | None = None,
) -> str:
    """Build a compact, token-efficient structural skeleton of a file.

    Includes:
      - Routes (METHOD /path -> handler)
      - Classes with base classes, schema fields, and method signatures
      - Top-level function signatures with types, behavioral role, and 1-line docstrings
      - Inner Landmark Map for monolith functions (>220 lines) so agents never peephole-crawl
      - Zero implementation bodies!
    """
    behaviors = behaviors or {}
    focus_terms = [t.lower() for t in (focus_terms or [])]
    out: list[str] = [f"# File Skeleton: {rel_path}"]
    routes = info.get("routes", [])
    if routes:
        route_strs = [f"{r['method']} {r['path']} -> {r.get('handler') or '?'}" for r in routes]
        out.append(f"# Routes: {', '.join(route_strs)}")

    # Map class -> methods
    methods_by_cls: dict[str, list[dict]] = {}
    top_fns: list[dict] = []
    for fn in info.get("functions", []):
        pcls = fn.get("parent_class")
        if pcls:
            methods_by_cls.setdefault(pcls, []).append(fn)
        else:
            top_fns.append(fn)

    for cls in info.get("classes", []):
        cname = cls["name"]
        sig = cls.get("signature") or f"class {cname}:"
        lrange = f"L{cls.get('line_start', '?')}-{cls.get('line_end', '?')}"
        out.append(f"{sig}  # [{lrange}]")
        if cls.get("docstring"):
            doc_line = cls["docstring"].splitlines()[0][:100]
            out.append(f'    """{doc_line}"""')
        for fld in cls.get("fields", [])[:12]:
            out.append(f"    {fld}")
        cls_methods = methods_by_cls.get(cname, [])
        for m in cls_methods[:20]:
            msig = m.get("signature") or f"def {m['name']}()"
            mlines = f"L{m.get('line_start', '?')}-{m.get('line_end', '?')}"
            qname = f"{cname}.{m['name']}"
            beh = behaviors.get(qname, "")
            beh_tag = f" [{beh}]" if beh and beh != "unknown" else ""
            out.append(f"    {msig}: ...  # {mlines}{beh_tag}")

    file_lines: list[str] | None = None
    for fn in top_fns[:25]:
        fsig = fn.get("signature") or f"def {fn['name']}()"
        l_s = fn.get("line_start", 1)
        l_e = fn.get("line_end", l_s)
        flines = f"L{l_s}-{l_e}"
        beh = behaviors.get(fn["name"], "")
        beh_tag = f" [{beh}]" if beh and beh != "unknown" else ""
        doc_tag = ""
        if fn.get("docstring"):
            doc_first = fn["docstring"].splitlines()[0][:80]
            doc_tag = f' — "{doc_first}"'
        out.append(f"{fsig}: ...  # {flines}{beh_tag}{doc_tag}")
        if root is not None and isinstance(l_s, int) and isinstance(l_e, int) and (l_e - l_s) >= 220:
            if file_lines is None:
                try:
                    file_lines = (root / rel_path).read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    file_lines = []
            if file_lines:
                lms = extract_monolith_landmarks(file_lines, l_s, l_e, max_landmarks=16)
                if lms:
                    lm_str = " | ".join(f"L{lm['line_start']}-L{lm['line_end']}:{lm['label']}" for lm in lms)
                    out.append(f"    # Inner Landmarks: {lm_str}")

    return "\n".join(out)


# Re-exported from .contracts for backward compatibility
from .contracts import (
    _find_balanced_end,
    _NOMINAL_DISJOINT_TYPES,
    _norm_py_type_annotation,
    _infer_py_expr_type,
    _analyze_python_ast_contracts,
    _count_call_args,
    _analyze_js_ts_ast_contracts,
    _balanced_paren_end,
    _split_top_level_params,
    extract_4d_coordinate_matrix,
    query_4d_coordinate_matrix,
    apply_sql_alters_to_schema,
)
