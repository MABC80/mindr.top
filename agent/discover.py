"""Descubre agentes A2A públicos y solo devuelve tarjetas que pudo leer.

Combina tres fuentes, con un presupuesto de tiempo:
1. Semillas conocidas, entre ellas el agente hermano https://agent.agiq.top.
2. Registros y listas públicas (a2aregistry.org, AgentPeering, awesome-a2a).
3. Claude, si hay clave, con la herramienta propose_candidates.

Cada candidato se descarga de /.well-known/agent-card.json o agent.json.
Este agente no se incluye en los resultados. Hay caché en memoria para no
repetir la búsqueda ni la llamada al modelo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx

from a2a import validate_agent_card

log = logging.getLogger("mindr.discover")

USER_AGENT = "mindr-a2a-discovery/1.0 (+https://mindr.top)"
BUDGET_SECONDS = 40.0
SOURCE_BUDGET_SECONDS = 10.0
PER_REQUEST_TIMEOUT = 5.0
CONCURRENCY = 8
MAX_RESULTS = 10
MAX_FETCH = 36
CACHE_TTL_SECONDS = 30 * 60
CARD_BYTES = 500_000
SOURCE_BYTES = 1_000_000

# El hermano en agent.agiq.top puede no estar desplegado todavía: se intenta igual.
SEED_ORIGINS = (
    "https://agent.agiq.top",
    "https://agentpeering.com",
    "https://a2a.atlassian.com",
    "https://walnutlink.ai",
    "https://perkoon.com",
    "https://numbers.online",
    "https://api.cogdepot.com",
    "https://bitcoinsapi.com",
    "https://api.voidly.ai",
    "https://aureliusagent.dev",
)

REGISTRY_URLS = (
    "https://a2aregistry.org/api/agents?limit=12&task_verified=true",
    "https://agentpeering.com/api/agents?limit=12",
)

AWESOME_URLS = (
    "https://raw.githubusercontent.com/pab1it0/awesome-a2a/main/README.md",
    "https://raw.githubusercontent.com/ai-boost/awesome-a2a/main/README.md",
    "https://raw.githubusercontent.com/inference-gateway/awesome-a2a/main/README.md",
)

# Hostname público fijo, además del host de PUBLIC_URL (is_self compara ambos).
SELF_HOSTS = {"mindr-a2a.onrender.com"}

_CARD_URL = re.compile(
    r"https?://[^\s`<>\"')\]]+/\.well-known/agent(?:-card)?\.json",
    re.IGNORECASE,
)
_BARE_CARD = re.compile(
    r"(?<![:/\w])((?:[a-z0-9-]+\.)+[a-z]{2,})(/\.well-known/agent(?:-card)?\.json)",
    re.IGNORECASE,
)

HostOk = Callable[[str], Awaitable[bool]]
Proposer = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class FoundAgent:
    name: str
    description: str
    card_url: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    agents: tuple[FoundAgent, ...]
    cached: bool
    elapsed_ms: int
    query: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents": [agent.as_dict() for agent in self.agents],
            "count": len(self.agents),
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
            "query": self.query,
        }


class DiscoveryCache:
    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, tuple[float, DiscoveryResult]] = {}
        self._inflight: dict[str, asyncio.Future[DiscoveryResult]] = {}
        self._lock = asyncio.Lock()

    def _fresh(self, key: str) -> DiscoveryResult | None:
        item = self._data.get(key)
        if item is None:
            return None
        stored_at, result = item
        if time.monotonic() - stored_at > self.ttl_seconds:
            self._data.pop(key, None)
            return None
        return replace(result, cached=True, elapsed_ms=0)

    async def get_or_run(
        self,
        key: str,
        factory: Callable[[], Awaitable[DiscoveryResult]],
    ) -> DiscoveryResult:
        async with self._lock:
            hit = self._fresh(key)
            if hit is not None:
                return hit
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                leader = True
            else:
                leader = False
        if not leader:
            result = await future
            return replace(result, cached=True, elapsed_ms=0)
        try:
            result = await factory()
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            async with self._lock:
                self._data[key] = (time.monotonic(), result)
            if not future.done():
                future.set_result(result)
            return result
        finally:
            async with self._lock:
                self._inflight.pop(key, None)


DEFAULT_CACHE = DiscoveryCache()


def card_urls_for(raw: str) -> list[str]:
    """Convierte un origen o una URL de tarjeta en una o dos URLs https."""
    text = raw.strip().strip("<>").rstrip(".,);")
    if not text or len(text) > 300:
        return []
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    if parsed.username or parsed.password or parsed.port not in (None, 443, 80):
        return []
    host = parsed.hostname.lower().rstrip(".")
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    lowered = path.rstrip("/").lower()
    if lowered.endswith("/.well-known/agent-card.json") or lowered.endswith("/.well-known/agent.json"):
        return [f"https://{host}{path}{query}"]
    if path not in {"", "/"}:
        return []
    return [
        f"https://{host}/.well-known/agent-card.json",
        f"https://{host}/.well-known/agent.json",
    ]


def candidates_from_registry(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    agents = data.get("agents")
    if not isinstance(agents, list):
        return []
    found: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for key in ("wellKnownURI", "well_known_uri", "card_url", "cardUrl", "agentCardUrl"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
                break
    return found


def candidates_from_markdown(text: str) -> list[str]:
    found = [match.rstrip(".,);") for match in _CARD_URL.findall(text)]
    for host, path in _BARE_CARD.findall(text):
        found.append(f"https://{host}{path}")
    return found


def select_card_urls(
    seeds: list[str],
    registries: list[str],
    awesome: list[str],
    proposed: list[str],
    *,
    limit: int = MAX_FETCH,
) -> list[str]:
    """Reserva sitio para lo que proponga Claude y deduplica el resto."""
    claude = _dedupe(proposed)[:6]
    room = max(0, limit - len(claude))
    ordered = _dedupe(seeds + registries + awesome)
    return _dedupe(ordered[:room] + claude)[:limit]


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url.split("?", 1)[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def is_self(url: str, public_url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    public_host = (urlparse(public_url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    return host in SELF_HOSTS or (bool(public_host) and host == public_host)


def _host_blocked(host: str) -> bool:
    name = host.lower().rstrip(".")
    if name in {"localhost", "0.0.0.0", "metadata.google.internal"}:
        return True
    if name.endswith(".local") or name.endswith(".internal") or name.endswith(".home.arpa"):
        return True
    try:
        ip = ip_address(name)
    except ValueError:
        return False
    return not ip.is_global


def ip_address(value: str):
    import ipaddress

    return ipaddress.ip_address(value)


async def host_is_public(host: str) -> bool:
    name = host.lower().rstrip(".")
    if not name or _host_blocked(name):
        return False
    try:
        ip_address(name)
        return True
    except ValueError:
        pass
    try:
        name.encode("idna")
    except UnicodeError:
        return False

    def lookup() -> list[Any]:
        return socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)

    try:
        infos = await asyncio.wait_for(asyncio.to_thread(lookup), timeout=3)
    except (TimeoutError, socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


async def _url_allowed(url: str, host_ok: HostOk) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    return await host_ok(parsed.hostname)


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    host_ok: HostOk,
    *,
    redirects: int = 0,
    byte_limit: int = CARD_BYTES,
) -> tuple[Any, str] | None:
    if redirects > 2 or timeout <= 0.05:
        return None
    if not await _url_allowed(url, host_ok):
        return None
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            return None
        return await _fetch_json(
            client,
            urljoin(url, location),
            timeout,
            host_ok,
            redirects=redirects + 1,
            byte_limit=byte_limit,
        )
    if response.status_code != 200:
        return None
    ctype = response.headers.get("content-type", "")
    if "html" in ctype.lower():
        return None
    if len(response.content) > byte_limit:
        return None
    try:
        return response.json(), str(response.request.url)
    except ValueError:
        return None


async def _fetch_text(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    host_ok: HostOk,
    *,
    redirects: int = 0,
) -> str:
    if redirects > 2 or timeout <= 0.05 or not await _url_allowed(url, host_ok):
        return ""
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return ""
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            return ""
        return await _fetch_text(
            client,
            urljoin(url, location),
            timeout,
            host_ok,
            redirects=redirects + 1,
        )
    if response.status_code != 200 or len(response.content) > SOURCE_BYTES:
        return ""
    ctype = response.headers.get("content-type", "")
    if "html" in ctype.lower():
        return ""
    return response.text


def _expand(raws: list[str]) -> list[str]:
    urls: list[str] = []
    for raw in raws:
        urls.extend(card_urls_for(raw))
    return urls


async def _collect_sources(
    client: httpx.AsyncClient,
    query: str,
    proposer: Proposer | None,
    host_ok: HostOk,
    deadline: float,
) -> list[str]:
    source_deadline = min(deadline, time.monotonic() + SOURCE_BUDGET_SECONDS)

    async def registries() -> list[str]:
        found: list[str] = []
        for url in REGISTRY_URLS:
            remaining = source_deadline - time.monotonic()
            if remaining <= 0.2:
                break
            fetched = await _fetch_json(
                client,
                url,
                min(PER_REQUEST_TIMEOUT, remaining),
                host_ok,
                byte_limit=SOURCE_BYTES,
            )
            if fetched is None:
                continue
            found.extend(_expand(candidates_from_registry(fetched[0])))
        return found

    async def awesome() -> list[str]:
        found: list[str] = []
        for url in AWESOME_URLS:
            remaining = source_deadline - time.monotonic()
            if remaining <= 0.2:
                break
            text = await _fetch_text(client, url, min(PER_REQUEST_TIMEOUT, remaining), host_ok)
            if text:
                found.extend(_expand(candidates_from_markdown(text)))
        return found

    async def proposed() -> list[str]:
        if proposer is None:
            return []
        remaining = source_deadline - time.monotonic()
        if remaining <= 0.4:
            return []
        try:
            raws = await asyncio.wait_for(proposer(query), timeout=min(8.0, remaining))
        except Exception as exc:
            log.info("proposer skipped: %s", type(exc).__name__)
            return []
        if not isinstance(raws, list):
            return []
        return _expand([item for item in raws if isinstance(item, str)])

    registry_urls, awesome_urls, claude_urls = await asyncio.gather(
        registries(), awesome(), proposed()
    )
    seeds = _expand(list(SEED_ORIGINS))
    return select_card_urls(seeds, registry_urls, awesome_urls, claude_urls)


async def _probe(
    client: httpx.AsyncClient,
    url: str,
    public_url: str,
    host_ok: HostOk,
    deadline: float,
    semaphore: asyncio.Semaphore,
) -> FoundAgent | None:
    if is_self(url, public_url):
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        return None
    timeout = min(PER_REQUEST_TIMEOUT, remaining)
    async with semaphore:
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            return None
        fetched = await _fetch_json(client, url, min(timeout, remaining), host_ok)
    if fetched is None:
        return None
    data, final_url = fetched
    if is_self(final_url, public_url):
        return None
    parsed = validate_agent_card(data)
    if parsed is None:
        return None
    if is_self(parsed["url"], public_url):
        return None
    return FoundAgent(
        name=parsed["name"],
        description=parsed["description"],
        card_url=final_url,
        url=parsed["url"],
    )


def _unique_agents(agents: list[FoundAgent]) -> tuple[FoundAgent, ...]:
    seen: set[str] = set()
    chosen: list[FoundAgent] = []
    for agent in agents:
        key = agent.url.split("?", 1)[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        chosen.append(agent)
        if len(chosen) >= MAX_RESULTS:
            break
    return tuple(chosen)


async def run_discovery(
    *,
    public_url: str,
    query: str = "",
    client: httpx.AsyncClient,
    proposer: Proposer | None = None,
    host_ok: HostOk = host_is_public,
    budget_seconds: float = BUDGET_SECONDS,
) -> DiscoveryResult:
    started = time.monotonic()
    deadline = started + budget_seconds
    urls = await _collect_sources(client, query, proposer, host_ok, deadline)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    probed = await asyncio.gather(
        *[
            _probe(client, url, public_url, host_ok, deadline, semaphore)
            for url in urls
        ]
    )
    agents = _unique_agents([agent for agent in probed if agent is not None])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info("discovery found %s agents in %sms (candidates=%s)", len(agents), elapsed_ms, len(urls))
    return DiscoveryResult(agents=agents, cached=False, elapsed_ms=elapsed_ms, query=query)


async def discover_agents(
    *,
    public_url: str,
    query: str = "",
    client: httpx.AsyncClient | None = None,
    proposer: Proposer | None = None,
    host_ok: HostOk = host_is_public,
    cache: DiscoveryCache | None = None,
    budget_seconds: float = BUDGET_SECONDS,
) -> DiscoveryResult:
    store = cache if cache is not None else DEFAULT_CACHE
    key = f"{public_url.rstrip('/').lower()}|{query.strip().lower()[:300]}"

    async def factory() -> DiscoveryResult:
        if client is not None:
            return await run_discovery(
                public_url=public_url,
                query=query,
                client=client,
                proposer=proposer,
                host_ok=host_ok,
                budget_seconds=budget_seconds,
            )
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.8"},
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        ) as owned:
            return await run_discovery(
                public_url=public_url,
                query=query,
                client=owned,
                proposer=proposer,
                host_ok=host_ok,
                budget_seconds=budget_seconds,
            )

    return await store.get_or_run(key, factory)


async def _main() -> None:
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = await discover_agents(public_url="https://mindr-a2a.onrender.com", proposer=None)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
