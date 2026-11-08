def verify_audit_trail(session: dict):
    tok = session.get('billing_audit_token')
    return bool(tok)
