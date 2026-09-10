"""File-based route inference for frameworks that route by directory
convention rather than by decorator or call (Next.js App/Pages Router).

Kept separate from parsers.py because this needs the file's rel_path, not
just its source text — parsers.parse(source, ext) has no path to work with.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

_APP_ROUTER_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
_APP_ROUTER_FILENAMES = {"route.ts", "route.js", "route.tsx", "route.jsx"}
_PAGES_ROUTER_EXCLUDED_STEMS = {"_app", "_document", "_middleware"}

_CATCH_ALL = re.compile(r"\[\.\.\.(\w+)\]")
_PARAM = re.compile(r"\[(\w+)\]")


def _convert_segments(parts: list[str]) -> str:
    converted = []
    for part in parts:
        catch_all = _CATCH_ALL.fullmatch(part)
        param = _PARAM.fullmatch(part)
        if catch_all:
            converted.append("*")
        elif param:
            converted.append(f":{param.group(1)}")
        else:
            converted.append(part)
    return "/" + "/".join(converted)


def infer_file_based_routes(rel_path: str, info: dict) -> list[dict]:
    parts = PurePosixPath(rel_path).parts
    filename = PurePosixPath(rel_path).name
    stem = PurePosixPath(rel_path).stem

    if "app" in parts and filename in _APP_ROUTER_FILENAMES:
        app_idx = parts.index("app")
        path = _convert_segments(list(parts[app_idx + 1:-1]))
        exports = set(info.get("exports", []))
        return [
            {"method": verb, "path": path, "handler": verb, "framework": "nextjs-app-router"}
            for verb in _APP_ROUTER_VERBS
            if verb in exports
        ]

    if "pages" in parts and "api" in parts and stem not in _PAGES_ROUTER_EXCLUDED_STEMS:
        pages_idx = parts.index("pages")
        path = _convert_segments(list(parts[pages_idx + 1:-1]) + [stem])
        return [{"method": "ANY", "path": path, "handler": None, "framework": "nextjs-pages-router"}]

    return []
