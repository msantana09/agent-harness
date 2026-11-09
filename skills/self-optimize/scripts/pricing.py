"""List prices for Claude models, and the cost of one call from its logged usage.

Imported by scan.py, which prices the calls in the transcripts, and corrections.py, which
estimates its own calls before making them. Makes no network calls.

    import pricing
    cost = pricing.call_cost(model, pricing.token_counts(usage))

Inputs: a model id and the `usage` object a transcript logs for one call.
Outputs: USD at list price, or None for a model with no price here.

How the cost is made: input, output and cache reads at the model's rates; cache writes at 1.25x
the input price for the 5-minute cache and 2x for the 1-hour cache when the usage splits them,
and as 5-minute when it does not. Haiku 5.5 has two rate cards, picked by prompt size (input
plus cache tokens): one up to `HAIKU_5_5_PROMPT_STEP` tokens and one above it.
"""

from typing import Any

type Rates = tuple[float, float, float]  # USD per million (input, output), cache-read multiple

# Anthropic list prices, USD per million tokens: (input, output, cache-read multiple of input).
# Prices as published in November 2026; add a row when a model shows up as unpriced.
PRICES: dict[str, Rates] = {
    "claude-fable-5-2": (8.0, 40.0, 0.025),
    "claude-fable-5-1": (10.0, 50.0, 0.025),
    "claude-fable-5": (10.0, 50.0, 0.1),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-opus-5": (5.0, 25.0, 0.1),
    "claude-opus-4-8": (5.0, 25.0, 0.1),
    "claude-opus-4-7": (5.0, 25.0, 0.1),
    "claude-opus-4-6": (5.0, 25.0, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-sonnet-5": (2.0, 10.0, 0.1),
    "claude-sonnet-4-6": (3.0, 15.0, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.1),
}
HAIKU_5_5 = "claude-haiku-5-5"
HAIKU_5_5_PROMPT_STEP = 100_000
HAIKU_5_5_PRICES: tuple[Rates, Rates] = ((0.10, 0.50, 0.1), (0.50, 2.50, 0.1))  # (<= step, above)
CACHE_WRITE_5_MINUTE = 1.25  # multiple of the input price
CACHE_WRITE_1_HOUR = 2.0


def haiku_5_5_rates(prompt_tokens: float) -> Rates:
    """The Haiku 5.5 rate card for a prompt of `prompt_tokens` (input plus cache tokens)."""
    return HAIKU_5_5_PRICES[prompt_tokens > HAIKU_5_5_PROMPT_STEP]


def rates(model: str, prompt_tokens: float) -> Rates | None:
    """The rates a call to `model` with a prompt of `prompt_tokens` is billed at, or None."""
    return haiku_5_5_rates(prompt_tokens) if model == HAIKU_5_5 else PRICES.get(model)


def token_counts(usage: dict[str, Any]) -> dict[str, int]:
    """Input, cache-write (and its 1-hour part), cache-read and output tokens of one call."""

    def number(value: Any) -> int:
        return int(value) if isinstance(value, (int, float)) else 0

    split = usage.get("cache_creation")
    write = number(usage.get("cache_creation_input_tokens"))
    one_hour = number(split.get("ephemeral_1h_input_tokens")) if isinstance(split, dict) else 0
    return {
        "input": number(usage.get("input_tokens")),
        "cache_write": write,
        "cache_write_1h": min(one_hour, write),
        "cache_read": number(usage.get("cache_read_input_tokens")),
        "output": number(usage.get("output_tokens")),
    }


def call_cost(model: str, tokens: dict[str, int]) -> float | None:
    """List-price cost of one call; None when the model has no price here."""
    prices = rates(model, tokens["input"] + tokens["cache_write"] + tokens["cache_read"])
    if prices is None:
        return None
    price_in, price_out, read_multiple = prices
    five_minute_writes = tokens["cache_write"] - tokens["cache_write_1h"]
    return (
        tokens["input"] * price_in
        + five_minute_writes * price_in * CACHE_WRITE_5_MINUTE
        + tokens["cache_write_1h"] * price_in * CACHE_WRITE_1_HOUR
        + tokens["cache_read"] * price_in * read_multiple
        + tokens["output"] * price_out
    ) / 1e6
