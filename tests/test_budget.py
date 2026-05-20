"""Budget manager tests."""
import json
import pytest

from cato.budget import BudgetManager, BudgetExceeded


@pytest.fixture
def budget(tmp_path):
    # session_cap is informational (not enforced); daily_cap is the gate.
    return BudgetManager(
        session_cap=1.0,
        monthly_cap=10.0,
        daily_cap=1.0,
        budget_path=tmp_path / "budget.json",
    )


# ---------------------------------------------------------------------------
# Daily-cap enforcement (canonical short-horizon gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_budget_fires_before_call(budget):
    # gpt-4o-mini: $0.15/$0.60 per M -> 100k input + 50k output = $0.045
    cost = await budget.check_and_deduct("gpt-4o-mini", 100000, 50000)
    assert cost > 0
    # Now force a call that would breach the remaining daily cap
    with pytest.raises(BudgetExceeded) as exc:
        # claude-opus with huge tokens definitely breaches $1 cap
        await budget.check_and_deduct("claude-opus-4-6", 1000000, 500000)
    # The raised cap_type is the FIRST breached gate. With $1 daily cap and
    # $10 monthly cap, daily breaches first.
    assert exc.value.cap_type == "daily"


@pytest.mark.asyncio
async def test_session_cap_is_NOT_enforced(tmp_path):
    """Session cap is informational only; it must never block a call."""
    # Tiny session cap, generous daily + monthly.
    bm = BudgetManager(
        session_cap=0.000001,  # ridiculous
        monthly_cap=100.0,
        daily_cap=100.0,
        budget_path=tmp_path / "budget.json",
    )
    # Even a substantial call must succeed when only the session cap would
    # be breached.
    cost = await bm.check_and_deduct("claude-sonnet-4-6", 100_000, 50_000)
    assert cost > 0
    status = bm.get_status()
    # session_spend exceeds session_cap -- proves we don't gate on it.
    assert status["session_spend"] > status["session_cap"]


@pytest.mark.asyncio
async def test_daily_cap_blocks(tmp_path):
    bm = BudgetManager(
        session_cap=100.0,
        monthly_cap=100.0,
        daily_cap=0.01,
        budget_path=tmp_path / "budget.json",
    )
    with pytest.raises(BudgetExceeded) as exc:
        await bm.check_and_deduct("claude-opus-4-6", 1_000_000, 500_000)
    assert exc.value.cap_type == "daily"
    # Spend was not deducted
    assert bm.get_status()["daily_spend"] == 0


@pytest.mark.asyncio
async def test_monthly_backstop_blocks(tmp_path):
    """Daily cap is generous but monthly cap is tight -- monthly should fire."""
    bm = BudgetManager(
        session_cap=100.0,
        monthly_cap=0.01,
        daily_cap=100.0,
        budget_path=tmp_path / "budget.json",
    )
    with pytest.raises(BudgetExceeded) as exc:
        await bm.check_and_deduct("claude-opus-4-6", 1_000_000, 500_000)
    assert exc.value.cap_type == "monthly"


@pytest.mark.asyncio
async def test_budget_format_footer(budget):
    footer = budget.format_footer()
    assert "$" in footer
    # New footer reports Today + Month, not Session
    assert "Today" in footer
    assert "Month" in footer


def test_unknown_model_uses_fallback(budget):
    cost = budget.estimate_cost("unknown-model-xyz", 1_000_000, 0)
    assert cost == pytest.approx(3.00, rel=1e-4)


@pytest.mark.asyncio
async def test_budget_override_records_spend(tmp_path):
    """allow_over_budget=True still records the spend and tags overrides."""
    manager = BudgetManager(
        session_cap=100.0,
        monthly_cap=0.02,
        daily_cap=0.01,
        budget_path=tmp_path / "budget.json",
    )

    # Without override the daily cap fires first
    with pytest.raises(BudgetExceeded) as exc:
        await manager.check_and_deduct("claude-opus-4-6", 1_000_000, 500_000)
    assert exc.value.cap_type == "daily"
    assert manager.get_status()["daily_spend"] == 0

    # With override the spend goes through and is tagged
    cost = await manager.check_and_deduct(
        "claude-opus-4-6", 1_000_000, 500_000, allow_over_budget=True,
    )
    status = manager.get_status()
    assert cost > 0
    assert status["daily_spend"] == pytest.approx(cost)
    assert status["monthly_spend"] == pytest.approx(cost)
    log = manager._state["call_log"][-1]
    assert log["budget_overridden"] is True
    assert "daily" in log["override_reasons"]
    assert "monthly" in log["override_reasons"]


# ---------------------------------------------------------------------------
# Migration safety -- old budget.json without daily_cap/daily_spend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_from_legacy_budget_json(tmp_path):
    """A pre-existing budget.json without daily fields must migrate cleanly."""
    legacy_path = tmp_path / "budget.json"
    legacy_path.write_text(json.dumps({
        "month_key": "2026-05",
        "monthly_spend": 12.34,
        "monthly_calls": 5,
        "session_cap": 10.0,
        "monthly_cap": 50.0,
        "total_spend_all_time": 99.99,
        "call_log": [],
    }))

    bm = BudgetManager(
        session_cap=10.0,
        monthly_cap=50.0,
        daily_cap=3.0,
        budget_path=legacy_path,
    )

    # Migration preserves monthly history
    status = bm.get_status()
    assert status["monthly_cap"] == 50.0
    assert status["total_spend_all_time"] == 99.99
    # Daily tracker now exists
    assert status["daily_cap"] == 3.0
    assert status["daily_spend"] == 0.0
    assert "day_key" in status
    # session_cap is preserved but informational
    assert status["session_cap"] == 10.0


@pytest.mark.asyncio
async def test_daily_cap_setter_persists(tmp_path):
    bm = BudgetManager(
        session_cap=10.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    bm.set_daily_cap(7.50)
    reloaded = BudgetManager(
        session_cap=10.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    assert reloaded.get_status()["daily_cap"] == 7.50


# ---------------------------------------------------------------------------
# Slash-command parsing (round-trip through Gateway)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_budget_slash_command_status(tmp_path, monkeypatch):
    """'/budget' with no args returns a status snapshot."""
    from cato.config import CatoConfig
    from cato.gateway import Gateway

    cfg = CatoConfig(daily_cap=3.0, monthly_cap=50.0, session_cap=3.0)
    bm = BudgetManager(
        session_cap=3.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    gw = Gateway(cfg, bm, vault=None)

    sent: list[tuple[str, str, str]] = []

    async def fake_send(session_id, text, channel, **kw):
        sent.append((session_id, text, channel))

    monkeypatch.setattr(gw, "send", fake_send)

    handled = await gw._handle_slash_command("S1", "/budget", "web", "cato")
    assert handled is True
    assert any("Today:" in t for _, t, _ in sent)
    assert any("Month:" in t for _, t, _ in sent)


@pytest.mark.asyncio
async def test_budget_slash_command_daily_set(tmp_path, monkeypatch):
    """'/budget daily 5' raises the daily cap."""
    from cato.config import CatoConfig
    from cato.gateway import Gateway

    cfg = CatoConfig(daily_cap=3.0, monthly_cap=50.0, session_cap=3.0)
    cfg._path = tmp_path / "config.yaml"
    bm = BudgetManager(
        session_cap=3.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    gw = Gateway(cfg, bm, vault=None)

    async def fake_send(session_id, text, channel, **kw):
        pass

    monkeypatch.setattr(gw, "send", fake_send)

    await gw._handle_slash_command("S1", "/budget daily 5", "web", "cato")
    assert bm.get_status()["daily_cap"] == 5.0
    assert cfg.daily_cap == 5.0


@pytest.mark.asyncio
async def test_budget_slash_command_bypass_arms_flag(tmp_path, monkeypatch):
    from cato.config import CatoConfig
    from cato.gateway import Gateway

    cfg = CatoConfig(daily_cap=3.0, monthly_cap=50.0, session_cap=3.0)
    bm = BudgetManager(
        session_cap=3.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    gw = Gateway(cfg, bm, vault=None)

    async def fake_send(session_id, text, channel, **kw):
        pass

    monkeypatch.setattr(gw, "send", fake_send)

    assert gw._budget_bypass_armed is False
    await gw._handle_slash_command("S1", "/budget bypass", "web", "cato")
    assert gw._budget_bypass_armed is True


@pytest.mark.asyncio
async def test_budget_slash_command_monthly_set(tmp_path, monkeypatch):
    from cato.config import CatoConfig
    from cato.gateway import Gateway

    cfg = CatoConfig(daily_cap=3.0, monthly_cap=50.0, session_cap=3.0)
    cfg._path = tmp_path / "config.yaml"
    bm = BudgetManager(
        session_cap=3.0, monthly_cap=50.0, daily_cap=3.0,
        budget_path=tmp_path / "budget.json",
    )
    gw = Gateway(cfg, bm, vault=None)

    async def fake_send(session_id, text, channel, **kw):
        pass

    monkeypatch.setattr(gw, "send", fake_send)

    await gw._handle_slash_command("S1", "/budget monthly 100", "web", "cato")
    assert bm.get_status()["monthly_cap"] == 100.0
    assert cfg.monthly_cap == 100.0
