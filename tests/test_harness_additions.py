"""The two harness additions for step 2: structured outputs and the hard US$ cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from pretzel.harness import (
    AgentSpec,
    BudgetExceeded,
    ScriptedClient,
    text_turn,
    tool_turn,
    usage_cost_usd,
)
from pretzel.harness import run_agent
from pretzel.harness.cost import Price
from pretzel.review import analyze
from pretzel.review.reviewer import review_spec


def test_cost_matches_list_prices() -> None:
    class U:
        input_tokens = 1_000_000
        output_tokens = 1_000_000
        cache_read_input_tokens = 1_000_000
        cache_creation_input_tokens = 1_000_000

    # 5 in + 25 out + 0.5 cache read + 6.25 cache write
    assert usage_cost_usd(U(), "claude-opus-5") == pytest.approx(36.75)


def test_unknown_model_has_no_free_pass() -> None:
    with pytest.raises(ValueError):
        Price.for_model("claude-imaginary-9")


def test_cap_stops_the_loop_before_the_next_call() -> None:
    spec = AgentSpec(name="t", system_prompt="x", max_cost_usd=0.01)
    # 10k input tokens on Opus 5 = $0.05, over the $0.01 cap after the first call.
    client = ScriptedClient(
        [tool_turn(("nothing", {}), input_tokens=10_000), text_turn("unreachable")]
    )
    with pytest.raises(BudgetExceeded, match="over the \\$0.01 cap"):
        run_agent(spec, "go", client=client)
    assert len(client.requests) == 1 and client.remaining == 1


def test_no_schema_means_no_format_key() -> None:
    client = ScriptedClient([text_turn("hi")])
    run_agent(AgentSpec(name="t", system_prompt="x"), "go", client=client)
    assert "format" not in client.requests[0]["output_config"]


def test_review_agent_can_use_its_tools(fixture_repo: Path) -> None:
    """A two-turn run: the model reads the Flow, then answers. Tools run for real."""
    a = analyze(str(fixture_repo), "main", "feature/delete-field")
    spec = review_spec(a.repo, a.base, a.head, max_cost_usd=1.0)
    flow = "force-app/main/default/flows/Account_Sync.flow-meta.xml"
    client = ScriptedClient(
        [
            tool_turn(("read_file", {"path": flow, "side": "head"}), ("search_repo", {"text": "Legacy_Code__c"})),
            text_turn({"summary": "s", "components": [], "findings": []}),
        ]
    )
    result = run_agent(spec, a.review_input.prompt, client=client)
    read, search = result.tool_calls
    assert not read.is_error and "9|             <field>Legacy_Code__c</field>" in read.output
    assert not search.is_error and "Account_Sync.flow-meta.xml:9" in search.output
    # Both results went back in one user message.
    assert len(client.requests[1]["messages"][-1]["content"]) == 2
    assert result.cost_usd > 0
