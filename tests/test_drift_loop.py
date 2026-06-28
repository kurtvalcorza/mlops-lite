"""Phase 7 drift-loop integration test (T037, US5) — closes the feedback loop.

Stable data → no drift, no retrain. Induced drift → report flags it AND a retraining run is
launched on the training daemon. Requires the stack up; the retrain leg is asserted only if the
trainer is running (else reported as detected-but-not-launched). Exits non-zero on failure.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
REF, LIVE, QA = "feat-baseline", "feat-live", "qa-retrain"


def _req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _register(name, content: bytes, fmt):
    s, r = _req("POST", "/datasets",
                {"name": name, "content_b64": base64.b64encode(content).decode(), "format": fmt})
    assert s == 201, f"register {name} -> {s} {r}"
    return r["version"]


def _csv(values):
    return ("x\n" + "\n".join(str(v) for v in values)).encode()


def main() -> int:
    # Reference + a same-distribution copy (stable) + a shifted copy (drift).
    base = list(range(0, 10)) * 6              # 60 rows spanning 0..9
    stable = [v for v in base]                  # identical distribution
    shifted = [v + 12 for v in base]            # clear distribution shift → high PSI

    ref_v = _register(REF, _csv(base), "csv")
    stable_v = _register(REF, _csv(stable), "csv")  # same content as ref → same version, fine
    live_v = _register(LIVE, _csv(shifted), "csv")
    qa = b'{"instruction":"ping?","response":"pong"}\n{"instruction":"hi?","response":"hello"}'
    qa_v = _register(QA, qa, "jsonl")
    print(f"[OK] registered baseline@{ref_v}, live@{live_v}, qa@{qa_v}")

    # 1. Stable check: reference vs identical distribution → no drift, no retrain.
    s, res = _req("POST", "/monitor/check",
                  {"reference": {"name": REF, "version": ref_v},
                   "current": {"name": REF, "version": stable_v},
                   "retrain": {"dataset_name": QA, "dataset_version": qa_v,
                               "output_name": "qa-retrain-lora", "steps": 3}})
    rep = res["report"]
    if s != 200 or rep["dataset_drift"] or res.get("retrain") is not None:
        print(f"[FAIL] stable check flagged drift: max_psi={rep.get('max_psi')} retrain={res.get('retrain')}")
        return 1
    print(f"[OK] stable: max_psi={rep['max_psi']} -> no drift, no retrain")

    # 2. Drift check: reference vs shifted → drift flagged.
    serving_idle = not (_req("GET", "/serving/health")[1].get("reachable") and
                        _req("GET", "/serving/health")[1].get("resident", False))
    s, res = _req("POST", "/monitor/check",
                  {"reference": {"name": REF, "version": ref_v},
                   "current": {"name": LIVE, "version": live_v},
                   "retrain": {"dataset_name": QA, "dataset_version": qa_v,
                               "output_name": "qa-retrain-lora", "steps": 3}})
    rep = res["report"]
    if s != 200 or not rep["dataset_drift"] or rep["max_psi"] < rep["threshold"]:
        print(f"[FAIL] drift not detected: {rep}")
        return 1
    print(f"[OK] drift detected: max_psi={rep['max_psi']} >= {rep['threshold']} (cols {rep['drifted_columns']})")

    # 3. The breach launched a retraining run (loop closed) — asserted if the trainer is up.
    _, th = _req("GET", "/training/health")
    if not th.get("reachable"):
        print("[SKIP] trainer not running — drift detected but retrain not launched")
    elif res.get("retrain", {}).get("run_id"):
        print(f"[OK] drift -> retrain launched: run_id={res['retrain']['run_id']}")
    else:
        print(f"[FAIL] trainer up but no retrain launched: {res.get('retrain')}")
        return 1

    # 4. Report is persisted and retrievable.
    s, mon = _req("GET", "/monitor")
    if s != 200 or not any(r["current"] == f"{LIVE}@{live_v}" for r in mon["reports"]):
        print(f"[FAIL] drift report not persisted in /monitor")
        return 1
    print(f"[OK] /monitor lists {len(mon['reports'])} report(s), including the drift one")

    print("\nT037 PASS — stable=no-op, induced drift -> report + retrain trigger (loop closed)")
    return 0


def test_drift_loop(require_gateway, require_key):
    """Pytest wrapper (005 US5): skip if the stack is down / no key (the retrain leg self-skips)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
