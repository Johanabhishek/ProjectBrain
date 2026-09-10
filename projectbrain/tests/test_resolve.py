from projectbrain.resolve import (
    build_python_module_index,
    resolve_python_import,
    resolve_js_import,
    resolve_import,
)


PY_INDEX = {
    "app.py": {}, "sibling.py": {}, "pkg/__init__.py": {}, "pkg/foo.py": {},
    "pkg/sub/__init__.py": {}, "pkg/sub/deep.py": {},
    "services/users.py": {}, "other/users.py": {},
}
PY_FILE_SET = set(PY_INDEX)
PY_MODULE_INDEX = build_python_module_index(PY_INDEX)


def test_module_index_registers_all_suffixes():
    assert PY_MODULE_INDEX["pkg.foo"] == ["pkg/foo.py"]
    assert PY_MODULE_INDEX["foo"] == ["pkg/foo.py"]
    assert PY_MODULE_INDEX["pkg.sub.deep"] == ["pkg/sub/deep.py"]
    assert PY_MODULE_INDEX["sub.deep"] == ["pkg/sub/deep.py"]
    assert PY_MODULE_INDEX["deep"] == ["pkg/sub/deep.py"]
    assert PY_MODULE_INDEX["pkg"] == ["pkg/__init__.py"]


def test_module_index_tracks_ambiguous_suffix():
    assert set(PY_MODULE_INDEX["users"]) == {"services/users.py", "other/users.py"}


def test_resolve_absolute_import_unique_match():
    assert resolve_python_import("app.py", "services.users", PY_FILE_SET, PY_MODULE_INDEX) == "services/users.py"


def test_resolve_absolute_import_ambiguous_returns_none():
    assert resolve_python_import("app.py", "users", PY_FILE_SET, PY_MODULE_INDEX) is None


def test_resolve_absolute_import_unresolvable_external_returns_none():
    assert resolve_python_import("app.py", "requests", PY_FILE_SET, PY_MODULE_INDEX) is None


def test_resolve_relative_single_dot_targets_package_init():
    # "from . import x" inside pkg/foo.py -> current package is pkg -> pkg/__init__.py
    assert resolve_python_import("pkg/foo.py", ".", PY_FILE_SET, PY_MODULE_INDEX) == "pkg/__init__.py"


def test_resolve_relative_double_dot_targets_parent_package():
    # "from .. import x" inside pkg/sub/deep.py -> parent package is pkg
    assert resolve_python_import("pkg/sub/deep.py", "..", PY_FILE_SET, PY_MODULE_INDEX) == "pkg/__init__.py"


def test_resolve_relative_dot_with_name():
    # "from .sub.deep import x" inside pkg/foo.py -> pkg/sub/deep.py
    assert resolve_python_import("pkg/foo.py", ".sub.deep", PY_FILE_SET, PY_MODULE_INDEX) == "pkg/sub/deep.py"


def test_resolve_relative_at_root_level():
    # "from .sibling import x" inside a root-level file (no package dir at all)
    assert resolve_python_import("app.py", ".sibling", PY_FILE_SET, PY_MODULE_INDEX) == "sibling.py"


def test_resolve_relative_nonexistent_target_returns_none():
    assert resolve_python_import("pkg/sub/deep.py", ".foo", PY_FILE_SET, PY_MODULE_INDEX) is None


JS_FILES = {
    "src/routes/users.ts", "src/services/authService.ts",
    "src/services/index.ts", "src/services/util.js",
}


def test_resolve_js_relative_import_extension_added():
    assert resolve_js_import("src/routes/users.ts", "../services/authService", JS_FILES) == "src/services/authService.ts"


def test_resolve_js_relative_import_directory_index_fallback():
    assert resolve_js_import("src/routes/users.ts", "../services", JS_FILES) == "src/services/index.ts"


def test_resolve_js_relative_import_picks_correct_extension():
    assert resolve_js_import("src/routes/users.ts", "../services/util", JS_FILES) == "src/services/util.js"


def test_resolve_js_bare_package_import_returns_none():
    assert resolve_js_import("src/routes/users.ts", "react", JS_FILES) is None


def test_resolve_js_import_with_explicit_extension_already_present():
    files = {"src/a.ts", "src/b.ts"}
    assert resolve_js_import("src/a.ts", "./b.ts", files) == "src/b.ts"


def test_resolve_import_dispatches_by_extension():
    assert resolve_import("app.py", "services.users", PY_FILE_SET, PY_MODULE_INDEX) == "services/users.py"
    assert resolve_import("src/routes/users.ts", "../services/util", JS_FILES, {}) == "src/services/util.js"
