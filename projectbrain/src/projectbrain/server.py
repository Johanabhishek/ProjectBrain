import os
import sys

from mcp.server.fastmcp import FastMCP

from .brain import ProjectBrain


def _resolve_root() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("PROJECTBRAIN_ROOT", os.getcwd())


mcp = FastMCP("projectbrain")
_brain = ProjectBrain(_resolve_root())


@mcp.tool()
def projectbrain_query(prompt: str, top_n: int = 5) -> dict:
    """Find which files are relevant to a coding task and return a typed symbol graph.

    Call this when you don't already know which file to touch — open-ended
    tasks like "what handles audio progress?", "where does X live?", bug
    reports ("404 on /users"), or feature requests ("add a discount code
    field to checkout") in a repo you haven't recently been working in.

    Skip it when the target is already obvious from the conversation (e.g.
    you've been editing a file in this session, the user named the file, or
    the task is a trivial localised change like a CSS tweak). Calling it in
    those cases just adds a round-trip delay with no new information.
    Skip it also when you already have a node ID — use projectbrain_get_neighbors.

    Returns a dict with three keys:
      "results"     — ranked file list, each with reason, functions, classes,
                      routes, imports, exports, connections.
      "subgraph"    — {"nodes": [...], "edges": [...]} — typed symbol graph
                      spanning all returned files. Edges carry src, dst, type
                      (defines / imports / calls / inherits / handles /
                      has_method / exports), confidence, and reason. Confidence
                      is "exact" (structural fact or same-file resolution) or
                      "candidate" (name-only match, ambiguous target — treat as
                      a hint, not a certainty). Read this to understand how the
                      files connect without extra round trips.
      "entrypoints" — file node IDs in the result set that nothing else in the
                      set imports. Good starting points for reading or editing.

    Node ID format (use with other graph tools):
      file:<rel_path>            e.g. file:src/services/chat.py
      fn:<rel_path>:<name>       e.g. fn:src/services/chat.py:handle_chat
      cls:<rel_path>:<name>      e.g. cls:src/services/chat.py:ChatService
      route:<METHOD>:<path>      e.g. route:POST:/api/chat
      mod:<import_name>          e.g. mod:fastapi
    """
    return _brain.query_with_graph(prompt, top_n=top_n)


@mcp.tool()
def projectbrain_get_context(file_path: str) -> dict:
    """Get one file's functions/classes/routes/imports plus its 1-hop dependency chain.

    Use this once you already know which file you care about (e.g. from
    projectbrain_query, or because the user named it) and need to see what
    it connects to — what it imports, and what other files import it —
    without opening each of those files individually to check.

    For deeper symbol-level traversal (e.g. "what does this class call?"),
    use projectbrain_get_neighbors with the node ID from projectbrain_query.
    """
    return _brain.get_context_for_file(file_path)


@mcp.tool()
def projectbrain_get_neighbors(
    node_id: str,
    edge_types: list[str] | None = None,
    depth: int = 1,
) -> dict:
    """Traverse the symbol graph outward from a known node ID.

    Use this after projectbrain_query has given you a node ID and you want to
    explore its graph neighborhood without opening individual files.

    Typical patterns:
      "what calls ChatService?"      → node_id="cls:.../ChatService"
      "what does parse() depend on?" → node_id="fn:.../parse", edge_types=["calls"]
      "what methods does X have?"    → node_id="cls:.../X", edge_types=["has_method"]
      "what imports this module?"    → node_id="mod:fastapi"

    Parameters:
      node_id    — A node ID from a prior graph result.
                   Format: "fn:rel/path.py:func_name", "cls:rel/path.py:ClassName",
                   "file:rel/path.py", "route:POST:/path", "mod:module_name"
      edge_types — Optional list restricting edge types to follow.
                   Values: "defines", "imports", "calls", "inherits",
                   "handles", "has_method", "exports".
                   Reverse edges appear with "inverse_" prefix in results.
      depth      — Hops to traverse (1–3, default 1). Use sparingly above 1.

    Returns {"center": {...}, "nodes": [...], "edges": [...]}.
    Do NOT use this for file discovery — use projectbrain_query instead.
    """
    return _brain.get_neighbors(node_id, edge_types=edge_types, depth=depth)


@mcp.tool()
def projectbrain_find_relationship(name_a: str, name_b: str) -> dict:
    """Find the shortest typed path between any two named symbols or files.

    Use this to understand HOW two parts of the codebase connect:
      "how does AuthMiddleware reach UserService?"
      "what's the chain from POST /api/chat to the database?"
      "how does handle_payment relate to StripeClient?"

    Parameters:
      name_a — Name of the first symbol: function name, class name, file path
               fragment, module name, or route path. Case-insensitive, partial.
      name_b — Name of the second symbol, same format.

    Returns:
      Success:   {"path": [edges], "from_node": {...}, "to_node": {...}}
                 Each edge: {"src": node_id, "dst": node_id, "type": edge_type}
      Not found: {"path": [], "candidates_a": [...], "candidates_b": [...], "message": "..."}
                 Inspect candidates and retry with a more specific name, or use
                 projectbrain_list_nodes to find the exact node ID.

    Do NOT use this for file discovery. Do NOT use it when you already have
    both node IDs — call projectbrain_get_neighbors on one of them instead.
    """
    return _brain.find_relationship(name_a, name_b)


@mcp.tool()
def projectbrain_list_nodes(
    node_type: str | None = None,
    query: str | None = None,
) -> dict:
    """Enumerate and filter nodes in the symbol graph for discovery.

    Use this to browse available symbols without a specific task in mind:
      "list all routes"
      "show every class"
      "find nodes named 'auth'"
      "what external packages does this project import?"

    Parameters:
      node_type — Optional filter: "file", "fn", "cls", "route", "mod".
      query     — Optional case-insensitive substring matched against node ID
                  and name fields.

    Returns {"nodes": [...], "total": int}. Capped at 100 results.
    If total > 100, narrow with query or node_type.

    Do NOT use this to find files for a coding task — use projectbrain_query.
    """
    return _brain.list_nodes(node_type=node_type, query=query)


@mcp.tool()
def projectbrain_verify_claim(subject: str, relation: str, object: str) -> dict:
    """Fact-check a structural claim about the code against the symbol graph.

    Call this to ground a statement BEFORE asserting it — the anti-hallucination
    tool. Instead of confidently saying "checkout calls validate_coupon" (and
    risking a fabrication), verify it and report what the code actually supports.

    Returns {"verdict", "confidence", "explanation", "evidence": [...]} where
    verdict is:
      SUPPORTED — a structural or import-resolved fact backs the claim (evidence
                  cites the node, file, line, and confidence).
      REFUTED   — the claim is provably false: the subject is unambiguous, its
                  file parsed cleanly, and the object is absent from the complete
                  captured set. Only returned when that closed world is complete.
      UNKNOWN   — cannot confirm OR refute, with a specific reason: symbol not
                  indexed, file had parse errors, unsupported language (e.g. Go),
                  ambiguous name, or an ambiguous/external target. Treat UNKNOWN
                  as "don't assert it", NOT as "false".

    Parameters:
      subject  — a function, class, file path, or route (e.g. "checkout",
                 "UserService", "routes/checkout.py", "POST /checkout").
      relation — one of:
                   calls      subject(fn) directly calls object(fn)
                   reaches    subject transitively reaches object via call flow
                   inherits   subject(cls) directly extends object(cls)
                   has_method subject(cls) defines method object
                   defines    subject(file) defines function/class object
                   imports    subject(file) imports module object
                   handles    subject(route) is handled by function object
      object   — the name the relation targets.

    Note: `calls` is a DIRECT call. For "A eventually reaches B" use `reaches`.
    A direct-call claim across two hops is correctly REFUTED — that precision is
    the point.
    """
    return _brain.verify_claim(subject, relation, object)


@mcp.tool()
def brain_verify_batch(claims: list[list[str]]) -> dict:
    """Verify multiple structural claims in a single call.

    Batch version of projectbrain_verify_claim. Each claim is a list of
    [subject, relation, object].

    Example:
      brain_verify_batch([
          ["auth.ts", "defines", "getUserProfile"],
          ["CheckoutService", "calls", "validateCoupon"],
          ["User", "reaches", "CheckoutService"]
      ])

    Returns all verdicts plus a summary count of SUPPORTED/REFUTED/UNKNOWN.
    """
    tuples = [(c[0], c[1], c[2]) for c in claims if len(c) >= 3]
    return _brain.verify_batch(tuples)


@mcp.tool()
def brain_verify_plan(
    task: str,
    files_to_edit: list[str],
    files_to_create: list[str] | None = None,
) -> dict:
    """Validate an agent's plan BEFORE it starts editing files.

    Submit your plan and Brain will check whether you're covering all the
    files that need changes by tracing graph edges from each planned file.

    Brain surfaces:
      - Missing files: connected files not in your plan that may need changes
      - Test files: test files that import your planned edits
      - Risk assessment: per-file risk based on how many things depend on it

    Verdicts:
      COMPLETE         — plan covers all structurally connected files
      INCOMPLETE       — plan is missing connected files
      NEEDS_TEST_UPDATES — plan is complete but tests may need updating

    Parameters:
      task            — description of the task (used for smart matching)
      files_to_edit   — list of file paths you plan to modify
      files_to_create — optional list of new files you'll create
    """
    return _brain.verify_plan(task, files_to_edit, files_to_create)


@mcp.tool()
def brain_impact(files_changed: list[str]) -> dict:
    """Analyze downstream impact of changing a set of files.

    For each changed file, Brain finds:
      - Direct consumers: files that import the changed file
      - Callers: functions in other files that call functions in it
      - Risk level: low/medium/high/critical based on consumer count
      - Behavioral summary: what roles functions in this file play
      - Route exposure: any API routes defined in the file

    Returns overall risk, total impact zone, and per-file details.
    Use this AFTER making changes to understand the blast radius.
    """
    return _brain.impact(files_changed)


@mcp.tool()
def projectbrain_health() -> dict:
    """Report what the index does and does NOT actually know — its blind spots.

    Use this to calibrate trust before relying on other tools, or when a
    verify_claim comes back UNKNOWN and you want to know why. Returns per-language
    file counts and a parse_status breakdown:
      ok          — parsed cleanly; symbols are authoritative.
      partial     — tree-sitter hit errors (broken/edge-case syntax); extraction
                    is incomplete, so absence proves nothing.
      unsupported — walked but no grammar (e.g. .go/.rs/.java); zero symbols means
                    "not looked at", not "nothing there".
    Plus `skipped` files (unreadable/minified) with reasons. A REFUTED verdict is
    only trustworthy for files reported here as "ok".
    """
    return _brain.health()


# ── Session Tools (Iterative Routing) ────────────────────────────────────────

@mcp.tool()
def brain_start_session(task: str) -> dict:
    """Start a guided exploration session for a coding task.

    This is Brain's core feature: instead of dumping 40 files at once,
    Brain routes you through the codebase step by step — like a human brain
    navigates through a problem.

    Workflow:
      1. Call brain_start_session(task="add discount codes to checkout")
      2. Brain finds entry points and returns the first files to read
      3. Read the first suggestion, then call brain_mark_read(session_id, path)
      4. Brain expands the frontier — surfaces the NEXT file you need
      5. Repeat until Brain says "understanding complete"

    Returns:
      session_id       — use in subsequent calls
      initial_files    — the entry points Brain found for your task
      next_suggestions — ranked list of files to read first
      message          — human-readable guidance

    Use brain_next to pop files from the frontier.
    Use brain_status to see progress at any time.
    """
    return _brain.start_session(task)


@mcp.tool()
def brain_mark_read(session_id: str, file_path: str) -> dict:
    """Tell Brain you've read a file — it will route you to the next one.

    After reading a file, call this to:
      1. Update the session's "read" set
      2. Expand the frontier: Brain follows imports/calls from the read file
         to find connected files you haven't seen yet
      3. Get updated suggestions ranked by relevance to your task

    Returns the next suggestions and a completeness estimate.
    """
    return _brain.session_mark_read(session_id, file_path)


@mcp.tool()
def brain_mark_modified(session_id: str, file_path: str, summary: str = "") -> dict:
    """Tell Brain you've modified a file — it surfaces downstream impact.

    Call this after editing a file. Brain will:
      1. Track the modification
      2. Find all files that IMPORT the modified file (impact zone)
      3. Add unread impacted files to the frontier with high priority
      4. Return the list of potentially affected files

    Parameters:
      session_id — from brain_start_session
      file_path  — the file you modified
      summary    — optional short description of what you changed
    """
    return _brain.session_mark_modified(session_id, file_path, summary)


@mcp.tool()
def brain_next(session_id: str, count: int = 1) -> dict:
    """Pop the next file(s) from the exploration frontier.

    Unlike brain_mark_read (which also returns suggestions), this consumes
    items from the frontier queue. Use when you want to advance without
    marking something as read.

    Returns:
      files              — list of {rel_path, priority, reason, via, graph_distance}
      frontier_remaining — how many files are left
      completeness       — percent estimate and status message
    """
    return _brain.session_next(session_id, count)


@mcp.tool()
def brain_compress(
    file_paths: list[str],
    max_tokens: int = 4000,
    focus: str = "",
) -> dict:
    """Get a compressed structural summary of files — saves tokens.

    Instead of reading entire file contents, get function signatures,
    behavioral roles, class hierarchies, routes, imports/exports, and
    connections in a compact format.  Use this when you need to understand
    what a set of files DO without reading every line.

    Parameters:
      file_paths — list of file paths to compress (absolute or relative)
      max_tokens — approximate token budget (default 4000)
      focus      — optional keyword to prioritize matching functions

    Returns compressed summaries with estimated token usage.
    """
    return _brain.compress_context(file_paths, max_tokens=max_tokens, focus=focus)


@mcp.tool()
def brain_status(session_id: str) -> dict:
    """Check session progress: what's been read, what's left, estimated completeness.

    Returns:
      read_files       — files the agent has consumed
      modified_files   — files changed (with summaries)
      frontier_size    — unread files in the exploration queue
      next_suggestions — top 5 next files to read
      completeness     — percent complete with status message
      initial_files    — the original entry points
    """
    return _brain.session_status(session_id)


# ── Cross-Boundary Intelligence ──────────────────────────────────────────────

@mcp.tool()
def brain_trace_flow(url_path: str, method: str = "GET") -> dict:
    """Trace a request flow across the frontend→backend boundary.

    Given a URL and HTTP method, Brain finds the complete chain:
      1. UI triggers — frontend code calling fetch/axios to this URL
      2. Route handler — backend function registered for this endpoint
      3. Service chain — what the handler calls (validators, services, data access)

    Each step includes file, line number, behavioral role, and confidence.

    Example:
      brain_trace_flow("/api/checkout", method="POST")
      → UI trigger (CheckoutPage.tsx:42) → route (checkout.py:12) →
        service (checkout_service.py:30) → data (db.py:15)

    Parameters:
      url_path — the URL path to trace (e.g., "/api/checkout")
      method   — HTTP method (default: GET)
    """
    return _brain.trace_flow(url_path, method=method)


@mcp.tool()
def brain_co_changes(file_path: str, days: int = 90) -> dict:
    """Find files that historically change together with the given file.

    Uses git history to identify co-change patterns. When file A changes,
    which other files tend to change in the same commit? This reveals
    hidden coupling that isn't visible in the import graph.

    Parameters:
      file_path — the file to analyze
      days      — how far back in git history to look (default: 90)

    Returns co-change partners ranked by frequency, with percentage.
    """
    return _brain.co_change_analysis(file_path, days=days)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
