"""
cato/budget.py — Spending cap enforcement for CATO.

Tracks per-call, per-session, and monthly LLM spend across all 16 supported
models.  Raises BudgetExceeded before any call that would breach a cap.
State is persisted as human-readable JSON at ~/.cato/budget.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from .platform import get_data_dir

# Budget alert thresholds — warn when percent remaining hits these levels
BUDGET_ALERT_THRESHOLDS: list[int] = [20, 10, 5]

# Per-action costs for Conduit browser actions (cents)
_CONDUIT_ACTION_COSTS: dict[str, int] = {
    "navigate":   1,
    "click":      1,
    "type":       1,
    "extract":    2,
    "screenshot": 5,
}

# ---------------------------------------------------------------------------
# Pricing table  (USD per million tokens, [input, output])
# ---------------------------------------------------------------------------

_PRICING: dict[str, tuple[float, float]] = {
    # model-id                   input $/M   output $/M
    "claude-opus-4-6":          (15.00,      75.00),
    "claude-sonnet-4-6":        ( 3.00,      15.00),
    "claude-haiku-4-5":         ( 0.80,       4.00),
    "gpt-4o":                   ( 2.50,      10.00),
    "gpt-4o-mini":              ( 0.15,       0.60),
    "o3-mini":                  ( 1.10,       4.40),
    "gemini-2.0-pro":           ( 1.25,       5.00),
    "gemini-2.0-flash":         ( 0.10,       0.40),
    "gemini-2.0-flash-lite":    ( 0.075,      0.30),
    "deepseek-v3":              ( 0.27,       1.10),
    "deepseek-r1":              ( 0.55,       2.19),
    "groq-llama-3.3-70b":       ( 0.59,       0.79),
    "mistral-small":            ( 0.10,       0.30),
    "minimax-2.5":              ( 0.20,       1.00),
    "kimi-k2.5":                ( 0.15,       0.60),
    # SwarmSync routing is free; underlying model cost is tracked separately
    "swarmsync-router":         ( 0.00,       0.00),
}

_BUDGET_FILE = get_data_dir() / "budget.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Raised when a spend request would exceed a configured cap."""

    def __init__(
        self,
        message: str,
        cap_type: str,
        cap_value: float,
        current: float,
        call_cost: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.cap_type = cap_type
        self.cap_value = cap_value
        self.current = current
        self.call_cost = call_cost


# ---------------------------------------------------------------------------
# BudgetManager
# ---------------------------------------------------------------------------

class BudgetManager:
    """
    Tracks LLM spending and enforces session and monthly caps.

    All monetary amounts are in USD.

    Usage::

        bm = BudgetManager()
        cost = bm.estimate_cost("claude-sonnet-4-6", 1000, 500)
        bm.check_and_deduct("claude-sonnet-4-6", 1000, 500)
        print(bm.format_footer())
    """

    def __init__(
        self,
        session_cap: float = 3.00,
        monthly_cap: float = 20.00,
        budget_path: Optional[Path] = None,
    ) -> None:
        self._path = budget_path or _BUDGET_FILE
        self._session_cap = session_cap
        self._monthly_cap = monthly_cap

        # NOTE: Using float for monetary values. Accumulated rounding error is ~$0.01
        # over 100,000 calls at $0.001/call. Use decimal.Decimal in a future version
        # if sub-cent accuracy is required.
        self._session_spend: float = 0.0
        self._last_call_cost: float = 0.0
        self._lock = asyncio.Lock()

        self._state = self._load()

        # Override caps from persisted config if they were set differently
        if "session_cap" in self._state:
            self._session_cap = self._state["session_cap"]
        if "monthly_cap" in self._state:
            self._monthly_cap = self._state["monthly_cap"]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        """Load budget state from disk, returning defaults on first run."""
        if not self._path.exists():
            return self._default_state()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            # Roll over if we're in a new calendar month
            now_month = _current_month_key()
            if data.get("month_key") != now_month:
                data["monthly_spend"] = 0.0
                data["month_key"] = now_month
                data["monthly_calls"] = 0
            return data
        except (json.JSONDecodeError, KeyError):
            return self._default_state()

    def _default_state(self) -> dict:
        return {
            "month_key": _current_month_key(),
            "monthly_spend": 0.0,
            "monthly_calls": 0,
            "session_cap": self._session_cap,
            "monthly_cap": self._monthly_cap,
            "total_spend_all_time": 0.0,
            "call_log": [],          # last N calls for audit trail
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def estimate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Return estimated USD cost for the given model and token counts."""
        if model not in _PRICING:
            logger.warning(
                "Unknown model '%s' — using conservative fallback pricing ($3.00/$15.00 per M tokens)",
                model,
            )
            in_price, out_price = 3.00, 15.00
        else:
            in_price, out_price = _PRICING[model]
        cost = (input_tokens * in_price + output_tokens * out_price) / 1_000_000
        return round(cost, 8)

    async def check_and_deduct(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        allow_over_budget: bool = False,
    ) -> float:
        """
        Validate spend against caps, deduct if within budget, persist state.

        Returns the cost of the call.
        Raises BudgetExceeded if either session or monthly cap would be breached,
        unless allow_over_budget=True.
        Thread-safe via asyncio.Lock.
        """
        async with self._lock:
            cost = self.estimate_cost(model, input_tokens, output_tokens)
            override_reasons: list[str] = []

            # Session cap check
            if self._session_spend + cost > self._session_cap:
                if not allow_over_budget:
                    raise BudgetExceeded(
                        f"Session cap ${self._session_cap:.2f} would be exceeded "
                        f"(current ${self._session_spend:.4f}, call ${cost:.4f})",
                        cap_type="session",
                        cap_value=self._session_cap,
                        current=self._session_spend,
                        call_cost=cost,
                    )
                override_reasons.append("session")

            # Monthly cap check
            monthly = self._state["monthly_spend"]
            if monthly + cost > self._monthly_cap:
                if not allow_over_budget:
                    raise BudgetExceeded(
                        f"Monthly cap ${self._monthly_cap:.2f} would be exceeded "
                        f"(current ${monthly:.4f}, call ${cost:.4f})",
                        cap_type="monthly",
                        cap_value=self._monthly_cap,
                        current=monthly,
                        call_cost=cost,
                    )
                override_reasons.append("monthly")

            # Deduct
            self._session_spend += cost
            self._last_call_cost = cost
            self._state["monthly_spend"] = round(monthly + cost, 8)
            self._state["monthly_calls"] = self._state.get("monthly_calls", 0) + 1
            self._state["total_spend_all_time"] = round(
                self._state.get("total_spend_all_time", 0.0) + cost, 8
            )

            # Append to call log (keep last 100)
            log_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "budget_overridden": bool(override_reasons),
                "override_reasons": override_reasons,
            }
            call_log = self._state.get("call_log", [])
            call_log.append(log_entry)
            self._state["call_log"] = call_log[-100:]

            self._save()
            return cost

    def get_status(self) -> dict:
        """
        Return a dict with current spend, caps, and percentage remaining.

        Keys: session_spend, session_cap, session_pct_remaining,
              monthly_spend, monthly_cap, monthly_pct_remaining,
              monthly_calls, total_spend_all_time, month_key.
        """
        monthly = self._state["monthly_spend"]
        monthly_pct = max(0.0, (self._monthly_cap - monthly) / self._monthly_cap * 100)
        session_pct = max(0.0, (self._session_cap - self._session_spend) / self._session_cap * 100)

        return {
            "session_spend": round(self._session_spend, 6),
            "session_cap": self._session_cap,
            "session_pct_remaining": round(session_pct, 1),
            "monthly_spend": round(monthly, 6),
            "monthly_cap": self._monthly_cap,
            "monthly_pct_remaining": round(monthly_pct, 1),
            "monthly_calls": self._state.get("monthly_calls", 0),
            "total_spend_all_time": round(self._state.get("total_spend_all_time", 0.0), 6),
            "month_key": self._state.get("month_key", _current_month_key()),
        }

    def format_footer(self) -> str:
        """
        Return a one-line budget summary suitable for appending to agent responses.

        Example: [$0.003 this call | Month: $1.24/$20.00 | 75% remaining]
        """
        status = self.get_status()
        return (
            f"[${self._last_call_cost:.4f} this call"
            f" | Month: ${status['monthly_spend']:.2f}/${status['monthly_cap']:.2f}"
            f" | {status['monthly_pct_remaining']:.0f}% remaining]"
        )

    # ------------------------------------------------------------------
    # Cap management
    # ------------------------------------------------------------------

    def set_session_cap(self, cap: float) -> None:
        """Update the session cap and persist."""
        self._session_cap = cap
        self._state["session_cap"] = cap
        self._save()

    def set_monthly_cap(self, cap: float) -> None:
        """Update the monthly cap and persist."""
        self._monthly_cap = cap
        self._state["monthly_cap"] = cap
        self._save()

    def reset_session(self) -> None:
        """Reset session spend counter (call at the start of a new session)."""
        self._session_spend = 0.0
        self._last_call_cost = 0.0

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def estimate_task_cost(
        self,
        model: str,
        estimated_tokens: int,
        conduit_actions: Optional[list[str]] = None,
    ) -> dict:
        """
        Forecast the total cost of a task before it runs.

        Returns a dict with keys:
            model_cost_cents   — estimated LLM cost in cents (int)
            conduit_cost_cents — estimated Conduit browser cost in cents (int)
            total_cents        — total in cents (int)
            breakdown          — human-readable summary string

        Parameters:
            model            — model ID (must be in _PRICING)
            estimated_tokens — rough token estimate (input + output combined)
            conduit_actions  — list of browser action names (e.g. ["navigate", "click"])
        """
        if model not in _PRICING:
            raise ValueError(f"Unknown model '{model}'. Supported: {sorted(_PRICING)}")

        in_price, out_price = _PRICING[model]
        # Assume 60/40 input/output split for estimation
        in_tokens = int(estimated_tokens * 0.6)
        out_tokens = int(estimated_tokens * 0.4)
        model_cost_usd = (in_tokens * in_price + out_tokens * out_price) / 1_000_000
        model_cost_cents = max(0, int(round(model_cost_usd * 100)))

        conduit_cost_cents = 0
        if conduit_actions:
            for action in conduit_actions:
                conduit_cost_cents += _CONDUIT_ACTION_COSTS.get(action, 1)

        total_cents = model_cost_cents + conduit_cost_cents
        total_usd = total_cents / 100

        parts: list[str] = [f"~{estimated_tokens:,} tokens @ {model}"]
        if conduit_actions:
            parts.append(f"{len(conduit_actions)} browser action(s)")

        breakdown = (
            f"${total_usd:.4f} total "
            f"(model: {model_cost_cents}¢"
            + (f", browser: {conduit_cost_cents}¢" if conduit_actions else "")
            + f") — {' + '.join(parts)}"
        )

        return {
            "model_cost_cents": model_cost_cents,
            "conduit_cost_cents": conduit_cost_cents,
            "total_cents": total_cents,
            "breakdown": breakdown,
        }

    def prompt_cost_confirmation(self, estimate: dict, auto_confirm: bool = False) -> bool:
        """
        Show estimated cost and ask the user to confirm before proceeding.

        Returns True if the user confirms (or auto_confirm=True / estimate is free).
        Prints a summary: "Estimated cost: $X.XX (...). Proceed? [Y/n]"

        Also emits BUDGET_ALERT_THRESHOLDS warnings if session budget is running low.
        """
        if auto_confirm:
            return True

        total_usd = estimate["total_cents"] / 100
        breakdown = estimate.get("breakdown", "")

        # Check alert thresholds against session budget remaining
        session_pct = max(
            0.0,
            (self._session_cap - self._session_spend) / self._session_cap * 100
        ) if self._session_cap > 0 else 100.0

        for threshold in BUDGET_ALERT_THRESHOLDS:
            if session_pct <= threshold:
                _safe_print(
                    f"[BUDGET ALERT] Only {session_pct:.0f}% of session budget remaining "
                    f"(${self._session_spend:.4f} / ${self._session_cap:.2f} used)."
                )
                break

        _safe_print(f"\nEstimated cost: ${total_usd:.4f}  ({breakdown})")

        try:
            answer = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _safe_print("\nAborted.")
            return False

        return answer in ("", "y", "yes")

    @staticmethod
    def supported_models() -> list[str]:
        """Return sorted list of all supported model identifiers."""
        return sorted(_PRICING.keys())

    @staticmethod
    def pricing_table() -> dict[str, tuple[float, float]]:
        """Return the full pricing table (input $/M, output $/M) per model."""
        return dict(_PRICING)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_month_key() -> str:
    """Return 'YYYY-MM' for the current UTC month."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _safe_print(text: str) -> None:
    """Print using platform-safe print if available, else fallback to print()."""
    try:
        from .platform import safe_print
        safe_print(text)
    except Exception:
        print(text)
