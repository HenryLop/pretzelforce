"""`pretzel` command line.

    pretzel review --base main --head feature/x [--repo PATH]
        offline: components + manifests only, plus what a live run would cost
    pretzel review ... --canned review.json
        replay a recorded model answer through the real loop
    pretzel review ... --live --max-usd 0.50
        one real review by claude-opus-5, stopped hard at the cap
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pretzel.harness.cost import Price

from .review import analyze, dumps, render_markdown, run_review
from .review.reviewer import SYSTEM_PROMPT, review_spec

# Rough sizing for the estimate, not billing: ~3.5 characters per token for mixed
# code and XML, and a generous allowance for thinking + the JSON answer.
_CHARS_PER_TOKEN = 3.5
_OUTPUT_TOKENS_PER_CALL = 6_000


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; the report has emoji and Spanish API names.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="pretzel")
    sub = parser.add_subparsers(dest="command", required=True)

    rv = sub.add_parser("review", help="review a PR diff: manifests, references, findings")
    rv.add_argument("--base", required=True, help="target branch (what the PR merges into)")
    rv.add_argument("--head", required=True, help="source branch (the PR)")
    rv.add_argument("--repo", default=".", help="SFDX repo checkout (default: cwd)")
    rv.add_argument("--canned", type=Path, help="JSON model response to replay (no API call)")
    rv.add_argument("--live", action="store_true", help="call the Claude API (costs money)")
    rv.add_argument("--max-usd", type=float, help="hard spending cap for --live")
    rv.add_argument("--json", type=Path, help="also write the review JSON here")
    rv.add_argument("--out-dir", type=Path, help="write package.xml / destructiveChanges.xml here")

    args = parser.parse_args(argv)
    if args.command == "review":
        return _review(args)
    return 2


def _review(args: argparse.Namespace) -> int:
    if args.live and args.canned:
        print("--live and --canned are mutually exclusive", file=sys.stderr)
        return 2
    if args.live:
        if not args.max_usd or args.max_usd <= 0:
            print("--live needs --max-usd, a hard cap in US$ for this run", file=sys.stderr)
            return 2
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("--live needs ANTHROPIC_API_KEY (see .env.example)", file=sys.stderr)
            return 2

    analysis = analyze(args.repo, args.base, args.head)
    _print_estimate(analysis)

    if args.live:
        review = run_review(analysis, mode="live", max_cost_usd=args.max_usd)
    elif args.canned:
        canned = json.loads(args.canned.read_text(encoding="utf-8"))
        review = run_review(analysis, mode="canned", canned=canned)
    else:
        review = run_review(analysis, mode="offline")

    print(render_markdown(review))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(dumps(review), encoding="utf-8")
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "package.xml").write_text(review.manifests.package_xml, encoding="utf-8")
        destructive = args.out_dir / "destructiveChanges.xml"
        if review.manifests.destructive_xml:
            destructive.write_text(review.manifests.destructive_xml, encoding="utf-8")
        elif destructive.exists():
            destructive.unlink()  # a stale file from an earlier run would delete things
    return 1 if review.blocking else 0


def _print_estimate(analysis) -> None:
    """What one live review of this diff would cost, printed before anything is spent."""
    spec = review_spec(analysis.repo, analysis.base, analysis.head, max_cost_usd=None)
    price = Price.for_model(spec.model)
    chars = len(SYSTEM_PROMPT) + len(analysis.review_input.prompt)
    in_tokens = int(chars / _CHARS_PER_TOKEN) + 1_500  # + tool and schema definitions
    one_call = (
        in_tokens * price.input_per_mtok + _OUTPUT_TOKENS_PER_CALL * price.output_per_mtok
    ) / 1_000_000
    # Worst case: every allowed iteration resends the whole prompt (cache reads would
    # make it cheaper; the estimate ignores them on purpose).
    worst = one_call * spec.max_iterations
    print(
        f"[estimate] {len(analysis.changes)} component(s), prompt ~{in_tokens:,} tokens. "
        f"Live on {spec.model}: ~US${one_call:.2f} for a one-call review, "
        f"worst case ~US${worst:.2f} at {spec.max_iterations} calls.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
