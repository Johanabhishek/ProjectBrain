"""Behavioral classification of functions using AST-extracted data.

Classifies every function by what it *does* — detectable from the structural
information already captured by parsers.py.  No LLM required.

Categories:
    validator    — returns boolean, has conditional throws/returns, named validate*/check*/is*
    transformer  — takes input, returns transformed output, no side-effect calls
    io_reader    — contains fetch/db.query/fs.read/request.get patterns
    io_writer    — contains db.insert/fs.write/response.send/request.post patterns
    orchestrator — calls 3+ other functions, coordinates flow
    handler      — connected to a route (decorator or registration)
    constructor  — initializes state (__init__ / constructor)
    renderer     — returns JSX/HTML (detectable from call patterns)
    unknown      — cannot determine behavior from available data
"""
from __future__ import annotations

import re

# ── Name patterns ───────────────────────────────────────────────────────────

_VALIDATOR_NAMES = re.compile(
    r"^(validate|check|is_|has_|verify|assert|ensure|can_|should_|test_|"
    r"is[A-Z]|has[A-Z]|check[A-Z]|validate[A-Z]|verify[A-Z]|ensure[A-Z]|"
    r"can[A-Z]|should[A-Z])",
)

_CONSTRUCTOR_NAMES = {"__init__", "constructor", "setup", "setUp", "init", "initialize"}

_RENDERER_NAMES = re.compile(
    r"^(render|display|show|draw|paint|view|template|layout|component)",
    re.IGNORECASE,
)

# ── Call patterns for IO classification ─────────────────────────────────────

_IO_READ_CALLS = {
    # Network reads
    "fetch", "get", "request", "axios", "http",
    # Database reads
    "query", "find", "findOne", "findMany", "findAll", "findById",
    "select", "aggregate", "count", "exists",
    "find_one", "find_many", "find_all", "find_by_id",
    # Filesystem reads
    "readFile", "readFileSync", "read", "readdir", "readlink",
    "read_text", "read_bytes", "read_file",
    # ORM reads
    "objects", "filter", "all", "first", "last",
}

_IO_READ_QUALIFIED = {
    "db.query", "db.find", "db.select", "db.get",
    "fs.read", "fs.readFile", "fs.readFileSync", "fs.readdir",
    "request.get", "http.get", "axios.get",
    "response.json",
}

_IO_WRITE_CALLS = {
    # Database writes
    "insert", "update", "delete", "save", "create", "destroy",
    "insertOne", "insertMany", "updateOne", "updateMany", "deleteOne", "deleteMany",
    "insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many",
    "commit", "execute", "run", "upsert", "bulkWrite",
    "bulk_create", "bulk_update",
    # Filesystem writes
    "writeFile", "writeFileSync", "write", "appendFile", "unlink", "mkdir",
    "write_text", "write_bytes", "write_file",
    # Network writes
    "post", "put", "patch", "send", "emit", "publish", "push",
    # Response writes
    "sendStatus", "redirect", "render",
}

_IO_WRITE_QUALIFIED = {
    "db.insert", "db.update", "db.delete", "db.save", "db.create",
    "fs.write", "fs.writeFile", "fs.writeFileSync", "fs.unlink", "fs.mkdir",
    "request.post", "request.put", "request.patch",
    "http.post", "http.put", "axios.post", "axios.put",
    "response.send", "response.json", "response.redirect", "res.send", "res.json",
}

_RENDERER_CALLS = {
    "createElement", "jsx", "React", "render",
    "createRoot", "hydrateRoot", "renderToString",
    "html", "template",
}

# Minimum number of distinct callees to qualify as an orchestrator
_ORCHESTRATOR_MIN_CALLS = 3


def classify_function(
    func: dict,
    *,
    route_handlers: set[str] | None = None,
    is_jsx_file: bool = False,
) -> str:
    """Classify a single function by its behavioral role.

    Args:
        func: A function dict from parsers.parse() with at minimum
              ``name``, ``calls``, and optionally ``qualified_calls``.
        route_handlers: Set of function names known to be route handlers
                        (from route extraction).
        is_jsx_file: Whether the source file is .tsx/.jsx (hints at renderers).

    Returns:
        One of: "validator", "transformer", "io_reader", "io_writer",
        "orchestrator", "handler", "constructor", "renderer", "unknown".
    """
    name = func.get("name", "")
    calls = set(func.get("calls", []))
    qualified = set(func.get("qualified_calls", {}).keys())
    all_calls = calls | {q.split(".")[-1] for q in qualified}

    # ── 1. Constructor (highest priority, most specific) ────────────────
    if name in _CONSTRUCTOR_NAMES:
        return "constructor"

    # ── 2. Handler (structurally determined by route registration) ──────
    if route_handlers and name in route_handlers:
        return "handler"

    # ── 3. Validator (name-based + behavioral signal) ───────────────────
    if _VALIDATOR_NAMES.match(name):
        return "validator"

    # ── 4. IO classification (call patterns) ────────────────────────────
    has_write = bool(
        (all_calls & _IO_WRITE_CALLS) or (qualified & _IO_WRITE_QUALIFIED)
    )
    has_read = bool(
        (all_calls & _IO_READ_CALLS) or (qualified & _IO_READ_QUALIFIED)
    )

    if has_write:
        return "io_writer"
    if has_read:
        return "io_reader"

    # ── 5. Renderer (JSX/HTML signals) ──────────────────────────────────
    if is_jsx_file and (_RENDERER_NAMES.match(name) or (all_calls & _RENDERER_CALLS)):
        return "renderer"
    if all_calls & _RENDERER_CALLS:
        return "renderer"

    # ── 6. Orchestrator (calls many things) ─────────────────────────────
    if len(calls) >= _ORCHESTRATOR_MIN_CALLS:
        return "orchestrator"

    # ── 7. Transformer (has calls but no IO / low fan-out) ──────────────
    if calls and not has_write and not has_read:
        return "transformer"

    # ── 8. Unknown ──────────────────────────────────────────────────────
    return "unknown"


def classify_file_functions(
    info: dict,
    *,
    is_jsx: bool = False,
) -> dict[str, str]:
    """Classify all functions in a parsed file.

    Args:
        info: A file's parsed info dict (from brain._index[rel_path]).
        is_jsx: Whether the file is a .tsx/.jsx file.

    Returns:
        dict mapping function name → behavior label.
    """
    # Build the set of known route handler names for this file
    route_handlers = {r.get("handler", "") for r in info.get("routes", [])}

    result: dict[str, str] = {}
    for func in info.get("functions", []):
        qualified_name = func["name"]
        if func.get("parent_class"):
            qualified_name = f"{func['parent_class']}.{func['name']}"
        result[qualified_name] = classify_function(
            func, route_handlers=route_handlers, is_jsx_file=is_jsx,
        )
    return result
