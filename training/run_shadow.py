"""Subprocess entry for a single shadow-replay (CUDA isolation, 016 US2).

Same rationale as run_flow.py: the trainer daemon launches **one of these per shadow-replay** instead of
importing torch and running `replay_job` in a worker thread. The challenger's torch/CUDA context (the
vision scorer loads the model in-process) is created and torn down by the OS when this child exits, so it
never accumulates in the long-lived daemon — the daemon otherwise never holds a CUDA context (it spawns
every GPU job as a subprocess). LLM/ASR challengers already spawn their own transient llama.cpp/whisper.cpp
servers that free VRAM on teardown; running the whole job here isolates the vision path too and keeps the
one-model-in-VRAM invariant honest (Principle II).

Contract (kept dead simple so the daemon parses it robustly even if the process is SIGKILLed mid-run):
  argv:   run_shadow.py <request_json_path> <result_json_path>
  input:  {name, champion_version, challenger, modality, shadow_id, window_n?} read from the request path
  output: <result_json_path> is written with {"ok": true, "result": <verdict>} on success
          or                                 {"ok": false, "error": "..."}     on a handled failure
  exit:   0 on success, 1 on a handled failure. A death WITHOUT a result file (hard CUDA/OOM crash) is
          surfaced by the daemon as a failed replay with the captured log tail.

`replay_job` persists the advisory verdict itself (via the gateway's shadow orchestration → MinIO),
so the result file is only for the agent's in-memory job record. This process touches no GPU
admission itself — the agent holds the single job slot (`kind="job"`) for the whole replay and this
process is its child, so the one-GPU-tenant invariant covers it (T364).
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # training/ → flows on path
from flows.shadow_replay import replay_job  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: run_shadow.py <request_json_path> <result_json_path>", file=sys.stderr)
        return 2
    req_path, result_path = sys.argv[1], sys.argv[2]
    with open(req_path, encoding="utf-8") as f:
        req = json.load(f)
    try:
        out = replay_job(req["name"], req["champion_version"], req["challenger"], req["modality"],
                         shadow_id=req["shadow_id"], window_n=req.get("window_n"))
        payload, code = {"ok": True, "result": out}, 0
    except Exception as e:
        traceback.print_exc()  # full stack → stderr → the daemon's per-shadow log
        payload, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 1
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return code


if __name__ == "__main__":
    sys.exit(main())
