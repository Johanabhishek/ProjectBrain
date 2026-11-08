def outer_fn(x: int) -> int:
    def unused_inner(y: int) -> int:
        return y + 1
    return x * 2
