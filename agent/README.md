# Agente A2A de Mindr

Servicio pequeño que habla el protocolo [A2A](https://a2a-protocol.org/latest/specification/) (Agent2Agent). Responde `message/send` con Claude y puede buscar otros agentes A2A públicos.

La página estática de https://mindr.top llama a `GET /discover` en https://mindr-a2a.onrender.com. Este directorio es el servicio que se despliega en Render. La clave de Anthropic no va en el repositorio: solo existe en la variable `ANTHROPIC_API_KEY` del host.

El modelo por defecto es `claude-haiku-5-5` (el alias `claude-3-5-haiku-latest` está retirado). Se cambia con `ANTHROPIC_MODEL`.

## Arranque local

Hace falta Python 3.12.

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PUBLIC_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-ant-...   # opcional para charlar; no la guardes en git
uvicorn app:app --host 127.0.0.1 --port 8000
```

Sin `ANTHROPIC_API_KEY` siguen funcionando la tarjeta, `/health` y `/discover` (semillas, registros y listas públicas). `message/send` de conversación responde que falta la clave. Una petición de descubrimiento (`discover`, `encuentra agentes A2A`) no necesita el modelo.

Pruebas, también sin clave:

```bash
pip install -r requirements-dev.txt
pytest
```

Para repetir el descubrimiento real contra internet:

```bash
python discover.py
```

## Pruebas con curl

Sustituye el host por `http://127.0.0.1:8000` si lo corres en local. `PUBLIC_URL` debe coincidir con esa base para que la tarjeta anuncie la URL correcta.

Tarjeta del agente (las dos rutas canónicas):

```bash
curl -sS https://mindr-a2a.onrender.com/.well-known/agent-card.json
curl -sS https://mindr-a2a.onrender.com/.well-known/agent.json
curl -sS https://mindr-a2a.onrender.com/health
```

Conversación JSON-RPC 2.0 (`message/send`). El mismo cuerpo también se acepta en `POST /a2a`:

```bash
curl -sS https://mindr-a2a.onrender.com/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "kind": "message",
        "role": "user",
        "messageId": "msg-1",
        "parts": [{"kind": "text", "text": "Hola, ¿qué puedes hacer?"}]
      }
    }
  }'
```

Descubrimiento como endpoint y como mensaje A2A. Puede tardar unos 40 segundos; en el plan free de Render, el primer intento tras un rato de inactividad puede acercarse a un minuto mientras el proceso despierta. La respuesta queda en caché unos 30 minutos.

```bash
curl -sS https://mindr-a2a.onrender.com/discover
curl -sS https://mindr-a2a.onrender.com/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "msg-2",
        "parts": [{"kind": "text", "text": "discover"}]
      }
    }
  }'
```

`/discover` devuelve como máximo 10 agentes. Cada uno tiene `name`, `description`, `card_url` (la tarjeta que se descargó) y `url` (el endpoint del agente). Solo entra en la lista si esa tarjeta se obtuvo por HTTP y trae nombre, un endpoint https, y skills o capabilities. Este propio agente no aparece.

## Cómo descubre

1. Semillas fijas, incluida https://agent.agiq.top (el agente hermano; si aún no resuelve, se omite).
2. Registros públicos: `a2aregistry.org` y AgentPeering, más los README de listas awesome-a2a en GitHub.
3. Si hay `ANTHROPIC_API_KEY`, Claude propone más orígenes con la herramienta `propose_candidates`. Esas URLs se comprueban igual que el resto: no se listan solo porque el modelo las nombre.

Hay tope de concurrencia, timeout por petición y un presupuesto total de unos 40 segundos. No se piden hosts privados, de loopback ni de metadatos.

## Despliegue en Render

El blueprint está en `render.yaml`, en la raíz del repo.

1. En Render, crea un Blueprint a partir de este repositorio (o un Web Service Python con root directory `agent`). El plan es **free**. El proceso escucha en `$PORT`.
2. Cuando pida `ANTHROPIC_API_KEY`, pégala en el panel. `sync: false` evita que un futuro blueprint la borre o la escriba en git. No la pongas en un commit.
3. `PUBLIC_URL` ya vale `https://mindr-a2a.onrender.com`, el hostname gratuito de Render. No hace falta un dominio propio. El modelo por defecto es `claude-haiku-5-5`.
4. El CNAME de GitHub Pages (`mindr.top`) no se toca: la web sigue siendo estática y llama al servicio en `https://mindr-a2a.onrender.com`.
5. Comprueba `GET /health` y las curl de arriba. El plan free se duerme; el primer request puede tardar cerca de un minuto.

CORS permite `https://mindr.top`, `https://www.mindr.top` y, para probar la página en local, `http://localhost` y `http://127.0.0.1` con puerto. La página de producción siempre llama a `https://mindr-a2a.onrender.com`.
