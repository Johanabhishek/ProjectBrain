from projectbrain.frameworks import infer_file_based_routes


def test_app_router_single_verb():
    info = {"exports": ["GET"]}
    routes = infer_file_based_routes("app/api/users/route.ts", info)

    assert len(routes) == 1
    assert routes[0]["method"] == "GET"
    assert routes[0]["path"] == "/api/users"
    assert routes[0]["framework"] == "nextjs-app-router"


def test_app_router_multiple_verbs():
    info = {"exports": ["GET", "POST"]}
    routes = infer_file_based_routes("app/api/users/route.ts", info)

    assert {r["method"] for r in routes} == {"GET", "POST"}
    assert all(r["path"] == "/api/users" for r in routes)


def test_app_router_dynamic_param_segment():
    info = {"exports": ["GET"]}
    routes = infer_file_based_routes("app/api/users/[id]/route.ts", info)

    assert routes[0]["path"] == "/api/users/:id"


def test_app_router_catch_all_segment():
    info = {"exports": ["GET"]}
    routes = infer_file_based_routes("app/api/[...slug]/route.ts", info)

    assert routes[0]["path"] == "/api/*"


def test_app_router_no_verb_exports_returns_empty():
    info = {"exports": ["helper"]}
    routes = infer_file_based_routes("app/api/users/route.ts", info)
    assert routes == []


def test_pages_router_basic():
    info = {"exports": []}
    routes = infer_file_based_routes("pages/api/users.ts", info)

    assert len(routes) == 1
    assert routes[0]["method"] == "ANY"
    assert routes[0]["path"] == "/api/users"
    assert routes[0]["framework"] == "nextjs-pages-router"


def test_pages_router_dynamic_param():
    info = {"exports": []}
    routes = infer_file_based_routes("pages/api/users/[id].ts", info)

    assert routes[0]["path"] == "/api/users/:id"


def test_pages_router_excludes_special_files():
    info = {"exports": []}
    assert infer_file_based_routes("pages/api/_middleware.ts", info) == []


def test_non_matching_path_returns_empty():
    info = {"exports": ["Button"]}
    assert infer_file_based_routes("src/components/Button.tsx", info) == []
