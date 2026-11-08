import os

TIMEOUT = 30

def check_user(user, hydrated=False):
    if not user:
        return 'redirect:/login'
    if hydrated and not user.get('profile'):
        return 'redirect:/profile'
    if not hydrated:
        return 'wait'
    return 'ok'
