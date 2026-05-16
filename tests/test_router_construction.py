"""Regression guard for Router __init__ completeness.

A prior change inserted a method definition mid-__init__, which silently
truncated the constructor and left ``_wallet_debouncer`` unset — the bot
then crash-looped on every Wallet Tracker message in production. These
tests assert the constructor wires every attribute the handlers depend
on, with and without the optional collaborators.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.router import Router

_REQUIRED_ATTRS = (
    "_discord_cfg",
    "_dispatcher",
    "_analytics",
    "_open_signals",
    "_quick_trade_enabled",
    "_image_archive",
    "_executor_client",
    "_wallet_debouncer",
)


def test_router_minimal_construction_sets_all_attrs():
    r = Router(discord_cfg=MagicMock(), dispatcher=MagicMock())
    for attr in _REQUIRED_ATTRS:
        assert hasattr(r, attr), f"Router missing {attr} after __init__"


def test_router_full_construction_sets_all_attrs():
    r = Router(
        discord_cfg=MagicMock(),
        dispatcher=MagicMock(),
        analytics=MagicMock(),
        open_signals=MagicMock(),
        quick_trade_enabled=True,
        image_archive=MagicMock(),
        executor_client=MagicMock(),
    )
    for attr in _REQUIRED_ATTRS:
        assert hasattr(r, attr), f"Router missing {attr} after __init__"
    assert r._quick_trade_enabled is True
    assert r._executor_client is not None


def test_wallet_debouncer_is_callable_target():
    # The debouncer must be a real object with an `add` coroutine — the
    # exact thing the prod crash proved was missing.
    r = Router(discord_cfg=MagicMock(), dispatcher=MagicMock())
    assert r._wallet_debouncer is not None
    assert hasattr(r._wallet_debouncer, "add")
