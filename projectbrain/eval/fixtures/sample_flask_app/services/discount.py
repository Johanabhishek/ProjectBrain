from models.coupon import validate_coupon


def apply_discount(code=None):
    """Applies a discount code to the cart total."""
    return validate_coupon(code)
