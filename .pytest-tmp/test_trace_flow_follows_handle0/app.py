from routes.checkout import get_checkout


def create_app():
    """Creates the application entrypoint."""
    return get_checkout
