from types import SimpleNamespace

from llm import LLM, PROPOSE_TOOL


class FakeMessages:
    def __init__(self, message):
        self.message = message
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.message


class FakeClient:
    def __init__(self, message):
        self.messages = FakeMessages(message)


def test_answer_without_key():
    llm = LLM("", "claude-haiku-5-5")

    async def run():
        return await llm.answer("hola"), await llm.propose_candidates("")

    import asyncio

    text, proposed = asyncio.run(run())
    assert "ANTHROPIC_API_KEY" in text
    assert proposed == []


def test_answer_and_tool_use_are_parsed():
    import asyncio

    answer_client = FakeClient(SimpleNamespace(content=[SimpleNamespace(type="text", text="  hola mindr  ")]))
    tool_client = FakeClient(
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="propose_candidates",
                    input={"candidates": [" https://perkoon.com ", "no", "https://numbers.online"]},
                )
            ]
        )
    )

    async def run():
        chat = LLM("sk-test", "claude-haiku-5-5", client=answer_client)
        text = await chat.answer("¿Qué eres?")
        proposer = LLM("sk-test", "claude-haiku-5-5", client=tool_client)
        urls = await proposer.propose_candidates("mapas")
        return text, urls

    text, urls = asyncio.run(run())
    assert text == "hola mindr"
    assert urls == ["https://perkoon.com", "https://numbers.online"]
    call = tool_client.messages.calls[0]
    assert call["tools"] == [PROPOSE_TOOL]
    assert call["tool_choice"] == {"type": "tool", "name": "propose_candidates"}
    assert "mapas" in call["messages"][0]["content"]
