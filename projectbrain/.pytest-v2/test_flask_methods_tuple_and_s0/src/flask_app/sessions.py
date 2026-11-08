def get_signing_serializer(app):
    if not app.secret_key:
        return None
    return [app.secret_key]
