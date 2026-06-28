"""005 US2 fail-closed auth unit test (T096, FR-042 / SC-026).

Exercises the three resolved auth postures directly against gateway/app/auth.py — without a live
stack — by loading a fresh copy of the module under different env (the mode resolves once at import,
mirroring the real gateway, FR-046). Covers what the live test_auth.py cannot: the no-key boot modes.

  - closed (default)  : no key, no override -> require_api_key refuses protected routes (401).
  - open-override     : no key + GATEWAY_ALLOW_OPEN=1 -> require_api_key passes through (dev hatch).
  - keyed             : a configured key -> valid key passes, missing/wrong key 401 (unchanged 002).
  - keyed wins        : a key + GATEWAY_ALLOW_OPEN set -> still 'keyed' (override never downgrades).

Runs offline. Skips cleanly if FastAPI isn't importable in the test environment (the gateway runs in
its own container; the host pytest env may not carry FastAPI).
"""
import asyncio
import importlib.util
import os

import pytest

fastapi = pytest.importorskip("fastapi")  # auth.py imports fastapi; skip if absent in this env
from fastapi import HTTPException  # noqa: E402

AUTH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway", "app", "auth.py"
)
_AUTH_ENV = ("GATEWAY_API_KEYS", "GATEWAY_API_KEYS_FILE", "GATEWAY_ALLOW_OPEN")


def _load_auth(**env):
    """Load a fresh, isolated copy of auth.py with the given auth env (others cleared)."""
    saved = {k: os.environ.get(k) for k in _AUTH_ENV}
    try:
        for k in _AUTH_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location("auth_under_test", AUTH_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raises_401(auth, key):
    try:
        asyncio.run(auth.require_api_key(key))
    except HTTPException as e:
        return e.status_code == 401
    return False


def test_closed_mode_refuses_protected_routes():
    auth = _load_auth()  # no keys, no override -> fail-closed default
    assert auth.auth_mode() == "closed"
    assert auth.auth_enabled() is False
    assert _raises_401(auth, None)
    assert _raises_401(auth, "anything")  # closed refuses regardless of a presented value


def test_open_override_passes_through():
    auth = _load_auth(GATEWAY_ALLOW_OPEN="1")
    assert auth.auth_mode() == "open-override"
    assert asyncio.run(auth.require_api_key(None)) is None  # dev escape hatch: open


def test_keyed_mode_enforces_the_key():
    auth = _load_auth(GATEWAY_API_KEYS="mll_unit-test-key")
    assert auth.auth_mode() == "keyed"
    assert auth.auth_enabled() is True
    assert asyncio.run(auth.require_api_key("mll_unit-test-key")) is None
    assert _raises_401(auth, None)
    assert _raises_401(auth, "mll_wrong-key")


def test_keyed_beats_open_override():
    auth = _load_auth(GATEWAY_API_KEYS="mll_k", GATEWAY_ALLOW_OPEN="1")
    assert auth.auth_mode() == "keyed"  # a configured key always wins; override never downgrades
    assert _raises_401(auth, None)


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_override_truthy_values(truthy):
    assert _load_auth(GATEWAY_ALLOW_OPEN=truthy).auth_mode() == "open-override"


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
def test_override_falsy_values_stay_closed(falsy):
    assert _load_auth(GATEWAY_ALLOW_OPEN=falsy).auth_mode() == "closed"
