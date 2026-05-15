"""Foundry hosted-agent entrypoint that serves the LangGraph multi-agent graph.

The container runs the agentserver Responses HTTP protocol (port 8088). Each
incoming request becomes one ``app.invoke()`` call into the compiled LangGraph
supervisor graph; the final ``AIMessage`` is returned as an
``AgentResponse``.

This shows that an existing LangGraph project can be put on Foundry hosted
agents with very little glue: subclass ``BaseAgent``, translate messages
both ways, and call ``from_agent_framework(agent).run_async()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Iterable, List

from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    BaseAgent,
    Content,
    Message,
    ResponseStream,
)
from azure.ai.agentserver.agentframework import from_agent_framework
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from graph import build_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("langgraph-foundry-agent")

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_BIN_ROOT = Path("/opt/skills")


# ---------- skill loading (Pattern A + Pattern C) ----------

def load_skills_appendix() -> str:
    """Read every ./skills/<name>/SKILL.md and bucket into policies vs playbooks.

    Bucketing rule: SKILL.md *with* a sibling ``bin/`` dir → playbook (the
    Dockerfile copies bin/ to /opt/skills/<name>/bin/ on $PATH); SKILL.md
    *without* bin/ → behavior policy. See ``foundry-data-analyst-with-skills``
    for the canonical version of this loader.
    """
    if not SKILLS_DIR.is_dir():
        return ""
    policies: list[str] = []
    playbooks: list[str] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not (skill_dir.is_dir() and skill_md.is_file()):
            continue
        text = skill_md.read_text(encoding="utf-8").strip()
        if not text:
            continue
        name = skill_dir.name
        if (SKILLS_BIN_ROOT / name / "bin").is_dir():
            playbooks.append(
                f"### Playbook: {name}\n_executables on $PATH; invoke via the supervisor by "
                f"asking the relevant specialist to call them._\n\n{text}\n"
            )
        else:
            policies.append(f"### Policy: {name}\n\n{text}\n")
        log.info("loaded skill %s (chars=%d, playbook=%s)", name, len(text), bool(playbooks))

    parts: list[str] = []
    if policies:
        parts.append(
            "\n\n## MANDATORY behavior policies (Foundry Skills)\n\n"
            "These are RULES governing how the SUPERVISOR composes the final "
            "answer. Apply them on every turn. If a policy conflicts with "
            "the workflow above, the policy wins.\n\n"
            + "\n---\n\n".join(policies)
        )
    if playbooks:
        parts.append(
            "\n\n## Available playbooks (Foundry Skills)\n\n"
            + "\n---\n\n".join(playbooks)
        )
    return "\n".join(parts)


# ---------- message translation ----------

def _to_lc_messages(messages: Any) -> List[BaseMessage]:
    """Convert agent_framework Message(s) into LangChain BaseMessages."""
    if messages is None:
        return []
    if isinstance(messages, str):
        return [HumanMessage(content=messages)]
    if isinstance(messages, Message):
        messages = [messages]

    out: List[BaseMessage] = []
    for m in messages:
        if isinstance(m, str):
            out.append(HumanMessage(content=m))
            continue
        text = _extract_text(m)
        role = str(getattr(m, "role", "") or "").lower()
        if role == "system":
            out.append(SystemMessage(content=text))
        elif role == "assistant":
            out.append(AIMessage(content=text))
        elif role == "tool":
            out.append(ToolMessage(content=text, tool_call_id=getattr(m, "tool_call_id", "") or ""))
        else:
            out.append(HumanMessage(content=text))
    return out


def _extract_text(message: Message) -> str:
    """Concatenate any textual content parts of a Message."""
    text_attr = getattr(message, "text", None)
    if isinstance(text_attr, str) and text_attr:
        return text_attr
    contents: Iterable[Any] = getattr(message, "contents", None) or []
    chunks: list[str] = []
    for c in contents:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks).strip()


# ---------- BaseAgent wrapper ----------

class LangGraphSupervisorAgent(BaseAgent):
    """Adapter: every Foundry turn → one LangGraph supervisor invocation."""

    def __init__(self, *, system_appendix: str = "") -> None:
        super().__init__(
            name="LangGraphSupervisorAgent",
            description=(
                "Multi-agent LangGraph supervisor (research_expert + math_expert "
                "+ code_expert) running on Foundry hosted agents."
            ),
        )
        self._system_appendix = system_appendix
        self._app = build_app()

    def run(  # type: ignore[override]
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        thread: Any = None,
        session: Any = None,
        **kwargs: Any,
    ):
        # IMPORTANT: ``run`` must be a regular function (not ``async def``).
        # Non-streaming → return a coroutine the adapter awaits to get an
        # ``AgentResponse``. Streaming → return a ``ResponseStream`` (NOT a
        # bare async generator); the adapter relies on its
        # ``get_final_response()`` accumulator (Playground crashes otherwise
        # with "'async_generator' object has no attribute 'get_final_response'").
        if stream:
            return ResponseStream(
                self._run_stream(messages),
                finalizer=self._finalize_stream,
            )
        return self._run_once(messages)

    async def _run_once(self, messages: Any) -> AgentResponse:
        final_text = await self._invoke_supervisor(messages)
        return AgentResponse(
            messages=[Message("assistant", text=final_text)]
        )

    async def _run_stream(self, messages: Any):
        final_text = await self._invoke_supervisor(messages)
        yield AgentResponseUpdate(
            contents=[Content.from_text(text=final_text)],
            role="assistant",
        )

    @staticmethod
    def _finalize_stream(updates) -> AgentResponse:
        text_parts: list[str] = []
        for u in updates:
            for c in getattr(u, "contents", None) or []:
                t = getattr(c, "text", None)
                if isinstance(t, str):
                    text_parts.append(t)
        return AgentResponse(
            messages=[Message("assistant", text="".join(text_parts))]
        )

    async def _invoke_supervisor(self, messages: Any) -> str:
        lc_messages = _to_lc_messages(messages)
        if self._system_appendix and not any(isinstance(m, SystemMessage) for m in lc_messages):
            lc_messages.insert(0, SystemMessage(content=self._system_appendix.strip()))

        # LangGraph supervisor compiled apps are sync-only; offload to a thread
        # so the async server stays responsive.
        result = await asyncio.to_thread(
            self._app.invoke, {"messages": lc_messages}
        )
        return _final_assistant_text(result)


def _final_assistant_text(result: dict) -> str:
    """Pull the last AIMessage's textual content out of a LangGraph result."""
    msgs = result.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                joined = "\n".join(p for p in parts if p)
                if joined.strip():
                    return joined
    return "(no assistant content produced)"


async def main() -> None:
    if not (os.getenv("PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")):
        raise RuntimeError("PROJECT_ENDPOINT (or AZURE_AI_PROJECT_ENDPOINT) is required.")

    appendix = load_skills_appendix()
    agent = LangGraphSupervisorAgent(system_appendix=appendix)
    log.info(
        "langgraph supervisor listening on http://0.0.0.0:8088 (skills_chars=%d)",
        len(appendix),
    )
    await from_agent_framework(agent).run_async()


if __name__ == "__main__":
    asyncio.run(main())
