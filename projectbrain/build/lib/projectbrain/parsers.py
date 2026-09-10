import ast
import re


def parse_python(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"functions": [], "classes": [], "imports": [], "routes": []}

    functions = []
    classes = []
    imports = []
    routes = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
            })
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in ("get", "post", "put", "delete", "patch"):
                    continue
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    routes.append({
                        "method": func.attr.upper(),
                        "path": decorator.args[0].value,
                        "handler": node.name,
                        "line": node.lineno,
                    })

        elif isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
            })

        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return {
        "functions": functions,
        "classes": classes,
        "imports": list(set(imports)),
        "routes": routes,
    }


def parse_js(source: str) -> dict:
    functions = []
    classes = []
    imports = []
    exports = []
    routes = []

    for i, line in enumerate(source.splitlines(), 1):
        # ES module imports
        m = re.match(r'\s*import\s+.*\s+from\s+[\'"](.+)[\'"]', line)
        if m:
            imports.append(m.group(1))

        # CommonJS require
        m = re.match(r'\s*(?:const|let|var)\s+\w+\s*=\s*require\s*\(\s*[\'"](.+)[\'"]\s*\)', line)
        if m:
            imports.append(m.group(1))

        # named function declarations
        m = re.match(r'\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)', line)
        if m:
            functions.append({"name": m.group(1), "line_start": i, "line_end": i})

        # arrow functions assigned to const/let
        m = re.match(r'\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', line)
        if m:
            functions.append({"name": m.group(1), "line_start": i, "line_end": i})

        # class declarations
        m = re.match(r'\s*(?:export\s+)?class\s+(\w+)', line)
        if m:
            classes.append({"name": m.group(1), "line_start": i, "line_end": i})

        # exports
        m = re.match(r'\s*export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)', line)
        if m:
            exports.append(m.group(1))

        # Express/Fastify routes: app.get('/path') or router.post('/path')
        m = re.match(r'\s*(?:\w+)\.(get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]', line)
        if m and m.group(1) in ("get", "post", "put", "delete", "patch"):
            routes.append({"method": m.group(1).upper(), "path": m.group(2), "line": i})

    return {
        "functions": functions,
        "classes": classes,
        "imports": list(set(imports)),
        "exports": exports,
        "routes": routes,
    }
