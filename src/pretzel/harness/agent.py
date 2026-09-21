"""The agent loop.

The Claude API is stateless: `POST /v1/messages` remembers nothing between calls, so
you resend the entire conversation every time. An "agent" is therefore not an API
feature - it is a loop you own, wrapped around that stateless endpoint:

    send history -> model replies -> if it asked for tools, run them and append the
    results -> send history again -> repeat until it stops asking.

Every stage of the pipeline runs on this one function. Stages differ only in their
system prompt and tool surface; the machinery that drives them is identical.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import anthropic
from dotenv import load_dotenv

from .tools import RunContext, Tool, ToolCall, ToolRegistry

load_dotenv()

DEFAULT_MODEL = "claude-opus-5"
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class HarnessError(Exception):
    """Base class for every way a run can fail structurally."""


class IterationCapExceeded(HarnessError):
    """The agent kept calling tools past `AgentSpec.max_iterations`."""


class AgentRefused(HarnessError):
    """A safety classifier declined the request (`stop_reason == "refusal"`)."""


class OutputTruncated(HarnessError):
    """The model hit `max_tokens` mid-answer, so the transcript is unusable."""


@dataclass
class Usage:
    """Token accounting, summed across every iteration of one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, raw: Any) -> "Usage":
        for name in _USAGE_FIELDS:
            setattr(self, name, getattr(self, name) + (getattr(raw, name, 0) or 0))
        return self

    @classmethod
    def of(cls, raw: Any) -> "Usage":
        return cls().add(raw)


@dataclass(frozen=True)
class AgentSpec:
    """Everything that makes one stage different from another.

    This is the whole point of the harness: a stage is *configuration*, not code.
    """

    name: str
    system_prompt: str
    tools: tuple[Tool, ...] = ()
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_tokens: int = 16_000
    max_iterations: int = 25
    # Anthropic's default guidance for claude-opus-5: route around a classifier refusal
    # server-side instead of returning one. Set False to drop the beta endpoint.
    refusal_fallbacks: bool = True


@dataclass
class AgentResult:
    agent: str
    final_text: str
    messages: list[dict[str, Any]]
    stop_reason: str
    iterations: int
    usage: Usage
    tool_calls: list[ToolCall]
    iteration_usage: list[Usage] = field(default_factory=list)


def run_agent(
    spec: AgentSpec,
    user_input: str,
    ctx: RunContext | None = None,
    *,
    client: anthropic.Anthropic | None = None,
) -> AgentResult:
    """Drive one agent to completion and return its transcript plus accounting."""
    ctx = ctx or RunContext(run_id=uuid.uuid4().hex[:12], workdir=Path.cwd())
    client = client or anthropic.Anthropic()
    registry = ToolRegistry(spec.tools)

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_input}]
    usage = Usage()
    per_iteration: list[Usage] = []
    calls: list[ToolCall] = []

    for iteration in range(1, spec.max_iterations + 1):
        response = client.beta.messages.create(
            **_request_kwargs(spec, registry, messages)
        )
        usage.add(response.usage)
        per_iteration.append(Usage.of(response.usage))
        ctx.logger.debug(
            "%s iter %d: stop_reason=%s", spec.name, iteration, response.stop_reason
        )

        # `stop_reason` is the control signal - not the text, not the presence of tool
        # blocks. Branching on anything else is the classic way to write a loop that
        # works in the happy path and silently misbehaves everywhere else.
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise AgentRefused(f"{spec.name}: request refused ({detail})")

        if response.stop_reason == "max_tokens":
            raise OutputTruncated(
                f"{spec.name}: hit max_tokens ({spec.max_tokens}) on iteration "
                f"{iteration}; raise max_tokens or narrow the task"
            )

        if response.stop_reason == "pause_turn":
            # A long-running server-side tool paused the turn. Append it and re-send;
            # the server picks up where it left off.
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason == "end_turn":
            return AgentResult(
                agent=spec.name,
                final_text=_final_text(response),
                messages=messages + [{"role": "assistant", "content": response.content}],
                stop_reason=response.stop_reason,
                iterations=iteration,
                usage=usage,
                tool_calls=calls,
                iteration_usage=per_iteration,
            )

        # stop_reason == "tool_use"
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            raise HarnessError(
                f"{spec.name}: stop_reason=tool_use with no tool_use blocks"
            )

        # Append the raw content blocks, never the extracted text. They carry the
        # thinking blocks and the tool_use ids; rebuilding this message from a string
        # silently corrupts the transcript.
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in tool_uses:
            result, call = registry.dispatch(block, ctx, iteration)
            results.append(result)
            calls.append(call)

        # Every result for this assistant turn goes back in ONE user message.
        # Splitting them across messages teaches the model to stop calling in parallel.
        messages.append({"role": "user", "content": results})

    raise IterationCapExceeded(
        f"{spec.name}: still calling tools after {spec.max_iterations} iterations "
        f"({len(calls)} tool calls, {usage.output_tokens} output tokens)"
    )


def _request_kwargs(
    spec: AgentSpec, registry: ToolRegistry, messages: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        # The system prompt and tool schemas are byte-identical on every iteration of
        # the loop, and they sit at the front of the prefix. Caching them is where the
        # money is in an agent loop - watch usage.cache_read_input_tokens from iter 2.
        "system": [
            {
                "type": "text",
                "text": spec.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": spec.effort},
        "messages": list(messages),
    }
    if registry.specs:
        kwargs["tools"] = registry.specs
    if spec.refusal_fallbacks:
        kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def _final_text(response: Any) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
