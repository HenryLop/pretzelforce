"""Smoke test for the harness.

Run it two ways:

    python -m pretzel.demo_harness            # offline checks only (no API key needed)
    python -m pretzel.demo_harness --live     # + a real run against claude-opus-5

The offline half proves the mechanical invariants - tool_use_id round-trips, errors
become results instead of exceptions, strict schemas are enforced. The live half proves
the loop itself: a parallel tool turn, recovery from a failing tool, the iteration cap,
and prompt caching kicking in on the second iteration.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pretzel.harness import (
    AgentSpec,
    IterationCapExceeded,
    RunContext,
    Tool,
    ToolRegistry,
    object_schema,
    run_agent,
)

PASS = "  PASS"
FAIL = "  FAIL"

# ---------------------------------------------------------------------------
# Toy tools
# ---------------------------------------------------------------------------


def _roll_dice(args: dict[str, Any], ctx: RunContext) -> str:
    sides = int(args["sides"])
    if sides < 2:
        raise ValueError("a die needs at least 2 sides")
    return f"rolled a {random.randint(1, sides)} on a d{sides}"


def _org_info(args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    return {"alias": args["alias"], "type": "sandbox", "api_version": "67.0"}


def _always_fails(args: dict[str, Any], ctx: RunContext) -> str:
    raise ConnectionError("sandbox refused the connection (this failure is deliberate)")


def _never_satisfied(args: dict[str, Any], ctx: RunContext) -> str:
    return "Not done yet. Call check_progress again to continue."


ROLL_DICE = Tool(
    name="roll_dice",
    description="Roll a single die and return the result. Use for any random number.",
    input_schema=object_schema({"sides": {"type": "integer", "description": "Faces."}}),
    handler=_roll_dice,
)

ORG_INFO = Tool(
    name="org_info",
    description="Look up metadata about a Salesforce org by its CLI alias.",
    input_schema=object_schema({"alias": {"type": "string", "description": "Org alias."}}),
    handler=_org_info,
)

FLAKY = Tool(
    name="deploy_status",
    description="Check the status of a Salesforce deploy. May fail if the org is down.",
    input_schema=object_schema({"deploy_id": {"type": "string", "description": "Id."}}),
    handler=_always_fails,
)

TREADMILL = Tool(
    name="check_progress",
    description="Check whether the long-running job has finished.",
    input_schema=object_schema({"job_id": {"type": "string", "description": "Job id."}}),
    handler=_never_satisfied,
)


# ---------------------------------------------------------------------------
# Offline checks
# ---------------------------------------------------------------------------


@dataclass
class FakeToolUse:
    """Stands in for a `tool_use` content block, which is all dispatch() needs."""

    id: str
    name: str
    input: dict[str, Any]


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4().hex[:12], workdir=Path.cwd())


def offline_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    ctx = _ctx()
    registry = ToolRegistry([ROLL_DICE, ORG_INFO, FLAKY])

    block = FakeToolUse("toolu_01", "org_info", {"alias": "xerticaDev"})
    result, call = registry.dispatch(block, ctx, iteration=1)
    out.append(
        (
            "tool_use_id round-trips exactly",
            result["tool_use_id"] == "toolu_01" and call.tool_use_id == "toolu_01",
            result["tool_use_id"],
        )
    )
    out.append(
        (
            "non-string handler output is JSON-encoded",
            "api_version" in result["content"] and "is_error" not in result,
            result["content"].replace("\n", " ")[:48],
        )
    )

    block = FakeToolUse("toolu_02", "deploy_status", {"deploy_id": "0Af000"})
    result, call = registry.dispatch(block, ctx, iteration=1)
    out.append(
        (
            "handler exception becomes is_error, not a crash",
            result.get("is_error") is True and "ConnectionError" in result["content"],
            result["content"][:48],
        )
    )

    block = FakeToolUse("toolu_03", "no_such_tool", {})
    result, _ = registry.dispatch(block, ctx, iteration=1)
    out.append(
        (
            "unknown tool becomes is_error listing what exists",
            result.get("is_error") is True and "org_info" in result["content"],
            result["content"][:48],
        )
    )

    denied = Tool(
        name="deploy",
        description="Deploy to production.",
        input_schema=object_schema({"org": {"type": "string", "description": "Org."}}),
        handler=lambda a, c: "deployed",
        requires_approval=True,
    )
    gated = ToolRegistry([denied])
    refusing = _ctx()
    refusing.approve = lambda tool, args: False
    block = FakeToolUse("toolu_04", "deploy", {"org": "prod"})
    result, _ = gated.dispatch(block, refusing, iteration=1)
    out.append(
        (
            "requires_approval + a refusing reviewer blocks the call",
            result.get("is_error") is True and "declined" in result["content"],
            result["content"][:48],
        )
    )

    try:
        Tool(
            name="loose",
            description="x",
            input_schema={"type": "object", "properties": {}},
            handler=lambda a, c: "",
        )
        out.append(("strict mode rejects a loose schema", False, "no error raised"))
    except ValueError as exc:
        out.append(("strict mode rejects a loose schema", True, str(exc)[:48]))

    try:
        ToolRegistry([ROLL_DICE, ROLL_DICE])
        out.append(("duplicate tool names rejected", False, "no error raised"))
    except ValueError as exc:
        out.append(("duplicate tool names rejected", True, str(exc)[:48]))

    return out


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------


def live_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    ctx = _ctx()

    spec = AgentSpec(
        name="demo",
        system_prompt=(
            "You are a terse test fixture for an agent harness. Use the tools you are "
            "given rather than guessing values. When you have the answer, state it in "
            "one short sentence and stop."
        ),
        tools=(ROLL_DICE, ORG_INFO, FLAKY),
        effort="low",
        max_tokens=2_000,
    )

    result = run_agent(
        spec,
        "Roll a d20 and look up the org 'xerticaDev'. Report both results.",
        ctx,
    )
    names = [c.name for c in result.tool_calls]
    out.append(
        (
            "loop completes and both tools were called",
            result.stop_reason == "end_turn" and {"roll_dice", "org_info"} <= set(names),
            f"{result.iterations} iters, tools={names}",
        )
    )

    first_turn = [c for c in result.tool_calls if c.iteration == 1]
    out.append(
        (
            "parallel calls arrived in one turn",
            len(first_turn) >= 2,
            f"{len(first_turn)} call(s) on iteration 1",
        )
    )

    cache_reads = sum(u.cache_read_input_tokens for u in result.iteration_usage[1:])
    out.append(
        (
            "prompt cache hit after iteration 1",
            cache_reads > 0,
            f"{cache_reads} cached input tokens re-read",
        )
    )

    recovery = run_agent(
        spec,
        "Check the status of deploy 0Af000000000001. If the tool fails, say so plainly "
        "and do not retry more than once.",
        ctx,
    )
    errored = [c for c in recovery.tool_calls if c.is_error]
    out.append(
        (
            "agent recovers from a failing tool instead of crashing",
            recovery.stop_reason == "end_turn" and len(errored) >= 1,
            f"{len(errored)} failed call(s), then finished",
        )
    )

    capped = AgentSpec(
        name="treadmill",
        system_prompt="Keep polling check_progress until the job is done. Never give up.",
        tools=(TREADMILL,),
        effort="low",
        max_tokens=1_000,
        max_iterations=3,
    )
    try:
        run_agent(capped, "Poll job 42 until it completes.", ctx)
        out.append(("iteration cap trips cleanly", False, "no exception raised"))
    except IterationCapExceeded as exc:
        out.append(("iteration cap trips cleanly", True, str(exc)[:60]))

    total = result.usage.input_tokens + recovery.usage.input_tokens
    out.append(
        (
            "token accounting is populated",
            total > 0,
            f"{total} input tokens across the runs above",
        )
    )
    return out


def _report(title: str, checks: list[tuple[str, bool, str]]) -> bool:
    print(f"\n{title}")
    print("-" * len(title))
    for label, ok, detail in checks:
        print(f"{PASS if ok else FAIL}  {label}")
        print(f"        {detail}")
    return all(ok for _, ok, _ in checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="also run against the real API (costs money)"
    )
    args = parser.parse_args()

    ok = _report("Offline checks (no API calls)", offline_checks())

    if args.live:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("\n--live needs ANTHROPIC_API_KEY. Copy .env.example to .env first.")
            return 2
        ok = _report("Live checks (real API calls)", live_checks()) and ok
    else:
        print("\nSkipping live checks. Re-run with --live to exercise the loop itself.")

    print("\nOK" if ok else "\nFAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
