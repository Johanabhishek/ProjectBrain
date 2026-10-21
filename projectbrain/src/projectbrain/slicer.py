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

_IMPORTANT_CODE_PAT = re.compile(
    r"\b(setInterval|setTimeout|clearTimeout|clearInterval|addEventListener|fetch|Promise\.race|AbortController|AbortSignal|"
    r"catch|throw|finally|document\.cookie|localStorage|sessionStorage|"
    r"[a-zA-Z0-9_$]+(Cooldowns|Storage|Kv|Ref\.current)\.(get|set|has|add|delete)|"
    r"[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b"
)
_UPPER_CONST_PAT = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_CODE_STMT_PAT = re.compile(
    r"^\s*(const\s|let\s|var\s|if\s*\(|else\b|for\s*\(|while\s*\(|try\s*\{|catch\s*\(|return\b|await\s|throw\s|"
    r"[a-zA-Z0-9_$.]+\.(set|get|delete|clear|test|some|race|fetch|json|addEventListener)\()"
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
        is_code_like = is_code_stmt or any(ch in stripped for ch in ("=", "{", "}", ";"))
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
        if _JSX_NOISE_PAT.match(line) and sc <= 1.0:
            sc = -1.0
        line_scores[i] = sc

    # Smooth scores over a +/- 2 line window (weighting the center line highest so isolated statements aren't penalized by surrounding comments/blank lines)
    window_scores: list[tuple[float, int]] = []
    for i in range(head_n, total_len - tail_n):
        if line_scores[i] > 1.0:
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

    # If no focus terms matched inside the function, fall back to keeping the first (max_lines - tail_n) lines
    if kept_count < max_lines // 2:
        for i in range(min(total_len, max_lines - tail_n)):
            if not keep[i]:
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
        for ln in range(max(1, s), min(n, e) + 1):
            covered[ln] = True

    kept_lines: list[tuple[int, str]] = []
    in_block_comment = False
    in_import = False
    in_type_block = False
    type_brace_depth = 0
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

        # Skip pure TypeScript interface / type blocks
        if in_type_block:
            type_brace_depth += stripped.count("{") - stripped.count("}")
            if type_brace_depth <= 0 and (stripped.endswith("}") or stripped.endswith("};") or ";" in stripped):
                in_type_block = False
            continue
        if re.match(r"^(export\s+)?(interface|type)\s+\w+", stripped):
            if "{" in stripped and "}" not in stripped:
                in_type_block = True
                type_brace_depth = stripped.count("{") - stripped.count("}")
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


def build_file_skeleton(rel_path: str, info: dict, behaviors: dict[str, str] | None = None, focus_terms: list[str] | None = None) -> str:
    """Build a compact, token-efficient structural skeleton of a file.

    Includes:
      - Routes (METHOD /path -> handler)
      - Classes with base classes, schema fields, and method signatures
      - Top-level function signatures with types, behavioral role, and 1-line docstrings
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

    for fn in top_fns[:25]:
        fsig = fn.get("signature") or f"def {fn['name']}()"
        flines = f"L{fn.get('line_start', '?')}-{fn.get('line_end', '?')}"
        beh = behaviors.get(fn["name"], "")
        beh_tag = f" [{beh}]" if beh and beh != "unknown" else ""
        doc_tag = ""
        if fn.get("docstring"):
            doc_first = fn["docstring"].splitlines()[0][:80]
            doc_tag = f' — "{doc_first}"'
        out.append(f"{fsig}: ...  # {flines}{beh_tag}{doc_tag}")

    return "\n".join(out)
