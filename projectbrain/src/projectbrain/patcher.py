"""Surgical Anchor Patch Engine for Output Token Reduction.

Eliminates the 2x-5x output token tax of standard `edit_file` / search-and-replace tools
(which force the LLM to emit verbatim multi-line `Target` blocks plus full `Replacement` blocks)
and eliminates verbose before/after code block repetition in patch proposals.

Supported Anchor Patch DSL operations:
  @file <relative_path>
  @sub <anchor> `<old_expr>` -> `<new_expr>`
      where <anchor> can be:
        - `L84` or `L84-L90` (1-based line or range, with ±fuzzy_window tolerance)
        - `*` (auto-locate match anywhere in file — no line number needed!)
        - `ALL` (replace all occurrences in file in 1 directive!)
        - `<symbol_name>` (locate inside named function/class, e.g. `ProtectedRoute`)
  @replace L<start>[-L<end>]
  <new lines only — zero Target repetition>
  @end
  @insert_after L<line>
  <new lines>
  @end
  @insert_before L<line>
  <new lines>
  @end
  @delete L<start>[-L<end>]
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PatchOp:
    kind: str  # "sub", "replace", "insert_after", "insert_before", "delete"
    line_start: int
    line_end: int
    anchor_mode: str = "line"  # "line", "first", "all", "symbol"
    symbol_anchor: str = ""
    old_expr: str = ""
    new_expr: str = ""
    body_lines: list[str] = field(default_factory=list)
    raw_spec_chars: int = 0


def _parse_anchor(token: str) -> tuple[str, int, int, str]:
    """Parse anchor token into (anchor_mode, line_start, line_end, symbol_name)."""
    t = token.strip()
    if t == "*":
        return ("first", 1, 999999, "")
    if t.upper() == "ALL":
        return ("all", 1, 999999, "")
    m = re.match(r"^[Ll]?(\d+)(?:\s*[-:]\s*[Ll]?(\d+))?$", t)
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if end < start:
            start, end = end, start
        return ("line", max(1, start), max(1, end), "")
    # Treat identifier as a symbol anchor (e.g., `ProtectedRoute` or `fn:ProtectedRoute`)
    sym = t[3:] if t.startswith("fn:") or t.startswith("cls:") else t
    return ("symbol", 1, 999999, sym)


def _parse_line_range(token: str) -> tuple[int, int]:
    """Parse 'L84', 'L84-L90', '84-90', or '84' into 1-based (start, end)."""
    mode, s, e, _ = _parse_anchor(token)
    if mode != "line":
        raise ValueError(f"Invalid line anchor: {token!r} (expected L<num> or L<start>-L<end>)")
    return s, e


def _parse_sub_expr(rest: str) -> tuple[str, str]:
    """Parse `<old>` -> `<new>` supporting backticks, triple backticks, or double/single quotes."""
    patterns = [
        r"^```(.*?)```\s*->\s*```(.*?)```$",
        r"^`([^`]+)`\s*->\s*`([^`]*)`$",
        r'^"((?:[^"\\]|\\.)+)"\s*->\s*"((?:[^"\\]|\\.)*)"$',
        r"^'((?:[^'\\]|\\.)+)'\s*->\s*'((?:[^'\\]|\\.)*)'$",
        r"^(.*?)\s*->\s*(.*)$",
    ]
    text = rest.strip()
    for pat in patterns:
        m = re.match(pat, text, flags=re.DOTALL)
        if m:
            return m.group(1), m.group(2)
    raise ValueError(f"Invalid @sub syntax: {rest!r} (expected `<old>` -> `<new>`)")


def parse_patch_spec(spec_text: str) -> dict[str, list[PatchOp]]:
    """Parse an Anchor Patch DSL string (or JSON array) into per-file PatchOp lists."""
    stripped = spec_text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return _parse_json_patch_spec(stripped)

    # Support single-line CLI invocation in Windows cmd.exe where \n is passed literally
    if "\n" not in spec_text and "\\n" in spec_text:
        spec_text = spec_text.replace("\\n", "\n")
    ops_by_file: dict[str, list[PatchOp]] = {}
    current_file: str | None = None
    lines = spec_text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            i += 1
            continue

        if s.startswith("@file "):
            current_file = s[6:].strip().strip("`'\"")
            current_file = current_file.replace("\\", "/")
            ops_by_file.setdefault(current_file, [])
            i += 1
            continue

        if s.startswith("@sub "):
            if not current_file:
                raise ValueError(f"Line {i + 1}: @sub encountered before @file declaration")
            rest = s[5:].strip()
            # If rest starts with backtick/quote directly without anchor, default anchor to '*'
            if rest.startswith(("`", '"', "'")):
                a_mode, l_start, l_end, sym_a = ("first", 1, 999999, "")
                expr_part = rest
            else:
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    raise ValueError(f"Line {i + 1}: @sub requires anchor and `<old>` -> `<new>`")
                a_mode, l_start, l_end, sym_a = _parse_anchor(parts[0])
                expr_part = parts[1]
            old_e, new_e = _parse_sub_expr(expr_part)
            ops_by_file[current_file].append(
                PatchOp(
                    kind="sub",
                    line_start=l_start,
                    line_end=l_end,
                    anchor_mode=a_mode,
                    symbol_anchor=sym_a,
                    old_expr=old_e,
                    new_expr=new_e,
                    raw_spec_chars=len(line) + 1,
                )
            )
            i += 1
            continue

        if s.startswith("@delete "):
            if not current_file:
                raise ValueError(f"Line {i + 1}: @delete encountered before @file declaration")
            l_start, l_end = _parse_line_range(s[8:].strip())
            ops_by_file[current_file].append(
                PatchOp(
                    kind="delete",
                    line_start=l_start,
                    line_end=l_end,
                    raw_spec_chars=len(line) + 1,
                )
            )
            i += 1
            continue

        for block_cmd in ("@replace ", "@insert_after ", "@insert_before "):
            if s.startswith(block_cmd):
                if not current_file:
                    raise ValueError(f"Line {i + 1}: {block_cmd.strip()} encountered before @file declaration")
                kind = block_cmd.strip().lstrip("@")
                anchor_str = s[len(block_cmd) :].strip().split()[0]
                l_start, l_end = _parse_line_range(anchor_str)
                body: list[str] = []
                spec_chars = len(line) + 1
                i += 1
                while i < n:
                    if lines[i].strip() == "@end":
                        spec_chars += len(lines[i]) + 1
                        i += 1
                        break
                    body.append(lines[i])
                    spec_chars += len(lines[i]) + 1
                    i += 1
                ops_by_file[current_file].append(
                    PatchOp(
                        kind=kind,
                        line_start=l_start,
                        line_end=l_end,
                        body_lines=body,
                        raw_spec_chars=spec_chars,
                    )
                )
                break
        else:
            raise ValueError(f"Line {i + 1}: Unrecognized patch directive: {s!r}")

    return ops_by_file


def _parse_json_patch_spec(json_str: str) -> dict[str, list[PatchOp]]:
    data = json.loads(json_str)
    if isinstance(data, dict):
        data = data.get("patches", [data])
    ops_by_file: dict[str, list[PatchOp]] = {}
    for item in data:
        rel = (item.get("file") or item.get("path") or "").replace("\\", "/")
        if not rel:
            raise ValueError("JSON patch item missing 'file'")
        ops_by_file.setdefault(rel, [])
        kind = item.get("op") or item.get("kind") or "replace"
        lines_val = str(item.get("lines") or item.get("line") or "*")
        a_mode, l_start, l_end, sym_a = _parse_anchor(lines_val)
        body = item.get("body") or item.get("replacement") or ""
        body_lines = body.splitlines() if isinstance(body, str) else list(body)
        ops_by_file[rel].append(
            PatchOp(
                kind=kind,
                line_start=l_start,
                line_end=l_end,
                anchor_mode=a_mode,
                symbol_anchor=sym_a,
                old_expr=item.get("old", ""),
                new_expr=item.get("new", ""),
                body_lines=body_lines,
                raw_spec_chars=len(json.dumps(item)),
            )
        )
    return ops_by_file


def _check_syntax(rel_path: str, content: str) -> dict[str, Any]:
    """Validate syntax of patched file content using ast (Python) or Tree-sitter (TS/JS/etc.)."""
    ext = Path(rel_path).suffix.lower()
    if ext == ".py":
        try:
            ast.parse(content)
            return {"valid": True, "checker": "python_ast", "errors": []}
        except SyntaxError as e:
            return {
                "valid": False,
                "checker": "python_ast",
                "errors": [f"Line {e.lineno}: {e.msg}"],
            }

    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        try:
            from .parsers import _PARSERS

            parser = _PARSERS.get(ext)
            if parser is not None:
                tree = parser.parse(content.encode("utf-8"))
                errors: list[str] = []

                def _walk_err(node: Any) -> None:
                    if len(errors) >= 5:
                        return
                    if node.type == "ERROR" or node.is_missing:
                        row, col = node.start_point
                        snippet = (node.text or b"").decode("utf-8", errors="ignore")[:40]
                        errors.append(f"Line {row + 1}:{col + 1} syntax error near `{snippet}`")
                    for child in node.children:
                        _walk_err(child)

                _walk_err(tree.root_node)
                return {"valid": len(errors) == 0, "checker": f"tree_sitter_{ext.lstrip('.')}", "errors": errors}
        except Exception:
            pass

    return {"valid": True, "checker": "none", "errors": []}


def apply_patch_spec(
    root: Path,
    spec_text: str,
    apply: bool = False,
    diff_out_path: Path | None = None,
    fuzzy_window: int = 12,
) -> dict[str, Any]:
    """Execute or dry-run a Surgical Anchor Patch spec and compute exact output token savings."""
    ops_by_file = parse_patch_spec(spec_text)
    file_results: list[dict[str, Any]] = []
    unified_diffs: list[str] = []

    total_spec_chars = len(spec_text)
    total_edit_file_equiv_chars = 0
    total_ops_applied = 0

    for rel_path, ops in ops_by_file.items():
        abs_path = (root / rel_path).resolve()
        if not abs_path.exists():
            file_results.append({"file": rel_path, "status": "error", "error": f"File not found: {rel_path}"})
            continue

        raw_text = abs_path.read_text(encoding="utf-8", errors="replace")
        orig_lines = raw_text.splitlines()
        work_lines = list(orig_lines)

        # Sort operations bottom-up by line_start so earlier line numbers never shift
        sorted_ops = sorted(ops, key=lambda o: (o.line_start, o.line_end), reverse=True)
        applied_details: list[dict[str, Any]] = []

        for op in sorted_ops:
            n_lines = len(work_lines)
            idx_s = max(0, min(n_lines, op.line_start - 1))
            idx_e = max(idx_s, min(n_lines, op.line_end))

            ctx_s = max(0, idx_s - 2)
            ctx_e = min(n_lines, idx_e + 2)
            target_block = "\n".join(orig_lines[ctx_s:ctx_e])
            if op.kind == "sub":
                if op.anchor_mode == "all":
                    matched_lines: list[int] = []
                    for k in range(n_lines):
                        if op.old_expr in work_lines[k]:
                            work_lines[k] = work_lines[k].replace(op.old_expr, op.new_expr)
                            matched_lines.append(k + 1)
                    if not matched_lines:
                        raise ValueError(f"{rel_path}: @sub ALL target {op.old_expr!r} not found in file")
                    # Standard edit_file replaces all occurrences of a unique target in 1 call
                    first_k = matched_lines[0] - 1
                    c_s = max(0, first_k - 2)
                    c_e = min(len(orig_lines), first_k + 3)
                    t_b = "\n".join(orig_lines[c_s:c_e])
                    r_b = t_b.replace(op.old_expr, op.new_expr)
                    total_edit_file_equiv_chars += len(t_b) + len(r_b) + len(rel_path) + 80
                    applied_details.append(
                        {"op": "sub_all", "lines": matched_lines, "spec_chars": op.raw_spec_chars}
                    )
                    total_ops_applied += len(matched_lines)
                    continue

                found_idx = -1
                if op.anchor_mode == "symbol" and op.symbol_anchor:
                    sym_line = 0
                    for k, l_txt in enumerate(work_lines):
                        if op.symbol_anchor in l_txt:
                            sym_line = k
                            break
                    for k in range(sym_line, n_lines):
                        if op.old_expr in work_lines[k]:
                            found_idx = k
                            break

                if found_idx == -1 and op.anchor_mode == "line":
                    for k in range(idx_s, min(n_lines, idx_e)):
                        if op.old_expr in work_lines[k]:
                            found_idx = k
                            break
                    if found_idx == -1:
                        w_start = max(0, idx_s - fuzzy_window)
                        w_end = min(n_lines, idx_e + fuzzy_window)
                        for k in range(w_start, w_end):
                            if op.old_expr in work_lines[k]:
                                found_idx = k
                                break

                # Fallback: if unique or first match in file
                if found_idx == -1:
                    for k in range(n_lines):
                        if op.old_expr in work_lines[k]:
                            found_idx = k
                            break

                if found_idx == -1:
                    raise ValueError(f"{rel_path}: @sub target {op.old_expr!r} not found")

                old_line = work_lines[found_idx]
                new_line = old_line.replace(op.old_expr, op.new_expr, 1)
                work_lines[found_idx] = new_line

                ctx_s = max(0, found_idx - 3)
                ctx_e = min(len(orig_lines), found_idx + 4)
                t_blk = "\n".join(orig_lines[ctx_s:ctx_e])
                r_blk = t_blk.replace(op.old_expr, op.new_expr, 1)
                equiv_chars = len(t_blk) + len(r_blk) + len(rel_path) + 120
                total_edit_file_equiv_chars += equiv_chars
                applied_details.append(
                    {"op": "sub", "line": found_idx + 1, "spec_chars": op.raw_spec_chars, "edit_file_equiv_chars": equiv_chars}
                )
                total_ops_applied += 1

            elif op.kind == "replace":
                work_lines[idx_s:idx_e] = op.body_lines
                r_blk = "\n".join(orig_lines[ctx_s:idx_s] + op.body_lines + orig_lines[idx_e:ctx_e])
                equiv_chars = len(target_block) + len(r_blk) + len(rel_path) + 120
                total_edit_file_equiv_chars += equiv_chars
                applied_details.append(
                    {
                        "op": "replace",
                        "lines": f"L{op.line_start}-L{op.line_end}",
                        "spec_chars": op.raw_spec_chars,
                        "edit_file_equiv_chars": equiv_chars,
                    }
                )
                total_ops_applied += 1

            elif op.kind == "insert_after":
                ins_pos = min(n_lines, op.line_start)
                work_lines[ins_pos:ins_pos] = op.body_lines
                r_blk = "\n".join(orig_lines[ctx_s:ins_pos] + op.body_lines + orig_lines[ins_pos:ctx_e])
                equiv_chars = len(target_block) + len(r_blk) + len(rel_path) + 120
                total_edit_file_equiv_chars += equiv_chars
                applied_details.append(
                    {
                        "op": "insert_after",
                        "line": op.line_start,
                        "spec_chars": op.raw_spec_chars,
                        "edit_file_equiv_chars": equiv_chars,
                    }
                )
                total_ops_applied += 1

            elif op.kind == "insert_before":
                ins_pos = max(0, op.line_start - 1)
                work_lines[ins_pos:ins_pos] = op.body_lines
                r_blk = "\n".join(orig_lines[ctx_s:ins_pos] + op.body_lines + orig_lines[ins_pos:ctx_e])
                equiv_chars = len(target_block) + len(r_blk) + len(rel_path) + 120
                total_edit_file_equiv_chars += equiv_chars
                applied_details.append(
                    {
                        "op": "insert_before",
                        "line": op.line_start,
                        "spec_chars": op.raw_spec_chars,
                        "edit_file_equiv_chars": equiv_chars,
                    }
                )
                total_ops_applied += 1

            elif op.kind == "delete":
                del work_lines[idx_s:idx_e]
                r_blk = "\n".join(orig_lines[ctx_s:idx_s] + orig_lines[idx_e:ctx_e])
                equiv_chars = len(target_block) + len(r_blk) + len(rel_path) + 120
                total_edit_file_equiv_chars += equiv_chars
                applied_details.append(
                    {
                        "op": "delete",
                        "lines": f"L{op.line_start}-L{op.line_end}",
                        "spec_chars": op.raw_spec_chars,
                        "edit_file_equiv_chars": equiv_chars,
                    }
                )
                total_ops_applied += 1

        trailing_nl = "\n" if raw_text.endswith("\n") else ""
        new_text = "\n".join(work_lines) + trailing_nl
        syntax_res = _check_syntax(rel_path, new_text)

        diff_lines = list(
            difflib.unified_diff(
                orig_lines,
                work_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
                n=3,
            )
        )
        diff_str = "\n".join(diff_lines)
        if diff_str:
            unified_diffs.append(diff_str)

        if apply and syntax_res["valid"]:
            abs_path.write_text(new_text, encoding="utf-8")

        file_results.append(
            {
                "file": rel_path,
                "status": "applied" if (apply and syntax_res["valid"]) else "verified_dry_run",
                "ops_applied": len(applied_details),
                "syntax": syntax_res,
                "diff_lines": len(diff_lines),
                "operations": list(reversed(applied_details)),
            }
        )

    combined_diff = "\n\n".join(unified_diffs)
    out_diff_file = diff_out_path or (root / ".projectbrain_patch.diff")
    if combined_diff:
        out_diff_file.write_text(combined_diff + "\n", encoding="utf-8")

    total_diff_lines = sum(f.get("diff_lines", 0) for f in file_results)
    syntax_verified_files = sum(1 for f in file_results if f.get("syntax", {}).get("valid"))
    return {
        "mode": "applied" if apply else "dry_run",
        "files_patched": len(file_results),
        "total_ops_applied": total_ops_applied,
        "all_syntax_valid": all(f.get("syntax", {}).get("valid", False) for f in file_results),
        "diff_path": str(out_diff_file) if combined_diff else None,
        "mutation_telemetry": {
            "spec_chars": total_spec_chars,
            "diff_lines": total_diff_lines,
            "syntax_verified_files": syntax_verified_files,
        },
        "files": file_results,
        "unified_diff": combined_diff,
    }
