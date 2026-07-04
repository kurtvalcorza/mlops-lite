"""Shared adapter helpers (018 US2) — kept deliberately tiny; per-engine specifics stay in each
adapter. This is where the llama/whisper copy that the 2026-07 architecture review flagged (§4.2)
converges: both GPU-lease serving supervisors returned a byte-identical /health and both picked a
dynamic child port the same way. One definition here, one behaviour for every lease tenant.
"""
import os
import signal
import socket
import subprocess
import urllib.request


def free_port() -> int:
    """An ephemeral loopback port for an engine's child process. The small bind→close→respawn TOCTOU
    window (same host, ephemeral range) is acceptable and removes the fixed-port EADDRINUSE class of
    failure the old supervisors had against an orphaned child on a hardcoded port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProcGroup:
    """A Popen whose terminate/kill signal the whole process GROUP. BentoML forks worker processes,
    so killing only the launcher would orphan a worker still holding VRAM/RAM + its port. Spawned
    with start_new_session=True (the launcher leads the group). Exposes the Popen-like surface the
    shared lifecycle drives (`.pid .poll() .terminate() .kill() .wait()`)."""

    def __init__(self, popen):
        self._p = popen
        self.pid = popen.pid

    def poll(self):
        return self._p.poll()

    def _sig_group(self, sig):
        try:
            os.killpg(os.getpgid(self._p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._p.send_signal(sig)  # fall back to the single pid
            except Exception:
                pass

    def terminate(self):
        self._sig_group(signal.SIGTERM)

    def kill(self):
        self._sig_group(signal.SIGKILL)

    def wait(self, timeout=None):
        return self._p.wait(timeout)


def bento_spawn(run_sh_path: str):
    """Spawn a BentoML service via its run.sh on a fresh loopback port, in its OWN process group so
    the whole worker tree is reap-able (see ProcGroup). Returns (port, ProcGroup). The run scripts
    honor BENTO_HOST/BENTO_PORT and source .env for their MinIO creds — the agent stays torch-free
    (research R10) by wrapping them rather than importing their heavy deps."""
    port = free_port()
    env = dict(os.environ, BENTO_HOST="127.0.0.1", BENTO_PORT=str(port))
    p = subprocess.Popen(["bash", run_sh_path], env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return port, ProcGroup(p)


def http_200(url: str, timeout: float = 2.0) -> bool:
    """True iff `url` returns HTTP 200 — the BentoML `/readyz` readiness probe every bento child
    (vision/embed/tabular) shares."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def engine_health(admission, *, ok, reason, resident, model, vram_budget_gb, est_vram) -> dict:
    """The byte-compatible GPU-tenant /health payload the retired llama + whisper supervisors both
    returned (gateway `serving.gpu_state` / the transcribe health probe read
    `ok`/`resident`/`model`/`lease_holder`). `admission` (the single in-process GPU authority)
    supplies the GLOBAL holder + free VRAM — the `lease_holder` field keeps its legacy wire name for
    byte-compat but now reports `admission.holder()` (T364 retired the cross-process lockfile that
    used to be the shared source; with one owner, admission IS the whole truth).

    `ok` is the caller's `available()` result (Codex round 7, 018): an engine whose binary/model is
    missing must report NOT ok so the gateway readiness aggregator + swap target-probe don't treat an
    unavailable engine as usable. A cold/idle-but-available engine stays `ok`."""
    holder = admission.holder() if admission else None
    free = admission.free_gb() if admission else None
    payload = {
        "ok": ok,
        "resident": resident,
        "model": model,
        "vram_budget_gb": vram_budget_gb,
        "est_vram_gb": round(est_vram, 1) if est_vram is not None else None,
        "fits": (est_vram <= vram_budget_gb * 0.95) if est_vram is not None else None,
        "vram_free_gb": round(free, 1) if free is not None else None,
        "lease_holder": holder.get("tenant") if holder else None,
    }
    if not ok:
        payload["unavailable"] = reason
    return payload
