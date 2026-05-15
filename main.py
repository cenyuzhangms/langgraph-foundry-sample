"""Foundry hosted-agent entrypoint for the LangGraph supervisor in graph.py.

Deploying an existing LangGraph app to Foundry hosted agents requires only:
  1. a ``BaseAgent`` subclass with ``run(messages, *, stream, ...)``
  2. a translator: agent_framework Message -> langchain BaseMessage
  3. ``from_agent_framework(agent).run()`` to serve the Responses protocol

That is the entire file.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    BaseAgent,
    Content,
    Message,
    ResponseStream,
)
from azure.ai.agentserver.agentframework import from_agent_framework
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph import build_app

_ROLE_MAP = {"system": SystemMessage, "assistant": AIMessage}


def _to_lc(messages: Any) -> list:
    """Translate agent_framework Message(s) to langchain BaseMessage(s)."""
    if messages is None:
        return []
    if isinstance(messages, (str, Message)):
        messages = [messages]
    out = []
    for m in messages:
        if isinstance(m, str):
            out.append(HumanMessage(content=m))
            continue
        text = getattr(m, "text", None) or "\n".join(
            getattr(c, "text", "") for c in (getattr(m, "contents", None) or [])
        )
        cls = _ROLE_MAP.get(str(getattr(m, "role", "")).lower(), HumanMessage)
        out.append(cls(content=text))
    return out


class LangGraphSupervisorAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="LangGraphSupervisorAgent",
            description="LangGraph supervisor on Foundry hosted agents.",
        )
        self._app = build_app()

    # `run` MUST be a regular def: the adapter calls run(stream=True) without
    # awaiting and iterates the result.
    def run(self, messages: Any = None, *, stream: bool = False, **_: Any):
        if stream:
            return ResponseStream(self._stream(messages), finalizer=self._finalize)
        return self._invoke(messages)

    async def _invoke(self, messages: Any) -> AgentResponse:
        # LangGraph compiled apps are sync; offload so the async server stays responsive.
        result = await asyncio.to_thread(self._app.invoke, {"messages": _to_lc(messages)})
        return AgentResponse(messages=[Message("assistant", text=result["messages"][-1].content)])

    async def _stream(self, messages: Any):
        resp = await self._invoke(messages)
        yield AgentResponseUpdate(
            contents=[Content.from_text(text=resp.messages[0].text)],
            role="assistant",
        )

    @staticmethod
    def _finalize(updates) -> AgentResponse:
        text = "".join(
            c.text for u in updates for c in (u.contents or []) if getattr(c, "text", None)
        )
        return AgentResponse(messages=[Message("assistant", text=text)])


if __name__ == "__main__":
    from_agent_framework(LangGraphSupervisorAgent()).run()
