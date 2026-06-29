"""Offline test for the trainer's per-run subprocess protocol (CUDA-isolation refactor).

`training/run_flow.py` is the subprocess the trainer daemon spawns per fine-tune so each run gets a
fresh CUDA context. This validates the daemon↔subprocess contract WITHOUT a GPU or the heavy ML stack:
an unknown modality raises before any flow import, so the runner writes `{"ok": false, "error": ...}`
and exits 1, and a missing result file (hard-crash simulation) is the daemon's failure signal.

Runs anywhere (no live stack, no trainer daemon) — `flow_dispatch.dispatch` rejects an unknown modality
before importing torch.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_FLOW = os.path.join(REPO, "training", "run_flow.py")
REQUIRED = {"dataset_name": "d", "dataset_version": "v", "output_name": "o"}


def _invoke(req: dict):
    """Run run_flow.py <req> <result> in a subprocess; return (returncode, result_or_None)."""
    with tempfile.TemporaryDirectory() as tmp:
        req_path = os.path.join(tmp, "request.json")
        result_path = os.path.join(tmp, "result.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(req, f)
        proc = subprocess.run([sys.executable, RUN_FLOW, req_path, result_path],
                              capture_output=True, text=True, timeout=60)
        result = None
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as f:
                result = json.load(f)
        return proc.returncode, result


def _check_read_result() -> bool:
    """The daemon side of the protocol (run_flow.read_result) maps a missing/corrupt result file to a
    hard-crash failed run, and a valid file to the right job-record update. Pure — no GPU/fcntl."""
    sys.path.insert(0, os.path.join(REPO, "training"))
    from run_flow import read_result

    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "nope.json")
        rec = read_result(missing, returncode=137, log_tail="boom: CUDA error")
        if rec["status"] != "failed" or "without a valid result" not in rec["error"] \
                or "137" not in rec["error"] or "boom: CUDA error" not in rec["error"]:
            print(f"[FAIL] missing result file should be a hard-crash failure — got {rec}")
            return False
        print("[OK] missing result file -> failed (hard crash) with returncode + log tail")

        corrupt = os.path.join(tmp, "corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write('{"ok": true, "result": {"run_id"')  # truncated (SIGKILL mid-write)
        rec = read_result(corrupt, returncode=-9, log_tail="")
        if rec["status"] != "failed" or "without a valid result" not in rec["error"]:
            print(f"[FAIL] corrupt result file should be a hard-crash failure — got {rec}")
            return False
        print("[OK] corrupt result file -> failed (hard crash), not a JSONDecodeError")

        ok = os.path.join(tmp, "ok.json")
        with open(ok, "w", encoding="utf-8") as f:
            json.dump({"ok": True, "result": {"run_id": "r1", "model": {"name": "m", "version": "1"},
                                              "metrics": {"loss": 0.1}, "params": {"seed": 0}}}, f)
        rec = read_result(ok, returncode=0)
        if rec["status"] != "completed" or rec["mlflow_run_id"] != "r1" or rec["model"]["version"] != "1":
            print(f"[FAIL] ok result should be completed — got {rec}")
            return False
        print("[OK] ok result -> completed with run/model/metrics/params")

        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"ok": False, "error": "ValueError: dataset empty"}, f)
        rec = read_result(bad, returncode=1)
        if rec["status"] != "failed" or rec["error"] != "ValueError: dataset empty":
            print(f"[FAIL] not-ok result should be failed with its error — got {rec}")
            return False
        print("[OK] not-ok result -> failed with the flow's error")
    return True


def main() -> int:
    # 0. Daemon-side result parsing (missing/corrupt/ok/fail) — the hard-crash path included.
    if not _check_read_result():
        return 1

    # 1. Unknown modality → handled failure: result file written {ok:false}, exit 1 (no flow imported).
    code, result = _invoke({**REQUIRED, "modality": "bogus"})
    if code != 1 or not result or result.get("ok") is not False:
        print(f"[FAIL] unknown modality should exit 1 with ok:false — got code={code} result={result}")
        return 1
    if "unknown modality" not in (result.get("error") or ""):
        print(f"[FAIL] error should name the unknown modality — got {result.get('error')!r}")
        return 1
    print(f"[OK] unknown modality -> exit 1, {result['error'][:60]}…")

    # 2. Wrong arg count → usage error, exit 2, no result file.
    proc = subprocess.run([sys.executable, RUN_FLOW], capture_output=True, text=True, timeout=30)
    if proc.returncode != 2:
        print(f"[FAIL] missing args should exit 2 — got {proc.returncode}")
        return 1
    print("[OK] missing args -> exit 2 (usage)")

    print("\nPASS — trainer subprocess protocol: handled failures write a result + exit nonzero")
    return 0


def test_trainer_subprocess():
    """Pure offline test — no stack, no GPU, no heavy deps (unknown modality short-circuits)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
