"""005 US1 network-exposure test (T094, FR-041 / SC-025). Run after bring-up.

Proves the loopback-binding default holds end to end:
  1. every published service answers on 127.0.0.1 (gateway/MLflow/MinIO/Prometheus/Grafana over
     HTTP; Postgres + the MinIO console over a raw TCP connect),
  2. NONE of those ports is reachable on the host's routable LAN IP — a foreign host gets a refused
     connection (this is the whole point of binding 127.0.0.1, FR-041),
  3. the daemon-critical paths the WSL native daemons use (MinIO :9000, MLflow :5500) are reachable
     on loopback (so the loopback bind did not break the daemons' localhost path).

SKIPs cleanly if the stack is down, or if a single-interface box has no non-loopback IP to probe.
NOTE: with BIND_ADDR=0.0.0.0 (intentional LAN exposure) the LAN-refusal checks are EXPECTED to fail —
that override is the documented, opted-in exception to the secure default.

  python tests/test_exposure.py
"""
import os
import socket
import sys
import urllib.error
import urllib.request

GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8080")
MLFLOW_PORT = os.getenv("MLFLOW_PORT", "5500")
MINIO_API_PORT = os.getenv("MINIO_API_PORT", "9000")
MINIO_CONSOLE_PORT = os.getenv("MINIO_CONSOLE_PORT", "9001")
PROMETHEUS_PORT = os.getenv("PROMETHEUS_PORT", "9090")
GRAFANA_PORT = os.getenv("GRAFANA_PORT", "3001")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "55432")

# HTTP-probeable services: (label, port, health path).
HTTP_PROBES = [
    ("gateway", GATEWAY_PORT, "/healthz"),
    ("mlflow", MLFLOW_PORT, "/health"),
    ("minio", MINIO_API_PORT, "/minio/health/live"),
    ("prometheus", PROMETHEUS_PORT, "/-/healthy"),
    ("grafana", GRAFANA_PORT, "/api/health"),
]
# Every published port (TCP) — used for the LAN-refusal sweep.
ALL_PORTS = [GATEWAY_PORT, MLFLOW_PORT, MINIO_API_PORT, MINIO_CONSOLE_PORT,
             PROMETHEUS_PORT, GRAFANA_PORT, POSTGRES_PORT]


def _http_up(url, timeout=4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError:
        return True  # answered => up
    except Exception:
        return False


def _tcp_open(host, port, timeout=2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _lan_ip():
    """The host's routable (non-loopback) IP, or None on a single-interface box."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet sent; just selects the egress iface
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except Exception:
        return None


def main() -> int:
    failures = 0

    # 1. Loopback reachability (HTTP) — incl. the daemon-critical MinIO/MLflow paths (check 3).
    print("== loopback reachable (127.0.0.1) ==")
    for label, port, path in HTTP_PROBES:
        ok = _http_up(f"http://127.0.0.1:{port}{path}")
        print(f"[{'OK' if ok else 'FAIL'}] {label} :{port}{path} on 127.0.0.1 -> {'up' if ok else 'down'}")
        failures += 0 if ok else 1

    # Postgres + the MinIO console don't speak HTTP — a loopback TCP connect proves they're published.
    for label, port in (("postgres", POSTGRES_PORT), ("minio-console", MINIO_CONSOLE_PORT)):
        ok = _tcp_open("127.0.0.1", port)
        print(f"[{'OK' if ok else 'FAIL'}] {label} :{port} TCP on 127.0.0.1 -> {'open' if ok else 'closed'}")
        failures += 0 if ok else 1

    print("\n== daemon path intact (loopback MinIO/MLflow) ==")
    minio_ok = _http_up(f"http://127.0.0.1:{MINIO_API_PORT}/minio/health/live")
    mlflow_ok = _http_up(f"http://127.0.0.1:{MLFLOW_PORT}/health")
    print(f"[{'OK' if minio_ok else 'FAIL'}] WSL daemons can reach MinIO :{MINIO_API_PORT} (loopback)")
    print(f"[{'OK' if mlflow_ok else 'FAIL'}] WSL daemons can reach MLflow :{MLFLOW_PORT} (loopback)")
    failures += (0 if minio_ok else 1) + (0 if mlflow_ok else 1)

    # 2. LAN refusal — the loopback bind means a connect to the routable IP is refused.
    print("\n== LAN closed (routable IP refused) ==")
    ip = _lan_ip()
    if not ip:
        print("[SKIP] no non-loopback IP available — cannot probe LAN exposure on this box.")
    else:
        for port in ALL_PORTS:
            reachable = _tcp_open(ip, port, timeout=2.0)
            ok = not reachable
            print(f"[{'OK' if ok else 'FAIL'}] :{port} on LAN IP {ip} -> "
                  f"{'refused (good)' if ok else 'REACHABLE (exposed!)'}")
            failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} exposure check(s) failed.")
        return 1
    print("\nT094 PASS — services loopback-only; LAN refused; daemon MinIO/MLflow paths intact")
    return 0


def test_exposure(require_gateway):
    """Pytest wrapper (005 US5): skip if the stack is down, else assert loopback-only exposure."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
