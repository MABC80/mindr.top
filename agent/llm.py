"""Llamadas a Claude con el SDK oficial de Anthropic.

Sin ANTHROPIC_API_KEY el agente sigue sirviendo la tarjeta y el descubrimiento
por semillas y registros. La conversación libre explica que falta la clave.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("mindr.llm")

PROPOSE_TOOL = {
    "name": "propose_candidates",
    "description": (
        "Propose public HTTPS URLs of A2A (Agent2Agent) agents. Each item is "
        "either an origin like https://example.com or a card URL ending in "
        "/.well-known/agent-card.json or /.well-known/agent.json."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Public HTTPS origins or agent-card URLs.",
            }
        },
        "required": ["candidates"],
    },
}

_SYSTEM = (
    "Eres Mindr, un agente A2A público de mindr.top. Respondes en el idioma "
    "del usuario, con frases claras y breves. Si no sabes algo, lo dices. "
    "No inventes agentes, tarjetas ni URLs."
)

_PROPOSE_SYSTEM = (
    "Conoces el protocolo A2A (Agent2Agent). Usa solo la herramienta "
    "propose_candidates. Incluye como máximo 8 URLs HTTPS de agentes públicos "
    "que publiquen una Agent Card, y solo si los recuerdas de registros, "
    "listas awesome-a2a o documentación. Sirve un origen (https://host) o la "
    "tarjeta (/.well-known/agent-card.json o /.well-known/agent.json). "
    "No incluyas mindr-a2a.onrender.com ni hosts privados. No inventes dominios."
)


class LLM:
    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or "claude-haiku-5-5"
        self._client = client

    def configured(self) -> bool:
        return bool(self.api_key) or self._client is not None

    def _anthropic(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.api_key:
            return None
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=self.api_key, timeout=12.0)
        return self._client

    async def answer(self, text: str) -> str:
        client = self._anthropic()
        if client is None:
            return (
                "El modelo no está configurado: falta ANTHROPIC_API_KEY en el servidor. "
                "Puedes pedir 'discover' para buscar otros agentes A2A sin el modelo."
            )
        prompt = text.strip()[:8000]
        try:
            message = await client.messages.create(
                model=self.model,
                max_tokens=700,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            log.warning("anthropic answer failed: %s", type(exc).__name__)
            return "No pude consultar el modelo en este momento."
        return _text_from(message) or "No tengo una respuesta para eso."

    async def propose_candidates(self, query: str) -> list[str]:
        client = self._anthropic()
        if client is None:
            return []
        hint = query.strip()[:300]
        user = (
            "Propón agentes A2A públicos que pueda verificar descargando su Agent Card."
        )
        if hint:
            user += f" Si puedes, prioriza agentes relacionados con: {hint}."
        try:
            message = await client.messages.create(
                model=self.model,
                max_tokens=400,
                system=_PROPOSE_SYSTEM,
                tools=[PROPOSE_TOOL],
                tool_choice={"type": "tool", "name": "propose_candidates"},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            log.warning("anthropic propose failed: %s", type(exc).__name__)
            return []
        return _candidates_from(message)[:8]


def _text_from(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _candidates_from(message: Any) -> list[str]:
    found: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) not in {None, "propose_candidates"}:
            continue
        raw = getattr(block, "input", None)
        values: Any = []
        if isinstance(raw, dict):
            values = raw.get("candidates") or raw.get("urls") or []
        elif isinstance(raw, list):
            values = raw
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if "://" in text or ("." in text and " " not in text):
                found.append(text)
    return found
