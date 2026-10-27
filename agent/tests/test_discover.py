import asyncio
import time
from contextlib import contextmanager

import httpx

from a2a import build_agent_card
from discover import (
    DiscoveryCache,
    DiscoveryResult,
    _split_fallbacks,
    candidates_from_markdown,
    candidates_from_nanda,
    candidates_from_registry,
    card_urls_for,
    discover_agents,
    host_is_public,
    is_self,
    next_directory_url,
    select_card_urls,
)


@contextmanager
def _sources(**overrides: object):
    import discover as discover_module

    names = ("REGISTRY_URLS", "NEST_URLS", "AWESOME_URLS", "SEED_ORIGINS")
    saved = {name: getattr(discover_module, name) for name in names}
    try:
        for name, value in overrides.items():
            setattr(discover_module, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(discover_module, name, value)


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
    assert first.complete is True
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
    assert first.complete is True
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


def test_nanda_records_use_endpoint_and_facts_origins():
    found = candidates_from_nanda(
        {
            "agents": [
                {
                    "endpoint": "https://shop.example/a2a",
                    "factsUrl": "https://shop.example/.well-known/agent-card.json",
                },
                {
                    "endpoint": "https://agent.example",
                    "factsUrl": "https://static.example/agentfacts.json",
                },
                {"endpoint": "http://localhost:7778", "factsUrl": "http://127.0.0.1/agentfacts.json"},
                {"endpoint": "https://user:pw@evil.example/a2a"},
                {"endpoint": "https://10.1.2.3/secret"},
                "nope",
            ]
        }
    )
    assert found[:2] == [
        "https://shop.example/.well-known/agent-card.json",
        "https://shop.example/.well-known/agent.json",
    ]
    assert "https://agent.example/.well-known/agent-card.json" in found
    assert "https://static.example/.well-known/agent-card.json" in found
    assert "https://static.example/.well-known/agent.json" in found
    assert not any(host in url for url in found for host in ("localhost", "127.0.0.1", "evil.example", "10.1.2.3"))


def test_directory_pages_follow_offset_or_nest_page():
    full = "https://a2aregistry.org/api/agents?limit=50&offset=0&task_verified=true"
    page = next_directory_url(
        full,
        {"agents": [{}] * 50, "total": 298, "limit": 50, "offset": 0},
    )
    assert page is not None
    assert "offset=50" in page
    assert "limit=50" in page
    assert "task_verified=true" in page
    assert next_directory_url(full, {"agents": [{}] * 10, "total": 298, "limit": 50, "offset": 0}) is None
    assert (
        next_directory_url(
            "https://a2aregistry.org/api/agents?limit=50&offset=250&task_verified=true",
            {"agents": [{}] * 48, "total": 298, "limit": 50, "offset": 250},
        )
        is None
    )
    peer = next_directory_url(
        "https://agentpeering.com/api/agents?limit=30&offset=0",
        {"agents": [{}] * 30, "pagination": {"limit": 30, "offset": 0, "total": 156}},
    )
    assert peer is not None and "offset=30" in peer

    nest = "https://nest.projectnanda.org/api/agents?limit=100&page=1"
    nxt = next_directory_url(
        nest,
        {"agents": [{}] * 100, "pagination": {"page": 1, "limit": 100, "totalPages": 135, "hasNext": True}},
    )
    assert nxt is not None and "page=2" in nxt and "limit=100" in nxt
    assert (
        next_directory_url(
            nest,
            {"agents": [{}] * 100, "pagination": {"page": 1, "limit": 100, "hasNext": False}},
        )
        is None
    )
    # Sin offset ni page no se inventa una segunda petición.
    assert next_directory_url("https://registry.example/agents", {"agents": [{}, {}], "total": 4}) is None


def test_agent_json_is_only_the_fallback():
    primaries, fallbacks = _split_fallbacks(
        [
            "https://perkoon.com/.well-known/agent-card.json",
            "https://perkoon.com/.well-known/agent.json",
            "https://onlyjson.example/.well-known/agent.json",
        ]
    )
    assert primaries == [
        "https://perkoon.com/.well-known/agent-card.json",
        "https://onlyjson.example/.well-known/agent.json",
    ]
    assert fallbacks == {
        "https://perkoon.com/.well-known/agent-card.json": "https://perkoon.com/.well-known/agent.json",
    }


def test_cache_keeps_incomplete_results_for_a_short_ttl():
    async def run():
        now = {"t": 0.0}
        cache = DiscoveryCache(ttl_seconds=1800, incomplete_ttl_seconds=90, now=lambda: now["t"])
        calls = {"n": 0}

        async def missing() -> DiscoveryResult:
            calls["n"] += 1
            return DiscoveryResult(agents=(), cached=False, elapsed_ms=1, complete=False)

        async def done() -> DiscoveryResult:
            calls["n"] += 1
            return DiscoveryResult(agents=(), cached=False, elapsed_ms=1, complete=True)

        first = await cache.get_or_run("partial", missing)
        assert first.complete is False
        now["t"] = 89
        assert (await cache.get_or_run("partial", missing)).cached is True
        assert calls["n"] == 1
        now["t"] = 90.1
        assert (await cache.get_or_run("partial", missing)).cached is False
        assert calls["n"] == 2

        now["t"] = 1000
        await cache.get_or_run("full", done)
        now["t"] = 1000 + 89
        assert (await cache.get_or_run("full", done)).cached is True
        now["t"] = 1000 + 1801
        assert (await cache.get_or_run("full", done)).cached is False

    asyncio.run(run())


def _quiet(**overrides: object):
    base = {
        "REGISTRY_URLS": (),
        "NEST_URLS": (),
        "AWESOME_URLS": (),
        "SEED_ORIGINS": (),
    }
    base.update(overrides)
    return _sources(**base)


def test_a_timed_out_card_is_retried_once_and_not_cached_for_long():
    card = "https://listed.example/.well-known/agent-card.json"
    attempts = {"n": 0}
    now = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("cold")

    async def host_ok(host: str) -> bool:
        return host == "listed.example"

    async def proposer(_query: str) -> list[str]:
        return [card]

    async def run():
        cache = DiscoveryCache(ttl_seconds=1800, incomplete_ttl_seconds=90, now=lambda: now["t"])
        transport = httpx.MockTransport(handler)
        with _quiet():
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
                second = await discover_agents(**kwargs)
                after_second = attempts["n"]
                now["t"] = 90.1
                third = await discover_agents(**kwargs)
        return first, second, third, after_second

    first, second, third, after_second = asyncio.run(run())
    assert first.complete is False
    assert first.agents == ()
    # Un intento y un reintento; la segunda búsqueda sigue en la caché corta.
    assert after_second == 2
    assert second.cached is True
    assert third.cached is False
    assert third.complete is False
    assert attempts["n"] == 4


def test_retry_can_recover_a_cold_card_and_a_miss_is_not_retried():
    card = "https://listed.example/.well-known/agent-card.json"
    agent_json = "https://listed.example/.well-known/agent.json"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == card and requested.count(card) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if str(request.url) == card:
            return httpx.Response(200, json=CARD)
        return httpx.Response(404)

    async def host_ok(host: str) -> bool:
        return host == "listed.example"

    async def proposer(_query: str) -> list[str]:
        return ["https://listed.example"]

    async def run():
        transport = httpx.MockTransport(handler)
        with _quiet():
            async with httpx.AsyncClient(transport=transport) as client:
                found = await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="",
                    client=client,
                    proposer=proposer,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )
        return found

    found = asyncio.run(run())
    assert [agent.name for agent in found.agents] == ["Listed"]
    assert found.complete is True
    assert requested.count(card) == 2
    assert agent_json not in requested

    requested.clear()

    def missing(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == agent_json:
            return httpx.Response(200, json=OTHER)
        return httpx.Response(404)

    async def run_fallback():
        transport = httpx.MockTransport(missing)
        with _quiet():
            async with httpx.AsyncClient(transport=transport) as client:
                return await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="otro",
                    client=client,
                    proposer=proposer,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )

    fallback = asyncio.run(run_fallback())
    assert [agent.name for agent in fallback.agents] == ["Other"]
    assert fallback.complete is True
    assert requested == [card, agent_json]


def test_directories_are_paged_and_nest_endpoints_become_cards():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.startswith("https://registry.example/agents"):
            offset = 0 if "offset=0" in url or "offset=" not in url else 2
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "agents": [
                            {"wellKnownURI": "https://reg-a.example/.well-known/agent-card.json"},
                            {"wellKnownURI": "https://reg-b.example/.well-known/agent-card.json"},
                        ],
                        "total": 3,
                        "limit": 2,
                        "offset": 0,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "agents": [{"wellKnownURI": "https://reg-c.example/.well-known/agent-card.json"}],
                    "total": 3,
                    "limit": 2,
                    "offset": 2,
                },
            )
        if url.startswith("https://nest.example/api/agents"):
            if "page=2" in url:
                return httpx.Response(
                    200,
                    json={"agents": [], "pagination": {"page": 2, "limit": 1, "total": 1, "hasNext": False}},
                )
            return httpx.Response(
                200,
                json={
                    "agents": [
                        {
                            "endpoint": "https://nest-agent.example/a2a",
                            "factsUrl": "https://facts.example/agentfacts.json",
                        }
                    ],
                    "pagination": {"page": 1, "limit": 1, "total": 1, "totalPages": 2, "hasNext": True},
                },
            )
        if url.endswith("nest-agent.example/.well-known/agent-card.json"):
            return httpx.Response(200, json=CARD)
        return httpx.Response(404)

    async def host_ok(host: str) -> bool:
        return host.endswith(".example")

    async def run():
        transport = httpx.MockTransport(handler)
        with _quiet(
            REGISTRY_URLS=("https://registry.example/agents?limit=2&offset=0",),
            NEST_URLS=("https://nest.example/api/agents?limit=1&page=1",),
        ):
            async with httpx.AsyncClient(transport=transport) as client:
                return await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="",
                    client=client,
                    proposer=None,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )

    found = asyncio.run(run())
    assert any("registry.example" in url and "offset=2" in url for url in requested)
    assert any("nest.example" in url and "page=2" in url for url in requested)
    assert any(url.endswith("nest-agent.example/.well-known/agent-card.json") for url in requested)
    assert any(url.endswith("facts.example/.well-known/agent-card.json") for url in requested)
    assert any(url.endswith("reg-c.example/.well-known/agent-card.json") for url in requested)
    assert [agent.name for agent in found.agents] == ["Listed"]
    assert found.complete is True


def test_seeds_get_an_early_warmup_request():
    card = "https://listed.example/.well-known/agent-card.json"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=CARD)

    async def host_ok(host: str) -> bool:
        return host == "listed.example"

    async def run():
        transport = httpx.MockTransport(handler)
        with _quiet(SEED_ORIGINS=("https://listed.example",)):
            async with httpx.AsyncClient(transport=transport) as client:
                return await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="",
                    client=client,
                    proposer=None,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )

    found = asyncio.run(run())
    assert [agent.name for agent in found.agents] == ["Listed"]
    assert found.complete is True
    assert requested.count(card) == 2
    assert not any(url.endswith("/agent.json") for url in requested)


def test_a_gateway_error_is_retried_once():
    card = "https://listed.example/.well-known/agent-card.json"
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502)

    async def host_ok(host: str) -> bool:
        return host == "listed.example"

    async def proposer(_query: str) -> list[str]:
        return [card]

    async def run():
        transport = httpx.MockTransport(handler)
        with _quiet():
            async with httpx.AsyncClient(transport=transport) as client:
                return await discover_agents(
                    public_url="https://mindr-a2a.onrender.com",
                    query="",
                    client=client,
                    proposer=proposer,
                    host_ok=host_ok,
                    cache=DiscoveryCache(),
                    budget_seconds=5,
                )

    found = asyncio.run(run())
    assert found.complete is False
    assert found.agents == ()
    assert attempts["n"] == 2
