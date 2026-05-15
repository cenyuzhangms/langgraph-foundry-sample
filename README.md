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

## What changed vs the upstream `langgraph-supervisor` sample

### 1. LLM binding — Azure OpenAI via Foundry-managed identity

| Upstream README | This sample |
|---|---|
| `ChatOpenAI(model="gpt-4o")` + `OPENAI_API_KEY` | `AzureChatOpenAI(azure_endpoint=…, azure_deployment=…, azure_ad_token_provider=…)` |
| Public OpenAI | Foundry-deployed model (`MODEL_DEPLOYMENT_NAME`, default `gpt-4.1-mini`) |
| API key | `DefaultAzureCredential` → bearer for `https://cognitiveservices.azure.com/.default` (uses the per-instance MI inside the container — **no secrets**) |

`graph._derive_openai_endpoint()` resolves the Azure OpenAI endpoint from
either `AZURE_OPENAI_ENDPOINT` or by parsing the Foundry `PROJECT_ENDPOINT`
(`https://<acct>.services.ai.azure.com/...` → `https://<acct>.openai.azure.com`).

### 2. Real `web_search` tool instead of the README stub

Upstream returns a hard-coded string from `web_search`. This sample calls
DuckDuckGo via the `duckduckgo-search` package and returns the top-5 hits
with title / snippet / URL.

### 3. A third specialist + a sandboxed code tool

Upstream has `research_expert` + `math_expert`. This sample adds:

- `divide(a, b)` (with zero-guard) on `math_expert`
- `code_expert` agent with a single `python_eval` tool — a Python
  expression evaluator restricted by an **AST whitelist**: arithmetic,
  comparisons, and a small set of builtins (`abs/min/max/sum/len/round/pow/sorted/range`).
  No imports, no attribute access, no statements.

### 4. Stronger handoff prompts

Specialist prompts are explicit about *not* doing each other's job ("do NOT
search the web — defer to research_expert by handing back to the supervisor").
Upstream prompts are minimal.

### 5. Foundry adapter — `BaseAgent` wrapper around `app.invoke`

[`main.py`](main.py) defines `LangGraphSupervisorAgent(BaseAgent)`. On every
turn it:

1. Translates Foundry `Message`s ↔ LangChain `BaseMessage`s.
2. Prepends a system-message appendix loaded from `./skills/*/SKILL.md`.
3. Runs `await asyncio.to_thread(self._app.invoke, {"messages": lc_messages})`
   (LangGraph compiled apps are sync; offload to keep the async server
   responsive).
4. Returns the final assistant text as an `AgentResponse` — or wraps a
   one-update async generator in a `ResponseStream(…, finalizer=…)` for
   streaming (the Playground requires `get_final_response()` on the stream
   object; a bare async generator crashes it).

`from_agent_framework(agent).run()` then serves the Foundry Responses
protocol on container port 8088.

### 6. Foundry Skills — system-prompt injection from local `SKILL.md` files

LangGraph itself has no notion of "skills". This sample adds two via the
adapter, mirroring the `foundry-data-analyst-with-skills` pattern:

| Skill | Type | Effect |
|---|---|---|
| `exec-summary` | MANDATORY policy (no `bin/`) | Forces every final answer to open with **TL;DR / Confidence / Recommended action**. |
| `research-brief` | Playbook (with `bin/`) | Guidance for structuring multi-source research answers. |

`load_skills_appendix()` reads each `skills/<name>/SKILL.md` at startup and
buckets them into "MANDATORY policies" vs "Available playbooks" before
prepending them to the system prompt seen by the supervisor LLM.

### 7. Container packaging

- [`Dockerfile`](Dockerfile) — `python:3.12-slim`, installs requirements,
  copies source + `skills/` into `/opt/skills/<name>/`, runs `python main.py`.
- [`agent.yaml`](agent.yaml) — `kind: hosted`, `protocol: responses`, env
  vars (`PROJECT_ENDPOINT`, `AZURE_OPENAI_ENDPOINT`, `MODEL_DEPLOYMENT_NAME`).
- [`azure.yaml`](azure.yaml) — `azd` service of host `azure.ai.agent`.
- [`infra/main.bicep`](infra/main.bicep) — wires the per-agent MI to the
  required RBAC roles (see below).

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

---

## Verified gotchas (from building this sample)

- **`agent-framework==1.0.0rc3`** is what `azure-ai-agentserver-agentframework==1.0.0b16` requires. The rc3 surface uses `AgentResponse` / `Message` / `Content` (NOT the older `AgentRunResponse` / `ChatMessage` / `TextContent` names).
- **`BaseAgent.run` must be a regular `def` (not `async def`).** The adapter calls `agent.run(stream=True)` *without* awaiting and iterates the result. For `stream=False` return a coroutine; for `stream=True` return a `ResponseStream` (wrapping your async generator with a `finalizer`) — a bare async generator works for the CLI but crashes the Playground with `'async_generator' object has no attribute 'get_final_response'`.
- **Skills are loaded from local `SKILL.md` files at startup**, not from the Foundry runtime REST endpoint (which silently returns nothing from inside the container).
- The role you need on the project scope is **`Foundry User`**, not `Azure AI User`.
