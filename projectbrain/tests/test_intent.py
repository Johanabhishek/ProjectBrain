import pytest
from projectbrain.intent import (
    classify_intent,
    _extract_route_entities,
    _extract_symbol_entities,
    LOCATE_SYMBOL,
    LOCATE_ROUTE,
    UNDERSTAND_FLOW,
    FIND_RELATED,
    IMPACT_ANALYSIS,
    ADD_FEATURE
)

def test_locate_symbol_intent():
    res = classify_intent("where is getUserProfile defined?")
    assert res["intent"] == LOCATE_SYMBOL

def test_locate_route_intent():
    res = classify_intent("which file handles POST /checkout")
    assert res["intent"] == LOCATE_ROUTE

def test_locate_route_with_path():
    res = classify_intent("find the /api/users endpoint")
    assert res["intent"] == LOCATE_ROUTE

def test_understand_flow_intent():
    res = classify_intent("how does checkout work end to end")
    assert res["intent"] == UNDERSTAND_FLOW

def test_impact_analysis_intent():
    res = classify_intent("what breaks if I change the User model")
    assert res["intent"] == IMPACT_ANALYSIS

def test_add_feature_intent():
    res = classify_intent("add discount codes to checkout")
    assert res["intent"] == ADD_FEATURE

def test_find_related_fallback():
    res = classify_intent("checkout discount")
    assert res["intent"] == FIND_RELATED

def test_entities_extracted():
    res = classify_intent("where is getUserProfile defined?")
    entities = res["entities"]
    assert "getUserProfile" in entities
    assert "is" not in entities
    assert "where" not in entities
    assert "defined" not in entities
    
    # Direct test of _extract_symbol_entities
    extracted = _extract_symbol_entities("what breaks if I change the User model")
    assert "User" in extracted
    assert "model" in extracted
    assert "breaks" not in extracted
    assert "if" not in extracted

def test_route_entities_with_http_method():
    entities = _extract_route_entities("GET /api/users")
    assert "GET /api/users" in entities

    # Via intent
    res = classify_intent("how to test GET /api/users")
    assert res["intent"] == LOCATE_ROUTE
    assert "GET /api/users" in res["entities"]
