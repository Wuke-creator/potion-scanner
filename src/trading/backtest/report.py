"""Backtest run -> Telegram-sized plain-text report chunks (<= 3800 chars).

The report states its own caveats inline (fill-model bounds, notional
conviction proxy, mixed candle resolution, mark-to-market counts, sample
discipline) because a number without its caveat is how backtests lie.
"""

from __future__ import annotations

from src.trading.backtest.runner import RunResult
from src.trading.backtest.stats import TradeSummary
from src.trading.wallet_scout import short_addr

CHUNK_LIMIT = 3800


def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_summary_line(name: str, s: TradeSummary | None) -> str:
    if s is None or s.n == 0:
        return f"{name}: no trades"
    avg_r = f", avg R {s.avg_r:+.2f}" if s.avg_r is not None else ""
    return (
        f"{name}: {s.n} trades, WR {s.win_rate * 100:.0f}%, "
        f"net {_fmt_usd(s.net_usd)}, exp {_fmt_usd(s.expectancy_usd)}/trade"
        f"{avg_r}"
    )


def _verdict(result: RunResult) -> list[str]:
    lines = []
    pooled = result.pooled
    pess = result.pooled_pessimistic
    if pooled is None or pooled.n == 0:
        return ["VERDICT: no simulated trades. Nothing to conclude."]
    realistic_pos = pooled.expectancy_usd > 0
    avg_costs = (
        (pooled.fees_usd + pooled.funding_usd) / pooled.n if pooled.n else 0.0
    )
    pess_ok = pess is not None and pess.n > 0 and (
        pess.expectancy_usd > -avg_costs
    )
    if realistic_pos and pess_ok:
        lines.append(
            "VERDICT: PASS. Positive expectancy under realistic fills and "
            "the edge survives the pessimistic bound."
        )
    elif realistic_pos:
        lines.append(
            "VERDICT: FRAGILE. Positive under realistic fills but the "
            "pessimistic bound eats it: the edge may be a fill-model artifact."
        )
    else:
        lines.append(
            "VERDICT: FAIL. No positive expectancy under realistic fills. "
            "Do not copy this set live."
        )
    opt = result.pooled_optimistic
    if (
        opt is not None and opt.n and opt.expectancy_usd > 0
        and not realistic_pos
    ):
        lines.append(
            "Optimistic fills flip it positive: the 'edge' is latency, "
            "not alpha (you only win if you get THEIR price)."
        )
    if result.latency_ratio is not None:
        ok = "OK (>0.7)" if result.latency_ratio > 0.7 else "POOR (<0.7)"
        lines.append(
            f"Latency robustness PnL@15m/PnL@0m: {result.latency_ratio:.2f} {ok}"
        )
    elif result.net_by_delay.get(0, 0.0) <= 0:
        lines.append(
            "Latency robustness: n/a (no edge even at zero delay)."
        )
    if pooled.ci_low is not None:
        lines.append(
            f"Expectancy 95% CI (day-clustered): "
            f"{_fmt_usd(pooled.ci_low)} .. {_fmt_usd(pooled.ci_high)}"
        )
    else:
        lines.append(f"CI suppressed: {pooled.ci_suppressed_reason}.")
    return lines


def format_report(result: RunResult) -> list[str]:
    """Full report as a list of send-ready chunks."""
    spec = result.spec
    head = [
        f"Backtest {result.run_id} "
        f"({spec.days}d, {len(spec.addresses)} wallet(s), "
        f"{result.elapsed_sec:.0f}s)",
        "Primary cell: 2min confirm delay, realistic fills, ATR ladder, "
        "$1k notional/trade.",
        "",
    ]
    head += _verdict(result)
    head += [
        "",
        _fmt_summary_line("POOLED (realistic)", result.pooled),
        _fmt_summary_line("  pessimistic bound", result.pooled_pessimistic),
        _fmt_summary_line("  optimistic bound", result.pooled_optimistic),
    ]
    if result.pooled and result.pooled.n:
        p = result.pooled
        head.append(
            f"  costs: fees {_fmt_usd(p.fees_usd)}, "
            f"funding {_fmt_usd(p.funding_usd)}",
        )

    delay = ["", "Edge vs confirm delay (net $, realistic, ladder):"]
    for d in sorted(result.net_by_delay):
        mins = f"{d}s" if d < 60 else f"{d // 60}m"
        delay.append(f"  {mins:>4}: {_fmt_usd(result.net_by_delay[d])}")

    engines = ["", "Exit engines (identical entries):"]
    if result.ladder_vs_mirror and result.ladder_vs_mirror.n_pairs:
        c = result.ladder_vs_mirror
        engines.append(
            f"  ladder vs mirror: {c.n_pairs} pairs, ladder "
            f"{_fmt_usd(c.mean_diff_usd)}/trade "
            f"{'better' if c.mean_diff_usd > 0 else 'worse'}",
        )
    engines.append(_fmt_summary_line("  hop @2m (trade-hopping)",
                                     result.hop_summary))
    engines.append(_fmt_summary_line("  hop @15s (bot speed)",
                                     result.hop_bot_speed_summary))
    hop = result.hop_summary
    hop_bot = result.hop_bot_speed_summary
    if (
        hop is not None and hop_bot is not None and hop_bot.n
        and hop_bot.expectancy_usd > 0 and hop.expectancy_usd <= 0
    ):
        engines.append(
            "  hop verdict: only pays at bot speed. At human confirm "
            "latency it is the exit liquidity, as the research predicted."
        )
    elif hop is not None and hop.n and hop.expectancy_usd <= 0:
        engines.append("  hop verdict: negative even before bot-speed "
                       "comparison; skip.")

    plateau = ["", "atr_mult plateau (display only, never auto-picked):"]
    for mult, exp, avg_r in result.plateau:
        r_txt = f", avg R {avg_r:+.2f}" if avg_r is not None else ""
        plateau.append(f"  x{mult:g}: exp {_fmt_usd(exp)}/trade{r_txt}")

    floors = ["", "Conviction floor sensitivity (notional proxy):"]
    for floor, s in sorted(result.floor_sensitivity.items()):
        floors.append(
            f"  >={floor:.2f}: " + _fmt_summary_line("", s).lstrip(": "),
        )

    per_wallet = ["", "Per wallet (primary cell):"]
    for wr in result.wallets:
        label = short_addr(wr.address)
        if wr.error:
            per_wallet.append(f"  {label}: {wr.error}")
            continue
        s = wr.summaries.get(
            next(iter(wr.summaries)) if wr.summaries else "", None,
        )
        flags = []
        if wr.is_scalper:
            flags.append(f"SCALPER {wr.fills_per_day:.0f} fills/d "
                         "(watcher would not copy)")
        if not wr.fills_complete:
            flags.append("fills TRUNCATED at API cap")
        if wr.unmapped_coins:
            flags.append("unmapped: " + ",".join(wr.unmapped_coins[:4]))
        flag_txt = f"  [{'; '.join(flags)}]" if flags else ""
        prim = summarize_line_for_wallet(wr)
        per_wallet.append(f"  {label}: {prim}{flag_txt}")
        if wr.skip_counts:
            skips = ", ".join(
                f"{k}={v}" for k, v in sorted(wr.skip_counts.items())
            )
            per_wallet.append(f"    skips: {skips}")

    caveats = [
        "",
        "Caveats (read these):",
        "- Wallets were HAND-PICKED as past winners; this validates the "
        "copy MECHANICS and costs, not the selection method. Walk-forward "
        "selection needs the snapshot archive to age.",
        "- Conviction gate is a NOTIONAL proxy (fills carry no leverage); "
        "live uses margin/equity.",
    ]
    if result.n_coarse_trades:
        caveats.append(
            f"- {result.n_coarse_trades} trade(s) entered on 15m candles "
            "(1m archive still thin); their exits are coarser.",
        )
    if result.pooled and result.pooled.n_mtm:
        caveats.append(
            f"- {result.pooled.n_mtm} trade(s) unresolved, marked to market.",
        )
    caveats.append(
        "- No parameters were optimized; every cell is logged in "
        f"backtest_runs ({result.run_id}).",
    )

    text = "\n".join(
        head + delay + engines + plateau + floors + per_wallet + caveats,
    )
    return _chunk(text)


def summarize_line_for_wallet(wr) -> str:
    """Primary-cell one-liner for a wallet (first summary is primary)."""
    from src.trading.backtest.runner import _base_params  # local: avoid cycle
    from src.config.settings import BacktestConfig

    pid = _base_params(BacktestConfig()).params_id
    s = wr.summaries.get(pid)
    if s is None:
        # config drift: fall back to any realistic atr_ladder cell
        for k, v in wr.summaries.items():
            if k.startswith("atr_ladder") and "realistic" in k:
                s = v
                break
    if s is None or s.n == 0:
        return "no trades"
    avg_r = f", avg R {s.avg_r:+.2f}" if s.avg_r is not None else ""
    return (
        f"{s.n} trades, WR {s.win_rate * 100:.0f}%, "
        f"net {_fmt_usd(s.net_usd)}{avg_r}"
    )


def _chunk(text: str) -> list[str]:
    if len(text) <= CHUNK_LIMIT:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > CHUNK_LIMIT and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
