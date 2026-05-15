# exec-summary

A formatting policy for any answer the supervisor composes that runs longer
than 3 sentences or makes a recommendation. Forces every such response to
open with a short executive summary block, so the reader can decide whether
to keep reading.

## When to use

Apply if ANY of the following are true of the **supervisor's final message**:
- the response will exceed ~3 sentences
- the user asked "should we…?", "is it worth…?", "what do you recommend?"
- the response delivers a finding, a decision, an estimate, or a tradeoff
- the user is reasonably likely to forward this to someone else

If the answer is a one-line factual reply (e.g. "12"), skip the block — don't
pad short answers.

## Required form

Open the supervisor's final message with EXACTLY this Markdown block, before
any other content:

```
**TL;DR** — <one sentence, concrete, no hedging>
**Confidence** — high | medium | low (<one short reason>)
**Recommended action** — <a single imperative the reader can take, OR "no action — informational">
```

Then a blank line, then the rest of the response (details, citations from
the research_expert, calculations from the math_expert).

## Anti-patterns

- Hedging in the TL;DR. If you can't commit, lower the Confidence and write
  the most defensible single recommendation.
- Putting the block at the END. It MUST come first.
- Padding short factual answers with this block. Use judgement.
- The specialists themselves emitting the block — only the supervisor's
  final message uses it.
