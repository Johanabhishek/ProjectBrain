"""Resolve raw import strings captured by parsers.py to actual indexed files.

Pure functions only — no filesystem access, no dependency on brain.py/graph.py,
so every case here is directly unit-testable against a fabricated file set.
Honesty over guessing: an ambiguous or unresolvable import returns None rather
than a best-effort wrong answer.
"""
from __future__ import annotations

from pathlib import PurePosixPath

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


# ── Python ───────────────────────────────────────────────────────────────────

def _py_module_name(rel_path: str) -> str:
    """Dotted module path for a .py file, respecting __init__.py package semantics."""
    p = PurePosixPath(rel_path)
    if p.name == "__init__.py":
        parts = p.parent.parts
    else:
        parts = p.parent.parts + (p.stem,)
    return ".".join(parts)


def build_python_module_index(index: dict) -> dict[str, list[str]]:
    """Map every dotted-suffix of each .py file's module path back to its rel_path.

    e.g. "src/services/users.py" registers under "src.services.users",
    "services.users", and "users" — so both `import src.services.users` and a
    src-layout-relative `import services.users` can resolve to the same file.
    A suffix shared by multiple files is kept as an ambiguous multi-candidate
    list rather than picked arbitrarily.
    """
    module_index: dict[str, list[str]] = {}
    for rel_path in index:
        if not rel_path.endswith(".py"):
            continue
        module = _py_module_name(rel_path)
        segments = module.split(".") if module else []
        for start in range(len(segments)):
            suffix = ".".join(segments[start:])
            if not suffix:
                continue
            module_index.setdefault(suffix, [])
            if rel_path not in module_index[suffix]:
                module_index[suffix].append(rel_path)
    return module_index


def _dir_str(p: PurePosixPath) -> str:
    """str(p), but "." (pathlib's root-relative marker) becomes "" so joins
    below don't leak a literal "./" prefix that would never match file_set."""
    return "" if str(p) == "." else str(p)


def resolve_python_import(
    importer_rel_path: str,
    imp: str,
    file_set: set[str],
    module_index: dict[str, list[str]],
) -> str | None:
    if imp.startswith("."):
        dot_count = len(imp) - len(imp.lstrip("."))
        remainder = imp[dot_count:]

        target_dir = PurePosixPath(importer_rel_path).parent
        for _ in range(dot_count - 1):
            target_dir = target_dir.parent
        dir_str = _dir_str(target_dir)

        if remainder:
            module_rel = remainder.replace(".", "/")
            module_path = f"{dir_str}/{module_rel}" if dir_str else module_rel
            candidates = [f"{module_path}.py", f"{module_path}/__init__.py"]
        else:
            candidates = [f"{dir_str}/__init__.py" if dir_str else "__init__.py"]

        for candidate in candidates:
            if candidate in file_set:
                return candidate
        return None

    matches = module_index.get(imp, [])
    if len(matches) == 1:
        return matches[0]
    return None


# ── JavaScript / TypeScript ─────────────────────────────────────────────────

def _normalize_posix(path: PurePosixPath) -> str:
    """Collapse '.' and '..' segments via pure string arithmetic (no disk access)."""
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(part)
        elif part == ".":
            continue
        else:
            parts.append(part)
    return "/".join(parts)


def resolve_js_import(importer_rel_path: str, imp: str, file_set: set[str]) -> str | None:
    """Only resolves relative (./ or ../) imports — bare specifiers are npm
    packages and correctly stay unresolved/external."""
    if not (imp.startswith("./") or imp.startswith("../")):
        return None

    importer_dir = PurePosixPath(importer_rel_path).parent
    target = _normalize_posix(importer_dir / imp)

    if PurePosixPath(target).suffix and target in file_set:
        return target

    for ext in _JS_EXTS:
        candidate = f"{target}{ext}"
        if candidate in file_set:
            return candidate

    for ext in _JS_EXTS:
        candidate = f"{target}/index{ext}"
        if candidate in file_set:
            return candidate

    return None


# ── Dispatch ─────────────────────────────────────────────────────────────────

def resolve_import(
    importer_rel_path: str,
    imp: str,
    file_set: set[str],
    module_index: dict[str, list[str]],
) -> str | None:
    if importer_rel_path.endswith(".py"):
        return resolve_python_import(importer_rel_path, imp, file_set, module_index)
    return resolve_js_import(importer_rel_path, imp, file_set)
