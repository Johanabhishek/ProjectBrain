# Bump SCHEMA_VERSION when the on-disk cache envelope shape changes.
# Bump PARSER_VERSION when parsers.py changes what it extracts or how.
# Bump GRAPH_VERSION when graph.py changes node/edge shape or semantics.
# Any bump invalidates existing caches, forcing a full reindex on next load().

SCHEMA_VERSION = 4
PARSER_VERSION = "2026-10-19.1"
GRAPH_VERSION = "2026-10-18.1"
