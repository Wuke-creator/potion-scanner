"""Tests for fills -> position-event reconstruction, including the parity
property: the reconstructor must classify identically to a simulated
watcher (snapshots diffed with the SAME diff_positions)."""

from __future__ import annotations

import pytest

from src.trading.backtest.position_events import (
    AccountValueCurve,
    reconstruct_position_events,
)
from src.trading.hl_info_client import WalletPosition
from src.trading.wallet_metrics_db import StoredPosition
from src.trading.wallet_watcher import diff_positions

T0 = 1_760_000_000_000
MIN = 60_000


def _fill(coin, t, sz, side, px, start=None, closed=0.0, fee=0.0, dir_=""):
    f = {
        "coin": coin, "time": t, "sz": str(sz), "side": side, "px": str(px),
        "closedPnl": str(closed), "fee": str(fee),
    }
    if start is not None:
        f["startPosition"] = str(start)
    if dir_:
        f["dir"] = dir_
    return f


def _av(_ts):
    return 10_000.0


class TestReconstruction:
    def test_empty(self):
        assert reconstruct_position_events([], account_value_at=_av) == []

    def test_simple_open_then_close(self):
        fills = [
            _fill("HYPE", T0, 10, "B", 25.0, start=0.0, dir_="Open Long"),
            _fill("HYPE", T0 + 5 * MIN, 10, "A", 26.0, closed=10.0,
                  dir_="Close Long"),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert [e.kind for e in events] == ["open", "close"]
        opened = events[0]
        assert opened.coin == "HYPE" and opened.curr_szi == 10.0
        assert opened.fill_vwap == 25.0
        assert opened.notional == pytest.approx(250.0)
        assert opened.conviction == pytest.approx(0.025)

    def test_same_bucket_round_trip_is_invisible(self):
        # opened and closed within one 60s bucket: the watcher would never
        # have seen it, so the reconstructor must not emit it either
        fills = [
            _fill("HYPE", T0, 10, "B", 25.0, start=0.0),
            _fill("HYPE", T0 + 20_000, 10, "A", 25.2, closed=2.0),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert events == []

    def test_add_and_partial_reduce(self):
        fills = [
            _fill("HYPE", T0, 10, "B", 25.0, start=0.0),
            _fill("HYPE", T0 + 2 * MIN, 5, "B", 25.5),
            _fill("HYPE", T0 + 4 * MIN, 6, "A", 26.0, closed=4.0),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert [e.kind for e in events] == ["open", "add", "reduce"]
        red = events[2]
        assert red.prev_szi == 15.0 and red.curr_szi == 9.0
        assert red.reduce_fraction == pytest.approx(6 / 15)

    def test_flip_detected(self):
        fills = [
            _fill("A", T0, 10, "B", 100.0, start=0.0),
            _fill("A", T0 + 3 * MIN, 25, "A", 101.0, closed=10.0),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert [e.kind for e in events] == ["open", "flip"]
        assert events[1].curr_szi == -15.0

    def test_preexisting_position_does_not_fake_open(self):
        # window starts mid-position: startPosition=40, then a reduce
        fills = [
            _fill("ZEC", T0, 10, "A", 300.0, start=40.0, closed=25.0),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert [e.kind for e in events] == ["reduce"]

    def test_bucket_vwap_weights_by_size(self):
        fills = [
            _fill("A", T0, 10, "B", 100.0, start=0.0),
            _fill("A", T0 + 10_000, 30, "B", 104.0),
        ]
        events = reconstruct_position_events(fills, account_value_at=_av)
        assert len(events) == 1
        assert events[0].fill_vwap == pytest.approx(103.0)  # (10*100+30*104)/40

    def test_zero_account_value_gives_zero_conviction(self):
        fills = [_fill("A", T0, 10, "B", 100.0, start=0.0)]
        events = reconstruct_position_events(
            fills, account_value_at=lambda ts: 0.0,
        )
        assert events[0].conviction == 0.0


class TestWatcherParity:
    """Property: reconstructor events == a simulated watcher diffing
    snapshots at the same cadence via the same diff_positions."""

    def _watcher_events(self, fills, poll_ms):
        fills = sorted(fills, key=lambda f: int(f["time"]))
        positions: dict[str, float] = {}
        for f in fills:
            c = f["coin"]
            if c not in positions:
                positions[c] = float(f.get("startPosition", 0.0))
        t0 = (int(fills[0]["time"]) // poll_ms) * poll_ms
        t_end = int(fills[-1]["time"]) + poll_ms
        out = []
        prev_snap = {
            c: StoredPosition(coin=c, szi=s)
            for c, s in positions.items() if s != 0.0
        }
        i = 0
        t = t0 + poll_ms
        while t <= t_end + poll_ms:
            while i < len(fills) and int(fills[i]["time"]) < t:
                f = fills[i]
                sz = float(f["sz"])
                delta = sz if f["side"] in ("B", "BUY") else -sz
                positions[f["coin"]] = positions.get(f["coin"], 0.0) + delta
                if abs(positions[f["coin"]]) < 1e-12:
                    positions[f["coin"]] = 0.0
                i += 1
            curr_snap = {
                c: WalletPosition(coin=c, szi=s, entry_px=0.0, leverage=0.0,
                                  notional=0.0, margin_used=0.0)
                for c, s in positions.items() if s != 0.0
            }
            for d in diff_positions(prev_snap, curr_snap):
                out.append((t, d.kind, d.coin, d.prev_szi, d.curr_szi))
            prev_snap = {
                c: StoredPosition(coin=c, szi=p.szi)
                for c, p in curr_snap.items()
            }
            t += poll_ms
        return out

    @pytest.mark.parametrize("poll_ms", [60_000, 15_000])
    def test_parity_on_mixed_sequence(self, poll_ms):
        fills = [
            _fill("A", T0 + 10_000, 10, "B", 100.0, start=0.0),
            _fill("B", T0 + 70_000, 5, "A", 50.0, start=0.0),      # short open
            _fill("A", T0 + 200_000, 4, "B", 101.0),               # add
            _fill("A", T0 + 400_000, 20, "A", 102.0, closed=20.0), # flip
            _fill("B", T0 + 500_000, 5, "B", 49.0, closed=5.0),    # close short
            _fill("A", T0 + 700_000, 6, "B", 100.5, closed=-3.0),  # close short A
        ]
        recon = reconstruct_position_events(
            fills, bucket_ms=poll_ms, account_value_at=_av,
        )
        watcher = self._watcher_events(fills, poll_ms)
        assert [(e.kind, e.coin, e.prev_szi, e.curr_szi) for e in recon] == [
            (k, c, p, q) for _, k, c, p, q in watcher
        ]


class TestAccountValueCurve:
    def test_no_anchors_uses_fallback(self):
        curve = AccountValueCurve(anchors=[], fills=[], fallback=5000.0)
        assert curve.at(T0) == 5000.0

    def test_at_anchor_exact(self):
        curve = AccountValueCurve(anchors=[(T0, 10_000.0)])
        assert curve.at(T0) == 10_000.0

    def test_backward_interpolation_subtracts_later_pnl(self):
        # wallet made +500 net between t and the anchor -> earlier value lower
        fills = [
            _fill("A", T0 + 5 * MIN, 1, "A", 1.0, closed=500.0, fee=0.0),
        ]
        curve = AccountValueCurve(anchors=[(T0 + 10 * MIN, 10_000.0)], fills=fills)
        assert curve.at(T0) == pytest.approx(9_500.0)
        # after the fill, the anchor value stands
        assert curve.at(T0 + 6 * MIN) == pytest.approx(10_000.0)

    def test_forward_walk_past_last_anchor(self):
        fills = [
            _fill("A", T0 + 5 * MIN, 1, "A", 1.0, closed=200.0, fee=50.0),
        ]
        curve = AccountValueCurve(anchors=[(T0, 10_000.0)], fills=fills)
        assert curve.at(T0 + 10 * MIN) == pytest.approx(10_150.0)

    def test_fees_reduce_net(self):
        fills = [
            _fill("A", T0 + 5 * MIN, 1, "A", 1.0, closed=100.0, fee=10.0),
        ]
        curve = AccountValueCurve(anchors=[(T0 + 10 * MIN, 1_000.0)], fills=fills)
        assert curve.at(T0) == pytest.approx(1_000.0 - 90.0)
