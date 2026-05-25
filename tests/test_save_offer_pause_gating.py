"""Tests for PAUSE_FEATURE_ENABLED gating inside save_offer_router.

The save-offer router used to mint a pause-specific rejoin URL for
``not_using`` and ``market_slow`` exit reasons. With the pause feature
not yet shipped on Whop, those rejoin URLs pointed at a non-existent
page while the templates rendered discount-based copy. This file pins
the corrected behaviour both ways:

  - flag off (default): B/C resolve to discount $79/3mo with a real
    promo'd Whop URL. No pause URL anywhere.
  - flag on (future state): B/C resolve to pause and use the configured
    pause URL, identical to the pre-gating behaviour.
"""
from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch

import pytest

from src.email_bot.db import EmailDB


def _reload_router(*, pause_enabled: bool, monkeypatch):
    """Reload the router module with the requested flag state and
    return the freshly-reloaded module."""
    if pause_enabled:
        monkeypatch.setenv("PAUSE_FEATURE_ENABLED", "true")
    else:
        monkeypatch.delenv("PAUSE_FEATURE_ENABLED", raising=False)
    import src.automations.save_offer_router as router_mod
    importlib.reload(router_mod)
    return router_mod


@pytest.fixture(autouse=True)
def _restore_router_module(monkeypatch):
    """Reload back to default (flag off) at teardown so other tests
    don't see flipped state."""
    yield
    monkeypatch.delenv("PAUSE_FEATURE_ENABLED", raising=False)
    import src.automations.save_offer_router as router_mod
    importlib.reload(router_mod)


# ---------------------------------------------------------------------------
# OFFER_VARIANTS table shape
# ---------------------------------------------------------------------------


class TestOfferVariantsShape:
    def test_flag_off_falls_back_to_discount(self, monkeypatch):
        m = _reload_router(pause_enabled=False, monkeypatch=monkeypatch)
        for reason in ("not_using", "market_slow"):
            cfg = m.OFFER_VARIANTS[reason]
            assert cfg.offer_type == "discount"
            assert cfg.amount_off == 20.0
            assert cfg.duration_months == 3
            assert cfg.base_code == "STAY79"

    def test_flag_on_uses_pause(self, monkeypatch):
        m = _reload_router(pause_enabled=True, monkeypatch=monkeypatch)
        for reason in ("not_using", "market_slow"):
            cfg = m.OFFER_VARIANTS[reason]
            assert cfg.offer_type == "pause"
            assert cfg.pause_days == 30
            assert cfg.amount_off == 0.0

    def test_other_variants_unaffected_by_flag(self, monkeypatch):
        # Offer A/D/E/F should be unchanged across both states.
        for state in (True, False):
            m = _reload_router(pause_enabled=state, monkeypatch=monkeypatch)
            assert m.OFFER_VARIANTS["too_expensive"].offer_type == "discount"
            assert m.OFFER_VARIANTS["too_expensive"].amount_off == 20.0
            assert m.OFFER_VARIANTS["quality_declined"].offer_type == "trial"
            assert m.OFFER_VARIANTS["found_alternative"].offer_type == "trial"
            assert m.OFFER_VARIANTS["other"].offer_type == "discount"


# ---------------------------------------------------------------------------
# route_offer behaviour with flag off (the load-bearing test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRouteOfferFlagOff:
    async def test_not_using_routes_to_discount_with_promo_url(
        self, tmp_path, monkeypatch,
    ):
        m = _reload_router(pause_enabled=False, monkeypatch=monkeypatch)
        db = EmailDB(db_path=str(tmp_path / "save_offer.db"))
        await db.open()
        try:
            router = m.SaveOfferRouter(
                email_db=db,
                promo_api_key="fake-key",
                whop_company_id="biz_test",
                rejoin_url_base="https://whop.com/potion",
                pause_url="https://whop.com/orders",
            )
            with patch(
                "src.automations.save_offer_router.create_one_time_promo",
                new=AsyncMock(return_value="STAY79-AB12"),
            ) as mock_mint:
                result = await router.route_offer(
                    email="nu@x.com", name="N", reason="not_using",
                )
            # With the flag off, not_using becomes a discount offer:
            assert result.offer_type == "discount"
            # The promo mint was called (with STAY79 base code).
            mock_mint.assert_called_once()
            kwargs = mock_mint.call_args.kwargs
            assert kwargs["base_code"] == "STAY79"
            assert kwargs["amount_off"] == 20.0
            # Rejoin URL carries the promo, NOT the pause URL.
            assert result.rejoin_url == (
                "https://whop.com/potion?promo=STAY79-AB12"
            )
            assert "orders" not in result.rejoin_url
        finally:
            await db.close()

    async def test_market_slow_routes_to_discount_with_promo_url(
        self, tmp_path, monkeypatch,
    ):
        m = _reload_router(pause_enabled=False, monkeypatch=monkeypatch)
        db = EmailDB(db_path=str(tmp_path / "save_offer.db"))
        await db.open()
        try:
            router = m.SaveOfferRouter(
                email_db=db,
                promo_api_key="fake-key",
                whop_company_id="biz_test",
                rejoin_url_base="https://whop.com/potion",
                pause_url="https://whop.com/orders",
            )
            with patch(
                "src.automations.save_offer_router.create_one_time_promo",
                new=AsyncMock(return_value="STAY79-CD34"),
            ):
                result = await router.route_offer(
                    email="ms@x.com", name="M", reason="market_slow",
                )
            assert result.offer_type == "discount"
            assert "orders" not in result.rejoin_url
        finally:
            await db.close()

    async def test_too_expensive_still_works_normally(
        self, tmp_path, monkeypatch,
    ):
        # Offer A is unaffected by the flag in either state.
        m = _reload_router(pause_enabled=False, monkeypatch=monkeypatch)
        db = EmailDB(db_path=str(tmp_path / "save_offer.db"))
        await db.open()
        try:
            router = m.SaveOfferRouter(
                email_db=db,
                promo_api_key="fake-key",
                whop_company_id="biz_test",
                rejoin_url_base="https://whop.com/potion",
            )
            with patch(
                "src.automations.save_offer_router.create_one_time_promo",
                new=AsyncMock(return_value="STAY79-XY99"),
            ):
                result = await router.route_offer(
                    email="te@x.com", name="T", reason="too_expensive",
                )
            assert result.offer_type == "discount"
            assert "promo=STAY79-XY99" in result.rejoin_url
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Defense-in-depth: even if pause variant somehow leaks in, route_offer
# refuses to use the pause URL when the flag is off.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRouteOfferDefenseInDepth:
    async def test_pause_variant_in_table_blocked_by_route_offer(
        self, tmp_path, monkeypatch,
    ):
        """If a pause variant ends up in OFFER_VARIANTS (manual patch,
        stale fixture) while the flag is off, the route_offer body
        refuses to mint a pause URL. This is the last line of defense."""
        m = _reload_router(pause_enabled=False, monkeypatch=monkeypatch)
        # Manually inject a pause variant into OFFER_VARIANTS
        m.OFFER_VARIANTS["not_using"] = m.SaveOfferConfig(
            reason="not_using",
            label="Offer B — pause (injected for test)",
            offer_type="pause",
            pause_days=30,
        )
        db = EmailDB(db_path=str(tmp_path / "save_offer.db"))
        await db.open()
        try:
            router = m.SaveOfferRouter(
                email_db=db,
                promo_api_key="fake-key",
                whop_company_id="biz_test",
                rejoin_url_base="https://whop.com/potion",
                pause_url="https://whop.com/orders",
            )
            result = await router.route_offer(
                email="x@x.com", name="X", reason="not_using",
            )
            # The route_offer body's defense-in-depth check should
            # have refused the pause URL and fallen back to the bare
            # rejoin URL.
            assert result.rejoin_url == "https://whop.com/potion"
            assert "orders" not in result.rejoin_url
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Flag-on path still works (the un-deprecated future)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRouteOfferFlagOn:
    async def test_pause_offer_uses_pause_url(
        self, tmp_path, monkeypatch,
    ):
        m = _reload_router(pause_enabled=True, monkeypatch=monkeypatch)
        db = EmailDB(db_path=str(tmp_path / "save_offer.db"))
        await db.open()
        try:
            router = m.SaveOfferRouter(
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
                    email="p@x.com", name="P", reason="not_using",
                )
            assert result.offer_type == "pause"
            assert result.rejoin_url == "https://whop.com/orders"
            # Pause flow never mints a promo.
            mock_mint.assert_not_called()
        finally:
            await db.close()
