# 01 — The agent loop

> **Step 1.** Built: `src/pretzel/harness/`. Verified: `python -m pretzel.demo_harness`.

## What we built

A reusable harness with three public pieces:

| Piece | File | What it is |
| --- | --- | --- |
| `run_agent()` | [agent.py](../src/pretzel/harness/agent.py) | The loop. ~90 lines, and every stage runs on it. |
| `Tool` / `ToolRegistry` | [tools.py](../src/pretzel/harness/tools.py) | One capability, and the dispatcher that executes it. |
| `AgentSpec` / `RunContext` | both | What makes a stage different; what a handler is handed. |

## Why it exists architecturally

### The API is stateless

`POST /v1/messages` remembers nothing between calls. There is no session, no thread id, no
server-side conversation. You resend the entire history every single time.

So an "agent" is not an API feature. It is a loop you own, wrapped around a stateless endpoint:

```
send history → model replies → did it ask for tools?
                                 ├─ yes → run them, append results, send history again
                                 └─ no  → done
```

Two consequences fall out of that one fact, and both shape the rest of the project:

1. **Cost grows quadratically with turns.** Turn 10 re-sends everything from turns 1–9. This is
   why prompt caching is not an optimization here, it is load-bearing — and why context
   management (step 6) is an architecture problem rather than a tuning exercise.
2. **The transcript is the only state.** Whatever you fail to append is gone. That makes
   `messages` the thing to get exactly right.

### One loop, five stages

The five stages differ only in their **system prompt** and their **tool surface**. Everything
else — how to detect completion, how to dispatch a call, what to do when a tool explodes, when
to give up — is identical. So the harness owns it, once, and a stage becomes configuration:

```python
AgentSpec(
    name="change-analysis",
    system_prompt=CHANGE_ANALYSIS_PROMPT,
    tools=(GIT_DIFF, READ_FILE, LIST_METADATA),
    effort="high",
)
```

This is the payoff for building the harness first. If the loop lived inside stage 1, stage 2
would copy it, and by stage 5 there would be five subtly different loops with five different
bugs.

### Five responsibilities the stages must never re-implement

1. **Loop control** — when is the agent done?
2. **Tool dispatch** — name → handler, with schema enforcement.
3. **Failure containment** — a tool that raises must not kill the run.
4. **Budget enforcement** — an iteration cap, so a confused agent can't spin forever.
5. **Observability** — a transcript and token accounting for every run.

## The code

The whole loop, with the noise stripped out:

```python
for iteration in range(1, spec.max_iterations + 1):
    response = client.beta.messages.create(**_request_kwargs(spec, registry, messages))
    usage.add(response.usage)

    if response.stop_reason == "refusal":    raise AgentRefused(...)
    if response.stop_reason == "max_tokens": raise OutputTruncated(...)
    if response.stop_reason == "pause_turn":
        messages.append({"role": "assistant", "content": response.content})
        continue
    if response.stop_reason == "end_turn":   return AgentResult(...)

    # stop_reason == "tool_use"
    messages.append({"role": "assistant", "content": response.content})
    results = [registry.dispatch(b, ctx, iteration) for b in tool_uses]
    messages.append({"role": "user", "content": results})

raise IterationCapExceeded(...)
```

### Two API choices worth naming

**`cache_control` on the system prompt.** The system prompt and tool schemas are byte-identical
on every iteration and sit at the front of the prefix. In a ten-iteration run that prefix is
sent ten times. Caching it is the single highest-value line in `_request_kwargs`. The demo
verifies it by asserting `usage.cache_read_input_tokens > 0` from iteration 2 onward.

**`fallbacks="default"`.** Anthropic's guidance for `claude-opus-5` is to enable server-side
refusal fallbacks by default: if a safety classifier declines, the request is routed to another
model instead of returning a refusal. It's why the harness calls `client.beta.messages` rather
than `client.messages`. One flag on `AgentSpec` (`refusal_fallbacks=False`) drops it.

## What to remember

The exam-facing part. Seven things, roughly in order of how often they're gotten wrong.

**1. Statelessness drives everything.** No server-side session. You resend the full history each
turn, so cost is quadratic in turns and the transcript is your only state.

**2. `stop_reason` is the control signal** — not the text, not "does the response contain tool
blocks". Five values matter:

| `stop_reason` | Meaning | Correct handling |
| --- | --- | --- |
| `end_turn` | Model is finished | Return the result |
| `tool_use` | It wants tools run | Dispatch, append results, loop |
| `max_tokens` | Truncated mid-answer | Abort — the transcript is unusable |
| `pause_turn` | A server-side tool paused a long turn | Append and re-send to resume |
| `refusal` | A safety classifier declined | Abort, or let `fallbacks` route around it |

**3. Append `response.content` verbatim.** Never rebuild the assistant message from extracted
text. The raw blocks carry thinking blocks and `tool_use` ids; a string loses both, silently.

**4. All `tool_result` blocks for one assistant turn go in a single user message.** Splitting
them across several messages trains the model to stop making parallel calls.

**5. `tool_use_id` must round-trip exactly.** It is the only thing binding a result to the call
that produced it.

**6. Tool errors become results, not exceptions.** Unknown tool, handler raised, human declined
— all come back as `tool_result` with `is_error: True`. A model that can read its own error can
correct course; a harness that crashes cannot. This is the difference between an agent and a
script.

**7. The iteration cap is a safety property, not a nicety.** Without it, a confused agent bills
you until the context window fills.

### Two design choices worth being able to defend

**Why `RunContext` instead of globals.** Every handler receives `(args, ctx)`. Tools depend on
injected state, never module globals. That is what makes a stage unit-testable, and what lets a
sub-agent in step 3 be handed a *narrowed* context — different workdir, read-only DB handle —
without any tool knowing the difference.

**Why `requires_approval` and `read_only` exist now while doing nothing.** They're declared in
step 1 and consumed in steps 3 and 4. Declaring the flag at the point where the capability is
defined means the harness can later gate, serialize, or parallelize a tool without every tool
being rewritten. The general principle: *the harness needs an action-specific hook to act on,
and a tool's own definition is where that hook belongs.*

## Verification

```powershell
pip install -e .
python -m pretzel.demo_harness           # offline: 7/7 passing
python -m pretzel.demo_harness --live    # needs ANTHROPIC_API_KEY
```

Offline (no API key, no cost) — **all passing**:

- `tool_use_id` round-trips exactly
- non-string handler output is JSON-encoded
- a handler exception becomes `is_error` instead of a crash
- an unknown tool becomes `is_error` listing what does exist
- `requires_approval` + a refusing reviewer blocks the call
- strict mode rejects a loose schema
- duplicate tool names are rejected

Live (`--live`, costs a few cents) — **not yet run, no API key on this machine**:

- the loop completes and both tools were called
- parallel calls arrive in one turn
- the prompt cache hits after iteration 1
- the agent recovers from a failing tool instead of crashing
- the iteration cap trips cleanly
- token accounting is populated

## Next

[Step 2 — Stage 1: change analysis](00-architecture.md#roadmap): a real `git diff` of
`force-app/` metadata into a structured risk report, where the system prompt starts mattering.
