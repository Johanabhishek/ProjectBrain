from services.discount import apply_discount
from services.util import helper


@app.get("/checkout")
def get_checkout():
    """Handles checkout requests."""
    helper()
    return apply_discount()
