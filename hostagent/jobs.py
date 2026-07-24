"""Jobs surface — the training daemon folded into the host agent (018 US2, T362, FR-172/FR-173).

`training/trainer.py`'s four launch paths (`/train`, `/study`, `/batch`, `/shadow-replay`) collapse
here into ONE parameterized `submit(kind, request)`. What the trainer did with its own lock + the
cross-process GPU lease + in-memory `_runs/_studies/_batches/_shadows` dicts is now done with the
agent's shared primitives:

  - **the single job slot** (`_active`) — one training/HPO/batch/shadow job at a time, exactly the
    trainer's one-`_lock` gate;
  - **GPU admission** (`hostagent.admission`) for the three GPU kinds — a fine-tune / HPO / shadow-
    replay is the heaviest single tenant (FR-097), acquired as `kind="job"` so the swap path refuses
    to preempt it *structurally* (`NON_PREEMPTABLE_KINDS`, FR-172) with no probe. A **batch** job
    does NOT acquire — a GPU batch drives a serving engine (which holds admission as `serving`);
    a tabular batch is off-GPU. `_gpu_batch_active` flags a GPU batch so the swap path won't
    evict the batch-driven serving holder (FR-155);
  - **the durable journal** (`hostagent.journal`) instead of restart-losing dicts (FR-173): every
    state transition is fsync'd, so a job that ran for minutes survives an agent restart.

CUDA isolation is preserved verbatim: fine-tune and shadow-replay each run in a **fresh subprocess**
(`training/run_flow.py` / `run_shadow.py`, unchanged) so a hard CUDA/OOM crash can't corrupt the
long-lived agent; the child pid is recorded as admission's VRAM owner. HPO and batch run in-process
(Optuna/MLflow/boto3 are torch-free; each HPO trial spawns its own training subprocess).

Legacy byte-compat (FR-177): `main.py` exposes `/train|/study|/batch|/shadow-replay` aliases that
render each JobRecord back to the trainer's exact response shape (`run_id`/`study_id`/…, `status`,
and the kind's top-level result fields), so the gateway's routers change base URL only.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from platformlib.topology import TRAINABLE_MODALITIES, Tenant, vram_budget_gb

from . import admission as adm

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRAINING = os.path.join(_REPO, "training")

VRAM_GB = vram_budget_gb()  # 020 US4 (FR-207): single shared resolver, no duplicated default
# Conservative VRAM floor for a run (base model + adapters + optimizer state) — the same value the
# trainer used so admission's live-VRAM gate refuses a start on a mostly-consumed GPU (507) instead
# of letting the worker hit CUDA OOM (008.1 #11), rather than an exact estimate.
TRAIN_EST_GB = float(os.getenv("TRAIN_EST_GB", "2.0"))
RUN_FLOW = os.path.join(_TRAINING, "run_flow.py")      # per-fine-tune subprocess (CUDA isolation)
RUN_SHADOW = os.path.join(_TRAINING, "run_shadow.py")  # per-shadow-replay subprocess
LOG_DIR = os.getenv("TRAINER_LOG_DIR",
                    os.path.join(tempfile.gettempdir(), "mlops-lite-trainer-logs"))

# Batch modalities that drive a serving engine's GPU (llm/vision/asr) — a batch of one of these
# makes the serving holder non-preemptable (FR-155). A tabular batch is off-GPU. The aliases (text-
# generation/image-classification) are the registry task names the gateway may forward.
GPU_BATCH_MODALITIES = {"llm", "vision", "asr"}
BATCH_MODALITIES = ("llm", "text-generation", "vision", "image-classification", "tabular")
# Registry-task aliases → their GPU modality. `BATCH_MODALITIES` admits both the canonical names and
# the task aliases the gateway may forward, but `GPU_BATCH_MODALITIES` (serving-holder protection) is
# keyed by the canonical modality — so an alias-named GPU batch must be normalized before the membership
# check, else it leaves `_gpu_batch_active` False and a promote reload / preempting request can change
# the engine mid-batch (mixed-version results).
_GPU_BATCH_ALIAS = {"text-generation": "llm", "image-classification": "vision"}


def _is_gpu_batch(modality: str) -> bool:
    """A batch that drives a GPU serving engine (so it must hold the non-preemptable serving slot)."""
    return _GPU_BATCH_ALIAS.get(modality, modality) in GPU_BATCH_MODALITIES

# How long a COMPLETED job's idempotency key still dedupes a re-issued identical launch (019/US4,
# FR-192): long enough to cover a lost-response re-detect or a park retry (a tick or two), short
# enough that a genuinely-new identical retrain much later still gets a fresh job. An in-flight job
# always dedupes regardless of this window.
IDEMPOTENCY_WINDOW_S = float(os.getenv("JOB_IDEMPOTENCY_WINDOW_S", "900"))


def _within_idempotency_window(rec, now, window_s):
    """Whether a prior job record `rec` should dedupe a re-issued identical launch (019/US4, FR-192):
    a still-active job (its response was lost while it runs) or one that completed within `window_s`
    (a park retry after it already finished). None / older → start fresh."""
    if rec is None:
        return False
    if rec.get("state") in ("queued", "running"):
        return True
    return now - (rec.get("submitted_at") or 0) <= window_s


class _Kind:
    """Static per-kind config: required fields, GPU-tenancy, modality validation, the legacy
    response id-key + terminal status word, and which run-record fields the legacy view exposes."""

    def __init__(self, name, *, route, id_key, gpu, required, terminal,
                 modality_set=None, modality_default=None, id_field=None, extra=()):
        self.name = name
        self.route = route              # legacy URL segment (train/study/batch/shadow-replay)
        self.id_key = id_key            # legacy id key (run_id/study_id/batch_id/shadow_id)
        self.gpu = gpu                  # acquires GPU admission as kind="job"
        self.required = required        # request fields that must be present (400 if missing)
        self.terminal = terminal        # legacy success status word (completed|succeeded)
        self.modality_set = modality_set
        self.modality_default = modality_default
        self.id_field = id_field        # request field carrying a client-supplied id (shadow_id)
        self.extra = extra              # run-record fields surfaced at the legacy top level


KINDS = {
    "finetune": _Kind(
        "finetune", route="train", id_key="run_id", gpu=True,
        required=("dataset_name", "dataset_version", "output_name"), terminal="completed",
        modality_set=TRAINABLE_MODALITIES, modality_default="llm",
        extra=("mlflow_run_id", "model", "metrics", "params", "error")),
    "hpo": _Kind(
        "hpo", route="study", id_key="study_id", gpu=True,
        required=("dataset_name", "dataset_version", "output_name"), terminal="completed",
        modality_set=TRAINABLE_MODALITIES, modality_default="llm",
        extra=("summary", "best", "error")),
    "batch": _Kind(
        "batch", route="batch", id_key="batch_id", gpu=False,
        required=("dataset_name", "dataset_version", "model"), terminal="succeeded",
        modality_set=BATCH_MODALITIES, modality_default="llm",
        extra=("result", "error")),
    "shadow": _Kind(
        "shadow", route="shadow-replay", id_key="shadow_id", gpu=True,
        required=("name", "challenger", "champion_version", "modality", "shadow_id"),
        terminal="succeeded", id_field="shadow_id", extra=("result", "error")),
}
ROUTE_TO_KIND = {k.route: k.name for k in KINDS.values()}


def _tail(path: str, limit: int = 1500) -> str:
    """Last `limit` chars of a file (failure-path log tail), seeking from the end so a verbose
    training log isn't read into memory whole."""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read()
    except OSError:
        return ""


class JobManager:
    """The agent's one-job-at-a-time launcher over `admission` + `journal`. Framework-free (no HTTP)
    so it is unit-testable; `spawn` is injectable for tests that must not fork a real subprocess."""

    def __init__(self, admission: adm.Admission, journal, *, tenant: str = Tenant.TRAINING,
                 est_gb: float = TRAIN_EST_GB, vram_budget_gb: float = VRAM_GB,
                 clock=time.time, spawn=subprocess.Popen, gpu_free_mib=None, runners=None):
        self.admission = admission
        self.journal = journal
        self.tenant = tenant
        self.est_gb = est_gb
        self.vram_budget_gb = vram_budget_gb
        self.clock = clock
        self._spawn = spawn
        self._gpu_free_mib = gpu_free_mib or self._read_gpu_free_mib
        # runner(job_manager, job_id, request) -> record update; injectable so a unit test can drive
        # the worker/journal path without importing the heavy training flows.
        self._runners = runners or _DEFAULT_RUNNERS
        self.lock = threading.Lock()
        self._active = None            # job_id holding the single job slot (queued/running)
        self._gpu_batch_active = False  # a GPU-modality batch runs → serving holder not preemptable
        self._children = {}            # job_id -> Popen (subprocess kinds only, for cancel)
        self._run_keys = {}            # idempotency_key -> job_id (019/US4 — dedupe a re-launch)

    # -- health fields the trainer's consumers still read (byte-compat, FR-177) ------------------
    def _read_gpu_free_mib(self):
        free_gb = self.admission.free_gb()
        return None if free_gb is None else int(free_gb * 1024)

    def health_fields(self) -> dict:
        """The trainer `/health` fields (`busy/active/gpu_batch_active/gpu_free_mib/vram_budget_gb`)
        swap.py, platform_metrics.py, and the gateway training-health view read. Merged into the
        agent's own `/health` so those consumers keep working with only a base-URL flip."""
        with self.lock:
            return {"busy": self._active is not None, "active": self._active,
                    "gpu_batch_active": self._gpu_batch_active,
                    "gpu_free_mib": self._gpu_free_mib(), "vram_budget_gb": self.vram_budget_gb}

    # -- submit (the one parameterized launch path) ---------------------------------------------
    def submit(self, kind: str, request: dict):
        """Validate, take the single job slot (+ GPU admission for GPU kinds), journal the queued
        record, and start the worker thread. Returns (status_code, payload) with the trainer's
        vocabulary: 400 bad body, 409 busy/GPU-held, 507 VRAM. Preserves per-kind validation order
        (fields → modality) so an unknown modality never reserves a slot."""
        spec = KINDS.get(kind)
        if spec is None:
            return 400, {"error": f"unknown job kind {kind!r}"}
        for f in spec.required:
            if not request.get(f):
                return 400, {"error": f"missing field: {f}"}
        modality = request.get("modality")
        if spec.modality_set is not None:
            modality = (request.get("modality") or spec.modality_default or "").lower()
            if modality not in spec.modality_set:
                return 400, {"error": f"unknown modality {modality!r} "
                                      f"(expected {'|'.join(spec.modality_set)})"}
            request = {**request, "modality": modality}

        with self.lock:
            # Idempotent re-launch (019/US4, FR-192): a policy retrain whose response was lost, or a
            # park re-launched after a store blip, re-issues this exact request with the same
            # idempotency_key. Return the existing job instead of starting a duplicate on the single
            # GPU. Checked BEFORE the busy gate so a re-launch of the still-active job returns it (not
            # a 409). Manual callers omit the key → no dedupe (today's behavior).
            key = request.get("idempotency_key")
            if key is not None:
                hit = self._run_keys.get(key)
                rec = self.journal.get(hit) if hit else None
                if _within_idempotency_window(rec, self.clock(), IDEMPOTENCY_WINDOW_S):
                    return 200, {spec.id_key: hit, "status": rec.get("state", "queued"),
                                 "idempotent": True}
            if self._active is not None:
                return 409, {"error": f"agent busy with job {self._active}"}
            if spec.gpu:
                try:  # the single GPU slot — a run/HPO/shadow is one GPU tenant (FR-097/FR-168)
                    self.admission.acquire(self.tenant, "job", self.est_gb)
                except adm.Held as e:
                    return 409, {"error": str(e), "holder": e.holder.get("tenant"),
                                 "kind": e.holder.get("kind")}
                except adm.VramExceeded as e:
                    return 507, {"error": str(e)}
            job_id = request[spec.id_field] if spec.id_field else uuid.uuid4().hex[:12]
            record = {"job_id": job_id, "kind": kind, "state": "queued", "modality": modality,
                      "request": request, "submitted_at": self.clock(), spec.id_key: job_id}
            for field in spec.extra:  # byte-compat: legacy record carries these (null until done)
                record.setdefault(field, None)
            try:
                self.journal.submit(record)
            except Exception:
                # US4 T375-B: the journal write is a Postgres upsert now (was a local fsync) — a store
                # blip must not strand the GPU lease we just took. Release it before propagating so the
                # next submit isn't wrongly refused with a 409 by a lease no live job holds. `_active`
                # is still unset here, so only the lease needs unwinding.
                if spec.gpu:
                    self.admission.release(self.tenant)
                raise
            if key is not None:
                # Prune keys whose job has aged out of the dedup window before recording this one, so
                # _run_keys stays bounded to roughly the in-flight + recent set over a long agent
                # uptime (review nit) — an out-of-window key can no longer dedupe anyway. Submits are
                # infrequent (one per launch), so the O(n) sweep is negligible.
                now = self.clock()
                self._run_keys = {
                    k: jid for k, jid in self._run_keys.items()
                    if _within_idempotency_window(self.journal.get(jid), now, IDEMPOTENCY_WINDOW_S)}
                self._run_keys[key] = job_id   # so a re-issued identical launch dedupes to this job
            self._active = job_id
            if kind == "batch" and _is_gpu_batch(modality):
                self._gpu_batch_active = True
        try:
            threading.Thread(target=self._worker, args=(kind, job_id, request),
                             name=f"job-{job_id}", daemon=True).start()
        except BaseException:  # spawn failed — never strand the slot/lease (same as the trainer)
            self._release(kind, spec)
            raise
        payload = {spec.id_key: job_id, "status": "queued"}
        if kind == "hpo":
            payload["note"] = ("HPO trials run sequentially on the one GPU; "
                               "wall-clock ~= n_trials x per-train-time")
        return 202, payload

    def _release(self, kind: str, spec: "_Kind") -> None:
        """Free the GPU slot (GPU kinds), clear the batch flag, and drop the single job slot —
        atomically under `lock` so a concurrent submit can't observe 'slot free but lease held'
        (the trainer's `_worker` finally discipline, Claude review F1)."""
        with self.lock:
            if spec.gpu:
                self.admission.release(self.tenant)
            self._active = None
            self._gpu_batch_active = False

    def _worker(self, kind: str, job_id: str, request: dict) -> None:
        spec = KINDS[kind]
        try:
            self.journal.transition(job_id, "running", started_at=self.clock())
        except Exception:
            # US4 T375-B: the running-transition is a networked Postgres write now (was a local
            # fsync that never raised) — a store blip HERE mustn't wedge the agent. This runs before
            # the try/finally below, so without this guard the daemon thread would die with the slot
            # and GPU lease still held → every later submit 409s forever until a manual restart.
            # Free the slot/lease (@claude PR#46); the record stays durably `queued` and the next
            # restart's `mark_interrupted` reconciles it.
            self._release(kind, spec)
            return
        update = {}
        try:
            update = self._runners[kind](self, job_id, request)
        except Exception as e:  # daemon-side plumbing failed (spawn/IO) — a failed job, no version
            update = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        finally:
            self._children.pop(job_id, None)
            status = update.pop("status", "failed")
            fields = {k: v for k, v in update.items() if k != "result"}
            # Journal the terminal state BEFORE freeing the slot (@claude PR#33): the durable write
            # must land first, else a crash in the window between release and this fsync leaves the
            # journal showing a COMPLETED job as queued/running — `mark_interrupted` would then fail
            # it on restart, losing its real result (the exact loss FR-173's journal prevents) — and
            # a concurrent /health could read busy=false while `jobs_active` still counts it.
            # Release runs in a nested finally so a journal-write error can never strand the slot.
            try:
                self.journal.transition(job_id, status, ended_at=self.clock(),
                                        result=update.get("result"), fields=fields or None)
            finally:
                self._release(kind, spec)

    # -- the four runners (verb bodies unchanged from trainer.py) --------------------------------
    def _run_finetune(self, job_id: str, request: dict) -> dict:
        self._ensure_training_path()
        from run_flow import read_result  # daemon side of the subprocess result protocol
        return self._run_subprocess(job_id, request, RUN_FLOW, read_result, prefix="run")

    def _run_shadow(self, job_id: str, request: dict) -> dict:
        return self._run_subprocess(job_id, {**request, "shadow_id": job_id}, RUN_SHADOW,
                                    self._read_shadow_result, prefix="shadow")

    def _run_subprocess(self, job_id: str, request: dict, entry: str, read_result, *,
                        prefix: str) -> dict:
        """Run one GPU job in a fresh subprocess (`run_flow.py`/`run_shadow.py`) and return its
        job-record update. CUDA isolation: the flow's torch/CUDA context lives and dies with the
        child, so a crash can't corrupt the agent. Request + result travel through temp files; the
        child's merged stdout/stderr is a per-job log tailed on failure. The child pid is recorded
        as admission's VRAM owner right after spawn (crash forensics; with in-process admission the
        agent's own liveness gates the slot, so the child needn't)."""
        self._ensure_training_path()
        os.makedirs(LOG_DIR, exist_ok=True)
        import json
        log_path = os.path.join(LOG_DIR, f"{prefix}-{job_id}.log")
        with tempfile.TemporaryDirectory(prefix=f"{prefix}-{job_id}-") as tmp:
            req_path = os.path.join(tmp, "request.json")
            result_path = os.path.join(tmp, "result.json")
            with open(req_path, "w", encoding="utf-8") as f:
                json.dump(request, f)
            with open(log_path, "w", encoding="utf-8") as logf:
                proc = self._spawn([sys.executable, entry, req_path, result_path],
                                   stdout=logf, stderr=subprocess.STDOUT, env=os.environ.copy())
                self._children[job_id] = proc
                self.admission.set_child(self.tenant, proc.pid)
                proc.wait()
            return read_result(result_path, proc.returncode, _tail(log_path))

    def _read_shadow_result(self, result_path: str, returncode: int, log_tail: str = "") -> dict:
        """Daemon side of the run_shadow.py protocol → a shadow job-record update. A missing/corrupt
        result file means the child died without a handled outcome → a failed replay + the tail."""
        import json
        payload = None
        if os.path.exists(result_path):
            try:
                with open(result_path, encoding="utf-8") as f:
                    payload = json.load(f)
            except (ValueError, OSError):
                payload = None  # truncated/corrupt (SIGKILL mid-write) → treat as a hard crash
        if payload is None:
            suffix = f"; log tail:\n{log_tail}" if log_tail else ""
            return {"status": "failed",
                    "error": f"shadow subprocess exited {returncode} without a valid result "
                             f"(likely a hard CUDA/OOM crash){suffix}"}
        if payload.get("ok"):
            return {"status": "succeeded", "result": payload["result"]}
        return {"status": "failed", "error": payload.get("error", "unknown shadow failure")}

    def _run_hpo(self, job_id: str, request: dict) -> dict:
        """One HPO study (012): an Optuna study whose trials are sequential fine-tune runs, each
        scored by 011's metric, best registered. Runs IN-PROCESS (Optuna/MLflow are torch-free)
        while each trial spawns its own training subprocess — trials never co-reside on the GPU."""
        self._ensure_training_path()
        from flows.hpo import run_study
        kwargs = {"overrides": request.get("overrides") or None}
        if request.get("n_trials") is not None:
            kwargs["n_trials"] = int(request["n_trials"])
        if request.get("timeout") is not None:
            kwargs["timeout"] = float(request["timeout"])
        summary = run_study(request, **kwargs)
        return {"status": "completed", "summary": summary, "best": summary.get("best")}

    def _run_batch(self, job_id: str, request: dict) -> dict:
        """One batch-inference job (014): score a dataset version through the existing serving path
        and write a content-addressed result to Garage. Runs IN-PROCESS and does NOT hold GPU
        admission — a GPU batch drives a serving engine (the admission holder); a tabular batch is
        off-GPU. `_gpu_batch_active` (set at submit) keeps a GPU batch's serving holder safe."""
        self._ensure_training_path()
        from flows.batch_infer import batch_infer_flow
        out = batch_infer_flow(
            request["dataset_name"], request["dataset_version"], request["model"],
            modality=request["modality"], registry_version=request.get("registry_version"),
            abort_threshold=float(request.get("abort_threshold", 0.5)))
        return {"status": "succeeded", "result": out}

    @staticmethod
    def _ensure_training_path() -> None:
        """Put `training/` on sys.path so the ported flows (`run_flow`, `flows.*`) import — the
        subprocess entries set their own path; this is for the in-process HPO/batch runners."""
        if _TRAINING not in sys.path:
            sys.path.insert(0, _TRAINING)

    # -- reads + cancel -------------------------------------------------------------------------
    def get(self, job_id: str):
        return self.journal.get(job_id)

    def jobs(self, kind: str = None) -> list:
        return self.journal.jobs(kind=kind)

    def cancel(self, job_id: str) -> dict:
        """Best-effort terminate of a run's subprocess (FR / contracts/agent-api.md). Only the
        subprocess kinds (finetune/shadow) have a killable child; HPO/batch run in-process and
        return `not-cancellable`. The worker's finally still journals the failed/terminated outcome
        and releases the slot."""
        proc = self._children.get(job_id)
        if proc is None:
            rec = self.journal.get(job_id)
            if rec is None:
                return {"status": "unknown", "detail": f"no job {job_id}"}
            if rec.get("state") in ("queued", "running"):
                return {"status": "not-cancellable",
                        "detail": "in-process job (HPO/batch) — no killable subprocess"}
            return {"status": rec.get("state"), "detail": "already terminal"}
        try:
            proc.terminate()
        except Exception:
            pass
        return {"status": "cancelling", "job_id": job_id}


#: kind -> runner(job_manager, job_id, request) -> record update. Bound at module scope so a test
#: can pass a subset override to `JobManager(runners=…)` without touching the class.
_DEFAULT_RUNNERS = {
    "finetune": JobManager._run_finetune, "hpo": JobManager._run_hpo,
    "batch": JobManager._run_batch, "shadow": JobManager._run_shadow,
}


def legacy_view(record: dict) -> dict:
    """Render a JobRecord to the trainer's exact legacy response shape (byte-compat, FR-177): the
    kind's id key (`run_id`/`study_id`/…), `status` (not the journal's `state`), and the kind's
    result fields at the top level. `interrupted` (a crash-replayed job) maps to `status:failed`
    so a poller reads a terminal non-success, exactly as it would for any failed run."""
    kind = record.get("kind")
    spec = KINDS.get(kind)
    if spec is None:
        return dict(record)
    job_id = record.get("job_id")
    state = record.get("state")
    status = "failed" if state == "interrupted" else state
    view = {spec.id_key: job_id, "status": status, "request": record.get("request")}
    for field in spec.extra:
        if field in record:
            view[field] = record.get(field)
    if record.get("error") is not None:
        view["error"] = record["error"]
    # (No top-level `trials` for HPO — the retired trainer never returned one; `trials` stays nested
    # under `summary`, which `spec.extra` already surfaces. Strict byte-compat, @claude PR#33.)
    return view
