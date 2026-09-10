def process():
    # Calls a function named `find`, but two files define one and this file
    # imports neither — the target is genuinely ambiguous (name collision).
    return find(1)
