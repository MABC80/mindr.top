from fastapi.testclient import TestClient

from app import Settings, create_app
from discover import DiscoveryResult, FoundAgent
from llm import LLM


class FakeLLM(LLM):
    def __init__(self):
        super().__init__("", "claude-haiku-5-5")
        self.seen: list[str] = []

    def configured(self) -> bool:
        return True

    async def answer(self, text: str) -> str:
        self.seen.append(text)
        return f"eco:{text}"

    async def propose_candidates(self, query: str) -> list[str]:
        return ["https://proposed.example"]


def _client():
    llm = FakeLLM()
    app = create_app(Settings(public_url="https://mindr-a2a.onrender.com"), llm)
    return app, llm


def test_health_card_and_cors():
    app, _llm = _client()
    with TestClient(app) as client:
        health = client.get("/health", headers={"Origin": "https://www.mindr.top"})
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers["access-control-allow-origin"] == "https://www.mindr.top"

        blocked = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in blocked.headers

        card = client.get("/.well-known/agent-card.json")
        other = client.get("/.well-known/agent.json")
        assert card.status_code == 200
        assert card.json() == other.json()
        assert card.json()["url"] == "https://mindr-a2a.onrender.com/"
        assert card.json()["name"] == "Mindr"
        assert "skills" in card.json() and "capabilities" in card.json()
        assert card.headers["etag"]

        cached = client.get("/.well-known/agent-card.json", headers={"If-None-Match": card.headers["etag"]})
        assert cached.status_code == 304


def test_message_send_uses_the_model_and_discover_does_not(monkeypatch):
    app, llm = _client()
    calls = []

    async def fake_discover(**kwargs):
        calls.append(kwargs)
        return DiscoveryResult(
            agents=(
                FoundAgent(
                    name="Listed",
                    description="Demo",
                    card_url="https://listed.example/.well-known/agent-card.json",
                    url="https://listed.example/a2a",
                ),
            ),
            cached=False,
            elapsed_ms=12,
        )

    monkeypatch.setattr("app.discover_agents", fake_discover)
    with TestClient(app) as client:
        chat = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": "abc",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": "m1",
                        "contextId": "ctx-1",
                        "parts": [{"kind": "text", "text": "Hola"}],
                    }
                },
            },
        )
        body = chat.json()
        assert body["id"] == "abc"
        assert body["result"]["kind"] == "message"
        assert body["result"]["parts"][0]["text"] == "eco:Hola"
        assert body["result"]["contextId"] == "ctx-1"
        assert llm.seen == ["Hola"]

        wrapped = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "SendMessage",
                "params": {"message": {"parts": [{"kind": "text", "text": "discover"}]}},
            },
        )
        discovered = wrapped.json()["result"]["message"]["parts"][0]["text"]
        assert "Listed" in discovered
        assert "https://listed.example/.well-known/agent-card.json" in discovered
        assert llm.seen == ["Hola"]
        assert calls and calls[0]["proposer"] is not None

        missing = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})
        assert missing.json()["error"]["code"] == -32602
        unknown = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {}})
        assert unknown.json()["error"]["code"] == -32601
        assert client.post("/", content=b"nope", headers={"Content-Type": "application/json"}).json()["error"]["code"] == -32700
        assert client.post("/", json={"jsonrpc": "2.0", "method": "message/send"}).status_code == 204


def test_discover_endpoint(monkeypatch):
    app, _llm = _client()

    async def fake_discover(**kwargs):
        return DiscoveryResult(agents=(), cached=True, elapsed_ms=0, query=kwargs.get("query") or "")

    monkeypatch.setattr("app.discover_agents", fake_discover)
    with TestClient(app) as client:
        response = client.get("/discover", headers={"Origin": "https://mindr.top"})
        assert response.status_code == 200
        assert response.json()["agents"] == []
        assert response.headers["access-control-allow-origin"] == "https://mindr.top"
        posted = client.post("/discover", json={"q": "mapas"})
        assert posted.json()["query"] == "mapas"
