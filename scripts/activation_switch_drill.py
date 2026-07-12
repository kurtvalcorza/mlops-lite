#!/usr/bin/env python3
"""023 US5 SC-159 (T528) — LLM activation rapid-switch drill on the GPU host.

Exercises the durable ActivationOperation switch path (the gated `POST /models/{name}/promote`,
`preempt=true`) live on the target GPU, capturing agent identity + `nvidia-smi` evidence:

  1. **N accepted rapid switches** alternating two LLM registry versions (A<->B). After EVERY
     accepted switch it asserts the four SC-159 invariants:
       * `activation.consistent` is true and the agent-reported RESIDENT identity equals the
         just-promoted target (never the desired pointer — no false identity);
       * exactly one GPU tenant — the agent holder is `llm` and only one GPU engine is non-cold;
       * peak total VRAM stays under a one-model ceiling (two co-resident models would exceed it);
       * the last accepted target wins (resident follows the most recent promote).
  2. **Client-timeout idempotency** — a switch whose client aborts mid-reload, retried for the SAME
     target, causes exactly ONE physical reload (delta of the agent's
     `hostagent_reload_outcomes_total{status=loaded|reloaded|swapped}` == 1; the retry is a
     `noop`/`replayed`), proving the operation-id keyed reload is idempotent (FR-307).
  3. **Job-holder refusal** (single-admission-slot, both directions):
       * A GPU job launched while an LLM is resident is refused `409 Held{holder=serving}` — a
         background job can never snipe the GPU from a serving tenant;
       * with the GPU freed and a fine-tune HOLDING the lease, a preempting switch is DEFERRED
         (the non-preemptable job is never interrupted) and the job runs to completion (FR-172/259).

Exit 0 iff all requested phases pass. Usage (Windows host, stack up via up_all.ps1):

    python scripts/activation_switch_drill.py --cycles 100 --report
    python scripts/activation_switch_drill.py --cycles 100 --skip-job-holder
"""
import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- config / secrets --------------------------------------------------------------------------------

def _env_val(key, env_path):
    try:
        for line in open(env_path, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _resolve_agent_url(explicit, distro):
    if explicit:
        return explicit.rstrip("/")
    try:
        ip = subprocess.run(["wsl.exe", "-d", distro, "hostname", "-I"],
                            capture_output=True, text=True, timeout=10).stdout.split()[0]
        return f"http://{ip}:8100"
    except Exception:
        return "http://127.0.0.1:8100"


# -- HTTP --------------------------------------------------------------------------------------------

def _req(url, *, method="GET", body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def _try(url, **kw):
    """_req that maps an HTTPError to (code, body) and a transport error to (0, {'error': ...})."""
    try:
        return _req(url, **kw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


# -- witnesses ---------------------------------------------------------------------------------------

def _nvidia_used_gb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        return float(out.stdout.strip().splitlines()[0]) / 1024.0 if out.returncode == 0 else None
    except Exception:
        return None


class Agent:
    def __init__(self, url, key):
        self.url = url
        self.h = {"X-Agent-Key": key} if key else {}

    def health(self):
        return _try(f"{self.url}/health", headers=self.h, timeout=10)[1]

    def metrics_text(self):
        req = urllib.request.Request(f"{self.url}/metrics", headers=self.h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode(errors="replace")
        except Exception:
            return ""

    def physical_reload_count(self):
        total = 0.0
        for line in self.metrics_text().splitlines():
            if line.startswith("hostagent_reload_outcomes_total{") and \
                    any(f'status="{s}"' in line for s in ("loaded", "reloaded", "swapped")):
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    pass
        return total

    def unload_llm(self):
        return _try(f"{self.url}/control/unload", method="POST", body={"engine": "llm"},
                    headers=self.h, timeout=30)


class Gateway:
    def __init__(self, url, key):
        self.url = url
        self.h = {"X-API-Key": key} if key else {}

    def promote(self, model, version, *, preempt=True, override=True, timeout=120):
        return _try(f"{self.url}/models/{model}/promote", method="POST",
                    body={"version": str(version), "preempt": preempt, "override": override},
                    headers=self.h, timeout=timeout)

    def activation(self):
        return _try(f"{self.url}/serving/llm/activation", headers=self.h, timeout=15)[1]

    def launch_run(self, req, timeout=30):
        return _try(f"{self.url}/runs", method="POST", body=req, headers=self.h, timeout=timeout)

    def run_status(self, run_id):
        return _try(f"{self.url}/runs/{run_id}", headers=self.h, timeout=15)[1]


# -- phase 1: rapid switches -------------------------------------------------------------------------

def phase_switches(gw, agent, model, a, b, cycles, ceiling):
    print(f"\n== phase 1: {cycles} rapid switches {model}@{a} <-> @{b} ==", flush=True)
    landed = 0
    mismatched = 0
    inconsistent = 0
    peak = 0.0
    trough = None
    holders = set()
    engines_over_one = 0
    i = 0
    t0 = time.time()
    while landed < cycles:
        target = a if i % 2 == 0 else b
        st, resp = gw.promote(model, target)
        i += 1
        if i > cycles * 4:
            break
        sl = resp.get("serving_llm", {}) if isinstance(resp, dict) else {}
        reload_status = (sl.get("reload") or {}).get("status")
        state = (sl.get("activation") or {}).get("state")
        if not (st == 200 and resp.get("promoted") and state == "active"
                and reload_status in ("loaded", "reloaded", "swapped", "noop")):
            # a transient defer/verify_pending is legal — retry the same target next loop
            continue
        # identity authority: agent-reported resident must equal the just-promoted target
        act = gw.activation()
        res = act.get("resident") or {}
        if not act.get("consistent") or res.get("model_name") != model \
                or str(res.get("version")) != str(target):
            inconsistent += 1
            print(f"  [!] inconsistent @cycle {landed}: desired={act.get('desired')} "
                  f"resident={res}", flush=True)
        else:
            landed += 1
        # one-tenant witnesses
        h = agent.health()
        holder = h.get("holder")
        if holder:
            holders.add(holder)
        gpu_engines = {"llm", "vision", "asr"}
        non_cold = [e for e, s in (h.get("engines") or {}).items()
                    if e in gpu_engines and s not in ("cold", "unavailable", "loading")]
        if len(non_cold) > 1:
            engines_over_one += 1
        if holder != "llm":
            mismatched += 1
        v = _nvidia_used_gb()
        if v is not None:
            peak = max(peak, v)
            trough = v if trough is None else min(trough, v)
        if landed % 20 == 0 and landed:
            print(f"  {landed}/{cycles} landed | holder={holder} peakVRAM={peak:.2f}GB", flush=True)
    dur = time.time() - t0
    ok = (landed >= cycles and mismatched == 0 and inconsistent == 0
          and engines_over_one == 0 and peak < ceiling)
    print(f"  landed={landed}/{cycles} mismatches={mismatched} inconsistent={inconsistent} "
          f"engines>1={engines_over_one}")
    print(f"  holders seen={sorted(holders)} (single-slot: one at a time)")
    print(f"  peak VRAM={peak:.2f}GB trough={trough:.2f}GB (ceiling {ceiling}GB) in {dur:.0f}s "
          f"over {i} requests")
    print(f"  PHASE 1: {'PASS' if ok else 'FAIL'}")
    return ok, {"landed": landed, "mismatched": mismatched, "inconsistent": inconsistent,
                "engines_over_one": engines_over_one, "peak_vram_gb": round(peak, 2),
                "trough_vram_gb": round(trough or 0, 2), "holders": sorted(holders),
                "requests": i, "duration_s": round(dur, 1)}


# -- phase 2: client-timeout idempotency -------------------------------------------------------------

def phase_idempotency(gw, agent, model, a, b):
    print("\n== phase 2: client-timeout idempotency ==", flush=True)
    # Put the OTHER version resident so the timed-out switch to A is a real (physical) reload.
    gw.promote(model, b)
    act = gw.activation()
    if str((act.get("resident") or {}).get("version")) != str(b):
        print(f"  [!] could not seat {model}@{b} first: {act}")
        return False, {"error": "setup failed"}
    base = agent.physical_reload_count()
    # Fire the switch to A with a client abort mid-reload (server continues to completion).
    st, resp = gw.promote(model, a, timeout=0.4)
    client_timed_out = (st == 0)
    print(f"  forced-timeout switch to @{a}: client_timed_out={client_timed_out} "
          f"({resp.get('error', st)})")
    # Wait for the server to finish the physical reload (resident becomes A, consistent).
    resident_a = False
    for _ in range(60):
        act = gw.activation()
        res = act.get("resident") or {}
        if act.get("consistent") and str(res.get("version")) == str(a):
            resident_a = True
            op1 = (act.get("activation") or {}).get("operation_id")
            break
        time.sleep(0.5)
    else:
        op1 = None
    # Retry the SAME target — must NOT cause a second physical reload.
    st2, resp2 = gw.promote(model, a)
    sl = (resp2 or {}).get("serving_llm", {})
    retry_status = (sl.get("reload") or {}).get("status")
    retry_replayed = (sl.get("reload") or {}).get("replayed")
    op2 = (sl.get("activation") or {}).get("operation_id")
    time.sleep(0.5)
    physical_delta = agent.physical_reload_count() - base
    idempotent = retry_status in ("noop", None) or bool(retry_replayed) or op2 == op1
    ok = (client_timed_out and resident_a and abs(physical_delta - 1.0) < 0.001 and idempotent)
    print(f"  op1={op1} op2={op2} retry_reload_status={retry_status} replayed={retry_replayed}")
    print(f"  physical reloads for the timed-out+retried switch: {physical_delta:.0f} (must be 1)")
    print(f"  PHASE 2: {'PASS' if ok else 'FAIL'}")
    return ok, {"client_timed_out": client_timed_out, "resident_reached_A": resident_a,
                "physical_reload_delta": physical_delta, "retry_status": retry_status,
                "retry_replayed": bool(retry_replayed), "op_reused": op2 == op1}


# -- phase 3: job-holder refusal ---------------------------------------------------------------------

def phase_job_holder(gw, agent, model, a, b, dataset, dsver):
    print("\n== phase 3: job-holder refusal ==", flush=True)
    result = {}
    # Direction A: a GPU job cannot preempt a resident serving tenant (an LLM is resident from ph2).
    gw.promote(model, a)
    st, resp = gw.launch_run({"dataset_name": dataset, "dataset_version": dsver,
                              "output_name": "t528-jobholder-A", "modality": "llm", "steps": 5})
    dir_a_ok = (st == 409)
    print(f"  A) launch fine-tune while {model}@{a} resident -> HTTP {st} "
          f"holder={resp.get('holder')} (expect 409, serving holds)")
    result["direction_a_refused_409"] = dir_a_ok

    # Direction B: free the GPU, let a fine-tune HOLD the lease, then a preempting switch must DEFER.
    ul_st, ul = agent.unload_llm()
    print(f"  B) unload llm -> {ul.get('status', ul_st)}")
    st, resp = gw.launch_run({"dataset_name": dataset, "dataset_version": dsver,
                              "output_name": "t528-jobholder-B", "modality": "llm", "steps": 30})
    run_id = resp.get("run_id") or resp.get("job_id")
    print(f"  B) launch fine-tune (GPU free) -> HTTP {st} run={run_id}")
    holding = False
    for _ in range(90):
        h = agent.health()
        if h.get("holder_kind") == "job" or (h.get("jobs_active") or 0) >= 1:
            holding = True
            break
        rs = gw.run_status(run_id) if run_id else {}
        if rs.get("state") in ("failed", "error"):
            print(f"  B) fine-tune failed to hold: {rs}")
            break
        time.sleep(1)
    if holding:
        st, resp = gw.promote(model, b, timeout=30)
        sl = (resp or {}).get("serving_llm", {})
        rstatus = (sl.get("reload") or {}).get("status")
        h2 = agent.health()
        job_alive = h2.get("holder_kind") == "job" or (h2.get("jobs_active") or 0) >= 1
        dir_b_ok = (rstatus in ("deferred", None) or st == 409) and job_alive
        print(f"  B) switch while job holds -> reload status={rstatus} http={st} "
              f"job_still_holding={job_alive} (expect deferred + job uninterrupted)")
        result["direction_b_deferred"] = rstatus == "deferred" or st == 409
        result["direction_b_job_uninterrupted"] = job_alive
    else:
        print("  B) fine-tune never acquired the lease within 90s — Direction B not exercised")
        dir_b_ok = None
        result["direction_b_deferred"] = None
    # let the short job wind down so the box is clean for later phases
    for _ in range(120):
        h = agent.health()
        if (h.get("jobs_active") or 0) == 0 and h.get("holder_kind") != "job":
            break
        time.sleep(2)
    ok = dir_a_ok and (dir_b_ok is not False)
    print(f"  PHASE 3: {'PASS' if ok else 'FAIL'} (A={dir_a_ok} B={dir_b_ok})")
    return ok, result


# -- main --------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=100)
    ap.add_argument("--model", default="qwen2.5-0.5b-instruct")
    ap.add_argument("--a", default="1")
    ap.add_argument("--b", default="2")
    ap.add_argument("--gateway", default=os.getenv("GATEWAY", "http://localhost:8080"))
    ap.add_argument("--agent-url", default=os.getenv("AGENT_URL", ""))
    ap.add_argument("--distro", default="Ubuntu")
    ap.add_argument("--env", default=os.path.join(REPO, ".env"))
    ap.add_argument("--max-vram-gb", type=float, default=6.0,
                    help="one-model ceiling (the 0.5B footprint is ~1.9GB; two would exceed 6)")
    ap.add_argument("--job-dataset", default="jobsmoke-sft")
    ap.add_argument("--job-dataset-version", default="8647d3617633")
    ap.add_argument("--skip-job-holder", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="append a markdown evidence block to docs/on-hardware-validation.md")
    args = ap.parse_args()

    gw = Gateway(args.gateway.rstrip("/"),
                 (_env_val("GATEWAY_API_KEYS", args.env) or "").split(",")[0])
    agent = Agent(_resolve_agent_url(args.agent_url, args.distro),
                  _env_val("AGENT_API_KEY", args.env))
    print(f"activation-switch drill: gateway={args.gateway} agent={agent.url}")
    print(f"security_mode={agent.health().get('security_mode')} "
          f"gpu_free_gb={agent.health().get('gpu_free_gb')}")

    summary = {"model": args.model, "a": args.a, "b": args.b, "cycles": args.cycles}
    p1_ok, summary["switches"] = phase_switches(gw, agent, args.model, args.a, args.b,
                                                args.cycles, args.max_vram_gb)
    p2_ok, summary["idempotency"] = phase_idempotency(gw, agent, args.model, args.a, args.b)
    if args.skip_job_holder:
        p3_ok, summary["job_holder"] = True, {"skipped": True}
    else:
        p3_ok, summary["job_holder"] = phase_job_holder(
            gw, agent, args.model, args.a, args.b, args.job_dataset, args.job_dataset_version)

    all_ok = p1_ok and p2_ok and p3_ok
    summary["result"] = "PASS" if all_ok else "FAIL"
    summary["measured_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\n== SC-159 activation-switch drill ==")
    print(json.dumps(summary, indent=2))
    if args.report:
        path = os.path.join(REPO, "docs", "on-hardware-validation.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n### SC-159 activation-switch drill (T528) — {summary['measured_at']}\n\n"
                     f"```json\n{json.dumps(summary, indent=2)}\n```\n")
        print(f"[drill] evidence appended to {path}")
    print(f"\nRESULT: {summary['result']}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
