"""Graph-Aware Command, Test & Log Output Compressor for ProjectBrain.

Provides:
  1. Stack-trace frame extraction across Python, JS/TS, Go, Rust, and Java (`_extract_stack_trace_frames`).
  2. Multi-stage test/log output compression with 6D Code Graph traceback resolution (`compress_command_output`).
  3. Subprocess command execution with compressed output (`run_command_compressed`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import TYPE_CHECKING

from .graph import fn_id

if TYPE_CHECKING:
    from .brain import ProjectBrain


def _extract_stack_trace_frames(prompt: str) -> list[dict]:
    """Extract (file_hint, line, func_name) frames from Python, JS/TS, Go, Rust, and Java stack traces."""
    frames: list[dict] = []
    seen: set[tuple[str, int]] = set()

    # 1. Python: File "path/to/mod.py", line 42, in my_func
    for m in re.finditer(r'File\s+"([^"]+\.py)"\s*,\s*line\s+(\d+)(?:\s*,\s*in\s+([A-Za-z_]\w*))?', prompt):
        f_hint = m.group(1).replace("\\", "/")
        ln = int(m.group(2))
        fn = m.group(3) or ""
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": fn})

    # 2. JS/TS V8: at funcName (path/to/file.ts:42:10) or at path/to/file.ts:42:10
    for m in re.finditer(
        r"\bat\s+(?:([A-Za-z_$][\w$.]*)\s+\()?([^\s():]+\.(?:ts|tsx|js|jsx|mjs|cjs)):(\d+)(?::\d+)?\)?",
        prompt,
    ):
        fn = (m.group(1) or "").split(".")[-1]
        f_hint = m.group(2).replace("\\", "/")
        ln = int(m.group(3))
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": fn})

    # 3. General file.ext:line (Go, Rust, Java, Python, TS)
    for m in re.finditer(r"\b([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java)):(\d+)\b", prompt):
        f_hint = m.group(1).replace("\\", "/")
        ln = int(m.group(2))
        if (f_hint, ln) not in seen:
            seen.add((f_hint, ln))
            frames.append({"file_hint": f_hint, "line": ln, "function": ""})

    return frames


def compress_command_output(brain: "ProjectBrain", raw_output: str, max_lines: int = 80) -> dict:
    """Graph-Aware Command, Test & Log Output Compressor ('Read less' + 6D Traceback Resolution).

    1. Strips ANSI escape sequences and carriage-return progress overwrites.
    2. Folds passing test noise (pytest, unittest, jest/vitest, go test, cargo test) into compact counts.
    3. Deduplicates repetitive log/JSON/CSV lines sharing the same structural template.
    4. Cross-references any stack-trace `file:line` frames against the live 6D Code Graph
       to surface the exact failing symbols, signatures, 6D roles, and 1-hop callers.
    """
    raw_text = raw_output or ""
    raw_chars = len(raw_text)
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_text)
    clean = re.sub(r"[^\r\n]+\r(?!\n)", "", clean)
    clean = clean.replace("\r\n", "\n")

    lines = clean.splitlines()
    pass_re = re.compile(
        r"^(?:"
        r"\s*\S+::\S+\s+PASSED(?:\s+\[\s*\d+%\])?\s*$|"
        r"\s*PASSED\s+\S+.*$|"
        r"\s*[\w./\\-]+(?:\.py|\.ts|\.tsx|\.js|\.go|\.rs)\s+[.s]+\s*(?:\[\s*\d+%\])?\s*$|"
        r"\s*[.s]{3,}\s*(?:\[\s*\d+%\])?\s*$|"
        r"\s*(?:ok\s+\S+\s+[\d.]+s|---\s+PASS:\s+\S+|===\s+RUN\s+\S+)\s*$|"
        r"\s*test\s+\S+\s+\.\.\.\s+ok\s*$|"
        r"\s*(?:PASS|✓|√)\s+.+$"
        r")"
    )
    urgent_re = re.compile(
        r"(?:FAIL|FAILED|ERROR|Traceback|Exception|AssertionError|panic:|--- FAIL:)",
        re.IGNORECASE,
    )

    stage1_lines: list[str] = []
    passing_folded = 0
    run_pass_streak = 0
    for ln in lines:
        if pass_re.match(ln):
            passing_folded += 1
            run_pass_streak += 1
        else:
            if run_pass_streak > 0:
                stage1_lines.append(f"[... folded {run_pass_streak} passing test line(s) ...]")
                run_pass_streak = 0
            stage1_lines.append(ln)
    if run_pass_streak > 0:
        stage1_lines.append(f"[... folded {run_pass_streak} passing test line(s) ...]")

    def _line_template(s: str) -> str:
        t = s.strip()
        if not t:
            return ""
        t = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "<TS>", t)
        t = re.sub(r"\b[0-9a-fA-F]{8,}\b", "<HEX>", t)
        t = re.sub(r"\b\d+(?:\.\d+)?\b", "<NUM>", t)
        return t

    stage2_lines: list[str] = []
    repeated_folded = 0
    prev_tmpl: str | None = None
    tmpl_streak = 0
    for ln in stage1_lines:
        tmpl = _line_template(ln)
        if tmpl and tmpl == prev_tmpl and not urgent_re.search(ln):
            tmpl_streak += 1
            if tmpl_streak <= 2:
                stage2_lines.append(ln)
            else:
                repeated_folded += 1
        else:
            if tmpl_streak > 2:
                stage2_lines.append(f"[... folded {tmpl_streak - 2} repetitive line(s) ...]")
            prev_tmpl = tmpl
            tmpl_streak = 1
            stage2_lines.append(ln)
    if tmpl_streak > 2:
        stage2_lines.append(f"[... folded {tmpl_streak - 2} repetitive line(s) ...]")

    if max_lines > 0 and len(stage2_lines) > max_lines:
        head_n = max(10, max_lines // 3)
        tail_n = max(15, max_lines - head_n)
        head = stage2_lines[:head_n]
        tail = stage2_lines[-tail_n:]
        middle = stage2_lines[head_n:-tail_n]
        urgent_mid = [m for m in middle if urgent_re.search(m)][:20]
        omitted = len(middle) - len(urgent_mid)
        windowed = list(head)
        if urgent_mid:
            windowed.extend(urgent_mid)
        if omitted > 0:
            windowed.append(f"[... truncated {omitted} non-error middle line(s) ...]")
        windowed.extend(tail)
        stage3_lines = windowed
    else:
        stage3_lines = stage2_lines

    frames = _extract_stack_trace_frames(clean)
    resolved_6d_frames: list[dict] = []
    if frames:
        if not brain._index and brain._db_store.db_path.exists():
            brain.load()
        for fr in frames:
            f_hint = fr["file_hint"]
            ln_no = fr["line"]
            matched_rel = None
            if f_hint in brain._index:
                matched_rel = f_hint
            else:
                for rel_k in brain._index:
                    if rel_k.endswith(f_hint) or f_hint.endswith(rel_k):
                        matched_rel = rel_k
                        break
            if not matched_rel:
                continue
            f_info = brain._index.get(matched_rel, {})
            enclosing_fn = None
            for fn in f_info.get("functions", []):
                if fn.get("line_start", 1) <= ln_no <= fn.get("line_end", 1):
                    enclosing_fn = fn
                    break
            cell_role = "LOGIC"
            for cell in f_info.get("cells_4d", []):
                if cell.get("line_start", 1) <= ln_no <= cell.get("line_end", 1):
                    cell_role = cell.get("role", cell_role)
                    break
            sym_name = enclosing_fn["name"] if enclosing_fn else (fr.get("function") or "<module>")
            sig = enclosing_fn.get("signature", sym_name) if enclosing_fn else sym_name
            l_span = (
                f"L{enclosing_fn.get('line_start', ln_no)}-L{enclosing_fn.get('line_end', ln_no)}"
                if enclosing_fn
                else f"L{ln_no}"
            )
            callers: list[str] = []
            if brain._graph and enclosing_fn:
                nid = fn_id(matched_rel, enclosing_fn["name"], enclosing_fn.get("parent_class"))
                callers = [
                    e["src"]
                    for e in (brain._graph.edges[i] for i in brain._graph._radj.get(nid, []))
                    if e.get("type") == "calls"
                ][:3]
            entry = {
                "rel_path": matched_rel,
                "line": ln_no,
                "symbol": sym_name,
                "lines": l_span,
                "role_6d": cell_role,
                "signature": sig,
                "callers": callers,
            }
            if entry not in resolved_6d_frames:
                resolved_6d_frames.append(entry)

    if resolved_6d_frames:
        stage3_lines.append("")
        stage3_lines.append("# [6D Failing Frame Resolution]")
        for rf in resolved_6d_frames:
            caller_str = f" | Callers: {', '.join(rf['callers'])}" if rf["callers"] else ""
            stage3_lines.append(
                f"#   -> {rf['rel_path']}:L{rf['line']} in {rf['symbol']} ({rf['lines']}) "
                f"| Role:{rf['role_6d']} | Sig: `{rf['signature']}`{caller_str}"
            )

    compressed_text = "\n".join(stage3_lines)
    comp_chars = len(compressed_text)
    raw_tokens = max(1, raw_chars // 4) if raw_chars > 0 else 0
    comp_tokens = max(1, comp_chars // 4) if comp_chars > 0 else 0
    saved_tokens = max(0, raw_tokens - comp_tokens)
    savings_pct = round((saved_tokens / raw_tokens) * 100.0, 1) if raw_tokens > 0 else 0.0

    brain._ledger.record_input_op(
        op="run_compress",
        query="command_output",
        files_covered=len({r["rel_path"] for r in resolved_6d_frames}),
        cells_4d_resolved=len(resolved_6d_frames),
        naive_bytes=raw_chars,
        emitted_chars=comp_chars,
    )

    return {
        "compressed_output": compressed_text,
        "raw_chars": raw_chars,
        "compressed_chars": comp_chars,
        "raw_tokens_est": raw_tokens,
        "compressed_tokens_est": comp_tokens,
        "tokens_saved_est": saved_tokens,
        "savings_pct": savings_pct,
        "passing_tests_folded": passing_folded,
        "repeated_lines_folded": repeated_folded,
        "resolved_6d_frames": resolved_6d_frames,
    }


def run_command_compressed(
    brain: "ProjectBrain",
    command: str | list[str],
    timeout: int = 120,
    max_lines: int = 80,
) -> dict:
    """Execute a command in `brain.root` and compress stdout/stderr with 6D traceback resolution."""
    if isinstance(command, str):
        has_shell_meta = any(op in command for op in ("|", "&&", "||", ";", ">", "<", "$("))
        if not has_shell_meta:
            try:
                cmd_args: str | list[str] = shlex.split(command, posix=(os.name != "nt"))
                use_shell = False
            except ValueError:
                cmd_args = command
                use_shell = True
        else:
            cmd_args = command
            use_shell = True
    else:
        cmd_args = command
        use_shell = False
    try:
        proc = subprocess.run(
            cmd_args,
            cwd=str(brain.root),
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out_s = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err_s = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        combined = f"{out_s}\n{err_s}\n[ProjectBrain] Command timed out after {timeout}s."
        exit_code = 124

    res = compress_command_output(brain, combined, max_lines=max_lines)
    res["command"] = command if isinstance(command, str) else " ".join(command)
    res["exit_code"] = exit_code
    return res
