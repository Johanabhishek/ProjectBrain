def handler():
    # `compute` is external (defined nowhere in this fixture).
    return compute()


# The next line is deliberately invalid so tree-sitter flags the file as
# parse_status == "partial". `handler` above still parses, so it remains a
# subject we can match — but its file is a blind spot, so absence cannot be
# refuted, only reported UNKNOWN.
def &&&broken syntax here
