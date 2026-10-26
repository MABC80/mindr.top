import json

from a2a import (
    build_agent_card,
    discovery_query,
    extract_endpoint,
    extract_text,
    format_discovery,
    validate_agent_card,
    wants_discovery,
)


V03 = {
    "protocolVersion": "0.3.0",
    "name": "WalnutLink AI",
    "description": "Research desk",
    "url": "https://walnutlink.ai/a2a",
    "capabilities": {"streaming": False},
    "skills": [{"id": "research", "name": "Research", "description": "Look things up", "tags": ["research"]}],
}

V1 = {
    "name": "Geo",
    "description": "Routes",
    "supportedInterfaces": [
        {"url": "https://geo.example/a2a/v1", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {"streaming": False},
    "skills": [{"id": "route", "name": "Route", "description": "Plan", "tags": ["maps"]}],
}


def test_own_card_validates_and_uses_public_url():
    card = build_agent_card("https://mindr-a2a.onrender.com")
    parsed = validate_agent_card(card)
    assert parsed is not None
    assert parsed["name"] == "Mindr"
    assert parsed["url"] == "https://mindr-a2a.onrender.com/"
    assert card["supportedInterfaces"][0]["url"] == "https://mindr-a2a.onrender.com/"
    json.dumps(card)


def test_accepts_v03_and_v1_cards():
    assert validate_agent_card(V03)["url"] == "https://walnutlink.ai/a2a"
    assert validate_agent_card(V1)["url"] == "https://geo.example/a2a/v1"


def test_rejects_incomplete_cards():
    assert validate_agent_card({"name": "Solo"}) is None
    assert validate_agent_card({"name": "Solo", "url": "https://a.example/a2a"}) is None
    assert validate_agent_card({"name": "Solo", "url": "http://a.example/a2a", "skills": [{"id": "x", "name": "X"}]}) is None
    assert validate_agent_card({"url": "https://a.example/a2a", "capabilities": {"streaming": False}}) is None
    assert validate_agent_card(["name"]) is None
    assert validate_agent_card({"name": "Loop", "url": "https://127.0.0.1/a2a", "capabilities": {"streaming": False}}) is None


def test_prefers_jsonrpc_interface():
    card = {
        "name": "Multi",
        "supportedInterfaces": [
            {"url": "https://multi.example/grpc", "protocolBinding": "GRPC", "protocolVersion": "1.0"},
            {"url": "https://multi.example/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        ],
        "skills": [{"id": "echo", "name": "Echo"}],
    }
    assert extract_endpoint(card) == "https://multi.example/rpc"


def test_extract_text_from_message_parts():
    params = {
        "message": {
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "text", "text": "Hola"}, {"kind": "file", "text": "no"}, {"kind": "text", "text": "Mindr"}],
        }
    }
    assert extract_text(params) == "Hola\nMindr"


def test_discovery_intent_and_query():
    assert wants_discovery("discover", {})
    assert wants_discovery("Descubre agentes A2A", {})
    assert wants_discovery("encuentra agentes de clima", {})
    assert wants_discovery("please discover agents for maps", {})
    assert wants_discovery("hola", {"skill": "discover"})
    assert not wants_discovery("¿Qué es un agente A2A?", {})
    assert not wants_discovery("Discover the capital of France", {})
    assert discovery_query("discover weather agents") == "weather agents"
    assert "Walnut" in format_discovery(
        [{"name": "Walnut", "description": "Desk", "card_url": "https://w.example/c", "url": "https://w.example/a2a"}],
        english=False,
    )
    assert format_discovery([], english=True).startswith("I could not find")
