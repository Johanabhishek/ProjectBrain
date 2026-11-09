"""Resolve raw import strings and symbol bindings captured by parsers.py to actual indexed files.

Pure functions only — no filesystem access, no dependency on brain.py/graph.py,
so every case here is directly unit-testable against a fabricated file set.
Honesty over guessing: an ambiguous or unresolvable import returns None rather
than a best-effort wrong answer.
"""
from __future__ import annotations

from pathlib import PurePosixPath

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_ALIAS_PREFIXES = ("@/", "~/", "#/")
_ALIAS_ROOTS = ("src", "app", "")


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


def _try_js_target(target: str, file_set: set[str]) -> str | None:
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


_WORKSPACE_CONTAINERS = ("packages", "libs", "apps", "modules", "services")


def _importer_package_roots(importer_rel_path: str) -> list[str]:
    """If `importer_rel_path` lives inside a monorepo package (e.g. `apps/web/src/pages/Home.tsx`),
    return candidate package-local alias roots (`apps/web/src`, `apps/web/app`, `apps/web`)."""
    parts = PurePosixPath(importer_rel_path).parts
    roots: list[str] = []
    if len(parts) >= 3 and parts[0] in _WORKSPACE_CONTAINERS:
        pkg_base = f"{parts[0]}/{parts[1]}"
        for sub in ("src", "app", ""):
            cand = f"{pkg_base}/{sub}" if sub else pkg_base
            if cand not in roots:
                roots.append(cand)
    elif "src" in parts:
        idx = parts.index("src")
        if idx > 0:
            pkg_base = "/".join(parts[:idx])
            for sub in ("src", "app", ""):
                cand = f"{pkg_base}/{sub}" if sub else pkg_base
                if cand not in roots:
                    roots.append(cand)
    return roots


def resolve_js_import(importer_rel_path: str, imp: str, file_set: set[str]) -> str | None:
    """Resolves relative (./ or ../), path aliases (@/, ~/, #/), and monorepo workspace imports (@scope/pkg)."""
    if imp.startswith("./") or imp.startswith("../"):
        importer_dir = PurePosixPath(importer_rel_path).parent
        target = _normalize_posix(importer_dir / imp)
        return _try_js_target(target, file_set)

    for prefix in _ALIAS_PREFIXES:
        if imp.startswith(prefix):
            rest = imp[len(prefix):]
            # 1. Try package-local alias roots first in monorepos
            pkg_matches: list[str] = []
            for pkg_root in _importer_package_roots(importer_rel_path):
                target = _normalize_posix(PurePosixPath(pkg_root) / rest)
                hit = _try_js_target(target, file_set)
                if hit and hit not in pkg_matches:
                    pkg_matches.append(hit)
            if len(pkg_matches) == 1:
                return pkg_matches[0]

            # 2. Fall back to repo-level alias roots (`src/`, `app/`, ``)
            matches: list[str] = []
            for root_dir in _ALIAS_ROOTS:
                target = _normalize_posix(PurePosixPath(root_dir) / rest) if root_dir else _normalize_posix(PurePosixPath(rest))
                hit = _try_js_target(target, file_set)
                if hit and hit not in matches:
                    matches.append(hit)
            if len(matches) == 1:
                return matches[0]
            return None

    # 3. Monorepo scoped workspace package imports (`@scope/pkg` or `@scope/pkg/subpath`)
    if imp.startswith("@") and "/" in imp and not imp.startswith(_ALIAS_PREFIXES):
        parts = imp.split("/")
        if len(parts) >= 2 and parts[1]:
            pkg_name = parts[1]
            subpath = "/".join(parts[2:]) if len(parts) > 2 else ""
            ws_matches: list[str] = []
            for container in _WORKSPACE_CONTAINERS:
                for base_sub in ("src", "lib", ""):
                    base = f"{container}/{pkg_name}/{base_sub}" if base_sub else f"{container}/{pkg_name}"
                    target = f"{base}/{subpath}" if subpath else base
                    hit = _try_js_target(_normalize_posix(PurePosixPath(target)), file_set)
                    if hit and hit not in ws_matches:
                        ws_matches.append(hit)
            if len(ws_matches) == 1:
                return ws_matches[0]

    return None


def unwrap_barrel_reexport(
    barrel_rel: str,
    symbol: str,
    index: dict[str, dict],
    file_set: set[str],
    module_index: dict[str, list[str]],
    max_hops: int = 3,
) -> str:
    """Follow named and wildcard (`export * from` / `from .x import *`) barrel re-exports up to `max_hops`."""
    cur_rel = barrel_rel
    visited: set[str] = {cur_rel}
    for _ in range(max_hops):
        info = index.get(cur_rel)
        if not info or not symbol or symbol in ("*", "default"):
            break
        defined_here = (
            {f["name"] for f in info.get("functions", []) if not f.get("parent_class")}
            | {c["name"] for c in info.get("classes", [])}
        )
        if symbol in defined_here:
            return cur_rel

        bindings = info.get("import_bindings") or {}
        # 1. Explicit named re-export or import binding in barrel (`export { a } from './sub'` or `from .sub import a`)
        if symbol in bindings:
            bind = bindings[symbol]
            orig_sym = bind.get("symbol") or symbol
            mod = bind.get("module", "")
            qual_mod = bind.get("qualified_module", "")
            nxt = None
            if qual_mod and cur_rel.endswith(".py"):
                nxt = resolve_python_import(cur_rel, qual_mod, file_set, module_index)
            if not nxt and mod:
                nxt = resolve_import(cur_rel, mod, file_set, module_index)
            if nxt and nxt not in visited:
                visited.add(nxt)
                cur_rel = nxt
                symbol = orig_sym if orig_sym != "*" else symbol
                continue

        # 2. Wildcard barrel re-export (`export * from './sub'` or `from .sub import *`)
        star_hit = None
        for b_key, bind in bindings.items():
            if not b_key.startswith("__star__") and bind.get("symbol") != "*":
                continue
            mod = bind.get("module", "")
            if not mod:
                continue
            cand_rel = resolve_import(cur_rel, mod, file_set, module_index)
            if not cand_rel or cand_rel in visited:
                continue
            cand_info = index.get(cand_rel, {})
            cand_defs = (
                {f["name"] for f in cand_info.get("functions", []) if not f.get("parent_class")}
                | {c["name"] for c in cand_info.get("classes", [])}
                | set(cand_info.get("exports", []))
            )
            if symbol in cand_defs:
                star_hit = cand_rel
                break
        if star_hit:
            visited.add(star_hit)
            cur_rel = star_hit
            continue
        break
    return cur_rel


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


def resolve_file_imports_and_symbols(
    importer_rel_path: str,
    info: dict,
    file_set: set[str],
    module_index: dict[str, list[str]],
    index: dict[str, dict] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Resolve both file-level import targets AND symbol-level bindings for a file.

    Returns:
      (targets, symbol_to_file)
      where `targets` is the deduplicated list of resolved `rel_path`s this file imports,
      and `symbol_to_file` maps each local imported symbol/alias to its resolved `rel_path`
      (unwrapping barrel re-exports when `index` is provided).
    """
    targets: list[str] = []
    symbol_to_file: dict[str, str] = {}

    for imp in info.get("imports", []):
        t = resolve_import(importer_rel_path, imp, file_set, module_index)
        if t and t != importer_rel_path and t not in targets:
            targets.append(t)

    bindings = info.get("import_bindings", {})
    for local_name, bind in bindings.items():
        if local_name.startswith("__star__"):
            continue
        mod = bind.get("module", "")
        qual_mod = bind.get("qualified_module", "")
        resolved = None
        # First try qualified_module (e.g. `from services import discount` -> `services.discount`)
        if qual_mod and importer_rel_path.endswith(".py"):
            resolved = resolve_python_import(importer_rel_path, qual_mod, file_set, module_index)
        if not resolved and mod:
            resolved = resolve_import(importer_rel_path, mod, file_set, module_index)
        if resolved and resolved != importer_rel_path:
            if resolved not in targets:
                targets.append(resolved)
            if index is not None:
                orig_sym = bind.get("symbol") or local_name
                unwrapped = unwrap_barrel_reexport(resolved, orig_sym, index, file_set, module_index)
                if unwrapped and unwrapped != importer_rel_path:
                    resolved = unwrapped
                    if unwrapped not in targets:
                        targets.append(unwrapped)
            symbol_to_file[local_name] = resolved

    return targets, symbol_to_file
