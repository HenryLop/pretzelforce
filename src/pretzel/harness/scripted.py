"""A stand-in for `anthropic.Anthropic` that replays canned responses.

`run_agent` only touches one method on its client: `client.beta.messages.create(**kw)`.
So an object with that method, returning objects shaped like API responses, is a
complete substitute. That's what makes every stage testable offline: the loop, the
tools, the parsing and the report all run for real, and only the model is canned.

It also records every request it receives, so a test can assert on what *would* have
been sent: the prompt carried the evidence, the schema was attached, the cap was set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def text_turn(
    text: str | dict[str, Any],
    *,
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    """A final answer. Pass a dict to get it JSON-encoded, as structured outputs does."""
    if not isinstance(text, str):
        text = json.dumps(text)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=_usage(input_tokens, output_tokens),
    )


def tool_turn(
    *calls: tuple[str, dict[str, Any]],
    input_tokens: int = 1_000,
    output_tokens: int = 100,
) -> SimpleNamespace:
    """A turn where the model asks for one or more tools: `tool_turn(("name", {...}))`."""
    blocks = [
        SimpleNamespace(type="tool_use", id=f"toolu_scripted_{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return SimpleNamespace(
        content=blocks,
        stop_reason="tool_use",
        stop_details=None,
        usage=_usage(input_tokens, output_tokens),
    )


@dataclass
class _Messages:
    turns: list[SimpleNamespace]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot the messages list: the loop keeps appending to the same list object.
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self.turns:
            raise RuntimeError(
                "ScriptedClient ran out of canned turns: the loop made one more call "
                "than the script expected"
            )
        return self.turns.pop(0)


class ScriptedClient:
    """`ScriptedClient([tool_turn(...), text_turn({...})])` replays turns in order."""

    def __init__(self, turns: Iterable[SimpleNamespace]) -> None:
        self._messages = _Messages(list(turns))
        self.beta = SimpleNamespace(messages=self._messages)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._messages.requests

    @property
    def remaining(self) -> int:
        return len(self._messages.turns)
