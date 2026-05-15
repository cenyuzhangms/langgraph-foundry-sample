"""Multi-agent LangGraph app: supervisor + research_expert + math_expert + code_expert.

This is the canonical langgraph-supervisor pattern from
https://github.com/langchain-ai/langgraph-supervisor-py adapted with:

* a real `web_search` tool (DuckDuckGo) instead of the README's stub
* arithmetic tools for math_expert
* a `python_eval` tool for code_expert (sandboxed, no imports/eval of __)
* an LLM bound to the Foundry-deployed chat model via langchain-openai

The compiled graph is a `langgraph.graph.StateGraph` you can invoke with
``app.invoke({"messages": [...]})``.
"""

from __future__ import annotations

import ast
import os
import re
from functools import lru_cache
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor


# ---------- LLM (Foundry-deployed Azure OpenAI model) ----------

def _derive_openai_endpoint() -> str:
    """Resolve the Azure OpenAI endpoint for langchain-openai.

    Prefer the explicit AZURE_OPENAI_ENDPOINT env var (set by Foundry's bicep
    output and wired through agent.yaml). Fall back to deriving it from the
    project endpoint by swapping the host suffix.
    """
    explicit = os.getenv("AZURE_OPENAI_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    project = os.getenv("PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT") or ""
    # https://<account>.services.ai.azure.com/api/projects/<proj>
    #   -> https://<account>.openai.azure.com
    m = re.match(r"^(https?://)([^.]+)\.services\.ai\.azure\.com", project)
    if not m:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT not set and PROJECT_ENDPOINT does not look "
            "like a Foundry services.ai.azure.com URL."
        )
    return f"{m.group(1)}{m.group(2)}.openai.azure.com"


@lru_cache(maxsize=1)
def get_llm() -> AzureChatOpenAI:
    """Return a singleton AzureChatOpenAI bound to the Foundry deployment."""
    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        cred, "https://cognitiveservices.azure.com/.default"
    )
    return AzureChatOpenAI(
        azure_endpoint=_derive_openai_endpoint(),
        azure_deployment=os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_ad_token_provider=token_provider,
        temperature=0,
    )


# ---------- Tools ----------

def web_search(query: str) -> str:
    """Search the public web via DuckDuckGo and return up to 5 result snippets.

    Use for current events, recent news, biographical lookups, headcounts, etc.
    """
    try:
        from duckduckgo_search import DDGS  # imported lazily so unit tests can stub
    except Exception as exc:  # pragma: no cover
        return f"web_search unavailable: {exc!r}"
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=5))
    except Exception as exc:
        return f"web_search error: {exc!r}"
    if not hits:
        return "no results."
    lines = []
    for i, h in enumerate(hits, 1):
        title = h.get("title") or ""
        body = (h.get("body") or "").strip().replace("\n", " ")
        href = h.get("href") or ""
        lines.append(f"{i}. {title}\n   {body}\n   {href}")
    return "\n".join(lines)


def add(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


def multiply(a: float, b: float) -> float:
    """Return a * b."""
    return a * b


def divide(a: float, b: float) -> float:
    """Return a / b. Raises on division by zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


_ALLOWED_AST_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Tuple, ast.List, ast.Load,
    ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.BoolOp, ast.And, ast.Or, ast.Name, ast.Call,
)
_ALLOWED_NAMES = {
    "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
    "round": round, "pow": pow, "sorted": sorted, "range": range,
}


def python_eval(expression: str) -> str:
    """Safely evaluate a single Python expression and return its repr.

    Supports arithmetic, comparisons, and a small whitelist of builtins
    (abs/min/max/sum/len/round/pow/sorted/range). No imports, no attribute
    access, no statements. Use for quick numeric or list computations the
    `add`/`multiply`/`divide` tools can't express.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return f"syntax error: {exc.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_NAMES:
                return f"refused: only these functions are allowed: {sorted(_ALLOWED_NAMES)}"
            continue
        if isinstance(node, ast.Name):
            if node.id not in _ALLOWED_NAMES:
                return f"refused: name '{node.id}' is not allowed"
            continue
        if not isinstance(node, _ALLOWED_AST_NODES):
            return f"refused: AST node {type(node).__name__} is not allowed"
    try:
        result = eval(  # noqa: S307 — restricted by the AST whitelist above
            compile(tree, "<python_eval>", "eval"),
            {"__builtins__": {}},
            dict(_ALLOWED_NAMES),
        )
    except Exception as exc:
        return f"runtime error: {exc!r}"
    return repr(result)


# ---------- Specialist agents + supervisor ----------

RESEARCH_PROMPT = (
    "You are a world-class research assistant. Use the `web_search` tool to "
    "find up-to-date information from the public web. Cite the URLs you used. "
    "Do NOT do arithmetic — defer math to the math_expert by handing back to "
    "the supervisor."
)

MATH_PROMPT = (
    "You are a careful math assistant. Use the `add`, `multiply`, `divide`, "
    "and `python_eval` tools to compute results step by step. Show each step "
    "as a single tool call. Do NOT search the web — defer factual lookups to "
    "the research_expert by handing back to the supervisor."
)

CODE_PROMPT = (
    "You are a Python helper. Use the `python_eval` tool to evaluate small "
    "expressions (sorting, filtering, list comprehensions, statistics over "
    "small lists). The tool refuses imports, attribute access, and "
    "statements — keep expressions short."
)

SUPERVISOR_PROMPT = (
    "You are the supervisor of a small team of specialists:\n"
    "- research_expert: live web search and citation\n"
    "- math_expert: arithmetic and combining numbers\n"
    "- code_expert: small Python expressions on lists/numbers\n\n"
    "Decompose the user's request into the minimum number of specialist "
    "delegations needed. Delegate one specialist at a time. After all "
    "specialists report back, write the final answer yourself, opening "
    "with the TL;DR / Confidence / Recommended-action block when the "
    "exec-summary policy applies (see system prompt appendix)."
)


@lru_cache(maxsize=1)
def build_app() -> Any:
    """Build and compile the supervisor multi-agent graph (singleton)."""
    llm = get_llm()

    research_expert = create_react_agent(
        model=llm,
        tools=[web_search],
        name="research_expert",
        prompt=RESEARCH_PROMPT,
    )
    math_expert = create_react_agent(
        model=llm,
        tools=[add, multiply, divide, python_eval],
        name="math_expert",
        prompt=MATH_PROMPT,
    )
    code_expert = create_react_agent(
        model=llm,
        tools=[python_eval],
        name="code_expert",
        prompt=CODE_PROMPT,
    )

    workflow = create_supervisor(
        [research_expert, math_expert, code_expert],
        model=llm,
        prompt=SUPERVISOR_PROMPT,
    )
    return workflow.compile()
