"""Descubre agentes A2A públicos y solo devuelve tarjetas que pudo leer.

Combina estas fuentes, con un presupuesto de tiempo:
1. Semillas conocidas, entre ellas el agente hermano https://agent.agiq.top.
2. Registros y listas públicas (a2aregistry.org, AgentPeering, NANDA NEST,
   awesome-a2a). Los directorios se paginan dentro del presupuesto.
3. Claude, si hay clave, con la herramienta propose_candidates.

Cada candidato se descarga de /.well-known/agent-card.json. agent.json es el
respaldo si la primera ruta respondió y no era una tarjeta. Este agente no se
incluye en los resultados. Un resultado completo se cachea ~30 minutos; si
alguna tarjeta agotó el tiempo o la búsqueda quedó a medias, solo ~90 segundos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from a2a import validate_agent_card

log = logging.getLogger("mindr.discover")

USER_AGENT = "mindr-a2a-discovery/1.0 (+https://mindr.top)"
BUDGET_SECONDS = 40.0
SOURCE_BUDGET_SECONDS = 12.0
# Los directorios responden rápido; la tarjeta puede tardar si el free tier despierta.
SOURCE_REQUEST_TIMEOUT = 5.0
CARD_TIMEOUT = 18.0
WARMUP_TIMEOUT = 4.0
WARMUP_CONCURRENCY = 4
# No arrancar una página ni un reintento si queda menos de esto.
MIN_ATTEMPT_SECONDS = 1.0
CONCURRENCY = 8
MAX_RESULTS = 10
MAX_FETCH = 48
CACHE_TTL_SECONDS = 30 * 60
INCOMPLETE_CACHE_TTL_SECONDS = 90.0
CARD_BYTES = 500_000
SOURCE_BYTES = 1_000_000
# 502/503/504 los devuelve el proxy mientras un servicio free arranca.
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

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
    "https://a2aregistry.org/api/agents?limit=50&offset=0&task_verified=true",
    "https://agentpeering.com/api/agents?limit=30&offset=0",
)

# NEST pagina con page/limit. Con limit distinto de 100 la API ignora page.
NEST_URLS = (
    "https://nest.projectnanda.org/api/agents?limit=100&page=1",
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
    # False si alguna tarjeta agotó el tiempo, falló de forma transitoria
    # o una fuente se quedó a medias. Esa respuesta usa el TTL corto.
    complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents": [agent.as_dict() for agent in self.agents],
            "count": len(self.agents),
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
            "query": self.query,
            "complete": self.complete,
        }


class DiscoveryCache:
    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        incomplete_ttl_seconds: float = INCOMPLETE_CACHE_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.incomplete_ttl_seconds = incomplete_ttl_seconds
        self._now = now or time.monotonic
        # stored_at, ttl de esta entrada, resultado
        self._data: dict[str, tuple[float, float, DiscoveryResult]] = {}
        self._inflight: dict[str, asyncio.Future[DiscoveryResult]] = {}
        self._lock = asyncio.Lock()

    def _ttl_for(self, result: DiscoveryResult) -> float:
        if result.complete:
            return self.ttl_seconds
        return self.incomplete_ttl_seconds

    def _fresh(self, key: str) -> DiscoveryResult | None:
        item = self._data.get(key)
        if item is None:
            return None
        stored_at, ttl, result = item
        if self._now() - stored_at > ttl:
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
                self._data[key] = (self._now(), self._ttl_for(result), result)
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


def _endpoint_card_urls(raw: str) -> list[str]:
    """Tarjetas para un endpoint o factsUrl que no trae la URL de la card.

    Si el valor ya apunta a una tarjeta, se conserva. Además se prueban las
    dos rutas well-known del origen, aunque el path sea /a2a u otro.
    """
    text = raw.strip().strip("<>").rstrip(".,);")
    if not text or len(text) > 2000:
        return []
    direct = card_urls_for(text)
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return direct
    if parsed.username or parsed.password or parsed.port not in (None, 443, 80):
        return []
    host = parsed.hostname.lower().rstrip(".")
    if _host_blocked(host):
        return []
    origin = [
        f"https://{host}/.well-known/agent-card.json",
        f"https://{host}/.well-known/agent.json",
    ]
    return _dedupe(direct + origin)


def candidates_from_nanda(data: Any) -> list[str]:
    """NEST guarda endpoint y factsUrl, no la URL de la Agent Card."""
    agents = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(agents, list):
        return []
    found: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for key in ("endpoint", "factsUrl", "facts_url", "factsURL"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                found.extend(_endpoint_card_urls(value))
    return _dedupe(found)


def candidates_from_directory(data: Any) -> list[str]:
    return _dedupe(candidates_from_registry(data) + candidates_from_nanda(data))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _agent_count(data: Any) -> int:
    if isinstance(data, dict) and isinstance(data.get("agents"), list):
        return len(data["agents"])
    if isinstance(data, list):
        return len(data)
    return 0


def _with_query(url: str, **changes: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(changes)
    return urlunparse(parsed._replace(query=urlencode(query)))


def next_directory_url(url: str, data: Any) -> str | None:
    """Siguiente página offset/limit o page, si el cuerpo dice que hay más."""
    if not isinstance(data, dict):
        return None
    got = _agent_count(data)
    if got <= 0:
        return None
    query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
    has_next = pagination.get("hasNext")
    if has_next is False:
        return None
    total = pagination.get("total") if "total" in pagination else data.get("total")
    total_pages = pagination.get("totalPages")

    if "offset" in query:
        offset = _as_int(query.get("offset"))
        limit = _as_int(query.get("limit") or pagination.get("limit"))
        if offset is None:
            return None
        if limit is None or limit <= 0:
            limit = got
        if isinstance(total, int) and not isinstance(total, bool) and offset + got >= total:
            return None
        if has_next is not True and got < limit:
            return None
        return _with_query(url, offset=str(offset + limit))

    if "page" not in query and has_next is not True:
        return None
    page = _as_int(query.get("page") or pagination.get("page") or 1)
    limit = _as_int(query.get("limit") or pagination.get("limit"))
    if page is None:
        return None
    if limit is None or limit <= 0:
        limit = got
    if isinstance(total_pages, int) and not isinstance(total_pages, bool) and page >= total_pages:
        return None
    if (
        isinstance(total, int)
        and not isinstance(total, bool)
        and page * limit >= total
        and has_next is not True
    ):
        return None
    if has_next is True:
        return _with_query(url, page=str(page + 1))
    if isinstance(total_pages, int) and not isinstance(total_pages, bool) and page < total_pages:
        return _with_query(url, page=str(page + 1))
    if isinstance(total, int) and not isinstance(total, bool) and page * limit < total and got >= limit:
        return _with_query(url, page=str(page + 1))
    return None


def _max_directory_pages(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    if host == "nest.projectnanda.org":
        return 2
    if host == "agentpeering.com":
        return 4
    if host == "a2aregistry.org":
        return 6
    return 3


def _card_kind(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/").lower()
    if path.endswith("/.well-known/agent-card.json"):
        return "card"
    if path.endswith("/.well-known/agent.json"):
        return "agent"
    return None


def _split_fallbacks(urls: list[str]) -> tuple[list[str], dict[str, str]]:
    """agent.json no se pide a la vez que agent-card.json del mismo host.

    Si el servicio está frío, la segunda petición solo ocuparía otro hueco.
    Se intenta después, y solo si la tarjeta principal respondió sin ser válida.
    """
    hosts_with_card: set[str] = set()
    agent_for_host: dict[str, str] = {}
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        kind = _card_kind(url)
        if kind == "card":
            hosts_with_card.add(host)
        elif kind == "agent":
            agent_for_host.setdefault(host, url)
    primaries = [
        url
        for url in urls
        if not (
            _card_kind(url) == "agent"
            and (urlparse(url).hostname or "").lower() in hosts_with_card
        )
    ]
    card_for_host: dict[str, str] = {}
    for url in primaries:
        host = (urlparse(url).hostname or "").lower()
        if _card_kind(url) == "card":
            card_for_host.setdefault(host, url)
    fallbacks = {
        card_for_host[host]: agent_url
        for host, agent_url in agent_for_host.items()
        if host in card_for_host
    }
    return primaries, fallbacks


def _interleave(groups: list[list[str]]) -> list[str]:
    queues = [_dedupe(group) for group in groups]
    out: list[str] = []
    seen: set[str] = set()
    while any(queues):
        for queue in queues:
            if not queue:
                continue
            url = queue.pop(0)
            key = url.split("?", 1)[0].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
    return out


def _remember(bucket: list[str] | None, url: str) -> None:
    if bucket is not None:
        bucket.append(url)


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
) -> tuple[Any, str, bool]:
    """Devuelve (json, url final, incompleto).

    incompleto es un timeout, un corte de red o un estado transitorio.
    Un 404 u otro rechazo definitivo va con incompleto False.
    """
    if redirects > 2:
        return None, url, False
    if timeout <= 0.05:
        return None, url, True
    if not await _url_allowed(url, host_ok):
        return None, url, False
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return None, url, True
    final_url = str(response.request.url)
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            return None, final_url, False
        return await _fetch_json(
            client,
            urljoin(url, location),
            timeout,
            host_ok,
            redirects=redirects + 1,
            byte_limit=byte_limit,
        )
    if response.status_code in TRANSIENT_STATUS:
        return None, final_url, True
    if response.status_code != 200:
        return None, final_url, False
    ctype = response.headers.get("content-type", "")
    if "html" in ctype.lower():
        return None, final_url, False
    if len(response.content) > byte_limit:
        return None, final_url, False
    try:
        return response.json(), final_url, False
    except ValueError:
        return None, final_url, False


async def _fetch_text(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    host_ok: HostOk,
    *,
    redirects: int = 0,
) -> tuple[str, bool]:
    if redirects > 2 or not await _url_allowed(url, host_ok):
        return "", False
    if timeout <= 0.05:
        return "", True
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return "", True
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            return "", False
        return await _fetch_text(
            client,
            urljoin(url, location),
            timeout,
            host_ok,
            redirects=redirects + 1,
        )
    if response.status_code in TRANSIENT_STATUS:
        return "", True
    if response.status_code != 200 or len(response.content) > SOURCE_BYTES:
        return "", False
    ctype = response.headers.get("content-type", "")
    if "html" in ctype.lower():
        return "", False
    return response.text, False


def _expand(raws: list[str]) -> list[str]:
    urls: list[str] = []
    for raw in raws:
        urls.extend(card_urls_for(raw))
    return urls


async def _walk_directory(
    client: httpx.AsyncClient,
    start_url: str,
    host_ok: HostOk,
    deadline: float,
) -> tuple[list[str], bool]:
    found: list[str] = []
    incomplete = False
    url: str | None = start_url
    seen: set[str] = set()
    for _page in range(_max_directory_pages(start_url)):
        if not url or url in seen:
            break
        seen.add(url)
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_SECONDS:
            incomplete = True
            break
        data, _final, failed = await _fetch_json(
            client,
            url,
            min(SOURCE_REQUEST_TIMEOUT, remaining),
            host_ok,
            byte_limit=SOURCE_BYTES,
        )
        if failed:
            log.info("directory page failed: %s", url)
            incomplete = True
            break
        if data is None:
            break
        found.extend(_expand(candidates_from_directory(data)))
        url = next_directory_url(url, data)
    return found, incomplete


async def _collect_sources(
    client: httpx.AsyncClient,
    query: str,
    proposer: Proposer | None,
    host_ok: HostOk,
    deadline: float,
) -> tuple[list[str], dict[str, str], bool]:
    source_deadline = min(deadline, time.monotonic() + SOURCE_BUDGET_SECONDS)

    async def directories() -> tuple[list[list[str]], bool]:
        starts = (*REGISTRY_URLS, *NEST_URLS)
        if not starts:
            return [], False
        walked = await asyncio.gather(
            *[_walk_directory(client, url, host_ok, source_deadline) for url in starts]
        )
        return [found for found, _failed in walked], any(failed for _found, failed in walked)

    async def awesome() -> tuple[list[str], bool]:
        found: list[str] = []
        incomplete = False
        for url in AWESOME_URLS:
            remaining = source_deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_SECONDS:
                incomplete = True
                break
            text, failed = await _fetch_text(
                client, url, min(SOURCE_REQUEST_TIMEOUT, remaining), host_ok
            )
            if failed:
                incomplete = True
                continue
            if text:
                found.extend(_expand(candidates_from_markdown(text)))
        return found, incomplete

    async def proposed() -> tuple[list[str], bool]:
        if proposer is None:
            return [], False
        remaining = source_deadline - time.monotonic()
        if remaining <= 0.4:
            return [], True
        try:
            raws = await asyncio.wait_for(proposer(query), timeout=min(8.0, remaining))
        except Exception as exc:
            log.info("proposer skipped: %s", type(exc).__name__)
            return [], True
        if not isinstance(raws, list):
            return [], True
        return _expand([item for item in raws if isinstance(item, str)]), False

    (groups, directory_bad), (awesome_urls, awesome_bad), (claude_urls, claude_bad) = await asyncio.gather(
        directories(), awesome(), proposed()
    )
    seed_primaries, seed_fallbacks = _split_fallbacks(_expand(list(SEED_ORIGINS)))
    directory_primaries, directory_fallbacks = _split_fallbacks(_interleave(groups))
    awesome_primaries, awesome_fallbacks = _split_fallbacks(awesome_urls)
    claude_primaries, claude_fallbacks = _split_fallbacks(claude_urls)
    chosen = select_card_urls(
        seed_primaries, directory_primaries, awesome_primaries, claude_primaries
    )
    chosen_set = set(chosen)
    fallbacks: dict[str, str] = {}
    for mapping in (seed_fallbacks, directory_fallbacks, awesome_fallbacks, claude_fallbacks):
        for card_url, agent_url in mapping.items():
            if card_url in chosen_set:
                fallbacks[card_url] = agent_url
    return chosen, fallbacks, directory_bad or awesome_bad or claude_bad


def _warmup_urls(origins: tuple[str, ...] | list[str]) -> list[str]:
    """Una petición por semilla, a la tarjeta, para despertar el servicio."""
    urls: list[str] = []
    seen: set[str] = set()
    for raw in origins:
        cards = card_urls_for(raw)
        if not cards:
            continue
        host = (urlparse(cards[0]).hostname or "").lower()
        if not host or host in seen:
            continue
        seen.add(host)
        urls.append(cards[0])
    return urls


async def _warmup(
    client: httpx.AsyncClient,
    urls: list[str],
    host_ok: HostOk,
) -> None:
    if not urls:
        return
    semaphore = asyncio.Semaphore(WARMUP_CONCURRENCY)

    async def one(url: str) -> None:
        async with semaphore:
            try:
                if not await _url_allowed(url, host_ok):
                    return
                await client.get(url, timeout=WARMUP_TIMEOUT, follow_redirects=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    await asyncio.gather(*(one(url) for url in urls))


async def _probe(
    client: httpx.AsyncClient,
    url: str,
    public_url: str,
    host_ok: HostOk,
    deadline: float,
    semaphore: asyncio.Semaphore,
    retryable: list[str] | None = None,
    skipped: list[str] | None = None,
    misses: list[str] | None = None,
) -> FoundAgent | None:
    if is_self(url, public_url):
        return None
    async with semaphore:
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            _remember(skipped, url)
            return None
        data, final_url, failed = await _fetch_json(
            client, url, min(CARD_TIMEOUT, remaining), host_ok
        )
    if failed:
        _remember(retryable, url)
        return None
    if is_self(final_url, public_url):
        return None
    parsed = validate_agent_card(data)
    if parsed is None:
        # Respondió (o no había tarjeta). agent.json puede ser el respaldo.
        _remember(misses, url)
        return None
    if is_self(parsed["url"], public_url):
        return None
    return FoundAgent(
        name=parsed["name"],
        description=parsed["description"],
        card_url=final_url,
        url=parsed["url"],
    )


async def _probe_all(
    client: httpx.AsyncClient,
    urls: list[str],
    public_url: str,
    host_ok: HostOk,
    deadline: float,
) -> tuple[list[FoundAgent], list[str], bool]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    retryable: list[str] = []
    skipped: list[str] = []
    misses: list[str] = []

    async def once(url: str) -> FoundAgent | None:
        return await _probe(
            client,
            url,
            public_url,
            host_ok,
            deadline,
            semaphore,
            retryable=retryable,
            skipped=skipped,
            misses=misses,
        )

    found = [agent for agent in await asyncio.gather(*(once(url) for url in urls)) if agent is not None]
    pending = _dedupe([*retryable, *skipped])
    incomplete = False
    if pending:
        retryable.clear()
        skipped.clear()
        if deadline - time.monotonic() > MIN_ATTEMPT_SECONDS:
            log.info("retrying %s slow candidates", len(pending))
            extra = [
                agent
                for agent in await asyncio.gather(*(once(url) for url in pending))
                if agent is not None
            ]
            found.extend(extra)
            incomplete = bool(retryable or skipped)
        else:
            incomplete = True
    return found, misses, incomplete


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
    # La petición corta sale ya, en paralelo con los directorios, para que un
    # servicio dormido lleve unos segundos despierto cuando se lea la tarjeta.
    warm = asyncio.create_task(_warmup(client, _warmup_urls(SEED_ORIGINS), host_ok))
    try:
        urls, fallbacks, sources_bad = await _collect_sources(
            client, query, proposer, host_ok, deadline
        )
        found, misses, probe_bad = await _probe_all(
            client, urls, public_url, host_ok, deadline
        )
        fallback_urls = _dedupe([fallbacks[url] for url in misses if url in fallbacks])
        fallback_bad = False
        if fallback_urls:
            if deadline - time.monotonic() > MIN_ATTEMPT_SECONDS:
                extra, _fallback_misses, fallback_bad = await _probe_all(
                    client, fallback_urls, public_url, host_ok, deadline
                )
                found.extend(extra)
            else:
                fallback_bad = True
        agents = _unique_agents(found)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        complete = not (sources_bad or probe_bad or fallback_bad)
        log.info(
            "discovery found %s agents in %sms (candidates=%s complete=%s)",
            len(agents),
            elapsed_ms,
            len(urls),
            complete,
        )
        return DiscoveryResult(
            agents=agents,
            cached=False,
            elapsed_ms=elapsed_ms,
            query=query,
            complete=complete,
        )
    finally:
        if not warm.done():
            warm.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm


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
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
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
