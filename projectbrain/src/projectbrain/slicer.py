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

import re
from pathlib import Path
from .parsers import _PARSERS

_IMPORTANT_CODE_PAT = re.compile(
    r"\b(setInterval|setTimeout|clearTimeout|clearInterval|addEventListener|fetch|Promise\.race|AbortController|AbortSignal|"
    r"catch|throw|finally|document\.cookie|localStorage|sessionStorage|"
    r"setDoc|updateDoc|getDoc|getDocs|addDoc|deleteDoc|writeBatch|runTransaction|onSnapshot|hydrate|flush|"
    r"[a-zA-Z0-9_$]+(Cooldowns|Storage|Kv|Ref\.current|Payload|State|Data)\.(get|set|has|add|delete)|"
    r"[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b"
)
_UPPER_CONST_PAT = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_CODE_STMT_PAT = re.compile(
    r"^\s*(const\s|let\s|var\s|if\s*\(|else\b|for\s*\(|while\s*\(|try\s*\{|catch\s*\(|return\b|await\s|throw\s|"
    r"[a-zA-Z0-9_$.]+\.[a-zA-Z0-9_$]+\s*=|"
    r"[a-zA-Z0-9_$.]+\.(set|get|delete|clear|test|some|race|fetch|json|addEventListener|push|assign)\()"
)
_KV_PROP_PAT = re.compile(
    r"^\s*['\"]?[a-zA-Z_$][a-zA-Z0-9_$]*['\"]?\s*:\s*[^/\s]"
)
_JSX_NOISE_PAT = re.compile(r"^\s*(</?[a-z]+[\s>]|className=|d=\"M\d|stroke=|fill=)", re.IGNORECASE)


def slice_lines(
    root: Path,
    rel_path: str,
    line_start: int,
    line_end: int,
    max_lines: int = 80,
    focus_terms: list[str] | None = None,
) -> str:
    """Read [line_start..line_end] (1-indexed, inclusive) from root/rel_path,
    prefixed with line numbers.

    When the range exceeds `max_lines`, performs smart focus-window folding
    (keeping the signature header + high-signal windows matching `focus_terms`,
    calls, timers, and control flow, while collapsing boilerplate/JSX gaps)
    rather than blindly chopping off the bottom of the function.
    """
    full_path = root / rel_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    start_idx = max(line_start - 1, 0)
    end_idx = min(line_end, len(lines))
    selected = lines[start_idx:end_idx]
    total_len = len(selected)

    if total_len <= max_lines:
        return "\n".join(
            f"{start_idx + i + 1:4d} | {line}"
            for i, line in enumerate(selected)
        )

    # Smart Focus-Window Folding for long functions / React monoliths
    terms = list(dict.fromkeys(t.lower() for t in (focus_terms or []) if len(t) >= 3))
    keep = [False] * total_len

    # Always keep the first head_n lines (signature + initial guardrails) and last tail_n lines
    head_n = min(15, max(5, max_lines // 4))
    tail_n = min(4, max(2, max_lines // 12))
    for i in range(head_n):
        keep[i] = True
    for i in range(max(0, total_len - tail_n), total_len):
        keep[i] = True

    # Compute term frequency inside the function to discount ubiquitous local variable names
    low_lines = [ln.lower() for ln in selected]
    term_weights: dict[str, float] = {}
    freq_cap = max(6, total_len // 6)
    for t in terms:
        df = sum(1 for low in low_lines if t in low)
        term_weights[t] = 0.6 if df > freq_cap else 2.5

    # Score remaining lines by keyword matches and high-signal control/timing patterns
    _CONSOLE_PAT = re.compile(r"^\s*console\.(log|warn|error|info|debug)\b")
    line_scores = [0.0] * total_len
    for i, line in enumerate(selected):
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#"):
            continue
        if _CONSOLE_PAT.match(line):
            line_scores[i] = -2.0
            continue
        low = low_lines[i]
        sc = 0.0
        is_code_stmt = bool(_CODE_STMT_PAT.match(line))
        is_kv_prop = bool(_KV_PROP_PAT.match(line)) and not _JSX_NOISE_PAT.match(line)
        is_code_like = is_code_stmt or is_kv_prop or any(ch in stripped for ch in ("=", "{", "}", ";", ":"))
        for t in terms:
            if t in low:
                # Discount matches on pure English prose lines inside template strings
                sc += term_weights[t] if is_code_like else 0.2
        if _IMPORTANT_CODE_PAT.search(line):
            sc += 5.0
        if _UPPER_CONST_PAT.search(line) and is_code_like:
            sc += 3.5
        if re.match(r"^\s*for\s*\(", line):
            sc += 3.0
        if is_code_stmt:
            sc += 1.0
        if is_kv_prop:
            sc += 1.5
        if _JSX_NOISE_PAT.match(line) and sc <= 1.5:
            sc = -1.0
        line_scores[i] = sc

    # Smooth scores over a +/- 2 line window (weighting the center line highest so isolated statements aren't penalized by surrounding comments/blank lines)
    window_scores: list[tuple[float, int]] = []
    for i in range(head_n, total_len - tail_n):
        if line_scores[i] >= 1.0:
            neighbors = sum(
                max(0.0, line_scores[j])
                for j in range(max(0, i - 2), min(total_len, i + 3))
                if j != i
            )
            w_sc = line_scores[i] + 0.35 * neighbors
            window_scores.append((w_sc, i))
    window_scores.sort(key=lambda x: x[0], reverse=True)
    kept_count = sum(1 for k in keep if k)

    for _, center in window_scores:
        if kept_count >= max_lines:
            break
        radius = 2 if total_len < 500 else 3
        for j in range(max(0, center - radius), min(total_len, center + radius + 1)):
            if not keep[j] and line_scores[j] >= -0.5:
                keep[j] = True
                kept_count += 1
                if kept_count >= max_lines:
                    break

    # If few focus windows matched inside the function, fill remaining budget with non-noise code lines
    if kept_count < max_lines // 2:
        for i in range(min(total_len, max_lines - tail_n)):
            if kept_count >= max_lines:
                break
            if not keep[i] and line_scores[i] >= 0.0:
                keep[i] = True
                kept_count += 1
    # Merge small 1-3 line gaps between kept blocks
    last_kept = -1
    for i in range(total_len):
        if keep[i]:
            if last_kept >= 0 and 0 < (i - last_kept - 1) <= 3 and kept_count + (i - last_kept - 1) <= max_lines + 6:
                for g in range(last_kept + 1, i):
                    keep[g] = True
                    kept_count += 1
            last_kept = i

    formatted: list[str] = []
    i = 0
    while i < total_len:
        if keep[i]:
            formatted.append(f"{start_idx + i + 1:4d} | {selected[i]}")
            i += 1
        else:
            gap_start = i
            while i < total_len and not keep[i]:
                i += 1
            gap_lines = i - gap_start
            landmarks: list[str] = []
            if gap_lines >= 8:
                for k in range(gap_start, i):
                    st = selected[k].strip()
                    if len(st) > 6 and (
                        "onClick=" in st
                        or "className=" in st
                        or "&& (" in st
                        or st.startswith(("/* ", "{/*", "case ", "if (", "return ("))
                    ):
                        snippet = re.sub(r"\s+", " ", st)[:42]
                        landmarks.append(f"L{start_idx + k + 1}:{snippet}")
                        if len(landmarks) >= 4:
                            break
            lm_suffix = f" | landmarks: {', '.join(landmarks)}" if landmarks else ""
            formatted.append(
                f" ... | [folded {gap_lines} lines L{start_idx + gap_start + 1}-L{start_idx + i}{lm_suffix}]"
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
            elif ch == close_ch:
                depth -= 1
                if started and depth <= 0:
                    return idx
    return min(n - 1, start_idx + 45)


def extract_4d_coordinate_matrix(
    lines: list[str],
    line_start: int = 1,
    line_end: int | None = None,
    ext: str = ".tsx",
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

    # Pass 0: Tree-sitter AST traversal for exact block end lines & AST assignment mutations
    ast_span_by_start: dict[int, int] = {}
    ast_mutations_by_line: dict[int, list[str]] = {}
    parser = _PARSERS.get(ext.lower()) or _PARSERS.get(".tsx")
    if parser and n > 0:
        try:
            src_bytes = "\n".join(lines).encode("utf-8", errors="replace")
            tree = parser.parse(src_bytes)
            stack = [tree.root_node]
            _SPAN_NODE_TYPES = {
                "function_declaration", "arrow_function", "function_definition",
                "method_definition", "call_expression", "if_statement", "elif_clause",
                "else_clause", "try_statement", "except_clause", "catch_clause",
                "with_statement", "for_statement", "for_in_statement", "while_statement",
                "match_statement", "case_clause", "switch_case", "switch_default",
                "jsx_expression", "ternary_expression", "conditional_expression",
            }
            _ASSIGN_NODE_TYPES = {
                "assignment_expression", "augmented_assignment_expression",
                "update_expression", "assignment", "augmented_assignment",
            }
            while stack:
                nd = stack.pop()
                nd_type = nd.type
                s_ln = nd.start_point[0] + 1
                e_ln = nd.end_point[0] + 1
                if nd_type in _SPAN_NODE_TYPES and e_ln > s_ln:
                    prev_e = ast_span_by_start.get(s_ln, 0)
                    if (e_ln - s_ln) <= 2500 and e_ln > prev_e:
                        ast_span_by_start[s_ln] = e_ln
                if nd_type in _ASSIGN_NODE_TYPES:
                    lhs = nd.child_by_field_name("left") or (nd.children[0] if nd.children else None)
                    if lhs is not None:
                        lhs_txt = lhs.text.decode("utf-8", errors="replace").strip()
                        m_prop = re.match(r"^(?:self|this|state|store|draft|window|globalThis)\.([a-zA-Z_]\w*)", lhs_txt)
                        if m_prop:
                            prop_name = m_prop.group(1)
                            if not prop_name.startswith("_"):
                                ast_mutations_by_line.setdefault(s_ln, []).append(prop_name)
                if nd.children:
                    stack.extend(nd.children)
        except Exception:
            pass

    # Pass 1: Discover state variables & their setters across the file (useState, useRef, Zustand/Redux/Context hooks, self.*)
    state_vars: dict[str, str] = {}
    for ln in lines:
        for m_st in re.finditer(r"const\s+\[\s*(\w+)\s*,\s*(\w+)\s*\]\s*=\s*useState", ln):
            state_vars[m_st.group(1)] = m_st.group(2)
        for m_rf in re.finditer(r"const\s+(\w+)\s*=\s*useRef\b", ln):
            rv = m_rf.group(1)
            state_vars[rv] = f"{rv}\\.current\\s*="
        for m_py in re.finditer(r"\bself\.([a-zA-Z_]\w*)\s*=", ln):
            pv = m_py.group(1)
            if not pv.startswith("_"):
                state_vars[pv] = f"self\\.{pv}\\s*="
        # Zustand / Redux / Context / Custom Hook object destructuring: const { user, setUser, dispatch } = useAuthStore(...)
        m_hk = re.search(r"const\s+\{([^}]+)\}\s*=\s*(?:use[A-Z]\w*|useStore)\s*\(", ln)
        if m_hk:
            for raw_tok in m_hk.group(1).split(","):
                tok = raw_tok.split(":")[-1].split("=")[0].strip()
                if not re.match(r"^[a-zA-Z_]\w*$", tok):
                    continue
                m_act = re.match(r"^(?:set|update|add|remove|delete|toggle|clear|reset|mark)([A-Z]\w*)$", tok)
                if m_act:
                    target_k = m_act.group(1)[0].lower() + m_act.group(1)[1:]
                    state_vars[target_k] = tok
                elif tok == "dispatch":
                    state_vars["dispatch"] = "dispatch"
                elif tok not in state_vars:
                    state_vars[tok] = f"__readonly_{tok}__"
        # Zustand selector: const bears = useStore((s) => s.bears)
        m_zs = re.search(r"const\s+(\w+)\s*=\s*use\w*Store\s*\([^)]*=>\s*\w+\.(\w+)", ln)
        if m_zs:
            local_nm = m_zs.group(1)
            m_act = re.match(r"^(?:set|update|add|remove|delete|toggle|clear|reset)([A-Z]\w*)$", local_nm)
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

        # 0. SQL CREATE TABLE blocks (in .sql files or inline schema strings)
        m_ct = re.search(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[a-zA-Z_]\w*\.)?[\"`]?([a-zA-Z_][a-zA-Z0-9_]*)[\"`]?\s*\(",
            st,
            re.IGNORECASE,
        )
        if m_ct:
            tbl_nm = m_ct.group(1)
            end_i = _find_balanced_end(lines, i, "(", ")", max_scan=120)
            raw_blocks.append(("SCHEMA", f"table:{tbl_nm}", lnum, end_i + 1))
            i = end_i + 1
            continue
        # 1. Contiguous STATE declarations (useState / useRef)
        if re.search(r"\buse(?:State|Ref)\s*(?:<[^>]*>)?\s*\(", st):
            b_start = lnum
            while i + 1 < e_idx:
                nxt = lines[i + 1].strip()
                if re.search(r"\buse(?:State|Ref)\b", nxt) or not nxt or nxt.startswith("//"):
                    i += 1
                else:
                    break
            raw_blocks.append(("STATE", "state_declarations", b_start, i + 1))
            i += 1
            continue

        # 2. Reactive EFFECT / Memo / Callback blocks
        m_eff = re.search(r"\b(useEffect|useLayoutEffect|useMemo|useCallback)\s*\(", st)
        if m_eff:
            kind = m_eff.group(1)
            end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "(", ")", max_scan=300)
            tail = " ".join(lines[max(i, end_i - 3) : end_i + 1])
            m_deps = re.search(r"\[([^\]]*)\]\s*\)", tail)
            deps = m_deps.group(1).strip() if m_deps else ""
            role = "EFFECT" if "Effect" in kind else "ACTION"
            raw_blocks.append((role, f"{kind}[{deps[:36]}]", lnum, end_i + 1))
            i = end_i + 1
            continue

        # 3. Switch case dispatch branches: case "foo":
        m_case = re.match(r"^(?:case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:)+", st)
        if m_case:
            case_labels = re.findall(r"case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:", st)
            while i + 1 < e_idx and re.match(r"^case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:", lines[i + 1].strip()):
                i += 1
                case_labels.extend(re.findall(r"case\s+(['\"][^'\"]+['\"]|[a-zA-Z0-9_$.]+)\s*:", lines[i].strip()))
            lbl_str = "|".join(c.strip("'\"") for c in case_labels[:3])
            # Scan forward until next sibling case/default at depth <= 0 or switch closing brace
            b_depth = 0
            p_depth = 0
            end_i = i
            for k in range(i, min(e_idx, i + 2500)):
                k_st = lines[k].strip()
                if k > i and b_depth == 0 and p_depth == 0 and (
                    re.match(r"^(?:case\s+|default\s*:)", k_st)
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

        # 4. Named functions / arrow handlers / Python defs / TS methods (skip giant outer containers >220 lines so inner cells are indexed)
        m_fn = re.match(
            r"^(?:export\s+)?(?:async\s+)?(?:function|def)\s+([a-zA-Z_]\w*)|^(?:export\s+)?const\s+([a-zA-Z_]\w*)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::\s*[^=]+)?=>\s*\{|^(?:public\s+|private\s+|protected\s+)?(?:async\s+)?([a-zA-Z_]\w*)\s*\([^;]*\)\s*(?::\s*[^{]+)?\{",
            st,
        )
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
                    if re.search(
                        r"\b(?:fetch|axios|setItem|getItem|memoryKv|setDoc|updateDoc|execute|commit|prisma|supabase|dispatch|requests\.|httpx\.|aiohttp\.|redis\.|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+)\b",
                        body_txt,
                        re.IGNORECASE,
                    ):
                        role = "IO"
                    elif re.search(r"(?:</[A-Za-z]|/>)", body_txt) and "return" in body_txt:
                        role = "VIEW"
                    else:
                        role = "ACTION"
                    raw_blocks.append((role, f"fn:{fn_name}", lnum, end_i + 1))
                    # For functions >= 16 lines, step inside so inner branches, try/except, with, and SQL blocks are also extracted
                    i = (end_i + 1) if span < 8 else (i + 1)
                    continue

        # 5. Conditional dispatch branches: if (...) / elif (...) in JS/TS ({) and Python (:)
        m_if = re.match(r"^(?:else\s+if|elif|if)\s*\(?([^):{]{3,75})\)?\s*[{:]\s*(?:#.*)?$", st)
        if m_if:
            cond = m_if.group(1).strip()
            if st.endswith("{"):
                end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=400)
            else:
                end_i = _py_block_end(i, max_scan=250)
            span = (end_i + 1) - lnum
            if 4 <= span <= 220:
                sample = "\n".join(lines[i : min(end_i + 1, i + 30)])
                if re.search(
                    r"\b(?:fetch|axios|setItem|getItem|execute|commit|prisma|supabase|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+)\b",
                    sample,
                    re.IGNORECASE,
                ):
                    role = "IO"
                elif "<" in sample and "return" in sample:
                    role = "VIEW"
                else:
                    role = "ACTION"
                raw_blocks.append((role, f"branch[{cond[:40]}]", lnum, end_i + 1))
                i = (end_i + 1) if span <= 5 else (i + 1)
                continue

        # 5b. Context managers / transactions (`with ...:`) and exception handlers (`try` / `except` / `catch`)
        m_with = re.match(r"^(?:async\s+)?with\s+([^:]{3,65}):\s*(?:#.*)?$", st)
        if m_with:
            ctx_expr = m_with.group(1).strip()
            end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _py_block_end(i, max_scan=250)
            span = (end_i + 1) - lnum
            if 3 <= span <= 220:
                sample = "\n".join(lines[i : min(end_i + 1, i + 30)])
                role = "IO" if re.search(r"\b(?:tx|db|conn|cursor|open|lock|session|execute|INSERT\s+INTO|UPDATE\s+|SELECT\s+)\b", sample, re.IGNORECASE) else "ACTION"
                raw_blocks.append((role, f"with[{ctx_expr[:36]}]", lnum, end_i + 1))
                i = i + 1
                continue

        m_try = re.match(r"^(?:try\s*[:{]|(?:except|catch)\s*\(?([^):{]{0,50})\)?\s*[:{])\s*(?:#.*)?$", st)
        if m_try:
            is_catch = st.startswith(("except", "catch"))
            end_i = _find_balanced_end(lines, i, "{", "}", max_scan=200) if st.endswith("{") else _py_block_end(i, max_scan=200)
            span = (end_i + 1) - lnum
            if 3 <= span <= 180:
                sample = "\n".join(lines[i : min(end_i + 1, i + 25)])
                role = "IO" if re.search(r"\b(?:execute|commit|INSERT\s+INTO|UPDATE\s+|DELETE\s+FROM|SELECT\s+|fetch|axios)\b", sample, re.IGNORECASE) else "ACTION"
                lbl = f"catch[{(m_try.group(1) or 'all').strip()[:32]}]" if is_catch else "try_block"
                raw_blocks.append((role, lbl, lnum, end_i + 1))
                i = (end_i + 1) if (is_catch and span <= 6) else (i + 1)
                continue

        # 5c. Multi-line SQL execution statements (`conn.execute(...)`, `db.execute(...)`, `pool.query(...)`)
        if re.search(r"\b(?:conn|cur|cursor|db|session|pool|client|tx)\.(?:execute|executemany|query)\s*\(", st):
            end_i = _find_balanced_end(lines, i, "(", ")", max_scan=80)
            span = (end_i + 1) - lnum
            if 2 <= span <= 80:
                sql_sample = "\n".join(lines[i : end_i + 1])
                m_op = re.search(
                    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|SELECT\b[\s\S]{1,100}?\bFROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([a-zA-Z_][a-zA-Z0-9_]{2,})",
                    sql_sample,
                    re.IGNORECASE,
                )
                if m_op:
                    raw_verb = m_op.group(1).strip().upper()
                    verb = "SELECT" if raw_verb.startswith("SELECT") else raw_verb.split()[0]
                    tbl_nm = m_op.group(2)
                    raw_blocks.append(("IO", f"sql[{verb} {tbl_nm}]", lnum, end_i + 1))
                    i = end_i + 1
                    continue
        # 6. JSX conditional ({cond && (...)}, ternary {cond ? (...) : (...)}) or .map() render lists
        m_jsx = re.match(r"^\{\s*([a-zA-Z0-9_!.?\s&=|]{3,45})\s*(?:&&|\?)\s*\(", st)
        if m_jsx:
            cond = re.sub(r"\s+", " ", m_jsx.group(1).strip())
            end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=350)
            if 5 <= (end_i + 1 - lnum) <= 160:
                raw_blocks.append(("VIEW", f"jsx[{cond[:36]}]", lnum, end_i + 1))
                i = end_i + 1
                continue

        m_map = re.search(r"\{\s*([a-zA-Z_][\w.]*)\.map\s*\(", st)
        if m_map:
            arr_nm = m_map.group(1)
            end_i = (ast_span_by_start[lnum] - 1) if lnum in ast_span_by_start else _find_balanced_end(lines, i, "{", "}", max_scan=220)
            span_m = (end_i + 1) - lnum
            if 4 <= span_m <= 180:
                sample_m = "\n".join(lines[i : min(end_i + 1, i + 25)])
                if re.search(r"(?:</[A-Za-z]|/>|<[A-Z])", sample_m):
                    raw_blocks.append(("VIEW", f"jsx[{arr_nm}.map]", lnum, end_i + 1))
                    i = end_i + 1
                    continue

        i += 1

    cells: list[dict] = []
    for role, scope, s, e in raw_blocks:
        chunk = "\n".join(lines[s - 1 : e])
        z_write: list[str] = []
        z_read: list[str] = []
        for var, setter_pat in state_vars.items():
            if not setter_pat.startswith("__readonly_") and re.search(rf"\b{setter_pat}\b", chunk):
                z_write.append(var)
            elif re.search(rf"\b{var}\b", chunk):
                z_read.append(var)
        for ln_no in range(s, e + 1):
            for ast_w in ast_mutations_by_line.get(ln_no, []):
                if ast_w not in z_write:
                    z_write.append(ast_w)
        # ORM & Store writes (Prisma, Supabase, Zustand/Redux, SQLAlchemy/DB)
        for m_orm in re.finditer(
            r"\b(?:prisma\.(\w+)\.(?:create|update|upsert|delete)|supabase\.from\(\s*['\"](\w+)['\"]\s*\)\.(?:insert|update|upsert|delete)|(\w+Store)\.setState)\b",
            chunk,
        ):
            orm_target = m_orm.group(1) or m_orm.group(2) or m_orm.group(3)
            if orm_target and orm_target not in z_write:
                z_write.append(orm_target)
        # SQL table mutations & reads (Python / TS / Go universal SQL patterns)
        for m_sql_w in re.finditer(
            r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\b",
            chunk,
            re.IGNORECASE,
        ):
            tbl = m_sql_w.group(1)
            if tbl.lower() not in ("set", "values", "where", "table") and tbl not in z_write:
                z_write.append(tbl)
        for m_sql_r in re.finditer(
            r"\bSELECT\b[\s\S]{1,120}?\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\b",
            chunk,
            re.IGNORECASE,
        ):
            tbl = m_sql_r.group(1)
            if tbl not in z_write and tbl not in z_read:
                z_read.append(tbl)
        # Key-Value Storage, Dict/Session/Cache/Env State, and Event Channels (Python & JS/TS)
        for m_kv in re.finditer(
            r"(?:getItem|setItem|removeItem|memoryKv\.(?:get|set)|(?:state|store|session|cache|db|ledger|context|os\.environ)\.(?:get|set|put|write|update|delete)|os\.getenv)\(\s*['\"]([^'\"]{2,40})['\"]",
            chunk,
        ):
            k_name = m_kv.group(1)
            full_m = m_kv.group(0)
            if any(w in full_m for w in (".set", "setItem", "removeItem", ".put", ".write", ".update", ".delete")):
                if k_name not in z_write:
                    z_write.append(k_name)
            elif k_name not in z_read:
                z_read.append(k_name)
        for m_dk in re.finditer(
            r"\b(?:state|store|session|cache|db|ledger|context|os\.environ)\[\s*['\"]([^'\"]{2,40})['\"]\s*\](\s*=[^=])?",
            chunk,
        ):
            k_name = m_dk.group(1)
            is_assign = bool(m_dk.group(2))
            if is_assign:
                if k_name not in z_write:
                    z_write.append(k_name)
            elif k_name not in z_write and k_name not in z_read:
                z_read.append(k_name)
        # Event Bus / PubSub / Custom Event Channels (emit/publish/dispatch vs on/subscribe/listen)
        for m_ev_w in re.finditer(r"\b(?:emit|publish|trigger|broadcast)\(\s*['\"]([a-zA-Z0-9_.:-]{3,40})['\"]", chunk):
            ev_key = f"evt:{m_ev_w.group(1)}"
            if ev_key not in z_write:
                z_write.append(ev_key)
        for m_ev_r in re.finditer(r"\b(?:on|once|subscribe|addEventListener|listen)\(\s*['\"]([a-zA-Z0-9_.:-]{3,40})['\"]", chunk):
            ev_key = f"evt:{m_ev_r.group(1)}"
            if ev_key not in z_read:
                z_read.append(ev_key)
        # Python self.<attr> reads
        for m_py_r in re.finditer(r"\bself\.([a-zA-Z_]\w*)\b(?!\s*\()", chunk):
            attr = m_py_r.group(1)
            if not attr.startswith("_") and attr not in z_write and attr not in z_read:
                z_read.append(attr)

        # Axis 5 (E_exit): Failure, Exception & Early-Return Exit Contracts (Python raise/except + JS/TS throw/catch)
        e_exit: list[str] = []
        for m_ex in re.finditer(r"\b(?:raise|throw\s+new|throw)\s+([A-Za-z_]\w*)", chunk):
            exc = m_ex.group(1)
            if exc not in ("new", "return", "from") and f"throw:{exc}" not in e_exit:
                e_exit.append(f"throw:{exc}")
        for m_ct in re.finditer(r"\b(?:except|catch)\s*\(?([A-Za-z_]\w*)", chunk):
            exc = m_ct.group(1)
            if exc not in ("as",) and f"catch:{exc}" not in e_exit:
                e_exit.append(f"catch:{exc}")
        for m_st in re.finditer(
            r"\b(?:ApiError|HTTPException|status|status_code)\s*[\(=]\s*(?:status_code\s*=\s*)?(4\d\d|5\d\d)\b"
            r"(?:\s*,\s*['\"]([a-zA-Z0-9_\-]+)['\"])?",
            chunk,
        ):
            st_code = f"status:{m_st.group(1)}"
            if st_code not in e_exit:
                e_exit.append(st_code)
            if m_st.group(2):
                err_tag = f"err:{m_st.group(2).lower()}"
                if err_tag not in e_exit:
                    e_exit.append(err_tag)
        if re.search(r"\bif\b[^{:\n]*[:{]?\s*\n?\s*return\b(?:\s+(?:None|False|null|false|undefined|redirect\b))?", chunk):
            if "guard_return" not in e_exit:
                e_exit.append("guard_return")

        # Axis 6 (T_mode): Universal Temporal & Concurrency Mode (SYNC | ASYNC | REACTIVE | TIMER)
        if role == "EFFECT" or re.search(r"\b(?:useEffect|useLayoutEffect|onSnapshot|addEventListener|subscribe|on_event)\b", chunk):
            t_mode = "REACTIVE"
        elif re.search(r"\b(?:setTimeout|setInterval|requestAnimationFrame|asyncio\.sleep)\b", chunk):
            t_mode = "TIMER"
        elif re.search(r"\b(?:async\s+def|async\s+function|async\s*\(|await\s+|Promise\.|\.then\()\b", chunk):
            t_mode = "ASYNC"
        else:
            t_mode = "SYNC"

        w_comps = sorted(set(re.findall(r"<([A-Z][a-zA-Z0-9_]+)", chunk)))
        w_calls = sorted(
            {
                m.group(1)
                for m in re.finditer(r"\b([a-z][a-zA-Z0-9_]{3,})\s*\(", chunk)
                if m.group(1) not in ("if", "for", "while", "switch", "catch", "return", "function", "useEffect", "useState", "useMemo", "useCallback")
            }
        )
        sql_inserts: list[dict] = []
        for m_ins in re.finditer(
            r"\bINSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(([^)]+)\)\s*(?:VALUES|SELECT)\b",
            chunk,
            re.IGNORECASE,
        ):
            ins_tbl = m_ins.group(1)
            ins_cols = [
                c.strip().strip('"`')
                for c in m_ins.group(2).split(",")
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", c.strip().strip('"`'))
            ]
            if ins_cols:
                ins_line = s + chunk.count("\n", 0, m_ins.start())
                sql_inserts.append({"table": ins_tbl, "cols": ins_cols, "line": ins_line})

        sql_dialects: list[str] = []
        if re.search(r"\binterval\s+'[^']+'", chunk, re.IGNORECASE):
            sql_dialects.append("interval_literal")
        if re.search(r"\bmake_interval\s*\(", chunk, re.IGNORECASE):
            sql_dialects.append("make_interval")
        if re.search(r"=\s*ANY\s*\(\s*(?:%s|\$|\?)", chunk, re.IGNORECASE):
            sql_dialects.append("any_array")

        # Cross-stack HTTP client calls (fetch / axios / client.<verb>)
        # Cross-stack HTTP client calls (fetch / axios / client.<verb>) + expected response shape
        _JS_RES_BUILTINS = {
            "ok", "status", "statusText", "headers", "json", "text", "blob", "clone",
            "then", "catch", "finally", "length", "map", "filter", "forEach", "slice",
            "toString", "valueOf", "error", "errors", "detail", "message", "data",
        }
        expected_res_keys: list[str] = []
        for m_jvar in re.finditer(r"\b(?:const|let|var)\s+([a-zA-Z_]\w*)\s*=\s*await\s+\w+\.json\(\)", chunk):
            jvar = m_jvar.group(1)
            for m_prop in re.finditer(rf"\b{re.escape(jvar)}\.([a-zA-Z_]\w*)\b", chunk[m_jvar.end() :]):
                pk = m_prop.group(1)
                line_end_pos = chunk.find("\n", m_jvar.end() + m_prop.start())
                line_start_pos = chunk.rfind("\n", 0, m_jvar.end() + m_prop.start()) + 1
                prop_line_txt = chunk[line_start_pos : (line_end_pos if line_end_pos != -1 else len(chunk))]
                if "||" in prop_line_txt or "??" in prop_line_txt:
                    continue
                if pk not in _JS_RES_BUILTINS and pk not in expected_res_keys:
                    expected_res_keys.append(pk)

        http_calls: list[dict] = []
        for m_fe in re.finditer(
            r"\bfetch\s*\(\s*[`'\"](\/[a-zA-Z0-9_/\-${}:.]+)[`'\"](?:\s*,\s*\{([\s\S]{0,260}?)\})?",
            chunk,
        ):
            raw_u = m_fe.group(1).split("?")[0]
            opts_u = m_fe.group(2) or ""
            m_verb = re.search(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]", opts_u)
            verb_u = m_verb.group(1).upper() if m_verb else "GET"
            norm_u = re.sub(r"\$\{[^}]+\}|:[a-zA-Z_]\w*", "{*}", raw_u).rstrip("/") or "/"
            fe_line = s + chunk.count("\n", 0, m_fe.start())
            hc_entry: dict = {"method": verb_u, "url": raw_u, "norm_url": norm_u, "line": fe_line}
            if expected_res_keys:
                hc_entry["expected_keys"] = expected_res_keys[:8]
            http_calls.append(hc_entry)
        for m_ax in re.finditer(
            r"\b(?:axios|api|client|http)\.(get|post|put|delete|patch)\s*\(\s*[`'\"](\/[a-zA-Z0-9_/\-${}:.]+)[`'\"]",
            chunk,
            re.IGNORECASE,
        ):
            verb_u = m_ax.group(1).upper()
            raw_u = m_ax.group(2).split("?")[0]
            norm_u = re.sub(r"\$\{[^}]+\}|:[a-zA-Z_]\w*", "{*}", raw_u).rstrip("/") or "/"
            ax_line = s + chunk.count("\n", 0, m_ax.start())
            http_calls.append({"method": verb_u, "url": raw_u, "norm_url": norm_u, "line": ax_line})
        # Disjoint runtime type comparisons (e.g. Python UUID parameter compared directly to SQLite TEXT row["col"])
        type_comparisons: list[dict] = []
        if scope.startswith("fn:"):
            sig_head = "\n".join(lines[s - 1 : min(e, s + 8)])
            uuid_params = set(re.findall(r"\b([a-zA-Z_]\w*)\s*:\s*(?:uuid\.)?UUID\b", sig_head))
            if uuid_params:
                for off_idx, raw_ln in enumerate(lines[s - 1 : e]):
                    if "str(" in raw_ln or "UUID(" in raw_ln:
                        continue
                    for up in uuid_params:
                        m_cmp = re.search(
                            rf"\b\w+\[\s*['\"]([a-zA-Z_]\w*)['\"]\s*\]\s*(?:==|!=)\s*{re.escape(up)}\b"
                            rf"|\b{re.escape(up)}\s*(?:==|!=)\s*\w+\[\s*['\"]([a-zA-Z_]\w*)['\"]\s*\]",
                            raw_ln,
                        )
                        if m_cmp:
                            col_k = m_cmp.group(1) or m_cmp.group(2)
                            type_comparisons.append({
                                "param": up,
                                "param_type": "UUID",
                                "row_key": col_k,
                                "line": s + off_idx,
                            })
        # Producer-Consumer SQL SELECT projection vs row["key"] read algebra
        sql_select_violations: list[dict] = []
        return_dict_keys: list[str] = []
        if scope.startswith("fn:"):
            for m_sel in re.finditer(
                r"\b([a-zA-Z_]\w*)\s*=\s*[\w.]+\.execute\(\s*(?:f?['\"]{1,3})\s*SELECT\s+([\s\S]+?)\s+FROM\s+([a-zA-Z_]\w*)\b[\s\S]*?\)\.fetchone\(\)",
                chunk,
                re.IGNORECASE,
            ):
                r_var = m_sel.group(1)
                raw_c_str = m_sel.group(2)
                sel_tbl = m_sel.group(3)
                if "*" in raw_c_str or "(" in raw_c_str:
                    continue
                proj_cols: list[str] = []
                for c_part in raw_c_str.split(","):
                    cp = c_part.strip()
                    m_as = re.search(r"\bAS\s+([a-zA-Z_]\w*)\s*$", cp, re.IGNORECASE)
                    if m_as:
                        proj_cols.append(m_as.group(1))
                    else:
                        base_c = cp.split("::")[0].strip().split(".")[-1].strip().strip('"`')
                        if re.match(r"^[a-zA-Z_]\w*$", base_c):
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

            if ext == ".py":
                ret_blocks = re.findall(r"\breturn\s*\{([^{};]+)\}", chunk)
                if ret_blocks and not any("**" in rb for rb in ret_blocks):
                    for rb in ret_blocks:
                        for mk in re.finditer(r"['\"]([a-zA-Z_]\w*)['\"]\s*:", rb):
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

    import math
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
            # Behavioral 6D axis fallback when query uses symptom/intent terms rather than exact identifier names
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

    table_lines = [
        "# [4D Coordinate Matrix] Lstart-Lend | Y_Role | X_Scope | ?Z_Write -> W_Calls"
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
