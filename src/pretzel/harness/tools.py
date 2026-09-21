"""Tool definitions and dispatch.

A `Tool` is the harness's unit of capability: a JSON-Schema'd function Claude may call
by name. Nothing in this module talks to the API - `agent.py` owns that. Keeping the
two apart is what lets a pipeline stage swap its entire tool surface without touching
the loop that drives it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger("pretzel.harness")

# A tool result that dwarfs the rest of the conversation is a context-management
# problem, not a feature. Truncate loudly rather than silently blowing the window.
MAX_TOOL_RESULT_CHARS = 20_000


@dataclass
class RunContext:
    """State handed to every tool handler.

    Handlers receive this rather than reaching for globals. That is what makes a stage
    testable, and what lets a sub-agent (step 3) be given a *narrowed* context - a
    different workdir, a read-only db handle - without any tool knowing the difference.
    """

    run_id: str
    workdir: Path
    db: sqlite3.Connection | None = None  # wired in step 6
    approve: "ApprovalFn | None" = None  # wired in step 4
    logger: logging.Logger = field(default=log)


# Returns True to let the call proceed, False to refuse it.
ApprovalFn = Callable[["Tool", dict[str, Any]], bool]

# Handlers return anything JSON-serializable; the registry coerces it to a string.
ToolHandler = Callable[[dict[str, Any], RunContext], Any]


def object_schema(
    properties: dict[str, Any], required: Sequence[str] | None = None
) -> dict[str, Any]:
    """Build an input schema that satisfies strict mode.

    Strict mode guarantees `tool_use.input` validates against the schema exactly, but
    it requires `additionalProperties: false` and an explicit `required` list. Default
    to "every property is required" - optional arguments are a common source of the
    model quietly guessing.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties if required is None else required),
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Tool:
    """One capability exposed to the model.

    `description` is not documentation - it is the prompt the model reads when deciding
    whether to call this tool. It is the highest-leverage string in the whole harness.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    # Inert flags today; the point of declaring them now is that the harness can act on
    # them later without every tool having to be rewritten.
    requires_approval: bool = False  # human-in-the-loop gate (step 4)
    read_only: bool = True  # parallel-safety marker (step 3)
    strict: bool = True

    def __post_init__(self) -> None:
        schema = self.input_schema
        if schema.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: input_schema must be an object schema")
        if self.strict:
            if schema.get("additionalProperties") is not False:
                raise ValueError(
                    f"tool {self.name!r}: strict mode needs additionalProperties false "
                    f"- build the schema with object_schema()"
                )
            if "required" not in schema:
                raise ValueError(
                    f"tool {self.name!r}: strict mode needs an explicit required list"
                )

    def to_api_dict(self) -> dict[str, Any]:
        """The wire format the Messages API expects in `tools`."""
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.strict:
            spec["strict"] = True
        return spec


@dataclass
class ToolCall:
    """An audit record of one dispatch. Step 6 persists these to SQLite."""

    iteration: int
    tool_use_id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    duration_s: float


class ToolRegistry:
    """Name -> Tool lookup, plus the dispatch that turns a call into a result block."""

    def __init__(self, tools: Sequence[Tool]) -> None:
        by_name: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in by_name:
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            by_name[tool.name] = tool
        self._tools = by_name
        self.specs = [t.to_api_dict() for t in tools]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def dispatch(
        self, block: Any, ctx: RunContext, iteration: int
    ) -> tuple[dict[str, Any], ToolCall]:
        """Execute one `tool_use` block and return its `tool_result` block.

        This function never raises on tool failure. Every failure mode - unknown tool,
        human refusal, an exception inside the handler - comes back as a `tool_result`
        with `is_error: True`, because a model that can read its own error can correct
        course, and a harness that crashes cannot.
        """
        started = time.monotonic()
        tool = self._tools.get(block.name)

        if tool is None:
            known = ", ".join(sorted(self._tools)) or "(none)"
            return self._finish(
                block,
                iteration,
                started,
                f"Unknown tool {block.name!r}. Available tools: {known}",
                is_error=True,
            )

        if tool.requires_approval and ctx.approve is not None:
            if not ctx.approve(tool, dict(block.input)):
                return self._finish(
                    block,
                    iteration,
                    started,
                    "A human reviewer declined this action. Do not retry it; explain "
                    "what you would have done and stop.",
                    is_error=True,
                )

        try:
            raw = tool.handler(dict(block.input), ctx)
        except Exception as exc:  # noqa: BLE001 - deliberate: errors become results
            ctx.logger.warning("tool %s raised: %s", tool.name, exc)
            return self._finish(
                block,
                iteration,
                started,
                f"{type(exc).__name__}: {exc}",
                is_error=True,
            )

        return self._finish(block, iteration, started, _stringify(raw), is_error=False)

    @staticmethod
    def _finish(
        block: Any, iteration: int, started: float, output: str, *, is_error: bool
    ) -> tuple[dict[str, Any], ToolCall]:
        if len(output) > MAX_TOOL_RESULT_CHARS:
            dropped = len(output) - MAX_TOOL_RESULT_CHARS
            output = (
                output[:MAX_TOOL_RESULT_CHARS]
                + f"\n\n[truncated by harness: {dropped} more characters]"
            )
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.id,  # must round-trip exactly - it binds result to call
            "content": output,
        }
        if is_error:
            result["is_error"] = True
        call = ToolCall(
            iteration=iteration,
            tool_use_id=block.id,
            name=block.name,
            input=dict(block.input),
            output=output,
            is_error=is_error,
            duration_s=time.monotonic() - started,
        )
        return result, call


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
