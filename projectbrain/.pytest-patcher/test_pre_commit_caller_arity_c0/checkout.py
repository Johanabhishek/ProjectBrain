from billing import charge

def run_checkout(uid: str) -> bool:
    return charge(uid)
