"""Shared test helper: attach the gateway API key when auth is enabled (T048, SC-012).

001's integration tests run unchanged with hardening on by reading `GATEWAY_API_KEY` from the
environment and adding it as the `X-API-Key` header. With no key set (auth disabled), the headers
carry only the content type, so the same tests still pass against an open gateway.
"""
import os

API_KEY_HEADER = "X-API-Key"


def auth_headers(extra: dict | None = None) -> dict:
    """Return request headers including the API key (if GATEWAY_API_KEY is set)."""
    headers = dict(extra or {})
    key = os.getenv("GATEWAY_API_KEY")
    if key:
        headers[API_KEY_HEADER] = key
    return headers
