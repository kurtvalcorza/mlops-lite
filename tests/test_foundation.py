"""Phase 1-2 foundation smoke test (T012).

Verifies the foundational stack is up and reachable on the host ports. Run after `make up`
(or `docker compose up -d --build`). Exits non-zero if any service is unhealthy.
"""
import os
import sys
import urllib.request

CHECKS = {
    "gateway /healthz": f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}/healthz",
    "gateway /metrics": f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}/metrics",
    "mlflow /health": f"http://localhost:{os.getenv('MLFLOW_PORT', '5500')}/health",
    "minio live": f"http://localhost:{os.getenv('MINIO_API_PORT', '9000')}/minio/health/live",
    "prometheus": f"http://localhost:{os.getenv('PROMETHEUS_PORT', '9090')}/-/healthy",
    "grafana": f"http://localhost:{os.getenv('GRAFANA_PORT', '3001')}/api/health",
}


def check(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status < 400
    except Exception:
        return False


def main() -> int:
    failures = 0
    for name, url in CHECKS.items():
        ok = check(url)
        print(f"[{'OK' if ok else 'FAIL'}] {name} -> {url}")
        failures += 0 if ok else 1
    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print("\nAll foundation checks passed.")
    return 0


def test_foundation(require_gateway):
    """Pytest wrapper (005 US5): skip if the stack is down, else assert the smoke passes."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
