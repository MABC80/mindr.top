import asyncio
import time

import httpx

from a2a import build_agent_card
from discover import (
    DiscoveryCache,
    candidates_from_markdown,
    candidates_from_registry,
    card_urls_for,
    discover_agents,
    host_is_public,
    is_self,
    select_card_urls,
)


CARD = {
    "name": "Listed",
    "description": "A public demo",
    "url": "https://listed.example/a2a",
    "capabilities": {"streaming": False},
    "skills": [{"id": "echo", "name": "Echo", "description": "Echo", "tags": ["echo"]}],
}

OTHER = {
    "name": "Other",
    "description": "Second",
    "url": "https://other.example/a2a",
    "protocolVersion": "0.3.0",
    "skills": [{"id": "search", "name": "Search", "description": "Search", "tags": ["search"]}],
}


def test_card_url_expansion_and_parsers():
    assert card_urls_for("https://perkoon.com") == [
        "https://perkoon.com/.well-known/agent-card.json",
        "https://perkoon.com/.well-known/agent.json",
    ]
    assert card_urls_for("walnutlink.ai/.well-known/agent.json") == [
        "https://walnutlink.ai/.well-known/agent.json"
    ]
    assert card_urls_for("http://127.0.0.1:8080/secret") == []
    assert card_urls_for("https://user:pw@evil.example/a") == []

    markdown = (
        "Card: https://bitcoinsapi.com/.well-known/agent-card.json. "
        "Also perkoon.com/.well-known/agent.json and "
        "`https://apexfaucet.xyz/.well-known/agent-card.json`"
    )
    found = candidates_from_markdown(markdown)
    assert "https://bitcoinsapi.com/.well-known/agent-card.json" in found
    assert "https://perkoon.com/.well-known/agent.json" in found
    assert "https://apexfaucet.xyz/.well-known/agent-card.json" in found

    registry = {
        "agents": [
            {"name": "Walnut", "wellKnownURI": "https://walnutlink.ai/.well-known/agent.json"},
            {"name": "Hire", "card_url": "https://example.com/s/x/.well-known/agent-card.json"},
            {"name": "nope"},
        ]
    }
    assert candidates_from_registry(registry) == [
        "https://walnutlink.ai/.well-known/agent.json",
        "https://example.com/s/x/.well-known/agent-card.json",
    ]


def test_select_reserves_room_for_claude_and_dedupes():
    seeds = [f"https://seed{i}.example/card" for i in range(40)]
    proposed = ["https://claude.example/card", "https://seed0.example/card"]
    chosen = select_card_urls(seeds, [], [], proposed, limit=10)
    assert "https://claude.example/card" in chosen
    assert len(chosen) <= 10
    assert chosen.count("https://seed0.example/card") == 1


def test_private_hosts_are_blocked():
    async def check(host: str) -> bool:
        return await host_is_public(host)

    for host in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "localhost", "metadata.google.internal"):
        assert asyncio.run(check(host)) is False
    assert is_self("https://mindr-a2a.onrender.com/.well-known/agent-card.json", "http://127.0.0.1:8000")
    assert is_self("https://127.0.0.1/a2a", "http://127.0.0.1:8000")
    assert is_self("https://alias.example/a2a", "https://alias.example/")
    assert not is_self("https://other.example/a2a", "https://mindr-a2a.onrender.com")


def test_discovery_fetches_valid_cards_and_skips_self_and_private():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        url = str(request.url)
        if url.endswith("listed.example/.well-known/agent-card.json"):
            return httpx.Response(200, json=CARD)
        if url.endswith("other.example/.well-known/agent.json"):
            return httpx.Response(200, json=OTHER)
        if "mindr-a2a.onrender.com" in url:
            return httpx.Response(200, json=build_agent_card("https://mindr-a2a.onrender.com"))
        if url.endswith("thin.example/.well-known/agent-card.json"):
            return httpx.Response(200, json={"name": "Thin", "url": "https://thin.example/a2a"})
        if "registry.example" in url:
            return httpx.Response(
                200,
                json={"agents": [{"wellKnownURI": "https://other.example/.well-known/agent.json"}]},
            )
        return httpx.Response(404)

    async def host_ok(host: str) -> bool:
        return host.endswith(".example") or host == "mindr-a2a.onrender.com"

    async def proposer(query: str) -> list[str]:
        assert query == "clima"
        return [
            "https://listed.example",
            "https://127.0.0.1/.well-known/agent-card.json",
            "https://10.1.2.3/.well-known/agent-card.json",
            "http://169.254.169.254/latest/meta-data",
            "https://mindr-a2a.onrender.com",
            "https://thin.example",
        ]

    async def run():
        # Apunta los registros de prueba a hosts que host_ok acepta, parcheando las constantes.
        import discover as discover_module

        original_registries = discover_module.REGISTRY_URLS
        original_awesome = discover_module.AWESOME_URLS
        discover_module.REGISTRY_URLS = ("https://registry.example/agents",)
        discover_module.AWESOME_URLS = (
            "https://lists.example/readme.md",
        )
        transport = httpx.MockTransport(handler)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                # El README de prueba se pide como texto. MockTransport no distingue.
                first = await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="clima",
                    client=client,
                    proposer=proposer,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )
        finally:
            discover_module.REGISTRY_URLS = original_registries
            discover_module.AWESOME_URLS = original_awesome
        return first

    # El README mock devuelve 404. El registro aporta Other y Claude aporta Listed.
    # Thin no valida. Self y las IPs privadas no entran.
    first = asyncio.run(run())
    names = [agent.name for agent in first.agents]
    assert "Listed" in names
    assert "Other" in names
    assert "Mindr" not in names
    assert "Thin" not in names
    assert len(first.agents) <= 10
    assert all(url.startswith("https://") for url in requested)
    assert not any("127.0.0.1" in url or "10.1.2.3" in url or "169.254.169.254" in url for url in requested)


def test_cache_skips_a_second_fetch():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url).endswith("listed.example/.well-known/agent-card.json"):
            return httpx.Response(200, json=CARD)
        return httpx.Response(404)

    async def host_ok(host: str) -> bool:
        return host == "listed.example"

    async def proposer(_query: str) -> list[str]:
        return ["https://listed.example/.well-known/agent-card.json"]

    async def run():
        import discover as discover_module

        original_registries = discover_module.REGISTRY_URLS
        original_awesome = discover_module.AWESOME_URLS
        original_seeds = discover_module.SEED_ORIGINS
        discover_module.REGISTRY_URLS = ()
        discover_module.AWESOME_URLS = ()
        discover_module.SEED_ORIGINS = ()
        cache = DiscoveryCache(ttl_seconds=60)
        transport = httpx.MockTransport(handler)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                kwargs = dict(
                    public_url="https://mindr-a2a.onrender.com",
                    query="",
                    client=client,
                    proposer=proposer,
                    host_ok=host_ok,
                    cache=cache,
                    budget_seconds=5,
                )
                first = await discover_agents(**kwargs)
                after = len(requested)
                second = await discover_agents(**kwargs)
        finally:
            discover_module.REGISTRY_URLS = original_registries
            discover_module.AWESOME_URLS = original_awesome
            discover_module.SEED_ORIGINS = original_seeds
        return first, second, after

    first, second, after = asyncio.run(run())
    assert [agent.name for agent in first.agents] == ["Listed"]
    assert second.cached is True
    assert len(requested) == after
    assert first.agents[0].card_url.endswith("/.well-known/agent-card.json")


def test_probe_stops_when_the_budget_is_already_spent():
    from discover import _probe

    async def host_ok(_host: str) -> bool:
        return True

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=CARD))
        async with httpx.AsyncClient(transport=transport) as client:
            return await _probe(
                client,
                "https://listed.example/.well-known/agent-card.json",
                "https://mindr-a2a.onrender.com",
                host_ok,
                time.monotonic() - 1,
                asyncio.Semaphore(1),
            )

    assert asyncio.run(run()) is None
