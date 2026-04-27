"""Tests for Discord component button capture + mirror-mode forwarding.

Covers:
  - _extract_url_buttons pulls URL-style buttons out of message.components
  - Non-URL buttons (primary / secondary / etc.) are skipped silently
  - Empty / missing components return an empty list cleanly
  - Router._build_mirror_keyboard produces the expected Telegram layout
  - More than _MAX_BUTTONS_PER_ROW buttons split across multiple rows
"""
from __future__ import annotations

from types import SimpleNamespace

from src.discord_listener import _extract_url_buttons
from src.router import Router, _MAX_BUTTONS_PER_ROW


def _fake_action_row(*children):
    """Build a duck-typed ActionRow stand-in with .children attribute."""
    return SimpleNamespace(children=list(children))


def _fake_button(label: str, url: str | None):
    """Build a duck-typed discord.Button stand-in. URL-style buttons get a
    url; non-URL buttons get url=None (matching discord.py's behaviour)."""
    return SimpleNamespace(label=label, url=url)


# ---------------------------------------------------------------------------
# _extract_url_buttons
# ---------------------------------------------------------------------------

def test_extract_url_buttons_basic():
    components = [
        _fake_action_row(
            _fake_button("Trade via Onsight", "https://onsight.fi/trade/abc"),
            _fake_button("Mobile Waitlist", "https://onsight.fi/waitlist"),
        ),
    ]
    out = _extract_url_buttons(components)
    assert out == [
        ("Trade via Onsight", "https://onsight.fi/trade/abc"),
        ("Mobile Waitlist", "https://onsight.fi/waitlist"),
    ]


def test_extract_url_buttons_skips_non_url_buttons():
    components = [
        _fake_action_row(
            _fake_button("Submit", None),  # primary/secondary, no URL
            _fake_button("Open chart", "https://onsight.fi/chart/abc"),
        ),
    ]
    out = _extract_url_buttons(components)
    assert out == [("Open chart", "https://onsight.fi/chart/abc")]


def test_extract_url_buttons_empty_components_returns_empty():
    assert _extract_url_buttons([]) == []
    assert _extract_url_buttons(None) == []  # type: ignore[arg-type]


def test_extract_url_buttons_button_without_label_uses_default():
    components = [
        _fake_action_row(_fake_button(None, "https://x.com")),
    ]
    out = _extract_url_buttons(components)
    assert out == [("Open", "https://x.com")]


def test_extract_url_buttons_handles_multiple_action_rows():
    components = [
        _fake_action_row(_fake_button("Row1Btn1", "https://a.com")),
        _fake_action_row(
            _fake_button("Row2Btn1", "https://b.com"),
            _fake_button("Row2Btn2", "https://c.com"),
        ),
    ]
    out = _extract_url_buttons(components)
    assert len(out) == 3
    assert out[0][1] == "https://a.com"
    assert out[2][1] == "https://c.com"


# ---------------------------------------------------------------------------
# Router._build_mirror_keyboard
# ---------------------------------------------------------------------------

def test_build_mirror_keyboard_two_buttons_one_row():
    kb = Router._build_mirror_keyboard([
        ("Trade via Onsight", "https://onsight.fi/trade"),
        ("Mobile Waitlist", "https://onsight.fi/waitlist"),
    ])
    assert kb is not None
    assert len(kb.inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert row[0].text == "Trade via Onsight"
    assert row[0].url == "https://onsight.fi/trade"
    assert row[1].text == "Mobile Waitlist"


def test_build_mirror_keyboard_empty_returns_none():
    assert Router._build_mirror_keyboard([]) is None


def test_build_mirror_keyboard_overflow_splits_rows():
    """More than _MAX_BUTTONS_PER_ROW buttons should split into multiple
    rows so labels don't get truncated on mobile."""
    buttons = [(f"Btn{i}", f"https://x.com/{i}")
               for i in range(_MAX_BUTTONS_PER_ROW + 2)]
    kb = Router._build_mirror_keyboard(buttons)
    assert kb is not None
    # 6 buttons with max 4/row => 2 rows of [4, 2]
    assert len(kb.inline_keyboard) == 2
    assert len(kb.inline_keyboard[0]) == _MAX_BUTTONS_PER_ROW
    assert len(kb.inline_keyboard[1]) == 2
