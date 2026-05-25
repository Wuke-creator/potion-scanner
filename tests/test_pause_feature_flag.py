"""Tests for the PAUSE_FEATURE_ENABLED flag gating save_offer B and C.

The pause flow (30-day membership pause + auto-reactivate) is on the
Potion roadmap but not yet shipped. Until it ships, save_offer variants
B (``not_using``) and C (``market_slow``) MUST NOT promise pause -- they
render a discount-based fallback. These tests pin that behaviour both
directions:

  - flag off (default): rendered copy talks about discount, not pause
  - flag on:            rendered copy talks about pause as before

Other reasons (``too_expensive`` Offer A, ``quality_declined`` Offer D,
etc.) are unaffected by the flag in either state.
"""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace

import pytest

from src.email_bot.db import Subscriber


def _sub(reason: str) -> Subscriber:
    return Subscriber(
        email="member@example.com", name="Luke",
        trigger_type="cancellation", exit_reason=reason,
        rejoin_url="https://whop.com/potion/recover",
        created_at=int(time.time()),
    )


def _stats() -> SimpleNamespace:
    return SimpleNamespace(
        calls_7d_total=22, wins_7d_over_50pct=5,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2},
        top_calls_7d=[{"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2}],
        calls_30d_total=92, wins_30d_over_50pct=18,
        top_call_30d={"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
        top_calls_30d=[
            {"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
            {"pair": "BONK/USDT", "pnl_pct": 115.0, "days_ago": 25},
        ],
        top_pair_7d="ETH/USDT", top_pct_7d=89,
        top_pair_30d="SOL/USDT", top_pnl_pct_30d=142,
    )


def _render(reason: str, *, pause_enabled: bool, monkeypatch):
    """Reload templates with the flag set to ``pause_enabled`` and render
    the save_offer for ``reason``. Returns the RenderedEmail object.

    Importlib.reload picks up the env change because the flag is read
    at module import time. The reload is undone at the end of each test
    by monkeypatch restoring the env, and a final reload restores the
    default state for the next test.
    """
    monkeypatch.setenv(
        "PAUSE_FEATURE_ENABLED", "true" if pause_enabled else "false",
    )
    import src.email_bot.templates as templates
    importlib.reload(templates)
    return templates._save_offer_day0(_sub(reason), _stats())


@pytest.fixture(autouse=True)
def _restore_templates_module(monkeypatch):
    """Reload templates back to its default (flag off) at teardown so
    other test files don't see a flipped flag from this file."""
    yield
    monkeypatch.delenv("PAUSE_FEATURE_ENABLED", raising=False)
    import src.email_bot.templates as templates
    importlib.reload(templates)


# ---------------------------------------------------------------------------
# Flag off (default)
# ---------------------------------------------------------------------------


class TestPauseFlagOff:
    def test_not_using_renders_discount_not_pause(self, monkeypatch):
        email = _render("not_using", pause_enabled=False, monkeypatch=monkeypatch)
        # Subject does NOT mention pause
        assert "pause" not in email.subject.lower()
        # Body talks about discount instead of pause
        assert "pause" not in email.text.lower()
        assert "$79" in email.text
        assert "20% off" in email.text

    def test_market_slow_renders_discount_not_pause(self, monkeypatch):
        email = _render("market_slow", pause_enabled=False, monkeypatch=monkeypatch)
        assert "pause" not in email.subject.lower()
        assert "pause" not in email.text.lower()
        assert "$79" in email.text

    def test_too_expensive_unaffected(self, monkeypatch):
        email = _render("too_expensive", pause_enabled=False, monkeypatch=monkeypatch)
        # Offer A always uses $79/month discount regardless of flag.
        assert "$79" in email.text
        assert "cheaper way to stay in" in email.subject

    def test_other_unaffected(self, monkeypatch):
        email = _render("other", pause_enabled=False, monkeypatch=monkeypatch)
        # Offer F always uses 25% off regardless of flag.
        assert "25%" in email.text


# ---------------------------------------------------------------------------
# Flag on (future state, once pause feature ships)
# ---------------------------------------------------------------------------


class TestPauseFlagOn:
    def test_not_using_renders_pause(self, monkeypatch):
        email = _render("not_using", pause_enabled=True, monkeypatch=monkeypatch)
        assert "pause" in email.subject.lower()
        assert "30-day pause" in email.text.lower() or "pause" in email.text.lower()

    def test_market_slow_renders_pause(self, monkeypatch):
        email = _render("market_slow", pause_enabled=True, monkeypatch=monkeypatch)
        assert "pause" in email.subject.lower()
        assert "pause" in email.text.lower()

    def test_too_expensive_still_unaffected(self, monkeypatch):
        email = _render("too_expensive", pause_enabled=True, monkeypatch=monkeypatch)
        # Offer A never mentions pause regardless of flag.
        assert "pause" not in email.subject.lower()
