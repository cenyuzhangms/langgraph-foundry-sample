# research-brief

Generates a short, structured research brief (Markdown) from a topic plus a
list of bullet findings collected by the research_expert. The output is one
page, ready to paste into a wiki.

## When to use

Use when the user asks the team for any of:
- "give me a brief on X"
- "research X and write it up"
- "compile what's known about X"

For a one-line factual answer ("who is the CEO of X?"), skip this — answer
inline.

## Executable

The Dockerfile bakes this skill's `bin/research-brief` script onto $PATH
inside the container. The supervisor (or any specialist with shell access)
can invoke it as:

```
research-brief "<topic>" --findings findings.txt
```

`findings.txt` is a UTF-8 file with one bullet per line. The script prints
the rendered Markdown brief to stdout with these sections:

1. **Topic + one-line summary**
2. **Key findings** (the bullets, formatted)
3. **What we still don't know** (placeholder the supervisor fills in)
4. **Sources** (URLs the research_expert cited)

## How the supervisor uses it

1. Delegate fact-gathering to the research_expert (one or more `web_search`
   calls); collect both the bullets and the URLs.
2. Compose the final message with this brief structure. You can either:
   - reproduce the four sections directly in your final message (preferred
     for short briefs), OR
   - call `research-brief` via shell from a future Foundry deployment that
     adds a shell tool, then paste its output.
3. If the `exec-summary` policy applies, the brief comes AFTER the TL;DR
   block.
