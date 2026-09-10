import pytest
from projectbrain.classify import classify_function, classify_file_functions

def test_constructor_classification():
    func = {"name": "__init__", "calls": [], "qualified_calls": {}}
    assert classify_function(func) == "constructor"

def test_validator_classification():
    func = {"name": "validate_email", "calls": ["check", "match"], "qualified_calls": {}}
    assert classify_function(func) == "validator"

def test_handler_classification():
    func = {"name": "my_handler", "calls": [], "qualified_calls": {}}
    route_handlers = {"my_handler"}
    assert classify_function(func, route_handlers=route_handlers) == "handler"

def test_io_writer_classification():
    func = {"name": "create_user", "calls": ["save", "log"], "qualified_calls": {}}
    assert classify_function(func) == "io_writer"

def test_io_reader_classification():
    func = {"name": "get_user", "calls": ["find", "log"], "qualified_calls": {}}
    assert classify_function(func) == "io_reader"

def test_orchestrator_classification():
    func = {"name": "process_data", "calls": ["step1", "step2", "step3"], "qualified_calls": {}}
    assert classify_function(func) == "orchestrator"

def test_transformer_classification():
    func = {"name": "format_data", "calls": ["strip", "upper"], "qualified_calls": {}}
    assert classify_function(func) == "transformer"

def test_renderer_classification():
    func = {"name": "render_page", "calls": ["createElement"], "qualified_calls": {}}
    assert classify_function(func) == "renderer"

def test_unknown_classification():
    func = {"name": "do_something", "calls": [], "qualified_calls": {}}
    assert classify_function(func) == "unknown"

def test_classify_file_functions():
    file_info = {
        "routes": [{"handler": "my_handler"}],
        "functions": [
            {"name": "my_handler", "calls": []},
            {"name": "validate_email", "calls": ["match"]},
            {"name": "do_nothing", "calls": []}
        ]
    }
    result = classify_file_functions(file_info)
    assert result["my_handler"] == "handler"
    assert result["validate_email"] == "validator"
    assert result["do_nothing"] == "unknown"

def test_io_writer_beats_orchestrator():
    func = {"name": "process_and_save", "calls": ["step1", "step2", "step3", "save"], "qualified_calls": {}}
    assert classify_function(func) == "io_writer"

def test_qualified_calls_detected():
    func = {"name": "update_db", "calls": [], "qualified_calls": {"db.insert": 5}}
    assert classify_function(func) == "io_writer"
