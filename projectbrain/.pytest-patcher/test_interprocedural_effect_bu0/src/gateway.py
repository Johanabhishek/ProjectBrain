class PaymentDeclinedError(Exception):
    pass

async def commit_invoice_row(db, amount: int):
    if amount <= 0:
        raise PaymentDeclinedError('invalid amount')
    await db.execute('INSERT INTO invoices_ledger (amount) VALUES (100)')
    session['billing_audit_token'] = 'ok'
    return True
