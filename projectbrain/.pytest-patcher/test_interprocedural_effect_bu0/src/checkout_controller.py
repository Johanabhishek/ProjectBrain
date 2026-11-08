from billing_service import process_subscription_charge

async def handle_checkout_endpoint(db, payload: dict):
    amt = payload.get('amount', 0)
    return await process_subscription_charge(db, amt)
