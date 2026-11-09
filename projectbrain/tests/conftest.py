from pathlib import Path
import sys

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest


_FILES = {
    "app.py": '''\
from routes.checkout import get_checkout


def create_app():
    """Creates the application entrypoint."""
    return get_checkout
''',
    "routes/checkout.py": '''\
from services.discount import apply_discount
from services.util import helper


@app.get("/checkout")
def get_checkout():
    """Handles checkout requests."""
    helper()
    return apply_discount()
''',
    "services/discount.py": '''\
from services.util import helper


def apply_discount(code):
    """Applies a discount code to the cart total."""
    helper()
    return code
''',
    "services/util.py": '''\
def helper():
    """Shared utility helper."""
    return True
''',
    "services/pricing.py": '''\
from services.util import helper


def calculate_price():
    """Calculates price using shared helper."""
    return helper()
''',
    "standalone.py": '''\
def helper():
    """Standalone helper, not used elsewhere."""
    return None
''',
    "notes.py": '''\
def print_notes():
    """Some notes about the discount feature, but no logic here."""
    return None
''',
    "tests/notes_test.py": '''\
def print_notes():
    """Some notes about the discount feature, but no logic here."""
    return None
''',
}


@pytest.fixture
def tmp_project(tmp_path):
    """Writes a small synthetic multi-file Python project to tmp_path and returns its root."""
    for rel_path, content in _FILES.items():
        full = tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return tmp_path
