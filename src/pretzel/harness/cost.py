"""Turning token counts into dollars, and refusing to spend past a cap.

Every live run of PretzelForce costs real money, so the harness enforces a hard ceiling
in code rather than trusting anyone to watch a dashboard. The loop checks the running
total after every model call; the first call that pushes it over the cap raises
`BudgetExceeded` and nothing further is sent.

The cap is checked *after* a call, because you cannot know what a call costs until it
returns. So the real worst case is `cap + one call`. Size the cap with that in mind.
"""

from __future__ import annotations

from dataclasses import dataclass

# US$ per million tokens, first-party Claude API list prices (cached 2026-06-24).
# Cache writes are billed at 1.25x input (5-minute TTL), cache reads at 0.1x input.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    @classmethod
    def for_model(cls, model: str) -> "Price":
        try:
            return cls(*PRICES_PER_MTOK[model])
        except KeyError:
            # An unknown model must not silently cost $0 and slip past the cap.
            raise ValueError(
                f"no price on file for model {model!r}; add it to PRICES_PER_MTOK"
            ) from None


def usage_cost_usd(usage: object, model: str) -> float:
    """Dollar cost of a `Usage` (or an API usage object) on `model`."""
    price = Price.for_model(model)
    per_in = price.input_per_mtok / 1_000_000
    per_out = price.output_per_mtok / 1_000_000
    fresh = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (
        fresh * per_in
        + written * per_in * CACHE_WRITE_MULTIPLIER
        + read * per_in * CACHE_READ_MULTIPLIER
        + out * per_out
    )
