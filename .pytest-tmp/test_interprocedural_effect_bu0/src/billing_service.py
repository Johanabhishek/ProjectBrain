from gateway import commit_invoice_row

async def process_subscription_charge(db, amount: int):
    return await commit_invoice_row(db, amount)
