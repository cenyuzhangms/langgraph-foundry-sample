# LangGraph Supervisor on Microsoft Foundry Hosted Agents

A working sample proving that an **unchanged** LangGraph
[`langgraph-supervisor`][lgsup] multi-agent app can be deployed to
**Microsoft Foundry Hosted Agents** with a thin adapter.

[`graph.py`](graph.py) is the [upstream `langgraph-supervisor` README
quickstart][lgsup-readme] **copy-pasted verbatim**, with one line changed:
the LLM is constructed via Foundry's container managed identity instead of
an `OPENAI_API_KEY`. The Foundry glue is entirely in [`main.py`](main.py)
+ [`agent.yaml`](agent.yaml) + [`Dockerfile`](Dockerfile) +
[`azure.yaml`](azure.yaml) + [`infra/`](infra/).

[lgsup]: https://github.com/langchain-ai/langgraph-supervisor-py
[lgsup-readme]: https://github.com/langchain-ai/langgraph-supervisor-py#quickstart

```
            ┌─────────────────┐
 user ───▶  │   supervisor    │ ◀── routes by task description
            └─────────────────┘
                    │
             ┌──────┴──────┐
             ▼             ▼
      research_expert   math_expert
        (web_search)    (add, multiply)
```

The one-line diff vs upstream:

```diff
- from langchain_openai import ChatOpenAI
- model = ChatOpenAI(model="gpt-4o")
+ from langchain_openai import AzureChatOpenAI
+ from azure.identity import DefaultAzureCredential, get_bearer_token_provider
+ model = AzureChatOpenAI(
+     azure_endpoint=..., azure_deployment=..., api_version="2024-10-21",
+     azure_ad_token_provider=get_bearer_token_provider(
+         DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"),
+ )
```

---

## What's required to run on Foundry Hosted Agents

These are the **only** changes that matter for hosting; the two skills
shipped under `skills/` are an optional Foundry feature demo, not a
hosting requirement.

### 1. Authenticate to the model with the container's managed identity (no API keys)

The hosted-agent container has a per-instance managed identity. Use it
instead of an API key when constructing the LLM client:

```python
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
)
llm = AzureChatOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    azure_deployment=os.environ["MODEL_DEPLOYMENT_NAME"],
    api_version="2024-10-21",
    azure_ad_token_provider=token_provider,
)
```

The endpoint + deployment env vars are wired in by [`agent.yaml`](agent.yaml).
The MI must hold the RBAC roles listed under "Deploy" below.

### 2. Wrap the compiled LangGraph app in a `BaseAgent` and serve it

[`main.py`](main.py) is the entire Foundry adapter — ~50 lines:

```python
from agent_framework import (
    AgentResponse, AgentResponseUpdate, BaseAgent,
    Content, Message, ResponseStream,
)
from azure.ai.agentserver.agentframework import from_agent_framework
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from graph import build_app

_ROLE_MAP = {"system": SystemMessage, "assistant": AIMessage}

def _to_lc(messages):
    """agent_framework Message(s) -> langchain BaseMessage(s)."""
    if messages is None: return []
    if isinstance(messages, (str, Message)): messages = [messages]
    out = []
    for m in messages:
        if isinstance(m, str):
            out.append(HumanMessage(content=m)); continue
        text = getattr(m, "text", None) or "\n".join(
            getattr(c, "text", "") for c in (getattr(m, "contents", None) or []))
        cls = _ROLE_MAP.get(str(getattr(m, "role", "")).lower(), HumanMessage)
        out.append(cls(content=text))
    return out

class LangGraphSupervisorAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="LangGraphSupervisorAgent", description="...")
        self._app = build_app()

    # `run` MUST be a regular def: adapter calls run(stream=True) without awaiting.
    def run(self, messages=None, *, stream=False, **_):
        if stream:
            return ResponseStream(self._stream(messages), finalizer=self._finalize)
        return self._invoke(messages)

    async def _invoke(self, messages):
        result = await asyncio.to_thread(self._app.invoke, {"messages": _to_lc(messages)})
        return AgentResponse(messages=[Message("assistant", text=result["messages"][-1].content)])

    async def _stream(self, messages):
        resp = await self._invoke(messages)
        yield AgentResponseUpdate(
            contents=[Content.from_text(text=resp.messages[0].text)], role="assistant")

    @staticmethod
    def _finalize(updates):
        text = "".join(c.text for u in updates for c in (u.contents or [])
                       if getattr(c, "text", None))
        return AgentResponse(messages=[Message("assistant", text=text)])

if __name__ == "__main__":
    from_agent_framework(LangGraphSupervisorAgent()).run()  # serves port 8088
```

Three subtleties that bite:

- **`run` must be `def`, not `async def`.** Stream branch returns a
  `ResponseStream`; non-stream branch returns a coroutine.
- **Wrap the streaming generator in `ResponseStream(..., finalizer=...)`.**
  A bare `async def`/`yield` works in the CLI but the Playground crashes
  with `'async_generator' object has no attribute 'get_final_response'`.
- **Bridge the sync `app.invoke` with `asyncio.to_thread`.** LangGraph
  compiled apps are sync; the Foundry server is async.

### 3. Translate Foundry messages ↔ LangChain messages

LangGraph speaks `langchain_core.messages.BaseMessage`; the Foundry adapter
speaks `agent_framework.Message`. The `_to_lc` helper above maps roles
(`system` → `SystemMessage`, `assistant` → `AIMessage`, anything else →
`HumanMessage`) and concatenates text content parts.

### 4. Container packaging

- [`Dockerfile`](Dockerfile) — `python:3.12-slim`, `pip install -r requirements.txt`,
  `CMD ["python", "main.py"]`. The container must listen on port **8088**
  (handled by `from_agent_framework(...).run()`).
- [`agent.yaml`](agent.yaml) — `kind: hosted`, `protocol: responses`, env
  vars (`PROJECT_ENDPOINT`, `AZURE_OPENAI_ENDPOINT`, `MODEL_DEPLOYMENT_NAME`).
- [`azure.yaml`](azure.yaml) — `azd` service of host `azure.ai.agent`.

### 5. Pinned SDK versions

```
agent-framework==1.0.0rc3
azure-ai-agentserver-agentframework==1.0.0b16
```

The rc3 surface uses `AgentResponse` / `Message` / `Content` (NOT the older
`AgentRunResponse` / `ChatMessage` / `TextContent` names). Mismatched
versions surface as `agent_version_failed` with no clear error.

---

## Layout

```
main.py                 # Foundry adapter (~50 lines): BaseAgent subclass + msg translator
graph.py                # langgraph-supervisor README quickstart, verbatim except LLM
agent.yaml              # Hosted-agent manifest
azure.yaml              # azd service definition
Dockerfile              # python:3.12-slim + pip install -r requirements.txt
requirements.txt        # langgraph, langgraph-supervisor, langchain-openai, agent-framework, ...
infra/                  # Bicep
```

---

## Deploy to a Foundry project

```powershell
$projectId = "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"

azd ai agent init -p $projectId -d gpt-4.1-mini --src .
azd deploy
```

After the first deploy, the per-agent managed identity needs:

- `AcrPull` on the project's ACR
- `Cognitive Services OpenAI User` + `Cognitive Services User` on the Foundry account
- **`Foundry User`** on the **project** scope (role definition GUID
  `53ca6127-db72-4b80-b1b0-d745d6d5456d`; the Azure CLI rejects `--role
  "Foundry User"` by name, pass the GUID)
- `Storage Blob Data Contributor` on the project's storage account

Without `Foundry User` on the project scope, invocations return
`401 PermissionDenied "Principal does not have access to API/Operation."`

## Smoke test

The upstream README's example query:

```powershell
azd ai agent invoke "what's the combined headcount of the FAANG companies in 2024?"
```

Expected: the FAANG breakdown from `research_expert`'s `web_search` tool
plus the total `1,977,586` from `math_expert`'s `add` tool — exactly the
supervisor handoff the upstream README demonstrates.

---

## Try these prompts in the Playground

- `what's the combined headcount of the FAANG companies in 2024?` — research → math handoff (the upstream README example)
- `What is (12 * 7) + 100?` — math_expert only
