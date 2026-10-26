"""Agent Card, validación y JSON-RPC message/send del agente Mindr."""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlparse

JSONRPC_METHODS = {"message/send", "SendMessage", "tasks/send"}

# Pide descubrimiento si el texto es una orden corta de buscar agentes A2A.
_DISCOVER_START = re.compile(
    r"^\s*(discover|discovery|descubre|encuentra|busca)\b",
    re.IGNORECASE,
)
_DISCOVER_PHRASE = re.compile(
    r"\b(discover|discovery|descubre|encuentra|busca)\b.{0,60}\b(agentes?|agents?|a2a)\b"
    r"|\b(agentes?|agents?)\b.{0,40}\ba2a\b.{0,40}\b(discover|descubre|busca|encuentra)\b",
    re.IGNORECASE | re.DOTALL,
)


def build_agent_card(public_url: str) -> dict[str, Any]:
    """Tarjeta compatible con A2A 0.3 (url) y con supportedInterfaces de 1.0."""
    base = public_url.rstrip("/")
    endpoint = f"{base}/"
    return {
        "protocolVersion": "0.3.0",
        "name": "Mindr",
        "description": (
            "Agente A2A de mindr.top. Responde en texto con Claude y, si se lo "
            "pides, descubre otros agentes A2A públicos cuya tarjeta pudo leer."
        ),
        "url": endpoint,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "provider": {"organization": "mindr", "url": "https://mindr.top"},
        "documentationUrl": "https://mindr.top",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "chat",
                "name": "Conversar",
                "description": "Responde preguntas en texto usando Claude.",
                "tags": ["chat", "claude"],
                "examples": ["¿Qué es el protocolo A2A?"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
            {
                "id": "discover",
                "name": "Descubrir agentes",
                "description": (
                    "Busca agentes A2A públicos y devuelve hasta 10 tarjetas "
                    "que pudo descargar y validar."
                ),
                "tags": ["discovery", "a2a"],
                "examples": ["discover", "encuentra agentes A2A"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
        ],
        "supportedInterfaces": [
            {
                "url": endpoint,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "0.3",
            }
        ],
    }


def _public_host(host: str) -> bool:
    name = host.lower().rstrip(".")
    if name in {"localhost", "0.0.0.0", "metadata.google.internal"}:
        return False
    if name.endswith(".local") or name.endswith(".internal") or name.endswith(".home.arpa"):
        return False
    try:
        import ipaddress

        ip = ipaddress.ip_address(name)
    except ValueError:
        return True
    return bool(ip.is_global)


def _https_endpoint(value: str) -> str | None:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if not _public_host(parsed.hostname):
        return None
    return text


def extract_endpoint(card: dict[str, Any]) -> str | None:
    """URL JSON-RPC del agente: campo url (0.3) o supportedInterfaces (1.0)."""
    ordered: list[tuple[int, str]] = []
    raw_url = card.get("url")
    if isinstance(raw_url, str):
        ordered.append((0, raw_url))
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list):
        interfaces = card.get("additionalInterfaces")
    if isinstance(interfaces, list):
        for item in interfaces:
            if not isinstance(item, dict):
                continue
            raw = item.get("url")
            if not isinstance(raw, str):
                continue
            binding = str(item.get("protocolBinding") or item.get("transport") or "")
            rank = 0 if binding.upper().replace("-", "").replace("_", "") in {"JSONRPC", ""} else 1
            ordered.append((rank, raw))
    ordered.sort(key=lambda item: item[0])
    for _, raw in ordered:
        endpoint = _https_endpoint(raw)
        if endpoint:
            return endpoint
    return None


def _has_skills(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for skill in value:
        if not isinstance(skill, dict):
            continue
        label = skill.get("id") or skill.get("name")
        if isinstance(label, str) and label.strip():
            return True
    return False


def _has_capabilities(value: Any) -> bool:
    if isinstance(value, dict):
        return len(value) > 0
    if isinstance(value, list):
        return len(value) > 0
    return False


def validate_agent_card(data: Any) -> dict[str, str] | None:
    """Devuelve name, description y url si la tarjeta parece una Agent Card A2A.

    Exige name, un endpoint https (url o supportedInterfaces) y skills o
    capabilities. No basta con el anuncio de un registro: el llamador tiene
    que haber descargado este JSON.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    endpoint = extract_endpoint(data)
    if not endpoint:
        return None
    if not (_has_skills(data.get("skills")) or _has_capabilities(data.get("capabilities"))):
        return None
    description = data.get("description")
    if not isinstance(description, str):
        description = ""
    return {
        "name": name.strip()[:160],
        "description": description.strip()[:600],
        "url": endpoint,
    }


def extract_text(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    chunks: list[str] = []
    message = params.get("message")
    parts: Any = None
    if isinstance(message, dict):
        parts = message.get("parts")
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
    elif isinstance(message, str):
        chunks.append(message)
    if parts is None:
        parts = params.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = str(part.get("kind") or part.get("type") or "text").lower()
            if kind not in {"text", "text/plain"}:
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    for key in ("text", "content"):
        value = params.get(key)
        if isinstance(value, str):
            chunks.append(value)
    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip()).strip()


def context_id_from(params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    message = params.get("message")
    if isinstance(message, dict):
        for key in ("contextId", "context_id"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def wants_discovery(text: str, params: Any) -> bool:
    if isinstance(params, dict):
        for source in (params, params.get("metadata") if isinstance(params.get("metadata"), dict) else {}):
            skill = source.get("skill") or source.get("skillId") or source.get("skill_id")
            if isinstance(skill, str) and skill.strip().lower() == "discover":
                return True
        message = params.get("message")
        if isinstance(message, dict):
            metadata = message.get("metadata")
            if isinstance(metadata, dict):
                skill = metadata.get("skill") or metadata.get("skillId")
                if isinstance(skill, str) and skill.strip().lower() == "discover":
                    return True
    stripped = text.strip()
    if not stripped or len(stripped) > 280:
        return False
    # Orden corta: "discover", "descubre agentes", "busca agentes A2A de clima".
    if re.fullmatch(
        r"(discover|discovery|descubre|encuentra|busca)(\s+(agentes?|agents?|a2a)\b.*)?",
        stripped,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(_DISCOVER_PHRASE.search(stripped))


def discovery_query(text: str) -> str:
    """Quita la orden ('discover', 'busca agentes') y deja la pista, si hay."""
    cleaned = _DISCOVER_START.sub("", text.strip(), count=1).strip(" :,-")
    cleaned = re.sub(
        r"^(agentes?|agents?|a2a)\b",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip(" :,-")
    return cleaned[:300]


def discovery_in_english(text: str) -> bool:
    return bool(re.match(r"^\s*(discover|discovery|find)\b", text.strip(), re.IGNORECASE))


def format_discovery(agents: list[dict[str, str]], *, english: bool) -> str:
    if not agents:
        if english:
            return "I could not find public A2A agents with a valid agent card right now."
        return "No encontré agentes A2A públicos con una tarjeta válida en este momento."
    lines = []
    if english:
        lines.append(f"Found {len(agents)} public A2A agent(s) with a fetched, valid card:")
    else:
        lines.append(f"Encontré {len(agents)} agente(s) A2A público(s) con tarjeta válida:")
    for index, agent in enumerate(agents, start=1):
        description = agent.get("description") or ""
        if len(description) > 240:
            description = description[:237].rstrip() + "..."
        lines.append("")
        lines.append(f"{index}. {agent.get('name', '')}")
        if description:
            lines.append(f"   {description}")
        card = agent.get("card_url") or ""
        endpoint = agent.get("url") or ""
        if english:
            lines.append(f"   Card: {card}")
            lines.append(f"   Endpoint: {endpoint}")
        else:
            lines.append(f"   Tarjeta: {card}")
            lines.append(f"   Endpoint: {endpoint}")
    return "\n".join(lines)


def build_message(text: str, context_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id:
        message["contextId"] = context_id
    return message


def jsonrpc_result(req_id: Any, method: str, message: dict[str, Any]) -> dict[str, Any]:
    # SendMessage (binding 1.0) envuelve el mensaje. message/send (0.3) lo devuelve tal cual.
    if method == "SendMessage":
        result: Any = {"message": message}
    else:
        result = message
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
