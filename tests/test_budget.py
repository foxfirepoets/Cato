"""Budget manager tests."""
import pytest
import asyncio
from cato.budget import BudgetManager, BudgetExceeded


@pytest.fixture
def budget(tmp_path):
    return BudgetManager(session_cap=1.0, monthly_cap=10.0, budget_path=tmp_path / "budget.json")


@pytest.mark.asyncio
async def test_budget_fires_before_call(budget):
    # Use a cheap model + small token count to fit within $1.00 session cap
    # gpt-4o-mini: $0.15/$0.60 per M → 100k input + 50k output = $0.015 + $0.030 = $0.045
    # First call should succeed
    cost = await budget.check_and_deduct("gpt-4o-mini", 100000, 50000)
    assert cost > 0
    # Exhaust remaining budget by pushing session spend over cap
    # Now force a call that would breach the remaining budget
    with pytest.raises(BudgetExceeded):
        # This call: ~$0.955 * 1000 = too expensive even with small model
        # Use claude-opus with huge tokens to definitely breach $1.00 cap
        await budget.check_and_deduct("claude-opus-4-6", 1000000, 500000)


@pytest.mark.asyncio
async def test_budget_format_footer(budget):
    footer = budget.format_footer()
    assert "$" in footer


def test_unknown_model_uses_fallback(budget):
    # Unknown models should use conservative fallback pricing, not raise
    cost = budget.estimate_cost("unknown-model-xyz", 1_000_000, 0)
    assert cost == pytest.approx(3.00, rel=1e-4)
