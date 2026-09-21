# 01a — The harness, line by line

> Companion to [01 — The agent loop](01-the-agent-loop.md). That page is the *why*. This one
> walks every meaningful line of the two source files and names the concept behind it.
>
> Read with the files open: [tools.py](../src/pretzel/harness/tools.py) ·
> [agent.py](../src/pretzel/harness/agent.py)

---

## Part 0 — The packaging, briefly

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

**src layout.** The package lives at `src/pretzel/` rather than `pretzel/` at the repo root. The
reason is not taste: with a root-level package, Python's automatic "current directory is on
`sys.path`" behavior means `import pretzel` silently picks up the *source tree* whether or not
the package is installed. With src layout it cannot — you must actually install it. So
`pip install -e .` failing is a loud error instead of a mystery that appears only in CI.

`-e` (editable) means the install points at your working tree, so edits take effect without
reinstalling. That's why the demo runs immediately after every change.

---

## Part 1 — `tools.py`

### The header

```python
from __future__ import annotations
```

Postpones evaluation of type annotations: they're stored as strings and never executed at import
time. Two payoffs here. It lets us write `sqlite3.Connection | None` on Python versions where
that syntax would otherwise need `Optional[...]`, and it lets a dataclass field reference a type
defined *later in the file* (`ApprovalFn` is declared below `RunContext`) without a
`NameError`.

```python
MAX_TOOL_RESULT_CHARS = 20_000
```

**Concept: context hygiene is the harness's job.** A tool that returns a 2 MB deploy log would
blow the context window, and because history is re-sent every turn (see
[01](01-the-agent-loop.md#the-api-is-stateless)), it would blow it *repeatedly* and bill you
each time. The cap is enforced in one place so no individual tool has to remember.

### `RunContext` — dependency injection for tools

```python
@dataclass
class RunContext:
    run_id: str
    workdir: Path
    db: sqlite3.Connection | None = None   # wired in step 6
    approve: "ApprovalFn | None" = None    # wired in step 4
    logger: logging.Logger = field(default=log)
```

| Field | Why it exists |
| --- | --- |
| `run_id` | Correlates every log line and, in step 6, every DB row to one run |
| `workdir` | Tools resolve paths against *this*, never `os.getcwd()` — so a sub-agent can be pointed at a different tree |
| `db` | `None` today; step 6 fills it. Declared now so no handler signature changes later |
| `approve` | The HITL callback. `None` means "no gate configured" |
| `logger` | Injected so a sub-agent's output can be prefixed or silenced |

**Concept: injected state, not globals.** Every handler is called as `handler(args, ctx)`. No
tool reaches for a module-level client, connection, or cwd. This is what makes a stage unit-
testable, and it's what lets step 3 hand a sub-agent a *narrowed* context — read-only DB handle,
different workdir — without a single tool knowing the difference.

Note `field(default=log)` rather than `default_factory`. A `Logger` from `logging.getLogger` is
a process-wide singleton by name, so sharing one instance across contexts is correct and
intended; a factory would imply each context wants its own, which it doesn't.

### The type aliases

```python
ApprovalFn = Callable[["Tool", dict[str, Any]], bool]
ToolHandler = Callable[[dict[str, Any], RunContext], Any]
```

`ApprovalFn` returns `True` to allow, `False` to refuse. It receives the whole `Tool` — not just
its name — so an approval UI can show the description and the `read_only` flag to the human.

`ToolHandler` returns `Any`, not `str`. Handlers are allowed to return dicts and lists;
`_stringify` at the bottom of the file coerces them. Forcing every handler to call
`json.dumps` itself is the kind of boilerplate that eventually gets it wrong.

### `object_schema` — making strict mode possible

```python
def object_schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties if required is None else required),
        "additionalProperties": False,
    }
```

**Concept: strict tool schemas.** Setting `strict: true` on a tool definition makes the API
guarantee that `tool_use.input` validates against your schema exactly — no missing keys, no
invented ones, no string where you asked for an integer. Without it, you are parsing
model-generated JSON hopefully.

Strict mode has two prerequisites, and this helper enforces both: `additionalProperties: false`,
and an explicit `required` list.

The default — `required` is *every* property — is deliberate. An optional argument is an
invitation for the model to omit it and for your handler to guess a default. If a parameter is
genuinely optional, pass `required=[...]` explicitly and handle the absence on purpose.

### `Tool` — one capability

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    requires_approval: bool = False
    read_only: bool = True
    strict: bool = True
```

`frozen=True` makes instances immutable. Tools are module-level constants shared across agents
and sub-agents; if one stage could mutate a shared `Tool`, another stage's behavior would change
underneath it.

**`description` is the highest-leverage string in the harness.** It is not documentation for
you — it is the prompt the model reads when deciding *whether and when* to call this tool.
Compare:

```python
description="Runs tests."                        # model has no idea when to reach for it
description="Run the Apex test classes you name against a Salesforce org. "
            "Use after analyzing a diff, once you know which classes cover the change. "
            "Returns pass/fail counts and the failure messages."
```

The second tells the model *when*, *what it needs first*, and *what it gets back*. Stage 2 will
live or die on this.

#### The two inert flags

```python
    requires_approval: bool = False  # step 4
    read_only: bool = True           # step 3
```

They do nothing today. They are declared now on purpose.

**Concept: the harness needs an action-specific hook to act on.** A model that can only call
`bash` hands your harness one opaque string for every action — it cannot tell a parallel-safe
`grep` from an irreversible `sf project deploy`. Promoting an action to a dedicated tool with
typed arguments gives the harness something to gate, serialize, render, or audit. Declaring the
flag at the point where the capability is defined means step 4 can add an approval gate without
touching a single tool.

#### `__post_init__` — failing at definition time

```python
    def __post_init__(self) -> None:
        schema = self.input_schema
        if schema.get("type") != "object":
            raise ValueError(...)
        if self.strict:
            if schema.get("additionalProperties") is not False:
                raise ValueError(...)
            if "required" not in schema:
                raise ValueError(...)
```

`frozen=True` blocks assignment, but `__post_init__` still runs — it's called by the generated
`__init__` before the instance is handed back.

**Concept: fail at the earliest possible moment.** A malformed schema caught here raises when
the module is imported. Caught by the API instead, it raises mid-run, after you've paid for
several turns, with a message about a JSON path rather than a tool name.

Note `is not False`, not `!= False` or `not schema.get(...)`. A missing key returns `None`,
which is falsy — `not None` is `True`, so a naive check would *accept* a schema missing the key
entirely. Identity against `False` is the only check that distinguishes "explicitly false" from
"absent."

#### `to_api_dict` — the wire format

```python
    def to_api_dict(self) -> dict[str, Any]:
        spec = {"name": ..., "description": ..., "input_schema": ...}
        if self.strict:
            spec["strict"] = True
        return spec
```

`strict` is a **top-level field on the tool definition** — a sibling of `name` and
`description`. It is not part of `input_schema`, and it is not a `tool_choice` setting. This
trips people up constantly.

`handler`, `requires_approval`, and `read_only` are deliberately *not* sent. They are harness
concerns; the model neither needs nor should see them.

### `ToolCall` — the audit record

```python
@dataclass
class ToolCall:
    iteration: int
    tool_use_id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    duration_s: float
```

Not frozen — it's a record, not a shared constant. `iteration` is what lets the demo assert
"two calls happened on iteration 1", i.e. that the model called tools **in parallel** rather
than serially across two turns. Step 6 writes these rows to SQLite verbatim.

### `ToolRegistry.__init__`

```python
        by_name: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in by_name:
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            by_name[tool.name] = tool
        self._tools = by_name
        self.specs = [t.to_api_dict() for t in tools]
```

Duplicate names are rejected rather than last-wins. A silently shadowed tool is a bug you'd
diagnose by reading model transcripts.

`self.specs` is computed **once**, at construction, and reused for every request.

**Concept: prompt-cache stability.** The API caches by *exact prefix match*, and the render
order is `tools` → `system` → `messages`. Rebuilding the tool list each turn risks a different
dict ordering or a regenerated schema object, which changes the serialized bytes, which
invalidates the cache for the entire rest of the request. Build it once, freeze it, reuse it.

### `ToolRegistry.dispatch` — the heart of failure containment

```python
    def dispatch(self, block, ctx, iteration):
        started = time.monotonic()
        tool = self._tools.get(block.name)
```

`time.monotonic()` rather than `time.time()`: monotonic clocks never jump backwards on an NTP
correction, so a duration can't come out negative.

`block` is duck-typed — `dispatch` only ever touches `.id`, `.name`, and `.input`. That's why
the demo can test it with a five-line `FakeToolUse` dataclass and no API call.

```python
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "(none)"
            return self._finish(..., f"Unknown tool {block.name!r}. Available tools: {known}",
                                is_error=True)
```

The model hallucinated a tool name. Note what this **doesn't** do: raise. It hands the model an
error that *lists the tools that do exist*, so the next turn can self-correct. An error message
written for a machine reader is a design decision.

```python
        if tool.requires_approval and ctx.approve is not None:
            if not ctx.approve(tool, dict(block.input)):
                return self._finish(..., "A human reviewer declined this action. Do not retry "
                                    "it; explain what you would have done and stop.",
                                    is_error=True)
```

The refusal text is an instruction, not a status. Without "do not retry," a determined agent
will call the tool again and you'll prompt the human in a loop.

`ctx.approve is not None` means an unconfigured gate is a no-op. Whether that default is right
is worth arguing about in step 4 — fail-open is convenient and, for an irreversible production
deploy, arguably wrong.

```python
        try:
            raw = tool.handler(dict(block.input), ctx)
        except Exception as exc:
            ctx.logger.warning("tool %s raised: %s", tool.name, exc)
            return self._finish(..., f"{type(exc).__name__}: {exc}", is_error=True)
```

**The single most important block in the file.** A bare `except Exception` is normally a code
smell; here it is the entire point.

**Concept: tool errors are data, not control flow.** The model asked for something, the world
said no, and that answer belongs in the transcript where the model can read it and adapt —
retry with different arguments, try another tool, or tell the human it's blocked. A harness that
propagates the exception kills the run over a transient sandbox timeout. *This is much of what
separates an agent from a script.*

It catches `Exception`, not `BaseException`, so `KeyboardInterrupt` and `SystemExit` still
terminate the process. Ctrl-C must always work.

`dict(block.input)` copies the SDK's object into a plain dict. A handler that mutates its
arguments then can't corrupt the record we're about to store in `ToolCall.input`.

### `_finish` — building the result block

```python
        if len(output) > MAX_TOOL_RESULT_CHARS:
            dropped = len(output) - MAX_TOOL_RESULT_CHARS
            output = output[:MAX_TOOL_RESULT_CHARS] + \
                     f"\n\n[truncated by harness: {dropped} more characters]"
```

Truncation **announces itself in-band**. The model reads that marker and knows its view is
partial — it can narrow the query instead of confidently reasoning over half a log. Silent
truncation produces confident wrong answers.

```python
        result = {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        }
        if is_error:
            result["is_error"] = True
```

`tool_use_id` **must** match the id from the `tool_use` block exactly. It is the *only* thing
binding a result to the call that produced it. When a turn contains three parallel calls, order
is not the binding — the id is.

`is_error` is added only when true. The field is optional in the API, and omitting it on success
keeps the serialized bytes minimal.

### `_stringify`

```python
def _stringify(value):
    if isinstance(value, str):  return value
    if value is None:           return ""
    try:                        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
```

`default=str` catches the values `json` can't serialize natively — `datetime`, `Path`, `Decimal`
— instead of raising. `indent=2` costs tokens but materially improves the model's ability to
read nested structures. The bare `str()` fallback guarantees this function never raises, which
matters because it runs inside the success path of `dispatch`.

---

## Part 2 — `agent.py`

### Module setup

```python
load_dotenv()
DEFAULT_MODEL = "claude-opus-5"
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
```

`load_dotenv()` at import reads `.env` into the environment, so `anthropic.Anthropic()` finds
`ANTHROPIC_API_KEY` with no argument. **Never pass the key explicitly** — a hardcoded key is one
commit away from being public, and the SDK's resolution order (env var → auth token → CLI
profile) is what makes the same code work on a laptop and in CI.

### The exception hierarchy

```python
class HarnessError(Exception): ...
class IterationCapExceeded(HarnessError): ...
class AgentRefused(HarnessError): ...
class OutputTruncated(HarnessError): ...
```

**Concept: two classes of failure, deliberately handled differently.**

| Failure | Handling | Why |
| --- | --- | --- |
| A tool failed | `tool_result` with `is_error` | The model can read it and adapt |
| The *run* failed structurally | Raise | The model cannot fix being out of iterations |

Distinct subclasses let a caller respond differently: `IterationCapExceeded` might warrant a
retry with a higher cap; `AgentRefused` never should.

### `Usage`

```python
    def add(self, raw: Any) -> "Usage":
        for name in _USAGE_FIELDS:
            setattr(self, name, getattr(self, name) + (getattr(raw, name, 0) or 0))
        return self
```

The `or 0` is load-bearing: cache fields come back as `None`, not `0`, when caching is inactive,
and `int + None` raises. `getattr(raw, name, 0)` additionally tolerates an SDK version that
doesn't have the field at all.

Returning `self` is what lets `Usage.of()` be a one-liner.

The four fields are tracked separately because they're **priced differently** — a cache read is
roughly a tenth of a fresh input token. Summing them into one number would hide exactly the
signal you're trying to watch.

### `AgentSpec` — a stage is configuration

```python
@dataclass(frozen=True)
class AgentSpec:
    name: str
    system_prompt: str
    tools: tuple[Tool, ...] = ()
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_tokens: int = 16_000
    max_iterations: int = 25
    refusal_fallbacks: bool = True
```

`tools` is a **tuple**, not a list, because `frozen=True` only blocks reassignment of the field
— a list could still be mutated in place. A tuple makes the tool surface genuinely immutable
once the spec exists.

| Knob | What it controls |
| --- | --- |
| `model` | Which model. Sub-agents in step 3 may drop to a cheaper one |
| `effort` | Thinking depth and token spend: `low` … `max`. Lower ⇒ fewer, more consolidated tool calls, terser output |
| `max_tokens` | Hard ceiling on **one response**. The model is not told about it — hitting it truncates mid-sentence |
| `max_iterations` | Ceiling on **loop turns**. The safety property |
| `refusal_fallbacks` | Route around a classifier refusal server-side instead of returning one |

`max_tokens` and `max_iterations` are frequently confused. The first bounds how much the model
can say *in one reply*; the second bounds *how many replies* the loop will accept. A stage can
hit either.

### `run_agent` — the loop itself

```python
    ctx = ctx or RunContext(run_id=uuid.uuid4().hex[:12], workdir=Path.cwd())
    client = client or anthropic.Anthropic()
    registry = ToolRegistry(spec.tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_input}]
```

`client` is an injectable parameter so tests can pass a fake, and so a caller can supply a client
configured with a different timeout or retry policy. The registry is built once per run — same
cache-stability reason as before.

```python
    for iteration in range(1, spec.max_iterations + 1):
```

A bounded `for`, not `while True`. The cap is structural: you cannot forget to decrement a
counter. Starting at 1 makes log lines and `ToolCall.iteration` read naturally.

```python
        response = client.beta.messages.create(**_request_kwargs(spec, registry, messages))
        usage.add(response.usage)
        per_iteration.append(Usage.of(response.usage))
```

Two accountings: a running total, and a per-iteration list. The per-iteration list is what proves
caching works — iteration 1 pays `cache_creation`, iteration 2 onward should show
`cache_read_input_tokens > 0`. A single total would average that away.

#### The `stop_reason` ladder

```python
        if response.stop_reason == "refusal":
            raise AgentRefused(...)
        if response.stop_reason == "max_tokens":
            raise OutputTruncated(...)
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "end_turn":
            return AgentResult(...)
        # else: tool_use
```

**Concept: `stop_reason` is the control signal.** Not the text. Not "does `content` contain a
`tool_use` block." The five values and their correct handling:

| Value | Means | Handling |
| --- | --- | --- |
| `end_turn` | Finished | Return |
| `tool_use` | Wants tools run | Dispatch, append, loop |
| `max_tokens` | Truncated mid-answer | **Raise** — the transcript is unusable |
| `pause_turn` | A long server-side tool turn paused | Append and re-send to resume |
| `refusal` | A safety classifier declined | Raise (or let `fallbacks` route around it) |

`refusal` is checked *first* because on a refusal `content` may be empty or partial — reading it
before checking gives you a confusing error instead of a clear one. `stop_details` is populated
only for refusals; it's `null` for every other stop reason, so it must never be read
unconditionally.

`max_tokens` raises rather than continuing. The assistant message is cut off mid-sentence;
appending it and looping teaches the model that half-sentences are normal, and every later turn
inherits the corruption.

`pause_turn` appends and `continue`s **without** running tools — no client tool was called; the
server just needs another round trip. It's the one branch that loops without a `tool_result`.

#### The tool-use branch

```python
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            raise HarnessError(f"{spec.name}: stop_reason=tool_use with no tool_use blocks")
```

A defensive assertion. If it ever fires, the API contract changed or `content` was mangled;
better a named error than an empty loop iteration that silently burns a turn.

```python
        messages.append({"role": "assistant", "content": response.content})
```

**`response.content`, raw.** Not `_final_text(response)`. Not a rebuilt list.

**Concept: the transcript is the state, so it must be lossless.** The raw block list carries the
`thinking` blocks and the `tool_use` blocks with their ids. Rebuild it from a string and you
drop the ids — the `tool_result` you send next references an id that no longer appears anywhere,
and the API rejects it, or worse, the reasoning chain is quietly severed. The SDK objects
serialize correctly when passed straight back; nothing needs converting.

```python
        results = []
        for block in tool_uses:
            result, call = registry.dispatch(block, ctx, iteration)
            results.append(result)
            calls.append(call)

        messages.append({"role": "user", "content": results})
```

**All results, one message.** Not one message per result.

**Concept: message shape is training signal.** One assistant turn may contain several
`tool_use` blocks — that's parallel tool calling, and it's on by default. The API expects every
corresponding `tool_result` in a **single** following user message. Split them across several
messages and the model infers that parallel calls aren't well received, and quietly stops making
them. You lose the parallelism and never see an error explaining why.

Also note: results go in a message with `role: "user"`. Tool output is *not* an assistant or
system role. Counter-intuitive the first time, but consistent — the user turn is "everything the
outside world is telling the model."

```python
    raise IterationCapExceeded(
        f"{spec.name}: still calling tools after {spec.max_iterations} iterations "
        f"({len(calls)} tool calls, {usage.output_tokens} output tokens)"
    )
```

Reached only if the `for` completes without returning — i.e. the agent was *still* asking for
tools on the final allowed turn. The message carries the diagnostics you'd otherwise have to
reconstruct.

### `_request_kwargs`

```python
        "system": [
            {"type": "text", "text": spec.system_prompt,
             "cache_control": {"type": "ephemeral"}}
        ],
```

`system` is a **list of content blocks**, not a plain string. The string form works, but you
cannot attach `cache_control` to it — that's the only reason for the block form here.

**Concept: prompt caching in an agent loop.** The render order is `tools` → `system` →
`messages`, and a breakpoint caches everything *before* it. So this one `cache_control` covers
the tool schemas **and** the system prompt — the entire stable prefix. In a ten-iteration run
that prefix would otherwise be re-sent and re-billed ten times. This is the single
highest-value line in the file, and it's why `ToolRegistry.specs` is built once: any byte change
anywhere in the prefix invalidates everything after it.

```python
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": spec.effort},
```

Adaptive thinking lets the model decide how much to reason per turn, and interleaves reasoning
between tool calls. There is no token budget to tune — `budget_tokens` is the older API and is
**rejected with a 400** on this model. Depth is controlled through `effort` instead, which lives
*inside* `output_config`, not at the top level.

```python
    if registry.specs:
        kwargs["tools"] = registry.specs
```

Only send `tools` when there are some. An empty list is a different prefix from an absent key.

```python
    if spec.refusal_fallbacks:
        kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
```

`fallbacks="default"` routes around a safety-classifier refusal server-side rather than returning
one. It requires the beta flag, which is why the harness calls `client.beta.messages.create`
rather than `client.messages.create`. Setting `refusal_fallbacks=False` drops both.

```python
        "messages": list(messages),
```

A shallow copy. The SDK shouldn't be handed the list we're about to keep appending to.

### `_final_text`

```python
def _final_text(response):
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
```

Filters to `text` blocks — a response may also contain `thinking` blocks, and touching `.text` on
one of those would raise. **Always check `.type` before reading `.text`.**

Joins *all* text blocks rather than taking the first. A response can legitimately be split into
several, and `next(...)` — the pattern in a lot of sample code — silently returns only the
opening fragment.

---

## Part 3 — A worked trace

What `messages` actually looks like across a two-iteration run. This is the thing worth being
able to draw from memory.

**Start** — one user message:

```python
[
  {"role": "user", "content": "Roll a d20 and look up the org 'xerticaDev'."}
]
```

**Iteration 1** — the model replies with `stop_reason: "tool_use"` and *two* `tool_use` blocks
in one message (parallel calls). We append it raw, dispatch both, and append both results in one
user message:

```python
[
  {"role": "user", "content": "Roll a d20 and look up the org 'xerticaDev'."},
  {"role": "assistant", "content": [
      ThinkingBlock(...),
      ToolUseBlock(id="toolu_01A", name="roll_dice", input={"sides": 20}),
      ToolUseBlock(id="toolu_01B", name="org_info", input={"alias": "xerticaDev"}),
  ]},
  {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "toolu_01A", "content": "rolled a 14 on a d20"},
      {"type": "tool_result", "tool_use_id": "toolu_01B", "content": '{"alias": ...}'},
  ]},
]
```

Three things to notice:

- the assistant message holds the **objects the SDK returned**, not text we rebuilt
- both results sit in **one** user message, ids matching their calls
- tool output arrives under `role: "user"`

**Iteration 2** — the whole array above is re-sent (that's statelessness), the model answers,
`stop_reason: "end_turn"`, loop returns. Note that iteration 2 pays full price for the messages
but should read the `tools` + `system` prefix from cache.

---

## Part 4 — Concept index

| Concept | Where it lives | One-line version |
| --- | --- | --- |
| Statelessness | the whole loop | No server-side session; resend everything, every turn |
| `stop_reason` as control signal | the ladder in `run_agent` | Branch on it, never on the content |
| Lossless transcript | `messages.append(response.content)` | Append raw blocks; text loses ids and thinking |
| Parallel tool calling | one user message of results | Split them up and the model stops doing it |
| `tool_use_id` binding | `_finish` | The id binds result to call — never position |
| Errors as data | `dispatch`'s `except Exception` | The model reads its own errors and adapts |
| Structural vs tool failure | `HarnessError` vs `is_error` | Raise what the model can't fix; hand back what it can |
| Iteration cap | `for … in range(...)` | A safety property, not a nicety |
| Prompt-cache stability | `specs` built once, `cache_control` on system | Prefix match: one byte changes, everything after invalidates |
| Strict schemas | `object_schema` + `strict: True` | Stop hoping the JSON validates |
| Injected state | `RunContext` | Testable stages; narrowable sub-agent contexts |
| Action-specific hooks | `requires_approval`, `read_only` | Dedicated tools give the harness something to gate |
| Context hygiene | `MAX_TOOL_RESULT_CHARS` | Truncate loudly; silence produces confident wrong answers |
| Tool description as prompt | `Tool.description` | It tells the model *when*, not just *what* |

---

## Exercises

Worth doing before step 2 — each takes a couple of minutes and each breaks something
instructive.

1. In `run_agent`, change the assistant append to `{"role": "assistant", "content":
   _final_text(response)}`. Run `--live`. Read the API error and work out which invariant you
   just broke.
2. Split the tool results into one user message each. It will still work — that's the point.
   Then reason about why it's wrong anyway.
3. Delete `cache_control` from the system block and compare `result.iteration_usage` across a
   run. Put a number on what that one line saves.
4. Set `effort="low"` on the demo spec and watch the tool-call count and preamble change.
5. Change `roll_dice`'s description to `"Rolls."` and see how the model's tool selection degrades.

## Next

[Step 2 — Stage 1: change analysis](00-architecture.md#roadmap).
