"""Step 2: `pretzel review`. Deterministic change analysis plus one AI judgment pass.

    changes   = diff -> components          (code)
    manifests = components -> package.xml    (code)
    refs      = components -> who uses them  (code)
    judgment  = all of the above -> JSON     (model, strict schema)
    review    = facts + judgment, validated  (code)

Three ways to run it, and only the last one costs money:
    offline  no model at all; manifests and components only
    canned   a recorded model response replayed through the real loop (tests, demos)
    live     a real call to claude-opus-5, under a hard US$ cap
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from pretzel.harness import RunContext, ScriptedClient, run_agent, text_turn

from .components import ComponentChange, components_from_diff
from .manifest import Manifests, build_manifests
from .project import SfdxRepo
from .references import ComponentReferences, find_references
from .report import Review, dumps, merge, render_markdown
from .reviewer import ReviewInput, build_prompt, load_rules, parse_review, review_spec

__all__ = [
    "Analysis",
    "Review",
    "analyze",
    "dumps",
    "render_markdown",
    "run_review",
]


@dataclass
class Analysis:
    """Everything code computes before the model is involved."""

    repo: SfdxRepo
    base: str  # resolved SHA of the target branch
    head: str  # resolved SHA of the PR branch
    changes: list[ComponentChange]
    manifests: Manifests
    references: list[ComponentReferences]
    review_input: ReviewInput


def analyze(repo_root: str, base: str, head: str) -> Analysis:
    # sfdx-project.json is read at head: the PR may add a package directory.
    repo = SfdxRepo.open(repo_root, ref=head)
    base_sha, head_sha = repo.resolve(base), repo.resolve(head)
    files = repo.diff(base_sha, head_sha)
    changes = components_from_diff(repo, files, base_sha, head_sha)
    manifests = build_manifests(changes, repo.api_version)
    refs = find_references(repo, head_sha, changes)
    merge_base = repo.merge_base(base_sha, head_sha)
    review_input = build_prompt(repo, merge_base, head_sha, refs, load_rules(repo, base_sha))
    return Analysis(repo, base_sha, head_sha, changes, manifests, refs, review_input)


def run_review(
    analysis: Analysis,
    *,
    mode: str = "offline",
    canned: dict[str, Any] | None = None,
    client: Any = None,
    max_cost_usd: float | None = None,
) -> Review:
    a = analysis
    model_output: dict[str, Any] | None = None
    cost, calls = 0.0, 0

    if mode != "offline" and a.changes:
        if mode == "canned":
            if canned is None:
                raise ValueError("canned mode needs a canned model response")
            client = client or ScriptedClient([text_turn(canned)])
        elif mode == "live":
            if max_cost_usd is None or max_cost_usd <= 0:
                raise ValueError("live mode needs a positive max_cost_usd cap")
            if client is None:
                import anthropic

                client = anthropic.Anthropic()
        else:
            raise ValueError(f"unknown mode {mode!r}")

        merge_base = a.repo.merge_base(a.base, a.head)
        spec = review_spec(a.repo, merge_base, a.head, max_cost_usd=max_cost_usd)
        ctx = RunContext(run_id=uuid.uuid4().hex[:12], workdir=a.repo.root)
        # The first message (diffs, file text, references) is most of the input and is
        # resent on every loop iteration; a cache breakpoint on it bills those resends
        # at a tenth of the input price.
        first_message = [
            {
                "type": "text",
                "text": a.review_input.prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        result = run_agent(spec, first_message, ctx, client=client)
        model_output = parse_review(result.final_text)
        cost, calls = result.cost_usd, result.iterations

    def file_exists(path: str) -> bool:
        return a.repo.exists(a.head, path) or a.repo.exists(a.base, path)

    def line_count(path: str) -> int | None:
        text = a.repo.show(a.head, path)
        if text is None:
            text = a.repo.show(a.base, path)
        return None if text is None else len(text.splitlines())

    return merge(
        mode=mode if model_output is not None else "offline",
        base=a.base,
        head=a.head,
        changes=a.changes,
        refs=a.references,
        manifests=a.manifests,
        model_output=model_output,
        file_exists=file_exists,
        line_count=line_count,
        cost_usd=cost,
        model_calls=calls,
    )
