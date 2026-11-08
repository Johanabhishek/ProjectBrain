"""Surgical Anchor Patch Engine for ProjectBrain (`projectbrain patch`).

Eliminates output token waste during multi-file code editing by allowing agents
to emit ultra-compact line/symbol anchor specifications instead of repeating
large `old_string` / `new_string` context blocks, and verifies syntax via
Tree-sitter before or after applying to disk.

Supported DSL Directives:
  @file <rel_path>
  @sub L<line> `<old_ inline>` -> `<new_inline>`
  @sub L<start>-L<end> `<old_inline>` -> `<new_inline>`
  @sub ALL `<old_inline>` -> `<new_inline>`
  @sub <symbol> `<old_inline>` -> `<new_inline>`
  @replace L<start>-L<end>
  <new lines...>
  @end
  @replace <symbol>
  <new lines...>
  @end
  @replace_body <symbol>
  <new body lines...>
  @end
  @add_method <ClassName>
  <new method lines...>
  @end
  @insert_after L<line>
  <new lines...>
  @end
  @insert_before L<line>
  <new lines...>
  @end
  @delete L<start>-L<end>
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import parsers


@dataclass
class PatchOp:
    kind: str  # "sub" | "replace" | "replace_body" | "add_method" | "insert_after" | "insert_before" | "delete"
    line_start: int
    line_end: int
    old_frag: str = ""
    new_frag: str = ""
    block_lines: list[str] = field(default_factory=list)
    raw_header: str = ""
    anchor_mode: str = "line"  # "line" | "all" | "symbol"
    symbol_anchor: str = ""


@dataclass
class FilePatchPlan:
    rel_path: str
    ops: list[PatchOp] = field(default_factory=list)


_FILE_RE = re.compile(r"^@file\s+(.+?)\s*$")
_SUB_RE = re.compile(
    r"^@sub\s+(?:L?(\d+)(?:\s*-\s*L?(\d+))?|(ALL|\*)|([A-Za-z_][A-Za-z0-9_.]*))\s+`(.+?)`\s*->\s*`(.*?)`\s*$"
)
_REPLACE_RE = re.compile(r"^@replace\s+(?:L?(\d+)(?:\s*-\s*L?(\d+))?|([A-Za-z_][A-Za-z0-9_.]*))\s*$")
_REPLACE_BODY_RE = re.compile(r"^@replace_body\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$")
_ADD_METHOD_RE = re.compile(r"^@add_method\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$")
_INSERT_AFTER_RE = re.compile(r"^@insert_after\s+L?(\d+)\s*$")
_INSERT_BEFORE_RE = re.compile(r"^@insert_before\s+L?(\d+)\s*$")
_DELETE_RE = re.compile(r"^@delete\s+L?(\d+)(?:\s*-\s*L?(\d+))?\s*$")

_LAZY_ELISION_RE = re.compile(
    r"^\s*(?:#|//|/\*+)\s*\.\.\.\s*(?:existing|rest of|unchanged|previous|omitted|remaining|keep|same)\b",
    re.IGNORECASE,
)


def _has_lazy_elision(block_lines: list[str]) -> str | None:
    """Detect LLM lazy placeholder comments like `# ... existing code ...` inside replacement blocks."""
    for ln in block_lines:
        if _LAZY_ELISION_RE.search(ln):
            return ln.strip()
    return None


def parse_patch_spec(spec_text: str) -> tuple[list[FilePatchPlan], list[str]]:
    """Parse a Surgical Anchor Patch DSL string into per-file patch plans."""
    plans: list[FilePatchPlan] = []
    errors: list[str] = []
    current_plan: FilePatchPlan | None = None

    lines = spec_text.lstrip("\ufeff").splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        m_file = _FILE_RE.match(stripped)
        if m_file:
            rel = m_file.group(1).strip().strip("\"'").replace("\\", "/")
            current_plan = FilePatchPlan(rel_path=rel)
            plans.append(current_plan)
            i += 1
            continue

        if current_plan is None:
            errors.append(f"Spec line {i + 1}: directive '{stripped}' appears before any '@file <path>' header.")
            i += 1
            continue

        m_sub = _SUB_RE.match(stripped)
        if m_sub:
            s_str, e_str, all_tok, sym_tok, old_f, new_f = m_sub.groups()
            if all_tok:
                current_plan.ops.append(
                    PatchOp(
                        kind="sub",
                        line_start=1,
                        line_end=999999,
                        old_frag=old_f,
                        new_frag=new_f,
                        raw_header=stripped,
                        anchor_mode="all",
                    )
                )
            elif sym_tok:
                current_plan.ops.append(
                    PatchOp(
                        kind="sub",
                        line_start=1,
                        line_end=999999,
                        old_frag=old_f,
                        new_frag=new_f,
                        raw_header=stripped,
                        anchor_mode="symbol",
                        symbol_anchor=sym_tok,
                    )
                )
            else:
                s_line = int(s_str)
                e_line = int(e_str) if e_str else s_line
                current_plan.ops.append(
                    PatchOp(
                        kind="sub",
                        line_start=s_line,
                        line_end=e_line,
                        old_frag=old_f,
                        new_frag=new_f,
                        raw_header=stripped,
                        anchor_mode="line",
                    )
                )
            i += 1
            continue

        m_del = _DELETE_RE.match(stripped)
        if m_del:
            s_line = int(m_del.group(1))
            e_line = int(m_del.group(2)) if m_del.group(2) else s_line
            current_plan.ops.append(
                PatchOp(
                    kind="delete",
                    line_start=s_line,
                    line_end=e_line,
                    raw_header=stripped,
                )
            )
            i += 1
            continue

        m_rep = _REPLACE_RE.match(stripped)
        m_rep_body = _REPLACE_BODY_RE.match(stripped)
        m_add_meth = _ADD_METHOD_RE.match(stripped)
        m_ia = _INSERT_AFTER_RE.match(stripped)
        m_ib = _INSERT_BEFORE_RE.match(stripped)

        if m_rep or m_rep_body or m_add_meth or m_ia or m_ib:
            anchor_mode = "line"
            symbol_anchor = ""
            if m_rep:
                kind = "replace"
                if m_rep.group(3):
                    anchor_mode = "symbol"
                    symbol_anchor = m_rep.group(3)
                    s_line, e_line = 1, 999999
                else:
                    s_line = int(m_rep.group(1))
                    e_line = int(m_rep.group(2)) if m_rep.group(2) else s_line
            elif m_rep_body:
                kind = "replace_body"
                anchor_mode = "symbol"
                symbol_anchor = m_rep_body.group(1)
                s_line, e_line = 1, 999999
            elif m_add_meth:
                kind = "add_method"
                anchor_mode = "symbol"
                symbol_anchor = m_add_meth.group(1)
                s_line, e_line = 1, 999999
            elif m_ia:
                kind = "insert_after"
                s_line = e_line = int(m_ia.group(1))
            else:
                kind = "insert_before"
                s_line = e_line = int(m_ib.group(1))  # type: ignore[union-attr]

            header = stripped
            i += 1
            block: list[str] = []
            closed = False
            while i < n:
                if lines[i].strip() == "@end":
                    closed = True
                    i += 1
                    break
                block.append(lines[i])
                i += 1

            if not closed:
                errors.append(f"Unclosed block for '{header}' in {current_plan.rel_path} (missing '@end').")
            else:
                lazy_line = _has_lazy_elision(block)
                if lazy_line:
                    errors.append(
                        f"Lazy elision placeholder rejected in '{header}' ({current_plan.rel_path}): "
                        f"'{lazy_line}'. Emit complete replacement lines or use @sub."
                    )
                else:
                    current_plan.ops.append(
                        PatchOp(
                            kind=kind,
                            line_start=s_line,
                            line_end=e_line,
                            block_lines=block,
                            raw_header=header,
                            anchor_mode=anchor_mode,
                            symbol_anchor=symbol_anchor,
                        )
                    )
            continue

        errors.append(f"Spec line {i + 1}: unrecognized directive '{stripped}'.")
        i += 1

    return plans, errors


def _verify_syntax_with_treesitter(source_text: str, ext: str) -> dict:
    """Verify whether patched source text has zero syntax errors using Tree-sitter."""
    parser = parsers.get_parser(ext.lower())
    if parser is None:
        return {"checked": False, "valid": True, "error_Verify": "unsupported_extension", "error_lines": []}

    tree = parser.parse(source_text.encode("utf-8", errors="replace"))
    root = tree.root_node
    if not root.has_error:
        if ext.lower() == ".py":
            try:
                compile(source_text, "<patch>", "exec")
            except SyntaxError as exc:
                return {"checked": True, "valid": False, "error_lines": [exc.lineno or 1]}
        return {"checked": True, "valid": True, "error_lines": []}

    err_lines: list[int] = []

    def _walk_errors(node):
        if node.type == "ERROR" or node.is_missing:
            ln = node.start_point[0] + 1
            if ln not in err_lines:
                err_lines.append(ln)
        for ch in node.children:
            if ch.has_error:
                _walk_errors(ch)

    _walk_errors(root)
    return {
        "checked": True,
        "valid": False,
        "error_lines": sorted(err_lines)[:15],
    }


def _resolve_ast_symbol_span(
    parsed_info: dict, symbol_anchor: str, work_lines: list[str]
) -> tuple[int, int, dict | None] | None:
    """Resolve a function, method (`Class.method` or `method`), or class name to its 1-based [start, end] span."""
    sym_clean = symbol_anchor.strip()
    sym_low = sym_clean.lower()

    # 1. Exact match in functions (qualified `Class.method` or `func_name`)
    for fn in parsed_info.get("functions", []):
        qname = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
        if sym_clean in (fn["name"], qname):
            return fn["line_start"], fn["line_end"], fn
    for cls in parsed_info.get("classes", []):
        if sym_clean == cls["name"]:
            return cls["line_start"], cls["line_end"], cls

    # 2. Case-insensitive match in functions/classes
    for fn in parsed_info.get("functions", []):
        qname = f"{fn['parent_class']}.{fn['name']}" if fn.get("parent_class") else fn["name"]
        if sym_low in (fn["name"].lower(), qname.lower()):
            return fn["line_start"], fn["line_end"], fn
    for cls in parsed_info.get("classes", []):
        if sym_low == cls["name"].lower():
            return cls["line_start"], cls["line_end"], cls

    # 3. Fallback: declaration regex scan (`def sym`, `function sym`, `class sym`, `const sym =`)
    decl_pat = re.compile(
        rf"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|class|const|let|var)\s+{re.escape(sym_clean.split('.')[-1])}\b"
    )
    for idx, l_txt in enumerate(work_lines):
        if decl_pat.search(l_txt):
            start_ln = idx + 1
            # Estimate end of block by indentation or 120 lines
            base_indent = len(l_txt) - len(l_txt.lstrip())
            end_ln = len(work_lines)
            for j in range(idx + 1, len(work_lines)):
                s_j = work_lines[j].strip()
                if not s_j or s_j.startswith(("#", "//", "@")):
                    continue
                ind_j = len(work_lines[j]) - len(work_lines[j].lstrip())
                if ind_j <= base_indent and re.match(r"^(?:def |async def |class |export |function )", s_j):
                    end_ln = j
                    break
            return start_ln, end_ln, None
    return None


def _find_body_start_line(work_lines: list[str], sym_start: int, sym_end: int, ext: str) -> tuple[int, str]:
    """Given a function's [sym_start, sym_end] 1-based span, find the 1-based first line of its body and body indent."""
    is_py = ext.lower() == ".py"
    sig_indent = ""
    if 1 <= sym_start <= len(work_lines):
        raw_s = work_lines[sym_start - 1]
        sig_indent = raw_s[: len(raw_s) - len(raw_s.lstrip())]
    default_indent = sig_indent + ("    " if is_py else "  ")

    paren_depth = 0
    for ln in range(sym_start, min(sym_end, len(work_lines)) + 1):
        txt = work_lines[ln - 1]
        for ch in txt:
            if ch in "([{":
                paren_depth += 1
            elif ch in ")]}":
                paren_depth = max(0, paren_depth - 1)
        stripped = txt.strip()
        if is_py:
            if paren_depth == 0 and stripped.endswith(":"):
                body_ln = min(sym_end, ln + 1)
                if body_ln <= len(work_lines) and work_lines[body_ln - 1].strip():
                    b_raw = work_lines[body_ln - 1]
                    b_ind = b_raw[: len(b_raw) - len(b_raw.lstrip())]
                    if len(b_ind) > len(sig_indent):
                        return body_ln, b_ind
                return body_ln, default_indent
        else:
            if "{" in stripped:
                body_ln = min(sym_end, ln + 1)
                return body_ln, default_indent
    return min(sym_end, sym_start + 1), default_indent


def apply_patch_spec(
    root: Path,
    spec_text: str,
    apply: bool = False,
    diff_out_path: Path | None = None,
    fuzzy_window: int = 8,
) -> dict:
    """Execute or dry-run a Surgical Anchor Patch spec against `root`.

    Guarantees:
      1. Path-traversal sandbox (`abs_path.is_relative_to(root.resolve())`).
      2. Two-phase invariant application:
         - Phase 1: Line-preserving `@sub` operations (with AST symbol scoping & unique fuzzy healing).
         - Phase 2: Pre-resolved, conflict-checked, strictly bottom-up structural operations
           (`@replace`, `@replace_body`, `@add_method`, `@insert_after`, `@insert_before`, `@delete`).
      3. Native CRLF / LF line-ending preservation.
      4. Tree-sitter AST syntax verification on every patched buffer.
    """
    root_resolved = root.resolve()
    plans, parse_errors = parse_patch_spec(spec_text)
    file_results: list[dict] = []
    unified_diffs: list[str] = []
    total_ops_applied = 0
    all_syntax_valid = len(parse_errors) == 0

    for plan in plans:
        rel_path = plan.rel_path
        try:
            abs_path = (root_resolved / rel_path).resolve()
            in_sandbox = abs_path.is_relative_to(root_resolved)
        except (OSError, ValueError):
            in_sandbox = False
            abs_path = root_resolved / rel_path

        if not in_sandbox:
            file_results.append({
                "file": rel_path,
                "status": "error",
                "errors": [f"Security sandbox violation: path '{rel_path}' escapes repository root."],
                "ops_applied": 0,
            })
            all_syntax_valid = False
            continue

        if not abs_path.exists() or not abs_path.is_file():
            file_results.append({
                "file": rel_path,
                "status": "error",
                "errors": [f"File not found: {rel_path}"],
                "ops_applied": 0,
            })
            all_syntax_valid = False
            continue

        try:
            raw_bytes = abs_path.read_bytes()
            orig_text = raw_bytes.decode("utf-8", errors="replace")
        except OSError as e:
            file_results.append({
                "file": rel_path,
                "status": "error",
                "errors": [f"Read error: {e}"],
                "ops_applied": 0,
            })
            all_syntax_valid = False
            continue

        line_sep = "\r\n" if "\r\n" in orig_text else "\n"
        orig_lines = orig_text.splitlines()
        work_lines = list(orig_lines)
        op_errors: list[str] = []
        ops_applied = 0

        ext = abs_path.suffix.lower()
        parsed_info = parsers.parse(orig_text, ext)

        # Split into Phase 1 (line-count-preserving @sub) and Phase 2 (structural / line-count-changing)
        phase1_subs: list[tuple[int, PatchOp]] = []
        phase2_structural: list[tuple[int, PatchOp]] = []
        for idx, op in enumerate(plan.ops):
            if op.kind == "sub" and op.old_frag.count("\n") == op.new_frag.count("\n"):
                phase1_subs.append((idx, op))
            else:
                phase2_structural.append((idx, op))

        # ── Phase 1: Line-preserving @sub operations (in spec order) ──────────
        for _, op in phase1_subs:
            total_l = len(work_lines)
            if op.anchor_mode == "all":
                count_all = 0
                for l_idx in range(total_l):
                    if op.old_frag in work_lines[l_idx]:
                        c_occ = work_lines[l_idx].count(op.old_frag)
                        work_lines[l_idx] = work_lines[l_idx].replace(op.old_frag, op.new_frag)
                        count_all += c_occ
                if count_all > 0:
                    ops_applied += count_all
                else:
                    op_errors.append(f"Failed '{op.raw_header}': `{op.old_frag}` not found anywhere in {rel_path}.")
                continue

            if op.anchor_mode == "symbol":
                span_res = _resolve_ast_symbol_span(parsed_info, op.symbol_anchor, work_lines)
                if span_res is not None:
                    sym_s, sym_e, _ = span_res
                else:
                    sym_s, sym_e = 0, 0
                matched = False
                if sym_s > 0:
                    for l_idx in range(max(0, sym_s - 1), min(total_l, sym_e)):
                        if op.old_frag in work_lines[l_idx]:
                            work_lines[l_idx] = work_lines[l_idx].replace(op.old_frag, op.new_frag, 1)
                            matched = True
                            ops_applied += 1
                            break
                if not matched:
                    op_errors.append(
                        f"Failed '{op.raw_header}': symbol `{op.symbol_anchor}` or `{op.old_frag}` not found in symbol scope."
                    )
                continue

            # Standard line-anchored @sub (with unique fuzzy window drift healing)
            s_idx = max(1, min(op.line_start, max(1, total_l)))
            e_idx = max(s_idx, min(op.line_end, max(1, total_l)))
            matched = False
            for l_idx in range(s_idx - 1, e_idx):
                if op.old_frag in work_lines[l_idx]:
                    work_lines[l_idx] = work_lines[l_idx].replace(op.old_frag, op.new_frag, 1)
                    matched = True
                    ops_applied += 1
                    break
            if not matched:
                lo = max(1, s_idx - fuzzy_window)
                hi = min(total_l, e_idx + fuzzy_window)
                candidates: list[tuple[int, int]] = []
                for l_idx in range(lo - 1, hi):
                    if op.old_frag in work_lines[l_idx]:
                        dist = min(abs((l_idx + 1) - s_idx), abs((l_idx + 1) - e_idx))
                        candidates.append((dist, l_idx))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    # Apply if unique or strictly closest anchor within window
                    best_l_idx = candidates[0][1]
                    work_lines[best_l_idx] = work_lines[best_l_idx].replace(op.old_frag, op.new_frag, 1)
                    matched = True
                    ops_applied += 1
            if not matched:
                op_errors.append(
                    f"Failed '{op.raw_header}': `{op.old_frag}` not found near L{op.line_start} (±{fuzzy_window} lines)."
                )

        # ── Phase 2: Pre-resolve spans and apply structural ops bottom-to-top ─
        # Each entry in resolved_ops: (sort_line_start, sort_line_end, orig_idx, eff_s, eff_e, new_lines_to_splice, op)
        resolved_structural: list[tuple[int, int, int, int, int, list[str], PatchOp]] = []
        total_l = len(work_lines)

        for orig_idx, op in phase2_structural:
            if op.kind == "sub":
                # Multi-line @sub
                if op.anchor_mode == "symbol":
                    span_res = _resolve_ast_symbol_span(parsed_info, op.symbol_anchor, work_lines)
                    if not span_res:
                        op_errors.append(f"Failed '{op.raw_header}': symbol `{op.symbol_anchor}` not found.")
                        continue
                    s_idx, e_idx, _ = span_res
                elif op.anchor_mode == "all":
                    s_idx, e_idx = 1, total_l
                else:
                    s_idx = max(1, min(op.line_start - fuzzy_window, total_l))
                    e_idx = max(s_idx, min(op.line_end + fuzzy_window, total_l))
                sub_slice = "\n".join(work_lines[s_idx - 1 : e_idx])
                if op.old_frag not in sub_slice:
                    op_errors.append(f"Failed '{op.raw_header}': `{op.old_frag}` not found in L{s_idx}-L{e_idx}.")
                    continue
                replaced_slice = (
                    sub_slice.replace(op.old_frag, op.new_frag)
                    if op.anchor_mode == "all"
                    else sub_slice.replace(op.old_frag, op.new_frag, 1)
                )
                resolved_structural.append(
                    (s_idx, e_idx, orig_idx, s_idx - 1, e_idx, replaced_slice.splitlines(), op)
                )

            elif op.kind == "replace":
                if op.anchor_mode == "symbol":
                    span_res = _resolve_ast_symbol_span(parsed_info, op.symbol_anchor, work_lines)
                    if not span_res:
                        op_errors.append(f"Failed '{op.raw_header}': symbol `{op.symbol_anchor}` not found in {rel_path}.")
                        continue
                    s_idx, e_idx, _ = span_res
                else:
                    s_idx = max(1, min(op.line_start, max(1, total_l)))
                    e_idx = max(s_idx, min(op.line_end, max(1, total_l)))
                    # Auto-heal ±5 line drift if first line of block_lines is a `def`/`function`/`class` header that drifted
                    if op.block_lines and s_idx <= total_l:
                        first_blk = op.block_lines[0].strip()
                        m_hdr = re.match(r"^(?:export\s+)?(?:async\s+)?(?:def|function|class)\s+([A-Za-z_]\w*)", first_blk)
                        if m_hdr:
                            sym_nm = m_hdr.group(1)
                            if sym_nm not in work_lines[s_idx - 1]:
                                span_len = e_idx - s_idx
                                drift_hits = [
                                    ln_i
                                    for ln_i in range(max(1, s_idx - 5), min(total_l, s_idx + 5) + 1)
                                    if re.search(rf"\b(?:def|function|class)\s+{re.escape(sym_nm)}\b", work_lines[ln_i - 1])
                                ]
                                if len(drift_hits) == 1:
                                    s_idx = drift_hits[0]
                                    e_idx = min(total_l, s_idx + span_len)
                resolved_structural.append((s_idx, e_idx, orig_idx, s_idx - 1, e_idx, list(op.block_lines), op))

            elif op.kind == "replace_body":
                span_res = _resolve_ast_symbol_span(parsed_info, op.symbol_anchor, work_lines)
                if not span_res:
                    op_errors.append(f"Failed '{op.raw_header}': symbol `{op.symbol_anchor}` not found in {rel_path}.")
                    continue
                sym_s, sym_e, _ = span_res
                body_start, body_indent = _find_body_start_line(work_lines, sym_s, sym_e, ext)
                is_py = ext == ".py"
                eff_end = sym_e if is_py else max(body_start, sym_e - 1)
                blk = list(op.block_lines)
                # If block lines start with the function signature itself, replace from sym_s
                if blk and re.match(
                    rf"^\s*(?:export\s+)?(?:async\s+)?(?:def|function)\s+{re.escape(op.symbol_anchor.split('.')[-1])}\b",
                    blk[0],
                ):
                    resolved_structural.append((sym_s, sym_e, orig_idx, sym_s - 1, sym_e, blk, op))
                else:
                    # Auto-indent if caller supplied unindented body lines
                    non_empty = [ln for ln in blk if ln.strip()]
                    if non_empty and all(len(ln) == len(ln.lstrip()) for ln in non_empty):
                        blk = [(body_indent + ln if ln.strip() else ln) for ln in blk]
                    resolved_structural.append((body_start, eff_end, orig_idx, body_start - 1, eff_end, blk, op))

            elif op.kind == "add_method":
                span_res = _resolve_ast_symbol_span(parsed_info, op.symbol_anchor, work_lines)
                if not span_res:
                    op_errors.append(f"Failed '{op.raw_header}': class `{op.symbol_anchor}` not found in {rel_path}.")
                    continue
                cls_s, cls_e, _ = span_res
                is_py = ext == ".py"
                cls_raw = work_lines[cls_s - 1] if cls_s <= total_l else ""
                cls_indent = cls_raw[: len(cls_raw) - len(cls_raw.lstrip())]
                meth_indent = cls_indent + ("    " if is_py else "  ")
                blk = list(op.block_lines)
                non_empty = [ln for ln in blk if ln.strip()]
                if non_empty:
                    min_ind = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
                    if min_ind == 0:
                        blk = [(meth_indent + ln if ln.strip() else ln) for ln in blk]
                ins_pos = cls_e if is_py else max(cls_s, cls_e - 1)
                spliced = ([""] + blk) if is_py and ins_pos > 0 and work_lines[ins_pos - 1].strip() else blk
                resolved_structural.append((ins_pos, ins_pos, orig_idx, ins_pos, ins_pos, spliced, op))

            elif op.kind == "delete":
                s_idx = max(1, min(op.line_start, max(1, total_l)))
                e_idx = max(s_idx, min(op.line_end, max(1, total_l)))
                resolved_structural.append((s_idx, e_idx, orig_idx, s_idx - 1, e_idx, [], op))

            elif op.kind == "insert_after":
                s_idx = max(0, min(op.line_start, total_l))
                resolved_structural.append((s_idx, s_idx, orig_idx, s_idx, s_idx, list(op.block_lines), op))

            elif op.kind == "insert_before":
                s_idx = max(1, min(op.line_start, total_l + 1))
                resolved_structural.append((s_idx, s_idx - 1, orig_idx, s_idx - 1, s_idx - 1, list(op.block_lines), op))

        # Detect overlapping destructive spans among replace/replace_body/delete ops
        destructive_spans: list[tuple[int, int, str]] = []
        overlapping_indices: set[int] = set()
        for _, _, orig_idx, eff_s, eff_e, _, op in resolved_structural:
            if eff_e > eff_s:  # modifies an existing line interval [eff_s, eff_e)
                for prev_s, prev_e, prev_hdr in destructive_spans:
                    if max(eff_s, prev_s) < min(eff_e, prev_e):
                        op_errors.append(
                            f"Overlapping structural patch spans in {rel_path}: '{op.raw_header}' collides with '{prev_hdr}'."
                        )
                        overlapping_indices.add(orig_idx)
                        break
                destructive_spans.append((eff_s, eff_e, op.raw_header))

        # Sort structural operations strictly bottom-to-top by (eff_s, eff_e, orig_idx) descending
        valid_structural = [item for item in resolved_structural if item[2] not in overlapping_indices]
        valid_structural.sort(key=lambda x: (x[3], x[4], x[2]), reverse=True)

        for _, _, _, eff_s, eff_e, new_slice, _ in valid_structural:
            work_lines[eff_s:eff_e] = new_slice
            ops_applied += 1

        trailing_nl = line_sep if (orig_text.endswith("\n") or orig_text.endswith("\r\n") or not orig_text) else ""
        patched_text = line_sep.join(work_lines) + trailing_nl

        syntax_check = _verify_syntax_with_treesitter(patched_text, ext)
        if not syntax_check["valid"] or op_errors:
            all_syntax_valid = False

        diff_lines = list(
            difflib.unified_diff(
                orig_lines,
                work_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        diff_str = "\n".join(diff_lines)
        if diff_str:
            unified_diffs.append(diff_str)

        applied_to_disk = False
        if apply and syntax_check["valid"] and not op_errors and ops_applied > 0:
            abs_path.write_bytes(patched_text.encode("utf-8"))
            applied_to_disk = True

        total_ops_applied += ops_applied
        file_results.append({
            "file": rel_path,
            "status": "applied" if applied_to_disk else ("dry_run_ok" if syntax_check["valid"] and not op_errors else "failed"),
            "ops_requested": len(plan.ops),
            "ops_applied": ops_applied,
            "syntax_valid": syntax_check["valid"],
            "syntax_error_lines": syntax_check.get("error_lines", []),
            "errors": op_errors,
            "diff_lines": len(diff_lines),
            "_patched_text": patched_text,
        })

    combined_diff = "\n".join(unified_diffs)
    if diff_out_path is not None and combined_diff:
        try:
            diff_out_path.write_text(combined_diff + "\n", encoding="utf-8")
        except OSError:
            pass

    return {
        "mode": "applied" if apply else "dry_run",
        "files_patched": len(file_results),
        "total_ops_applied": total_ops_applied,
        "all_syntax_valid": all_syntax_valid,
        "parse_errors": parse_errors,
        "diff_path": str(diff_out_path) if (diff_out_path and combined_diff) else None,
        "unified_diff": combined_diff,
        "files": file_results,
        "mutation_telemetry": {
            "ops_applied": total_ops_applied,
            "files_patched": len(file_results),
            "syntax_verified_files": sum(1 for f in file_results if f.get("syntax_valid")),
        },
    }
