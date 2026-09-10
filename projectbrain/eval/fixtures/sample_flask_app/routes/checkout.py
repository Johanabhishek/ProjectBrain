from services.discount import apply_discount


@app.route("/checkout", methods=["POST"])
def checkout():
    """Handles checkout submission."""
    return apply_discount()
