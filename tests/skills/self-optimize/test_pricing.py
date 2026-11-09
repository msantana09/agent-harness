"""Tests for pricing.py: the cost of one call from its logged usage."""

from types import ModuleType

import pytest


@pytest.mark.parametrize(
    ("prompt", "price_in", "price_out"),
    [(100_000, 0.10, 0.50), (100_001, 0.50, 2.50)],
    ids=["prompt at the step", "prompt one token over the step"],
)
def test_haiku_5_5_is_priced_by_the_rate_card_its_prompt_size_falls_in(
    pricing: ModuleType, prompt: int, price_in: float, price_out: float
) -> None:
    usage = {
        "input_tokens": prompt - 60_000,
        "cache_read_input_tokens": 60_000,
        "output_tokens": 10,
    }
    tokens = pricing.token_counts(usage)
    expected = ((prompt - 60_000) * price_in + 60_000 * price_in * 0.1 + 10 * price_out) / 1e6
    assert pricing.call_cost("claude-haiku-5-5", tokens) == pytest.approx(expected)


def test_fable_5_2_is_priced_at_its_list_rates(pricing: ModuleType) -> None:
    usage = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000},
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    cost = pricing.call_cost("claude-fable-5-2", pricing.token_counts(usage))
    # $8 input, $16 one-hour cache write, $0.20 cache read, $40 output.
    assert cost == pytest.approx(64.20)


def test_a_model_without_a_price_costs_nothing_known(pricing: ModuleType) -> None:
    assert pricing.call_cost("claude-test-9", pricing.token_counts({"input_tokens": 10})) is None
