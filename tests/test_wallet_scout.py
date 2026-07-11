"""Tests for the wallet scout: screen, fill replay, scoring, hysteresis,
persistence, and the run_once integration with fully faked IO.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import WalletCopyConfig
from src.trading.hl_info_client import LeaderboardRow
from src.trading.wallet_metrics_db import (
    StoredPosition,
    TrackedWallet,
    WalletMetrics,
    WalletMetricsDB,
)
from src.trading.wallet_scout import (
    WalletScout,
    apply_hysteresis,
    compute_fill_stats,
    copyability_score,
    hl_coin_to_blofin_base,
    screen_leaderboard,
    short_addr,
)

CFG = WalletCopyConfig()

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
T0 = 1_760_000_000_000  # fixed epoch ms base for deterministic fills


def _row(
    address="0x" + "1" * 40, account=100_000.0, day=100.0, week=700.0,
    month=3000.0, alltime=20_000.0, vlm=1_000_000.0, month_roi=0.10,
):
    return LeaderboardRow(
        address=address, account_value=account,
        pnl={"day": day, "week": week, "month": month, "allTime": alltime},
        roi={"month": month_roi},
        volume={"allTime": vlm},
    )


def _fill(coin, t, sz, side, px, start, closed=0.0):
    return {
        "coin": coin, "time": t, "sz": str(sz), "side": side, "px": str(px),
        "startPosition": str(start), "closedPnl": str(closed),
    }


def _round_trip(coin, t0, *, win=True, size=10.0, px=100.0, hold_ms=4 * HOUR_MS):
    """One long round trip: open + close, closedPnl on the exit."""
    pnl = 50.0 if win else -40.0
    return [
        _fill(coin, t0, size, "B", px, 0.0),
        _fill(coin, t0 + hold_ms, size, "A", px * 1.01, size, closed=pnl),
    ]


class TestScreen:
    def test_passes_good_wallet(self):
        assert screen_leaderboard([_row()], CFG) == [_row()]

    def test_rejects_account_out_of_range(self):
        assert screen_leaderboard([_row(account=5_000.0)], CFG) == []
        assert screen_leaderboard([_row(account=50_000_000.0)], CFG) == []

    def test_rejects_negative_windows(self):
        assert screen_leaderboard([_row(week=-1.0)], CFG) == []
        assert screen_leaderboard([_row(month=0.0)], CFG) == []
        assert screen_leaderboard([_row(alltime=-5.0)], CFG) == []

    def test_rejects_one_month_wonder(self):
        # allTime barely above month: everything earned this month
        assert screen_leaderboard([_row(month=3000.0, alltime=3100.0)], CFG) == []

    def test_rejects_market_maker_volume(self):
        assert screen_leaderboard(
            [_row(vlm=100_000.0 * 200)], CFG,
        ) == []

    def test_sorts_by_month_roi(self):
        a = _row(address="0x" + "a" * 40, month_roi=0.05)
        b = _row(address="0x" + "b" * 40, month_roi=0.50)
        assert [r.address for r in screen_leaderboard([a, b], CFG)] == [
            b.address, a.address,
        ]


class TestFillStats:
    def test_empty(self):
        s = compute_fill_stats([], account_value=1000.0)
        assert s.n_fills == 0 and s.n_episodes == 0

    def test_round_trips_win_rate_and_pf(self):
        fills = (
            _round_trip("A", T0, win=True)
            + _round_trip("B", T0 + DAY_MS, win=True)
            + _round_trip("C", T0 + 2 * DAY_MS, win=False)
        )
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + 3 * DAY_MS)
        assert s.n_episodes == 3
        assert s.win_rate == pytest.approx(2 / 3)
        assert s.profit_factor == pytest.approx(100.0 / 40.0)

    def test_hold_time_median(self):
        fills = (
            _round_trip("A", T0, hold_ms=2 * HOUR_MS)
            + _round_trip("B", T0 + DAY_MS, hold_ms=6 * HOUR_MS)
        )
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + 2 * DAY_MS)
        assert s.median_hold_min == pytest.approx(4 * 60)

    def test_conviction_uses_peak_notional_over_account(self):
        # 10 units at $100 = $1000 peak on a $10k account -> 0.10
        fills = _round_trip("A", T0, size=10.0, px=100.0)
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + DAY_MS)
        assert s.conviction_median == pytest.approx(0.10, rel=0.05)

    def test_flip_ends_episode_and_starts_new(self):
        fills = [
            _fill("A", T0, 10, "B", 100, 0.0),
            # sell 25: closes the 10-long (pnl booked) and opens a 15-short
            _fill("A", T0 + HOUR_MS, 25, "A", 101, 10.0, closed=10.0),
            _fill("A", T0 + 2 * HOUR_MS, 15, "B", 100, -15.0, closed=15.0),
        ]
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + DAY_MS)
        assert s.n_episodes == 2
        assert s.win_rate == 1.0

    def test_drawdown_tracks_cumulative_curve(self):
        fills = (
            _round_trip("A", T0, win=True)          # +50 -> peak 50
            + _round_trip("B", T0 + DAY_MS, win=False)   # -40 -> dd 40
            + _round_trip("C", T0 + 2 * DAY_MS, win=False)  # -40 -> dd 80
        )
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + 3 * DAY_MS)
        assert s.max_drawdown_usd == pytest.approx(80.0)

    def test_top_trade_share(self):
        fills = (
            _round_trip("A", T0, win=True)   # +50
            + _round_trip("B", T0 + DAY_MS, win=True)  # +50
        )
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + 2 * DAY_MS)
        assert s.top_trade_share == pytest.approx(0.5)

    def test_fills_per_day_and_dormancy(self):
        fills = _round_trip("A", T0)
        s = compute_fill_stats(
            fills, account_value=10_000.0, now_ms=T0 + 10 * DAY_MS,
        )
        assert s.hours_since_fill > 200


class TestScore:
    def _stats(self, **kw):
        from src.trading.wallet_scout import FillStats

        base = dict(
            n_fills=40, span_days=30.0, fills_per_day=2.0, win_rate=0.6,
            profit_factor=2.0, max_drawdown_usd=3000.0, median_hold_min=300.0,
            conviction_median=0.15, top_trade_share=0.3, hours_since_fill=5.0,
            n_episodes=20,
        )
        base.update(kw)
        return FillStats(**base)

    def test_good_wallet_scores_above_promote_line(self):
        s = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        assert s >= CFG.promote_score

    def test_too_few_episodes_scores_zero(self):
        s = copyability_score(
            _row(), self._stats(n_episodes=2), blofin_coverage=1.0, cfg=CFG,
        )
        assert s == 0.0

    def test_scalper_hard_penalty(self):
        good = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        scalper = copyability_score(
            _row(), self._stats(fills_per_day=40.0), blofin_coverage=1.0, cfg=CFG,
        )
        assert scalper < good * 0.5

    def test_dormant_penalty(self):
        good = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        dormant = copyability_score(
            _row(), self._stats(hours_since_fill=200.0),
            blofin_coverage=1.0, cfg=CFG,
        )
        assert dormant <= good * 0.6

    def test_lucky_trade_and_coverage_lower_score(self):
        base = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        lucky = copyability_score(
            _row(), self._stats(top_trade_share=0.9), blofin_coverage=1.0, cfg=CFG,
        )
        uncovered = copyability_score(
            _row(), self._stats(), blofin_coverage=0.2, cfg=CFG,
        )
        assert lucky < base
        assert uncovered < base


class TestHysteresis:
    def test_promotion_needs_streak(self):
        addr = "0x" + "c" * 40
        r1 = apply_hysteresis({}, {addr: (80.0, False)}, CFG)
        assert r1.promoted == []
        assert r1.wallets[addr].streak_above == 1
        r2 = apply_hysteresis(r1.wallets, {addr: (75.0, False)}, CFG)
        assert r2.promoted == [addr]
        assert r2.wallets[addr].status == "tracked"

    def test_one_bad_night_does_not_demote(self):
        addr = "0x" + "d" * 40
        tracked = {addr: TrackedWallet(address=addr, status="tracked", score=70.0)}
        r = apply_hysteresis(tracked, {addr: (10.0, False)}, CFG)
        assert r.demoted == []
        assert r.wallets[addr].status == "tracked"
        assert r.wallets[addr].streak_below == 1

    def test_demotion_after_streak(self):
        addr = "0x" + "e" * 40
        state = {addr: TrackedWallet(address=addr, status="tracked", score=70.0)}
        for _ in range(CFG.demote_streak - 1):
            state = apply_hysteresis(state, {addr: (10.0, False)}, CFG).wallets
            assert state[addr].status == "tracked"
        r = apply_hysteresis(state, {addr: (10.0, False)}, CFG)
        assert r.demoted == [addr]
        assert r.wallets[addr].status == "candidate"

    def test_full_set_requires_swap_margin(self):
        tracked = {
            f"0xt{i}": TrackedWallet(
                address=f"0xt{i}", status="tracked", score=65.0,
            )
            for i in range(CFG.max_tracked)
        }
        cand = "0x" + "f" * 40
        # near-worst candidate with a full streak: no swap
        state = dict(tracked)
        state[cand] = TrackedWallet(
            address=cand, status="candidate", score=66.0,
            streak_above=CFG.promote_streak,
        )
        today = {a: (w.score, False) for a, w in state.items()}
        r = apply_hysteresis(state, today, CFG)
        assert r.promoted == []
        # clearly better candidate: swaps out the worst
        state[cand] = TrackedWallet(
            address=cand, status="candidate", score=90.0,
            streak_above=CFG.promote_streak,
        )
        today[cand] = (90.0, False)
        r = apply_hysteresis(state, today, CFG)
        assert cand in r.promoted
        assert len(r.demoted) == 1

    def test_scalper_never_promoted(self):
        addr = "0x" + "9" * 40
        state = {}
        for _ in range(CFG.promote_streak + 1):
            state = apply_hysteresis(state, {addr: (95.0, True)}, CFG).wallets
        assert state[addr].status == "candidate"

    def test_missing_wallet_keeps_state(self):
        addr = "0x" + "8" * 40
        tracked = {addr: TrackedWallet(address=addr, status="tracked", score=70.0)}
        r = apply_hysteresis(tracked, {}, CFG)
        assert r.demoted == [] and r.wallets[addr].status == "tracked"


class TestHelpers:
    def test_short_addr(self):
        assert short_addr("0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d") == "0xadd1..5e0d"

    def test_k_prefix_maps_to_1000(self):
        assert hl_coin_to_blofin_base("kPEPE") == "1000PEPE"
        assert hl_coin_to_blofin_base("HYPE") == "HYPE"
        assert hl_coin_to_blofin_base("k") == "K"


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = WalletMetricsDB(db_path=str(tmp_path / "wallet_scout.db"))
    await d.open()
    yield d
    await d.close()


class TestDB:
    @pytest.mark.asyncio
    async def test_metrics_roundtrip_and_history(self, db):
        for i, day in enumerate(["2026-07-09", "2026-07-10", "2026-07-11"]):
            await db.upsert_metrics(WalletMetrics(
                address="0x1", snapshot_date=day, score=50.0 + i,
            ))
        scores = await db.recent_scores("0x1", limit=2)
        assert scores == [52.0, 51.0]
        latest = await db.latest_metrics("0x1")
        assert latest.snapshot_date == "2026-07-11"

    @pytest.mark.asyncio
    async def test_metrics_upsert_same_day_overwrites(self, db):
        await db.upsert_metrics(WalletMetrics(
            address="0x1", snapshot_date="2026-07-11", score=10.0,
        ))
        await db.upsert_metrics(WalletMetrics(
            address="0x1", snapshot_date="2026-07-11", score=20.0,
        ))
        assert await db.recent_scores("0x1") == [20.0]

    @pytest.mark.asyncio
    async def test_tracked_roundtrip(self, db):
        await db.save_tracked_wallet(TrackedWallet(
            address="0x2", status="tracked", score=70.0, streak_above=2,
        ))
        w = await db.get_tracked_wallet("0x2")
        assert w.status == "tracked" and w.streak_above == 2
        assert [t.address for t in await db.list_wallets("tracked")] == ["0x2"]

    @pytest.mark.asyncio
    async def test_positions_baseline_roundtrip(self, db):
        assert not await db.is_baselined("0x3")
        await db.replace_positions("0x3", {
            "HYPE": StoredPosition(coin="HYPE", szi=10.0, entry_px=25.0),
        })
        assert await db.is_baselined("0x3")
        pos = await db.get_positions("0x3")
        assert pos["HYPE"].szi == 10.0
        await db.replace_positions("0x3", {})
        assert await db.get_positions("0x3") == {}
        assert await db.is_baselined("0x3")

    @pytest.mark.asyncio
    async def test_remove_wallet_clears_everything(self, db):
        await db.save_tracked_wallet(TrackedWallet(address="0x4"))
        await db.replace_positions("0x4", {
            "ZEC": StoredPosition(coin="ZEC", szi=-1.0),
        })
        await db.remove_wallet("0x4")
        assert await db.get_tracked_wallet("0x4") is None
        assert await db.get_positions("0x4") == {}
        assert not await db.is_baselined("0x4")


def _make_scout(db, *, leaderboard, fills_by_addr, instruments=None,
                allowlist=frozenset({99}), cfg=None):
    info = AsyncMock()
    info.get_leaderboard = AsyncMock(return_value=leaderboard)

    async def _fills(addr):
        return fills_by_addr.get(addr, [])

    info.get_user_fills = AsyncMock(side_effect=_fills)
    blofin = AsyncMock()
    blofin.get_instruments = AsyncMock(
        return_value=instruments if instruments is not None
        else {"A": object(), "B": object(), "C": object(), "HYPE": object()},
    )
    dms: list[tuple[int, str]] = []

    async def _dm(uid, text):
        dms.append((uid, text))

    scout = WalletScout(
        info_client=info, metrics_db=db, blofin_client=blofin,
        config=cfg or WalletCopyConfig(seed_addresses=()),
        send_dm=_dm, allowlist=allowlist,
    )
    return scout, dms


class TestScoutRunOnce:
    @pytest.mark.asyncio
    async def test_seeds_tracked_wallets_on_first_run(self, db, monkeypatch):
        monkeypatch.setattr("src.trading.wallet_scout._VERIFY_PACE_SEC", 0)
        cfg = WalletCopyConfig(seed_addresses=("0xseed1", "0xseed2"))
        scout, _ = _make_scout(
            db, leaderboard=[], fills_by_addr={}, cfg=cfg,
        )
        await scout.run_once()
        tracked = await db.list_wallets("tracked")
        assert {w.address for w in tracked} == {"0xseed1", "0xseed2"}

    @pytest.mark.asyncio
    async def test_verifies_and_persists_metrics(self, db, monkeypatch):
        monkeypatch.setattr("src.trading.wallet_scout._VERIFY_PACE_SEC", 0)
        addr = "0x" + "1" * 40
        fills = []
        for i in range(8):
            fills += _round_trip("A", T0 + i * DAY_MS, win=(i % 3 != 0))
        scout, _ = _make_scout(
            db, leaderboard=[_row(address=addr)],
            fills_by_addr={addr: fills},
        )
        summary = await scout.run_once()
        assert summary["screened"] == 1
        assert summary["verified"] == 1
        latest = await db.latest_metrics(addr)
        assert latest is not None
        assert latest.win_rate > 0

    @pytest.mark.asyncio
    async def test_digest_dm_only_on_changes(self, db, monkeypatch):
        monkeypatch.setattr("src.trading.wallet_scout._VERIFY_PACE_SEC", 0)
        scout, dms = _make_scout(db, leaderboard=[], fills_by_addr={})
        await scout.run_once()
        assert dms == []   # nothing screened, nothing changed

    @pytest.mark.asyncio
    async def test_promotion_after_streak_sends_digest(self, db, monkeypatch):
        monkeypatch.setattr("src.trading.wallet_scout._VERIFY_PACE_SEC", 0)
        addr = "0x" + "2" * 40
        fills = []
        for i in range(20):
            fills += _round_trip(
                "A", T0 + i * DAY_MS, win=(i % 4 != 0), size=20.0, px=100.0,
            )
        # make "now" close to the last fill so the wallet isn't dormant
        monkeypatch.setattr(
            "src.trading.wallet_scout.compute_fill_stats",
            lambda f, account_value, now_ms=None: compute_fill_stats(
                f, account_value=account_value, now_ms=T0 + 20 * DAY_MS,
            ),
        )
        scout, dms = _make_scout(
            db, leaderboard=[_row(address=addr)], fills_by_addr={addr: fills},
        )
        r1 = await scout.run_once()
        assert r1["promoted"] == []
        r2 = await scout.run_once()
        assert r2["promoted"] == [addr]
        assert len(dms) == 1
        assert "now tracking" in dms[0][1]
        assert "No positions were opened or closed" in dms[0][1]


class TestScoreV2:
    def _stats(self, **kw):
        from src.trading.wallet_scout import FillStats

        base = dict(
            n_fills=40, span_days=30.0, fills_per_day=2.0, win_rate=0.6,
            profit_factor=2.0, max_drawdown_usd=3000.0, median_hold_min=300.0,
            conviction_median=0.15, top_trade_share=0.3, hours_since_fill=5.0,
            n_episodes=20,
        )
        base.update(kw)
        return FillStats(**base)

    def test_liquidation_veto(self):
        s = copyability_score(
            _row(), self._stats(liquidation_count=1),
            blofin_coverage=1.0, cfg=CFG,
        )
        assert s == 0.0

    def test_hold_time_hard_gate(self):
        s = copyability_score(
            _row(), self._stats(median_hold_min=90.0),   # 1.5h < 4h gate
            blofin_coverage=1.0, cfg=CFG,
        )
        assert s == 0.0

    def test_hard_fills_gate_below_scalper_threshold(self):
        # 15/day is under the old 25 scalper line but over the v2 hard gate
        s = copyability_score(
            _row(), self._stats(fills_per_day=15.0),
            blofin_coverage=1.0, cfg=CFG,
        )
        assert s == 0.0

    def test_high_wr_trap_discount(self):
        base = copyability_score(
            _row(), self._stats(win_rate=0.75, avg_win=100.0, avg_loss=50.0),
            blofin_coverage=1.0, cfg=CFG,
        )
        trap = copyability_score(
            _row(), self._stats(win_rate=0.75, avg_win=50.0, avg_loss=200.0),
            blofin_coverage=1.0, cfg=CFG,
        )
        assert trap < base

    def test_negative_skew_discount(self):
        base = copyability_score(
            _row(), self._stats(pnl_skew=0.5), blofin_coverage=1.0, cfg=CFG,
        )
        skewed = copyability_score(
            _row(), self._stats(pnl_skew=-2.0), blofin_coverage=1.0, cfg=CFG,
        )
        assert skewed == pytest.approx(base * 0.8, rel=0.01)

    def test_btc_beta_discount(self):
        base = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        beta = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
            btc_corr=0.9,
        )
        assert beta == pytest.approx(base * 0.8, rel=0.01)

    def test_delta_neutral_discount(self):
        base = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        neutral = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
            net_direction_ratio=0.1,
        )
        assert neutral == pytest.approx(base * 0.5, rel=0.01)

    def test_latency_unfit_discount(self):
        base = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
        )
        unfit = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
            latency_fit=(-50.0, 0.9),
        )
        decayed = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
            latency_fit=(100.0, 0.3),
        )
        fit = copyability_score(
            _row(), self._stats(), blofin_coverage=1.0, cfg=CFG,
            latency_fit=(100.0, 0.9),
        )
        assert unfit == pytest.approx(base * 0.2, rel=0.01)
        assert decayed == pytest.approx(base * 0.2, rel=0.01)
        assert fit == pytest.approx(base, rel=0.01)

    def test_subwindow_consistency_lifts_and_drops(self):
        steady = copyability_score(
            _row(), self._stats(subwindow_pfs=[1.5, 1.8, 1.4]),
            blofin_coverage=1.0, cfg=CFG,
        )
        lumpy = copyability_score(
            _row(), self._stats(subwindow_pfs=[6.0, 0.4, 0.6]),
            blofin_coverage=1.0, cfg=CFG,
        )
        assert steady > lumpy

    def test_sortino_replaces_wr_weight_when_present(self):
        # same wr/pf, better downside profile scores higher
        low = copyability_score(
            _row(), self._stats(sortino_like=0.1), blofin_coverage=1.0, cfg=CFG,
        )
        high = copyability_score(
            _row(), self._stats(sortino_like=2.0), blofin_coverage=1.0, cfg=CFG,
        )
        assert high > low


class TestFillStatsV2:
    def test_liquidation_fills_counted(self):
        fills = _round_trip("A", T0)
        fills[1]["dir"] = "Market Liquidation"
        s = compute_fill_stats(fills, account_value=10_000.0, now_ms=T0 + DAY_MS)
        assert s.liquidation_count == 1

    def test_daily_pnl_series(self):
        fills = _round_trip("A", T0) + _round_trip("B", T0 + DAY_MS)
        s = compute_fill_stats(fills, account_value=10_000.0,
                               now_ms=T0 + 2 * DAY_MS)
        assert len(s.daily_pnl) == 2
        assert sum(s.daily_pnl.values()) == pytest.approx(100.0)

    def test_avg_win_loss_and_skew(self):
        fills = []
        for i in range(12):
            fills += _round_trip("A", T0 + i * DAY_MS, win=(i % 3 != 0))
        s = compute_fill_stats(fills, account_value=10_000.0,
                               now_ms=T0 + 13 * DAY_MS)
        assert s.avg_win == pytest.approx(50.0)
        assert s.avg_loss == pytest.approx(40.0)
        assert s.pnl_skew is not None
        assert s.sortino_like is not None
        assert len(s.subwindow_pfs) == 3

    def test_pearson_corr_helper(self):
        from src.trading.wallet_scout import pearson_corr

        xs = [float(i) for i in range(12)]
        assert pearson_corr(xs, xs) == pytest.approx(1.0)
        assert pearson_corr(xs, [-x for x in xs]) == pytest.approx(-1.0)
        assert pearson_corr(xs[:5], xs[:5]) is None
        assert pearson_corr(xs, [3.0] * 12) is None


class TestBlofinCoverageMapping:
    def test_maps_inst_ids_to_usdt_bases(self):
        from types import SimpleNamespace

        from src.trading.wallet_scout import blofin_usdt_bases

        instruments = {
            "BTC-USDT": SimpleNamespace(state="live"),
            "ETH-USDC": SimpleNamespace(state="live"),   # not USDT -> skip
            "HYPE-USDT": SimpleNamespace(state="live"),
            "DEAD-USDT": SimpleNamespace(state="suspend"),  # not live -> skip
            "1000PEPE-USDT": SimpleNamespace(state="live"),
        }
        bases = blofin_usdt_bases(instruments)
        assert bases == {"BTC", "HYPE", "1000PEPE"}

    def test_empty_and_none(self):
        from src.trading.wallet_scout import blofin_usdt_bases

        assert blofin_usdt_bases({}) == set()
        assert blofin_usdt_bases(None) == set()

    def test_coverage_now_matches_base_symbols(self):
        # regression: coverage used the raw inst-id keys, so a wallet
        # trading BTC/HYPE scored 0 coverage. It must now score 1.0.
        from types import SimpleNamespace

        from src.trading.wallet_scout import blofin_usdt_bases, hl_coin_to_blofin_base

        bases = blofin_usdt_bases({
            "BTC-USDT": SimpleNamespace(state="live"),
            "HYPE-USDT": SimpleNamespace(state="live"),
        })
        traded = {"BTC", "HYPE"}
        listed = sum(1 for c in traded if hl_coin_to_blofin_base(c) in bases)
        assert listed / len(traded) == 1.0
