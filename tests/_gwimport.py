"""Import the real gateway app without colliding with the suite's other module identity.

The repo loads `gateway/app/*.py` under **two module names**: the existing suites synthesize an `app`
package and load `app.datasets`, `app.platform_metrics`, … through `importlib` with stubbed settings
(see `tests/test_platform_health.py`), while the broker and console suites import the real
`gateway.app.main`. Both define the same module-level Prometheus metrics, and
`prometheus_client`'s **global default registry** refuses a second registration of a name it already
holds — so whichever identity imports second raises `Duplicated timeseries`, and which one that is
depends on pytest's collection order.

Neither side is wrong. The metrics are correct, the two importers are both legitimate, and the
collision is purely an artifact of the default registry being process-global.

So: register the gateway app's metrics into a **throwaway registry** by swapping the default
registry's contents out for the duration of the import, then restoring them. Monkeypatching
`prometheus_client.REGISTRY` cannot work — `Counter.__init__` binds the default registry at
*definition* time — so the mapping dicts are swapped instead. `test_platform_health.py` already
reaches into these same internals for the same reason, so this follows an established precedent
rather than inventing one.

The suites that use this never scrape `/metrics`; they exercise routes. If one ever needs to, it
should build its own `CollectorRegistry` and pass it explicitly rather than relying on the global.

Named `_gwimport`, not `_gwapp`, because `tests/_pkgload.py` mints synthetic packages called
`_gwapp1`, `_gwapp2`, … — two different things with nearly the same name in one test package is a
trap for the next reader.
"""
import contextlib


@contextlib.contextmanager
def isolated_metrics():
    """Swap the default registry's contents out, yield, then restore.

    Anything registered inside the block is discarded, so importing the gateway app leaves the
    process's metric registry exactly as it found it.
    """
    from prometheus_client import REGISTRY

    saved_collectors = dict(REGISTRY._collector_to_names)
    saved_names = dict(REGISTRY._names_to_collectors)
    REGISTRY._collector_to_names.clear()
    REGISTRY._names_to_collectors.clear()
    try:
        yield
    finally:
        REGISTRY._collector_to_names.clear()
        REGISTRY._collector_to_names.update(saved_collectors)
        REGISTRY._names_to_collectors.clear()
        REGISTRY._names_to_collectors.update(saved_names)


def gateway_app():
    """The real FastAPI app, imported with its metrics isolated. Safe to call repeatedly."""
    with isolated_metrics():
        from gateway.app.main import app
    return app
