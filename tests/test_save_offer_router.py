"""Tests for AUT-026 Targeted Save Offer Router (src/automations/save_offer_router.py).

Covers:
  - OFFER_VARIANTS table maps every spec'd reason to a config
  - SaveOfferRouter.route_offer() schedules a save-offer email
  - Discount/trial offers attempt to mint a promo code
  - Pause offers skip promo minting and use the pause URL
  - Unknown reasons (Subscriber.exit_reason='none') don't enrol
  - The save_offer template renders for every variant
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from src.automations.save_offer_router import (
    OFFER_VARIANTS,
    SaveOfferRouter,
)
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.stats import StatsBundle
from src.email_bot.templates import render


def _make_stats() -> StatsBundle:
    """Minimal StatsBundle with the fields the save-offer template touches."""
    return StatsBundle(
        calls_7d_total=3,
        wins_7d_over_50pct=1,
        top_call_7d={"pair": "$WIF", "pnl_pct": 85.0, "days_ago": 1},
        top_calls_7d=[
            {"pair": "$WIF", "pnl_pct": 85.0, "days_ago": 1},
            {"pair": "$BONK", "pnl_pct": 64.0, "days_ago": 2},
            {"pair": "$PEPE", "pnl_pct": 51.0, "days_ago": 3},
        ],
        calls_30d_total=10,
        top_call_30d={"pair": "$WIF", "pnl_pct": 120.0, "days_ago": 5},
        top_calls_30d=[
            {"pair": "$WIF", "pnl_pct": 120.0, "days_ago": 5},
            {"pair": "$BONK", "pnl_pct": 95.0, "days_ago": 9},
            {"pair": "$PEPE", "pnl_pct": 80.0, "days_ago": 12},
            {"pair": "$DOGE", "pnl_pct": 60.0, "days_ago": 18},
            {"pair": "$SHIB", "pnl_pct": 45.0, "days_ago": 24},
        ],
    )


# ---------------------------------------------------------------------------
# Variant table sanity
# ---------------------------------------------------------------------------

class TestOfferVariants:
    def test_all_six_spec_reasons_mapped(self):
        """REV-6 Task 24 lists Offers A–F mapped to six reasons. Plus the
        fulfillment alias falls into Offer F."""
        for reason in (
            "too_expensive", "not_using", "market_slow",
            "quality_declined", "found_alternative", "other", "fulfillment",
        ):
            assert reason in OFFER_VARIANTS, f"missing variant for {reason}"

    def test_unknown_reason_has_no_variant(self):
        """`'none'` must NOT be in the table — the router skips it and
        falls through to the standard winback flow only."""
        assert "none" not in OFFER_VARIANTS
        assert "" not in OFFER_VARIANTS
        assert OFFER_VARIANTS.get("totally_made_up_reason") is None

    def test_pause_offers_have_zero_promo_amount(self, monkeypatch):
        # OFFER_VARIANTS is built at module import. The pause-feature flag
        # gates whether B/C resolve to pause or discount-fallback. Reload
        # the module with the flag on to assert the pause variants exist
        # in the shipped table (this is what production looks like once
        # the Whop pause flow goes live).
        import importlib
        monkeypatch.setenv("PAUSE_FEATURE_ENABLED", "true")
        import src.automations.save_offer_router as router_mod
        importlib.reload(router_mod)
        try:
            for reason in ("not_using", "market_slow"):
                cfg = router_mod.OFFER_VARIANTS[reason]
                assert cfg.offer_type == "pause"
                assert cfg.amount_off == 0.0
                assert cfg.base_code == ""
                assert cfg.pause_days == 30
        finally:
            # Reload with the flag off again so other tests see the
            # default (discount-fallback) shape.
            monkeypatch.delenv("PAUSE_FEATURE_ENABLED", raising=False)
            importlib.reload(router_mod)

    def test_pause_offers_fall_back_to_discount_when_flag_off(self):
        # Default state in the test process: PAUSE_FEATURE_ENABLED is
        # not set, so B/C should fall back to discount $79/3mo.
        for reason in ("not_using", "market_slow"):
            cfg = OFFER_VARIANTS[reason]
            assert cfg.offer_type == "discount"
            assert cfg.amount_off == 20.0
            assert cfg.duration_months == 3
            assert cfg.base_code == "STAY79"

    def test_discount_offers_have_promo_params(self):
        for reason in ("too_expensive", "other", "fulfillment"):
            cfg = OFFER_VARIANTS[reason]
            assert cfg.offer_type == "discount"
            assert cfg.amount_off > 0
            assert cfg.base_code != ""
            assert cfg.duration_months > 0

    def test_trial_offers_use_full_discount(self):
        for reason in ("quality_declined", "found_alternative"):
            cfg = OFFER_VARIANTS[reason]
            assert cfg.offer_type == "trial"
            assert cfg.amount_off == 100.0
            assert cfg.base_code != ""


# ---------------------------------------------------------------------------
# SaveOfferRouter.route_offer behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSaveOfferRouter:
    async def _make_db(self, tmp_path):
        db = EmailDB(db_path=str(tmp_path / "save_offer_test.db"))
        await db.open()
        return db

    async def test_unknown_reason_skips_routing(self, tmp_path):
        db = await self._make_db(tmp_path)
        router = SaveOfferRouter(
            email_db=db,
            promo_api_key="fake-key",
            whop_company_id="biz_test",
        )
        result = await router.route_offer(
            email="user@example.com", name="User", reason="none",
        )
        assert result.routed is False
        # No subscriber row created, no scheduled send
        assert await db.get_subscriber("user@example.com") is None
        await db.close()

    async def test_pause_offer_skips_promo_mint(self, tmp_path, monkeypatch):
        # Pause flow is gated on PAUSE_FEATURE_ENABLED. Flip it on for
        # this test so the OFFER_VARIANTS table contains the pause
        # variants we're asserting on. importlib.reload picks up the
        # env change because the flag is read at module import.
        import importlib
        monkeypatch.setenv("PAUSE_FEATURE_ENABLED", "true")
        import src.automations.save_offer_router as router_mod
        importlib.reload(router_mod)
        try:
            db = await self._make_db(tmp_path)
            router = router_mod.SaveOfferRouter(
                email_db=db,
                promo_api_key="fake-key",
                whop_company_id="biz_test",
                pause_url="https://whop.com/orders",
            )
            with patch(
                "src.automations.save_offer_router.create_one_time_promo",
                new=AsyncMock(),
            ) as mock_mint:
                result = await router.route_offer(
                    email="pause@example.com", name="Pause",
                    reason="not_using",
                )
            assert result.routed is True
            assert result.offer_type == "pause"
            assert result.promo_code is None
            # Pause offers must NOT call the promo API
            mock_mint.assert_not_called()
            # Rejoin URL is the pause URL
            assert result.rejoin_url == "https://whop.com/orders"
            await db.close()
        finally:
            monkeypatch.delenv("PAUSE_FEATURE_ENABLED", raising=False)
            importlib.reload(router_mod)

    async def test_discount_offer_sends_plain_url_no_promo(self, tmp_path):
        # Promo codes/discounts were removed from save-offer emails.
        # Discount/trial variants now send the plain base rejoin URL and
        # never mint a code.
        db = await self._make_db(tmp_path)
        router = SaveOfferRouter(
            email_db=db,
            promo_api_key="fake-key",
            whop_company_id="biz_test",
            rejoin_url_base="https://whop.com/potion",
        )
        with patch(
            "src.automations.save_offer_router.create_one_time_promo",
            new=AsyncMock(return_value="STAY79-A8K2X9"),
        ) as mock_mint:
            result = await router.route_offer(
                email="disc@example.com",
                name="Disc",
                reason="too_expensive",
                discord_user_id="123456",
            )
        assert result.routed is True
        assert result.offer_type == "discount"
        assert result.promo_code is None
        assert result.rejoin_url == "https://whop.com/potion"
        assert "promo=" not in result.rejoin_url
        mock_mint.assert_not_called()
        await db.close()

    async def test_trial_offer_sends_plain_url_no_promo(self, tmp_path):
        # Trial variants (quality_declined / found_alternative) likewise
        # send the plain base rejoin URL with no minted code.
        db = await self._make_db(tmp_path)
        router = SaveOfferRouter(
            email_db=db,
            promo_api_key="fake-key",
            whop_company_id="biz_test",
            rejoin_url_base="https://whop.com/potion",
        )
        with patch(
            "src.automations.save_offer_router.create_one_time_promo",
            new=AsyncMock(return_value="FREE7-A8K2X9"),
        ) as mock_mint:
            result = await router.route_offer(
                email="trial@example.com", name="T", reason="quality_declined",
            )
        assert result.routed is True
        assert result.offer_type == "trial"
        assert result.promo_code is None
        assert result.rejoin_url == "https://whop.com/potion"
        assert "promo=" not in result.rejoin_url
        mock_mint.assert_not_called()
        await db.close()

    async def test_unconfigured_promo_skips_mint_attempt(self, tmp_path):
        db = await self._make_db(tmp_path)
        # No promo key. Router never calls the API for any variant now.
        router = SaveOfferRouter(
            email_db=db,
            promo_api_key="",
            whop_company_id="",
        )
        with patch(
            "src.automations.save_offer_router.create_one_time_promo",
            new=AsyncMock(),
        ) as mock_mint:
            result = await router.route_offer(
                email="nokey@example.com", name="N", reason="too_expensive",
            )
        assert result.routed is True
        assert result.promo_code is None
        assert result.rejoin_url == "https://whop.com/potion"
        mock_mint.assert_not_called()
        await db.close()

    async def test_routed_offer_schedules_save_offer_send(self, tmp_path):
        db = await self._make_db(tmp_path)
        router = SaveOfferRouter(
            email_db=db,
            promo_api_key="",
            whop_company_id="",
        )
        await router.route_offer(
            email="user@example.com", name="User", reason="market_slow",
        )
        # One pending send for the save_offer sequence at day 0
        sub = await db.get_subscriber("user@example.com")
        assert sub is not None
        assert sub.exit_reason == "market_slow"
        assert sub.trigger_type == "save_offer"
        sends = await db.due_sends(now=int(time.time()) + 1)
        assert len(sends) == 1
        assert sends[0].sequence == "save_offer"
        assert sends[0].day == 0
        await db.close()


# ---------------------------------------------------------------------------
# Template rendering — every variant must render without crashing
# ---------------------------------------------------------------------------

class TestSaveOfferTemplate:
    @pytest.mark.parametrize("reason", [
        "too_expensive", "not_using", "market_slow",
        "quality_declined", "found_alternative", "other", "fulfillment",
        # Defensive case: unknown reason should still render Offer F copy.
        "totally_unknown_reason",
    ])
    def test_renders_for_every_reason(self, reason):
        sub = Subscriber(
            email="t@example.com",
            name="Tester",
            trigger_type="save_offer",
            exit_reason=reason,
            rejoin_url="https://whop.com/potion?promo=TEST-CODE",
            created_at=int(time.time()),
        )
        rendered = render(
            sequence="save_offer", day=0, subscriber=sub, stats=_make_stats(),
        )
        assert rendered.subject
        assert "Tester" in rendered.text
        # CTA link present in both formats
        assert "TEST-CODE" in rendered.text or "https://whop.com/potion" in rendered.text
        assert "TEST-CODE" in rendered.html or "https://whop.com/potion" in rendered.html
