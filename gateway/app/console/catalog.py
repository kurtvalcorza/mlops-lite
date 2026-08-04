"""The model catalog and the compatibility verdict (027 T721/T722 — data-model §5).

Two things live here, and the second is the one that matters.

**The catalog** joins five systems into one row: the registry (name, version, aliases, tags), the
object store (does the artifact actually exist), the tracking server (the source run), the serving
pointer (what is deployed), and the evaluation record. A side that is missing is **marked absent,
never dropped** — a registry version with no artifact is precisely the row an operator is looking
for, and a join that filtered it out would produce a list that looks complete and is not.

**The compatibility verdict** answers "can this run here right now", and its whole value is in a
three-way distinction the obvious implementation collapses:

  * `incompatible` — **structural**. The engine is not available, the compute capability does not
    match, the artifact is missing, an adapter's base cannot be resolved, or the model does not fit
    on an *empty* GPU. Waiting will not help; eviction will not help.
  * `not-currently-eligible` — **transient**. A VRAM check failed while the model still fits alone,
    or a job holds the GPU. Eviction, an idle release, or a job finishing will help.
  * `unknown` — the agent is unreachable, or a check could not be evaluated. **Never** collapsed
    into either of the others: an unreachable agent is not a compatibility fact, and reporting it as
    `incompatible` would send an operator to rebuild a model that was fine.

The two VRAM checks are reported **separately** and each carries its **own** reservation term. This
is constitution v1.6.1, and getting it wrong is not cosmetic: telling an operator "not enough VRAM"
when the real constraint is the accounted budget sends them to the wrong remedy. Eviction fixes a
budget failure; a live-VRAM failure with headroom exhausted usually means a leaked or unaccounted
allocation, which eviction will not touch.
"""

#: `PlatformModel.modality`, data-model §5. `unknown` is a member so an unrecognized or missing task
#: tag renders as unknown rather than being filtered out of the catalog (FR-385) — a model the
#: console cannot classify is still a model the operator has.
MODALITIES = ("text-generation", "image-classification", "embedding", "asr", "tabular", "unknown")

#: The registry tag carrying the modality. Named here rather than imported so this module does not
#: pull the whole evaluation stack in to read one string.
TASK_TAG = "task"
ENGINE_TAG = "serving_engine"
BASE_MODEL_TAG = "base_model"

#: `PlatformModel.evaluationState`.
EVALUATION_STATES = ("passed", "failed", "warning", "not-evaluated", "incomplete")


def modality_of(tags) -> str:
    task = (tags or {}).get(TASK_TAG)
    return task if task in MODALITIES else "unknown"


def evaluation_state(*, evaluation=None, is_serving=False) -> str:
    """FR-402. A version with no logged metric that is **not** the serving version is
    `not-evaluated` — the platform's documented refusal to score it, never an error.

    The serving version is the exception: it should have been scored on the way to promotion, so an
    unscored one is `incomplete` rather than merely unscored. That difference is the whole point —
    "we chose not to evaluate this" and "this got promoted without evidence" are different findings.
    """
    if evaluation is None:
        return "incomplete" if is_serving else "not-evaluated"
    verdict = str(evaluation.get("verdict") or "").lower()
    if verdict in ("pass", "passed", "promote"):
        return "passed"
    if verdict in ("fail", "failed", "block", "blocked"):
        return "failed"
    if verdict in ("warn", "warning", "flagged"):
        return "warning"
    return "incomplete" if evaluation.get("metric") is None else "passed"


def platform_model(version, *, artifact_present=None, evaluation=None, deployment_ids=None,
                   serving_version=None):
    """One catalog row (FR-383/384).

    `artifact_present` is `None` when the object store could not be checked. It is **not** defaulted
    to `True`: assuming presence from a registry URI is how a console shows a download that 404s.
    """
    tags = dict(version.get("tags") or {})
    name = version.get("name")
    number = str(version.get("version"))
    is_serving = bool(version.get("serving")) or (
        serving_version is not None and str(serving_version) == number)

    base = tags.get(BASE_MODEL_TAG)
    return {
        "id": f"{name}:{number}",
        "name": name,
        "version": number,
        "modality": modality_of(tags),
        "registeredModelName": name,
        "aliases": ["serving"] if is_serving else [],
        "sourceRunId": version.get("run_id"),
        "artifactUri": version.get("source"),
        "artifactDigest": tags.get("artifact_digest"),
        "artifactSizeBytes": version.get("artifact_size_bytes"),
        # Tri-state on purpose: True / False / None-means-unchecked.
        "artifactPresent": artifact_present,
        "evaluationState": evaluation_state(evaluation=evaluation, is_serving=is_serving),
        "deploymentIds": list(deployment_ids or ([f"{name}@serving"] if is_serving else [])),
        "lineage": {
            "baseModel": base,
            # An adapter whose base cannot be resolved is structurally unservable — the platform
            # already refuses to promote it (FR-389), and the catalog says so rather than letting
            # the operator discover it at promotion time.
            "baseResolvable": base is None or bool(tags.get(ENGINE_TAG)),
            "parentRunId": version.get("run_id"),
        },
        "tags": tags,
        "serving": is_serving,
    }


# -- compatibility ---------------------------------------------------------------------------------

def _check(passes):
    """`pass` / `fail` / `unknown`. `None` in means the inputs were not all readable."""
    return "unknown" if passes is None else ("pass" if passes else "fail")


def compatibility(*, estimated_gb=None, admission=None, engines=None, required_engine=None,
                  accelerator_required=True, artifact_available=None, host_compatible=None,
                  required_compute_capability=None, base_resolvable=True):
    """`RuntimeCompatibility` computed **at request time** against live topology.

    Not cached beyond the device snapshot's own TTL, because it is a statement about *now*: a cached
    "eligible" outlives the free VRAM that made it true.

    `admission` is the agent's `/runtime/admission` view, or `None` when the agent is unreachable —
    which produces `unknown` for both checks and therefore an `unknown` verdict.
    """
    reasons = []

    budget = admission.get("usable_budget_gb") if admission else None
    accounted = admission.get("accounted_resident_gb") if admission else None
    reserved = admission.get("reserved_gb") if admission else None
    unmaterialized = admission.get("unmaterialized_gb") if admission else None
    live_free = admission.get("live_free_gb") if admission else None
    headroom = admission.get("headroom_gb") if admission else None
    job_exclusive = bool(admission.get("job_barrier") or admission.get("active_job")) if admission \
        else False

    # Check 1 — the accounted budget. Counts EVERY outstanding reservation.
    budget_ok = None
    if None not in (estimated_gb, budget, accounted, reserved):
        budget_ok = estimated_gb + accounted + reserved <= budget
        if not budget_ok:
            reasons.append(
                f"budget: {estimated_gb:.1f} GB + {accounted:.1f} GB accounted + {reserved:.1f} GB "
                f"reserved exceeds the {budget:.1f} GB usable budget")

    # Check 2 — measured free VRAM. Deducts only the NOT-YET-materialized reservations: one already
    # reconciled to a real delta is by then visible in `live_free_gb` itself, and subtracting it
    # twice would report a model as not fitting when admission would accept it.
    live_ok = None
    if None not in (estimated_gb, live_free, unmaterialized, headroom):
        live_ok = estimated_gb + headroom <= live_free - unmaterialized
        if not live_ok:
            reasons.append(
                f"live-vram: {estimated_gb:.1f} GB + {headroom:.1f} GB headroom exceeds the "
                f"{live_free:.1f} GB measured free less {unmaterialized:.1f} GB unmaterialized")

    # Fits alone — the structural question. No amount of eviction or waiting changes this.
    fits_alone = None
    if None not in (estimated_gb, budget):
        fits_alone = estimated_gb <= budget
        if not fits_alone:
            reasons.append(
                f"cannot-fit-alone: {estimated_gb:.1f} GB exceeds the {budget:.1f} GB usable budget "
                "on an empty GPU")

    engine_available = None
    if engines is not None and required_engine:
        engine_available = any(e.get("engine_id") == required_engine for e in engines)
        if not engine_available:
            reasons.append(f"engine {required_engine!r} is not available on this host")

    if artifact_available is False:
        reasons.append("the artifact is not present in the object store")
    if host_compatible is False:
        reasons.append(f"compute capability {required_compute_capability} is not met by this host")
    if not base_resolvable:
        reasons.append("the adapter's base model cannot be resolved")
    if job_exclusive:
        reasons.append("a job holds the GPU exclusively. A running job is never preempted.")

    verdict = _verdict(budget_ok=budget_ok, live_ok=live_ok, fits_alone=fits_alone,
                       engine_available=engine_available, artifact_available=artifact_available,
                       host_compatible=host_compatible, base_resolvable=base_resolvable,
                       job_exclusive=job_exclusive, agent_reachable=admission is not None)

    return {
        "requiredEngine": required_engine,
        "acceleratorRequired": accelerator_required,
        "requiredComputeCapability": required_compute_capability,
        "artifactAvailable": artifact_available,
        "hostCompatible": host_compatible,
        "estimatedVramGb": estimated_gb,
        "usableBudgetGb": budget,
        "accountedResidentGb": accounted,
        "reservedGb": reserved,
        "unmaterializedGb": unmaterialized,
        "liveFreeVramGb": live_free,
        "headroomGb": headroom,
        # Reported separately, never merged into one number — the two failures have opposite
        # remedies, and one number cannot say which one applies.
        "budgetCheck": _check(budget_ok),
        "liveVramCheck": _check(live_ok),
        "fitsAlone": fits_alone,
        "jobExclusive": job_exclusive,
        "verdict": verdict,
        "reasons": reasons,
    }


def _verdict(*, budget_ok, live_ok, fits_alone, engine_available, artifact_available,
             host_compatible, base_resolvable, job_exclusive, agent_reachable):
    """The three-way distinction (FR-388). Structural first, then transient, then unknown."""
    # Structural. None of these are fixed by waiting.
    if (engine_available is False or artifact_available is False or host_compatible is False
            or not base_resolvable or fits_alone is False):
        return "incompatible"

    # Unknown before transient: an unreachable agent has not told us a check failed, and calling
    # that "not currently eligible" would state a fact nobody observed.
    if not agent_reachable or budget_ok is None or live_ok is None:
        return "unknown"

    if job_exclusive or budget_ok is False or live_ok is False:
        # Transient — and only reachable here with `fits_alone` true, so eviction, an idle release,
        # or the job finishing genuinely can change the answer.
        return "not-currently-eligible"

    return "eligible"
