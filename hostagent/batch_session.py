"""025 US1 (T597) — the version-honoring batch-session orchestrator (agent-side, stdlib-only).

A batch that names a *specific* registry version must score THAT version, not whatever the shared
serving engine currently holds (the explicit-`registry_version`-honoring gap batch never got; 015's
SC-068 batch-vs-`@serving` scoring stays correct). On single-GPU hardware the batch drives the same
engine online `/infer` uses, so honoring the version means, in order:

  1. refuse cleanly if a non-preemptable job holds the GPU — never preempt (Principle II);
  2. hold a batch-wide EXCLUSION over the shared engine so online `/infer` never sees the batch's
     temporary version — while letting the batch's OWN rows through by a session token (the batch and
     online traffic post to the same `/engines/*` paths, so a naive global exclusion would deadlock
     the batch against itself);
  3. load/assert the target INSIDE the try, so a load/OOM failure that already disturbed the prior
     engine still hits the restore;
  4. in `finally`, RE-READ the latest desired target (a promote can land mid-batch, its reload
     deferred while the batch held the engine) and restore THAT — not the stale captured snapshot —
     then release the exclusion.

This module is the pure ORDERING. The engine-load, the exclusion gate, and the desired-pointer are
injected seams (duck-typed), so the sequence unit-tests offline with fakes
(`tests/test_batch_version_assert.py`), exactly like `gateway/app/activation.py`. The REAL seams —
the agent engine wiring that loads a specific version, the request-routing exclusion, and the
serving-pointer read/restore — are the on-hardware work (T599); they slot in behind these interfaces
without changing this sequence.
"""


class BatchRefused(Exception):
    """The batch cannot run now without violating the one-GPU-tenant rule (a job holds the slot) — a
    clean refusal, never a preemption (FR-350 / Principle II)."""


class BatchSession:
    """Sequences one version-honoring batch over the shared serving engine. Injected seams:

      admission.job_holds_gpu() -> bool     a non-preemptable job holds the slot → refuse
      exclusion.acquire()       -> token    gate online /infer; the returned token is the batch's bypass
      exclusion.release()       -> None
      engine.load(target)       -> None     make `target` the resident version (raises on load failure)
      desired.read()            -> target   the latest desired serving target (re-read on restore)
      desired.restore(target)   -> None     converge the shared engine back to `target`
    """

    def __init__(self, *, admission, exclusion, engine, desired, log=lambda *a, **k: None):
        self._admission = admission
        self._exclusion = exclusion
        self._engine = engine
        self._desired = desired
        self._log = log

    def run(self, target, score):
        """Load `target` under the exclusion, run `score(token)` against it, then restore the latest
        desired target and release — on success AND any failure. `score` receives the session token so
        the batch's own engine calls bypass the exclusion; it returns whatever `score` returns.

        Raises `BatchRefused` (before acquiring anything) if a non-preemptable job holds the GPU."""
        if self._admission.job_holds_gpu():
            raise BatchRefused(
                "a non-preemptable job holds the GPU — the batch queues/refuses, never preempts")
        prior = self._desired.read()        # generation snapshot (telemetry; the restore re-reads)
        token = self._exclusion.acquire()   # online /infer excluded; `token` is the batch's bypass
        loaded = False
        try:
            self._engine.load(target)       # INSIDE the try — a load/OOM failure still restores
            loaded = True
            return score(token)
        finally:
            # A promote can land mid-batch (its reload deferred while the batch held the engine), so
            # the desired pointer may now name a NEWER target than `prior`. Restore what's desired NOW —
            # restoring the stale `prior` would erase that promotion.
            desired = self._desired.read()
            if desired != prior:
                self._log("batch: desired target changed mid-batch "
                          f"({prior!r} -> {desired!r}); restoring the newer target", flush=True)
            self._desired.restore(desired)
            self._exclusion.release()
            if not loaded:
                self._log("batch: target load failed; desired target restored, exclusion released",
                          flush=True)
