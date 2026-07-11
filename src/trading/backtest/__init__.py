"""Backtesting layer for the wallet copy stack.

data_store    SQLite archive (candles, funding, fills, leaderboard
              snapshots, wallet states, backtest runs)
snapshot_job  nightly archiver: the ONLY source of point-in-time
              leaderboard history and deep 1m candle history, since the
              upstream API keeps 5000 candles and no leaderboard history
"""
