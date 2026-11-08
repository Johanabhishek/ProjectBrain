from projectbrain.parsers import parse


# ── Python ───────────────────────────────────────────────────────────────────

PY_SRC = '''
import os
import os.path as osp
from foo import bar
from . import sibling
from .foo import bar2
from ..pkg import baz

class Base:
    pass

class Foo(Base):
    """Foo class."""
    def method_one(self):
        def nested_helper():
            return 1
        return nested_helper()

    def method_two(self):
        return self.method_one()

def top_level():
    """Top level func."""
    return Foo().method_one()

@app.get("/checkout")
def get_checkout():
    return 1

@app.route("/legacy")
def legacy_handler():
    return 1
'''


def test_py_function_extraction():
    result = parse(PY_SRC, ".py")
    by_name = {f["name"]: f for f in result["functions"]}

    assert by_name["top_level"]["docstring"] == "Top level func."
    assert by_name["top_level"]["line_start"] == 22
    assert by_name["top_level"]["line_end"] == 24
    assert set(by_name["top_level"]["calls"]) == {"method_one", "Foo"}


def test_py_class_extraction_and_bases():
    result = parse(PY_SRC, ".py")
    by_name = {c["name"]: c for c in result["classes"]}

    assert by_name["Base"]["bases"] == []
    assert by_name["Foo"]["bases"] == ["Base"]
    assert by_name["Foo"]["docstring"] == "Foo class."
    assert by_name["Foo"]["methods"] == ["method_one", "method_two"]


def test_py_nested_function_not_misclassified_as_method():
    """Regression lock: _py_direct_methods walks direct children of the class
    body block only, so a helper nested inside a method's body must not be
    treated as a class method or marked is_method."""
    result = parse(PY_SRC, ".py")
    by_name = {f["name"]: f for f in result["functions"]}

    assert "nested_helper" in by_name
    assert "is_method" not in by_name["nested_helper"]
    assert "nested_helper" not in {c["name"] for c in result["classes"]}
    for c in result["classes"]:
        assert "nested_helper" not in c["methods"]


def test_py_imports_forms():
    result = parse(PY_SRC, ".py")
    imports = set(result["imports"])

    assert "os" in imports
    assert "os.path" in imports          # `import x as y` captures the dotted name, not the alias
    assert "foo" in imports              # `from foo import bar`
    assert ".sibling" in imports                # `from . import sibling`
    assert ".foo" in imports             # `from .foo import bar2`
    assert "..pkg" in imports            # `from ..pkg import baz`


def test_py_fastapi_style_route_extracted():
    result = parse(PY_SRC, ".py")
    routes = [r for r in result["routes"] if r["framework"] == "fastapi"]

    assert len(routes) == 1
    route = routes[0]
    assert route["method"] == "GET"
    assert route["path"] == "/checkout"
    assert route["handler"] == "get_checkout"


def test_py_flask_style_route_extracted_with_default_get():
    result = parse(PY_SRC, ".py")
    flask_routes = [r for r in result["routes"] if r["framework"] == "flask"]

    assert len(flask_routes) == 1
    route = flask_routes[0]
    assert route["method"] == "GET"
    assert route["path"] == "/legacy"
    assert route["handler"] == "legacy_handler"


def test_py_flask_style_route_with_methods_kwarg():
    src = '''
@app.route("/items", methods=["POST"])
def create_item():
    return 1
'''
    result = parse(src, ".py")
    routes = result["routes"]

    assert len(routes) == 1
    assert routes[0]["method"] == "POST"
    assert routes[0]["framework"] == "flask"


def test_py_flask_style_route_with_multiple_methods():
    src = '''
@app.route("/items", methods=["GET", "POST"])
def items():
    return 1
'''
    result = parse(src, ".py")
    routes = result["routes"]

    assert len(routes) == 2
    assert {r["method"] for r in routes} == {"GET", "POST"}
    assert all(r["path"] == "/items" for r in routes)
    assert all(r["handler"] == "items" for r in routes)


def test_py_django_path_and_re_path_routes():
    src = '''
urlpatterns = [
    path("users/", views.user_list),
    path("users/<int:pk>/", user_detail),
    re_path(r"^users/$", views.user_list),
]
'''
    result = parse(src, ".py")
    routes = result["routes"]

    assert len(routes) == 3
    for r in routes:
        assert r["framework"] == "django"
        assert r["method"] == "ANY"

    by_path = {r["path"]: r["handler"] for r in routes}
    assert by_path["users/"] == "user_list"
    assert by_path["users/<int:pk>/"] == "user_detail"
    assert by_path["^users/$"] == "user_list"


def test_py_django_ignores_unrelated_calls():
    src = '''
something_else("nope", x)
'''
    result = parse(src, ".py")
    assert result["routes"] == []


# ── JavaScript ───────────────────────────────────────────────────────────────

JS_SRC = '''
import React from "react";
import { helper } from "./util";
const fs = require("fs");

class Base {}

class Foo extends Base {
  methodOne() {
    return this.methodTwo();
  }
  methodTwo() {
    return 1;
  }
}

function topLevel() {
  return new Foo().methodOne();
}

const arrowFn = () => {
  return topLevel();
};

export function exportedFn() {
  return 1;
}

export class ExportedClass {}

export const exportedConst = 1;

app.get("/users", (req, res) => {});
app.use("/mid", middleware);
'''


def test_js_function_extraction():
    result = parse(JS_SRC, ".js")
    by_name = {f["name"]: f for f in result["functions"]}

    assert "methodOne" in by_name
    assert by_name["methodOne"]["is_method"] is True
    assert by_name["methodOne"]["parent_class"] == "Foo"
    assert by_name["methodOne"]["calls"] == ["methodTwo"]
    assert by_name["arrowFn"]["calls"] == ["topLevel"]


def test_js_class_methods_direct_only():
    result = parse(JS_SRC, ".js")
    by_name = {c["name"]: c for c in result["classes"]}

    assert by_name["Foo"]["methods"] == ["methodOne", "methodTwo"]
    assert by_name["Base"]["methods"] == []


def test_js_class_bases_extracted():
    result = parse(JS_SRC, ".js")
    by_name = {c["name"]: c for c in result["classes"]}
    assert by_name["Foo"]["bases"] == ["Base"]


def test_js_imports():
    result = parse(JS_SRC, ".js")
    imports = set(result["imports"])

    assert imports == {"react", "./util", "fs"}


def test_js_exports():
    result = parse(JS_SRC, ".js")
    exports = set(result["exports"])

    assert exports == {"exportedFn", "ExportedClass", "exportedConst"}


def test_js_named_and_default_exports_extracted():
    src = '''
    function helper() { return 1; }
    export { helper };
    export default helper;
    '''
    result = parse(src, ".js")
    assert "helper" in result["exports"]


def test_js_express_route_extracted():
    result = parse(JS_SRC, ".js")
    routes = result["routes"]

    assert len(routes) == 1
    assert routes[0]["method"] == "GET"
    assert routes[0]["path"] == "/users"
    assert routes[0]["framework"] == "express"


def test_js_express_app_all_route_extracted():
    src = 'app.all("/wildcard", handler);'
    result = parse(src, ".js")
    routes = result["routes"]

    assert len(routes) == 1
    assert routes[0]["method"] == "ALL"
    assert routes[0]["path"] == "/wildcard"


def test_js_non_http_verb_call_not_a_route():
    result = parse(JS_SRC, ".js")
    paths = {r["path"] for r in result["routes"]}
    assert "/mid" not in paths


def test_tsx_jsx_smoke():
    for ext in (".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        result = parse("function foo() { return 1; }", ext)
        assert set(result.keys()) >= {"functions", "classes", "imports", "routes"}
        assert result["functions"][0]["name"] == "foo"


def test_unknown_extension_returns_empty_shape():
    result = parse("anything", ".unknown")
    assert result == {
        "functions": [], "classes": [], "imports": [], "routes": [],
        "parse_status": "unsupported",
    }


def test_parse_status_ok_and_partial():
    ok = parse("def foo():\n    return 1\n", ".py")
    assert ok["parse_status"] == "ok"
    # Broken syntax must be surfaced, never silently treated as a clean parse.
    broken = parse("def foo(:\n    return\n\ndef &&&x\n", ".py")
    assert broken["parse_status"] == "partial"


def test_call_sites_capture_line_numbers():
    src = "def caller():\n    helper()\n    other()\n"
    result = parse(src, ".py")
    fn = next(f for f in result["functions"] if f["name"] == "caller")
    assert fn["call_sites"]["helper"] == 2
    assert fn["call_sites"]["other"] == 3


def test_py_qualified_calls_captured():
    src = '''
def handler():
    result = user_service.find_user()
    data = process(result)
'''
    result = parse(src, ".py")
    fn = next(f for f in result["functions"] if f["name"] == "handler")
    assert "find_user" in fn["calls"]
    assert "process" in fn["calls"]
    assert "user_service.find_user" in fn["qualified_calls"]
    assert "process" not in fn["qualified_calls"]  # plain call, no receiver


def test_js_qualified_calls_captured():
    src = '''
function handler() {
    const result = userService.findUser();
    const data = process(result);
}
'''
    result = parse(src, ".js")
    fn = next(f for f in result["functions"] if f["name"] == "handler")
    assert "findUser" in fn["calls"]
    assert "process" in fn["calls"]
    assert "userService.findUser" in fn["qualified_calls"]
    assert "process" not in fn["qualified_calls"]


def test_js_commonjs_and_prototype_method_assignments():
    src = '''
var res = Object.create(http.ServerResponse.prototype);
module.exports = res;

res.send = function send(body) {
    this.type('html');
    return this;
};

res.contentType =
res.type = function contentType(type) {
    return this.set('Content-Type', type);
};

Router.prototype.handle = function(req, res, out) {
    return out();
};

defineGetter(req, 'protocol', function protocol() {
    return 'http';
});
'''
    result = parse(src, ".js")
    names = {f["name"]: f for f in result["functions"]}
    assert "send" in names
    assert names["send"]["parent_class"] == "res"
    assert "contentType" in names
    assert "type" in names
    assert "handle" in names
    assert names["handle"]["parent_class"] == "Router"
    assert "protocol" in names
    assert names["protocol"]["parent_class"] == "req"


def test_py_typing_overload_stubs_filtered():
    src = '''
from typing import overload

@overload
def convert(val: int) -> str: ...

@overload
def convert(val: str) -> str: ...

def convert(val: int | str) -> str:
    return str(val)
'''
    result = parse(src, ".py")
    fns = [f for f in result["functions"] if f["name"] == "convert"]
    assert len(fns) == 1
    assert fns[0]["line_start"] == 10
