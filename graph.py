"""LangGraph multi-agent supervisor — verbatim from the upstream README.

Source: https://github.com/langchain-ai/langgraph-supervisor-py#quickstart

The ONLY change vs the upstream sample is the LLM binding:
the upstream README uses

    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(model="gpt-4o")        # needs OPENAI_API_KEY

inside a Foundry hosted-agent container we use the per-instance managed
identity instead of an API key (see ``_get_model`` below).

Everything below ``_get_model`` is copy-paste from the upstream README.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor


# ---------- the only Foundry-specific change ----------

def _derive_openai_endpoint() -> str:
    explicit = os.getenv("AZURE_OPENAI_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    project = os.getenv("PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT") or ""
    m = re.match(r"^(https?://)([^.]+)\.services\.ai\.azure\.com", project)
    if not m:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT not set and PROJECT_ENDPOINT not Foundry-shaped.")
    return f"{m.group(1)}{m.group(2)}.openai.azure.com"


def _get_model() -> AzureChatOpenAI:
    """Replacement for the README's ``ChatOpenAI(model="gpt-4o")`` line.

    Uses the Foundry container's managed identity instead of an API key.
    """
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureChatOpenAI(
        azure_endpoint=_derive_openai_endpoint(),
        azure_deployment=os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_ad_token_provider=token_provider,
        temperature=0,
    )


# ---------- BELOW THIS LINE: copied from the upstream README ----------
# https://github.com/langchain-ai/langgraph-supervisor-py#quickstart

model = _get_model()  # was: model = ChatOpenAI(model="gpt-4o")


# Create specialized agents

def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


def web_search(query: str) -> str:
    """Search the web for information."""
    return (
        "Here are the headcounts for each of the FAANG companies in 2024:\n"
        "1. **Facebook (Meta)**: 67,317 employees.\n"
        "2. **Apple**: 164,000 employees.\n"
        "3. **Amazon**: 1,551,000 employees.\n"
        "4. **Netflix**: 14,000 employees.\n"
        "5. **Google (Alphabet)**: 181,269 employees."
    )


math_agent = create_react_agent(
    model=model,
    tools=[add, multiply],
    name="math_expert",
    prompt="You are a math expert. Always use one tool at a time.",
)

research_agent = create_react_agent(
    model=model,
    tools=[web_search],
    name="research_expert",
    prompt="You are a world class researcher with access to web search. Do not do any math.",
)


# Create supervisor workflow
workflow = create_supervisor(
    [research_agent, math_agent],
    model=model,
    prompt=(
        "You are a team supervisor managing a research expert and a math expert. "
        "For current events, use research_agent. "
        "For math problems, use math_agent."
    ),
)


@lru_cache(maxsize=1)
def build_app():
    """Compile the supervisor graph (cached so the container builds it once)."""
    return workflow.compile()
