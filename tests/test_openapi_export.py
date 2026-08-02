"""027 T762 — the exported OpenAPI matches the app, and covers every allowlisted route (SC-203).

A hand-maintained contract file drifts the first time a route is added without remembering it, and
the drift is invisible: the file still *looks* like a contract. Generating it from the live app and
pinning the checked-in copy turns that silent drift into a failing test.

The second assertion is the one that matters for the console: **every route the BFF can proxy must
exist in the app**. A path in the allowlist with no route behind it is a panel that 404s, and the
allowlist is exactly the place where such a mistake survives review — it reads like configuration.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tests import _gwimport  # noqa: E402

EXPORT = os.path.join(REPO, "specs", "001-mlops-platform", "contracts", "openapi.json")
ALLOWLIST = os.path.join(REPO, "ui", "lib", "gw-allowlist.ts")

_ENTRY = re.compile(r"\{\s*method:\s*'([A-Z]+)'\s*,\s*pattern:\s*'([^']+)'\s*\}")


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    yield from _gwimport.isolate_module_metrics()


@pytest.fixture(scope="module")
def exported():
    with open(EXPORT, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_checked_in_export_matches_what_the_app_produces(exported, monkeypatch):
    """The drift check. Regenerate with `python scripts/export_openapi.py`."""
    monkeypatch.setenv("GATEWAY_MIGRATIONS_ENABLED", "0")
    live = _gwimport.gateway_app().openapi()
    assert set(exported["paths"]) == set(live["paths"]), (
        "openapi.json is stale — run: python scripts/export_openapi.py")


def _allowlist_entries():
    with open(ALLOWLIST, encoding="utf-8") as fh:
        return _ENTRY.findall(fh.read())


def _to_openapi_path(pattern):
    """`console/catalog/:name/:version` -> `/console/catalog/{…}/{…}`, parameter names normalized.

    Names are normalized away because the allowlist and the route signature legitimately choose
    different ones — `:id` versus `{job_id}` — and comparing the names would fail on a difference
    that has no consequence.
    """
    return "/" + re.sub(r":[A-Za-z_]+", "{}", pattern)


def _normalized_openapi_paths(document):
    return {re.sub(r"\{[^}]+\}", "{}", path) for path in document["paths"]}


def test_every_allowlisted_route_exists_in_the_app(exported):
    """A path the BFF can proxy with no route behind it is a panel that 404s — and the allowlist is
    exactly where that mistake survives review, because it reads like configuration."""
    available = _normalized_openapi_paths(exported)
    missing = [pattern for _method, pattern in _allowlist_entries()
               if _to_openapi_path(pattern) not in available]
    assert missing == [], f"allowlisted but not routed: {missing}"


def test_every_console_route_appears_in_the_export(exported):
    """FR-438: the console's read surface is part of the platform's published contract, not a
    private side door."""
    console_paths = [p for p in exported["paths"] if p.startswith(("/console/", "/runtime/"))]
    assert len(console_paths) >= 40, f"only {len(console_paths)} console routes exported"


def test_the_payload_reveal_is_the_only_non_get_console_operation(exported):
    """The read-only guarantee, asserted against the published contract rather than the source —
    so a future write lands as a contract change someone has to justify."""
    writes = []
    for path, operations in exported["paths"].items():
        if not path.startswith(("/console/", "/runtime/")):
            continue
        for verb in operations:
            if verb.lower() not in ("get", "parameters"):
                writes.append(f"{verb.upper()} {path}")
    assert writes == ["POST /console/predictions/{prediction_id}/payload"], writes
