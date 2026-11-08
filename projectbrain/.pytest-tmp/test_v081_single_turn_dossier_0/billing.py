def compute_invoice_tax(subtotal: float, rate: float) -> float:
    if rate < 0:
        raise ValueError('negative tax rate')
    return round(subtotal * rate, 2)

def checkout_order(order_id: str, amount: float) -> dict:
    tax = compute_invoice_tax(amount, -0.05)
    return {'order_id': order_id, 'total': amount + tax}
