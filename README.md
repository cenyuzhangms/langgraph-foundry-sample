# LangGraph Supervisor on Microsoft Foundry Hosted Agents

A working sample showing that the canonical [LangGraph
`langgraph-supervisor`][lgsup] multi-agent pattern can be deployed to
**Microsoft Foundry Hosted Agents** with a small adapter — no rewrite of the
graph required.

The LangGraph code in [`graph.py`](graph.py) is functionally the same shape
as the [upstream `langgraph-supervisor` README][lgsup-readme]. The Foundry
glue lives in [`main.py`](main.py), [`agent.yaml`](agent.yaml),
[`Dockerfile`](Dockerfile), [`azure.yaml`](azure.yaml), and
[`infra/`](infra/).

[lgsup]: https://github.com/langchain-ai/langgraph-supervisor-py
[lgsup-readme]: https://github.com/langchain-ai/langgraph-supervisor-py#example

```
            ┌─────────────────┐
 user ───▶  │   supervisor    │ ◀── routes by task description
            └─────────────────┘
                    │
        ┌───────────┼─────────────┐
        ▼           ▼             ▼
 research_expert  math_expert  code_expert
   (web_search)  (add/mul/div, (python_eval)
                  python_eval)
```

---

## What's required to run on Foundry Hosted Agents

These are the **only** changes that matter for hosting; everything else
(DuckDuckGo search, the extra `code_expert`, prompt tweaks, the two skills)
is sample content, not a hosting requirement.

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

[`main.py`](main.py) is the entire Foundry adapter. The pieces that *have*
to be there:

```python
from agent_framework import (
    AgentResponse, AgentResponseUpdate, BaseAgent,
    Content, Message, ResponseStream,
)
from azure.ai.agentserver.agentframework import from_agent_framework

class LangGraphSupervisorAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="...", description="...")
        self._app = build_app()  # your compiled LangGraph

    # IMPORTANT: regular def, not async def — the adapter calls
    # run(stream=True) WITHOUT awaiting and iterates the result.
    def run(self, messages=None, *, stream=False, thread=None, **kw):
        if stream:
            return ResponseStream(self._stream(messages), finalizer=self._finalize)
        return self._run_once(messages)  # coroutine

    async def _run_once(self, messages):
        text = await self._invoke_supervisor(messages)
        return AgentResponse(messages=[Message("assistant", text=text)])

    async def _stream(self, messages):
        text = await self._invoke_supervisor(messages)
        yield AgentResponseUpdate(
            contents=[Content.from_text(text=text)], role="assistant"
        )

    @staticmethod
    def _finalize(updates):
        parts = [c.text for u in updates for c in (u.contents or []) if getattr(c,"text",None)]
        return AgentResponse(messages=[Message("assistant", text="".join(parts))])

    async def _invoke_supervisor(self, messages):
        lc_messages = _to_lc_messages(messages)            # Foundry Message -> LangChain BaseMessage
        result = await asyncio.to_thread(                  # LangGraph compiled app is sync
            self._app.invoke, {"messages": lc_messages}
        )
        return result["messages"][-1].content              # final AIMessage text

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
speaks `agent_framework.Message`. The translator in [`main.py`](main.py)
maps roles (`system` → `SystemMessage`, `assistant` → `AIMessage`,
`tool` → `ToolMessage`, anything else → `HumanMessage`) and concatenates
text content parts.

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
main.py                 # Foundry adapter: BaseAgent subclass + skill loader
graph.py                # LangGraph supervisor + 3 specialists + tools (only LLM/tools differ from upstream)
agent.yaml              # Hosted-agent manifest
azure.yaml              # azd service definition
Dockerfile              # python:3.12-slim + skills/ baked into /opt/skills/
requirements.txt        # langgraph, langgraph-supervisor, langchain-openai, agent-framework, ...
skills/
  exec-summary/SKILL.md             # prose-only mandatory policy
  research-brief/SKILL.md           # playbook description
  research-brief/bin/research-brief # placeholder executable on $PATH
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

```powershell
azd ai agent invoke "What is (12 * 7) + 100, and briefly tell me what LangGraph is?"
```

Expected: a TL;DR / Confidence / Recommended-action header (proves the
`exec-summary` skill applied), the math result `184` (proves `math_expert`
ran), and a one-paragraph LangGraph description (proves `research_expert`
ran).

---

## Try these prompts in the Playground

**Tools**

- `What is (12 * 7) + 100, then divided by 4?` — math_expert chain
- `Use Python to compute the sum of squares from 1 to 50.` — code_expert / python_eval
- `Search for the current population of Tokyo, then divide it by 1000 to get it in thousands.` — research + math handoff

**Skills**

- `What mandatory policies and playbooks are you operating under? List them by name.` — should name `exec-summary` and `research-brief`
- `Just give me a one-line answer, no preamble: what's 2+2?` — `exec-summary` should still force the TL;DR header
