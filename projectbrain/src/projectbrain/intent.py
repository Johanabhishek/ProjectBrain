"""Query intent classification for ProjectBrain.

Rule-based classifier that routes natural language queries to the optimal
search strategy.  No LLM required — keyword patterns + structural analysis.
"""
from __future__ import annotations

import re

# ── Intent types ────────────────────────────────────────────────────────────

LOCATE_SYMBOL  = "locate_symbol"
LOCATE_ROUTE   = "locate_route"
UNDERSTAND_FLOW = "understand_flow"
FIND_RELATED   = "find_related"
IMPACT_ANALYSIS = "impact_analysis"
ADD_FEATURE    = "add_feature"

# ── Pattern banks ───────────────────────────────────────────────────────────

_LOCATE_PATTERNS = re.compile(
    r"\b(where\s+is|find|locate|which\s+file|defined?\s+in|definition\s+of|"
    r"look\s*up|search\s+for)\b",
    re.IGNORECASE,
)

_ROUTE_PATTERNS = re.compile(
    r"\b(route|endpoint|api|handler|url|path|"
    r"GET|POST|PUT|DELETE|PATCH)\b",
    re.IGNORECASE,
)

_FLOW_PATTERNS = re.compile(
    r"\b(how\s+does|flow|end.to.end|trace|walk\s*through|"
    r"what\s+happens\s+when|sequence|pipeline|chain|lifecycle)\b",
    re.IGNORECASE,
)

_IMPACT_PATTERNS = re.compile(
    r"\b(what\s+breaks|impact|affect|depend|break|change.*will|"
    r"who\s+(uses|imports|calls)|if\s+i\s+change|ripple|risk|"
    r"downstream|upstream|consumers?\s+of)\b",
    re.IGNORECASE,
)

_ADD_PATTERNS = re.compile(
    r"\b(add|create|implement|build|introduce|extend|write|new\s+feature|"
    r"set\s*up|integrate|hook\s*up|wire\s*up)\b",
    re.IGNORECASE,
)

# HTTP methods that strongly signal a route query
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "all"}

# Route-like path fragments
_ROUTE_PATH = re.compile(r"(/[a-zA-Z0-9_\-/:{}]+)")


def classify_intent(prompt: str) -> dict:
    """Classify the intent of a natural language query.

    Returns::

        {
            "intent": str,          # one of the *_* constants above
            "entities": list[str],  # extracted symbol/route/file names
            "confidence": float,    # 0.0–1.0
            "reason": str,          # human-readable explanation
        }
    """
    prompt_lower = prompt.lower().strip()

    # ── 1. Route queries (strongest signal: HTTP method + path) ─────────
    route_entities = _extract_route_entities(prompt)
    if route_entities:
        return {
            "intent": LOCATE_ROUTE,
            "entities": route_entities,
            "confidence": 0.9,
            "reason": "query contains HTTP method and/or URL path pattern",
        }

    # ── 2. Impact analysis ──────────────────────────────────────────────
    if _IMPACT_PATTERNS.search(prompt):
        return {
            "intent": IMPACT_ANALYSIS,
            "entities": _extract_symbol_entities(prompt),
            "confidence": 0.85,
            "reason": "query asks about impact, dependencies, or breakage",
        }

    # ── 3. Flow / understanding ─────────────────────────────────────────
    if _FLOW_PATTERNS.search(prompt):
        return {
            "intent": UNDERSTAND_FLOW,
            "entities": _extract_symbol_entities(prompt),
            "confidence": 0.8,
            "reason": "query asks about flow, sequence, or how something works",
        }

    # ── 4. Add / create feature ─────────────────────────────────────────
    if _ADD_PATTERNS.search(prompt):
        return {
            "intent": ADD_FEATURE,
            "entities": _extract_symbol_entities(prompt),
            "confidence": 0.75,
            "reason": "query asks to add, create, or implement something",
        }

    # ── 5. Locate symbol ───────────────────────────────────────────────
    if _LOCATE_PATTERNS.search(prompt):
        return {
            "intent": LOCATE_SYMBOL,
            "entities": _extract_symbol_entities(prompt),
            "confidence": 0.8,
            "reason": "query asks to find or locate a specific symbol",
        }

    # ── 6. Fallback: find related ──────────────────────────────────────
    return {
        "intent": FIND_RELATED,
        "entities": _extract_symbol_entities(prompt),
        "confidence": 0.5,
        "reason": "general query, searching for related files",
    }


def _extract_route_entities(prompt: str) -> list[str]:
    """Extract HTTP method + path pairs, or just paths."""
    entities = []

    # Look for explicit "METHOD /path" patterns
    method_path = re.findall(
        r"\b(GET|POST|PUT|DELETE|PATCH|ALL)\s+(/[a-zA-Z0-9_\-/:{}*]+)",
        prompt, re.IGNORECASE,
    )
    for method, path in method_path:
        entities.append(f"{method.upper()} {path}")

    # Look for standalone URL paths if HTTP method mentioned anywhere
    if not entities:
        has_http = any(m in prompt.lower().split() for m in _HTTP_METHODS)
        route_kw = _ROUTE_PATTERNS.search(prompt)
        paths = _ROUTE_PATH.findall(prompt)
        if (has_http or route_kw) and paths:
            entities.extend(paths)

    return entities


def _extract_symbol_entities(prompt: str) -> list[str]:
    """Extract likely code identifiers from a prompt.

    Looks for camelCase, snake_case, PascalCase, dotted.names, and
    file-like paths.  Filters out English stopwords and short tokens.
    """
    _STOP = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "i", "my", "me", "we", "our",
        "in", "on", "at", "to", "for", "of", "and", "or", "but", "not",
        "why", "am", "getting", "get", "how", "what", "when", "where", "which",
        "this", "that", "it", "its", "with", "from", "by", "about", "into",
        "add", "create", "implement", "build", "new", "find", "locate",
        "if", "change", "breaks", "affect", "does", "work", "end",
        "defined", "called", "named", "used", "handles", "file",
        "function", "class", "method", "variable",
    }

    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_./-]*", prompt)
    entities = []
    seen: set[str] = set()
    for tok in tokens:
        if tok.lower() in _STOP or len(tok) < 2:
            continue
        if tok.lower() not in seen:
            seen.add(tok.lower())
            entities.append(tok)
    return entities
