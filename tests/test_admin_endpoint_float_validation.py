"""Tests for the NaN / Infinity guard helpers in the admin endpoint.

We don't spin up the full aiohttp server (those tests would mock too
much to be meaningful). Instead we exercise the validation primitives
in isolation — that's where the security bug would actually live.
"""

from __future__ import annotations

import math

import pytest


def test_safe_float_accepts_finite_numbers():
    from src.trading.admin_endpoint import _safe_float
    assert _safe_float(65000) == 65000.0
    assert _safe_float("0.2350") == 0.2350
    assert _safe_float(-0.5) == -0.5


def test_safe_float_rejects_nan():
    from src.trading.admin_endpoint import _safe_float
    with pytest.raises(ValueError):
        _safe_float("nan")
    with pytest.raises(ValueError):
        _safe_float("NaN")
    with pytest.raises(ValueError):
        _safe_float(float("nan"))


def test_safe_float_rejects_infinity():
    from src.trading.admin_endpoint import _safe_float
    with pytest.raises(ValueError):
        _safe_float("inf")
    with pytest.raises(ValueError):
        _safe_float("-Infinity")
    with pytest.raises(ValueError):
        _safe_float(float("inf"))
    with pytest.raises(ValueError):
        _safe_float(float("-inf"))


def test_safe_float_rejects_non_numeric():
    from src.trading.admin_endpoint import _safe_float
    with pytest.raises(ValueError):
        _safe_float("not a number")
    with pytest.raises(TypeError):
        _safe_float(None)


def test_safe_optional_float_allows_none():
    from src.trading.admin_endpoint import _safe_optional_float
    assert _safe_optional_float(None) is None


def test_safe_optional_float_validates_present_values():
    from src.trading.admin_endpoint import _safe_optional_float
    assert _safe_optional_float(42) == 42.0
    with pytest.raises(ValueError):
        _safe_optional_float("inf")
    with pytest.raises(ValueError):
        _safe_optional_float("nan")
