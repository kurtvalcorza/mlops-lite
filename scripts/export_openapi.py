"""Export the gateway's OpenAPI document (027 T762 — FR-438 / SC-203).

Generated from the live FastAPI app rather than hand-maintained. A hand-written contract file drifts
the first time a route is added without remembering it, and the drift is invisible: the file still
looks like a contract. `tests/test_openapi_export.py` pins that the checked-in copy matches what the
app currently produces, so the drift becomes a failing test instead of a stale document.

Usage: python scripts/export_openapi.py [--check]
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

TARGET = os.path.join(REPO, "specs", "001-mlops-platform", "contracts", "openapi.json")


def document() -> dict:
    # The gateway registers module-level Prometheus metrics; importing it twice in one process (as
    # the test suite does) collides on the global registry. The same isolation the suites use.
    from tests import _gwimport

    return _gwimport.gateway_app().openapi()


def main() -> int:
    doc = document()
    rendered = json.dumps(doc, indent=2, sort_keys=True) + "\n"

    if "--check" in sys.argv:
        current = open(TARGET, encoding="utf-8").read() if os.path.isfile(TARGET) else ""
        if current != rendered:
            print(f"{TARGET} is out of date — run: python scripts/export_openapi.py")
            return 1
        print(f"{TARGET} is current ({len(doc.get('paths', {}))} paths)")
        return 0

    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print(f"wrote {TARGET} ({len(doc.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
