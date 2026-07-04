"""Subprocess entry for a single fine-tune run (CUDA isolation).

The trainer daemon (`trainer.py`) launches **one of these per run** instead of importing torch and
running the flow in a worker thread. Running each fine-tune in a fresh process means its CUDA context
is created and torn down by the OS on exit — so a crash (e.g. the Whisper/ASR `illegal memory access`
seen when a long-lived daemon had accumulated GPU state across heterogeneous runs) cannot corrupt the
daemon, and the next run always starts from a clean context.

Contract (kept dead simple so the daemon parses it robustly even if the process is SIGKILLed mid-run):
  argv:   run_flow.py <request_json_path> <result_json_path>
  input:  the `/train` request (incl. `modality`) read from <request_json_path>
  output: <result_json_path> is written with {"ok": true, "result": {...}}  on success
          or                                 {"ok": false, "error": "..."}  on a handled failure
  exit:   0 on success, 1 on a handled failure. If the process dies WITHOUT writing the result file
          (hard crash / OOM-kill / CUDA abort), the daemon treats the absent file as a failed run and
          surfaces the captured stderr tail — no partial version is registered (FR-032/FR-097).

All flow logs go to stdout/stderr (the agent redirects them to a per-run log file); the machine-
readable result travels only through <result_json_path>, so logs never corrupt it. This process
touches no GPU admission itself — the agent holds the single job slot (`kind="job"`) for the whole
run and this process is its child, so the one-GPU-tenant invariant covers it (T364).
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # training/ → flow_dispatch + flows
from flow_dispatch import dispatch  # noqa: E402


def read_result(result_path: str, returncode: int, log_tail: str = "") -> dict:
    """Daemon side of the protocol: turn a finished run's result file into a job-record update.

    A **missing OR unparseable** result file means the child died without a handled outcome — a hard
    crash / OOM-kill / signal (a SIGKILL mid-`json.dump` leaves a truncated file). Both map to the same
    failed-run record with the captured log tail and **no partial version** (FR-032/FR-097), so the
    daemon never surfaces a confusing `JSONDecodeError` for what is really a crash. Pure + stdlib-only
    so it is unit-testable without a GPU or the daemon's fcntl-backed lease.
    """
    payload = None
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (ValueError, OSError):
            payload = None  # truncated/corrupt (e.g. SIGKILL mid-write) → treat as a hard crash
    if payload is None:
        suffix = f"; log tail:\n{log_tail}" if log_tail else ""
        return {"status": "failed",
                "error": f"run subprocess exited {returncode} without a valid result "
                         f"(likely a hard CUDA/OOM crash){suffix}"}
    if payload.get("ok"):
        out = payload["result"]
        return {"status": "completed", "mlflow_run_id": out["run_id"],
                "model": out["model"], "metrics": out["metrics"], "params": out["params"]}
    return {"status": "failed", "error": payload.get("error", "unknown subprocess failure")}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: run_flow.py <request_json_path> <result_json_path>", file=sys.stderr)
        return 2
    req_path, result_path = sys.argv[1], sys.argv[2]
    with open(req_path, encoding="utf-8") as f:
        req = json.load(f)
    try:
        out = dispatch(req.get("modality", "llm"), req)
        payload, code = {"ok": True, "result": out}, 0
    except Exception as e:
        traceback.print_exc()  # full stack → stderr → the daemon's per-run log
        payload, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 1
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return code


if __name__ == "__main__":
    sys.exit(main())
