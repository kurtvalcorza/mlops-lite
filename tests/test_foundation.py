"""Phase 1-2 foundation smoke test (T012).

Verifies the foundational stack is up and reachable on the host ports. Run after `make up`
(or `docker compose up -d --build`). Exits non-zero if any service is unhealthy.
"""
import os
import sys
import socket
import urllib.request

CHECKS = {
    "gateway /healthz": f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}/healthz",
    "gateway /metrics": f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}/metrics",
    "mlflow /health": f"http://localhost:{os.getenv('MLFLOW_PORT', '5500')}/health",
    "prometheus": f"http://localhost:{os.getenv('PROMETHEUS_PORT', '9090')}/-/healthy",
    "grafana": f"http://localhost:{os.getenv('GRAFANA_PORT', '3001')}/api/health",
}


def check(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status < 400
    except Exception:
        return False


def check_tcp(port: str) -> bool:
    """Garage's S3 port publishes no unauthenticated HTTP health path — a TCP connect proves
    the store is up and published (same posture as test_exposure)."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
            return True
    except Exception:
        return False


def main() -> int:
    failures = 0
    garage_port = os.getenv("GARAGE_S3_PORT", "3900")
    ok = check_tcp(garage_port)
    print(f"[{'OK' if ok else 'FAIL'}] garage live -> tcp 127.0.0.1:{garage_port}")
    failures += 0 if ok else 1
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
