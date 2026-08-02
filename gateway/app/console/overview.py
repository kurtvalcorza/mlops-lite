"""Overview composition — attention, activity, search (027 T701/T702/T703).

Three projections that share one property: **they are derived, never stored**. Nothing here adds a
table (research R7). Each is recomputed from whatever sources answered on this request, which is why
every function below takes its inputs as plain values with `None` meaning *that source did not
answer* — not *that source had nothing*.

That distinction is the whole design. `attention_items(jobs=None)` cannot claim "no failed runs",
because it never saw the job table; it contributes nothing and the caller marks `jobs` degraded, so
the panel renders "unknown" instead of a reassuring empty list. `attention_items(jobs=[])` genuinely
saw an empty table and genuinely contributes nothing. Same output, opposite meaning, and the
envelope's `degraded` is what tells them apart.

The functions are pure so the rules are testable without a platform: the router reads, these compose.
"""
import re

#: `AttentionItem.kind`, data-model §14. All nine are produced here — a kind that is declared but
#: never emitted is a promise the panel silently breaks.
ATTENTION_KINDS = (
    "engine-crash",
    "gpu-memory-pressure",
    "failed-training-run",
    "evaluation-gate-failure",
    "drift-significant",
    "unlabeled-backlog",
    "version-unsigned",
    "missing-artifact",
    "stale-agent-heartbeat",
)

#: The kinds the **polled** attention panel can actually produce.
#:
#: `missing-artifact` needs an object-store existence check per version and `evaluation-gate-failure`
#: needs a gate verdict, which this platform computes at promote time and never persists — neither
#: is affordable on a five-second poll over the whole registry. Both ARE computed on demand in the
#: Models area (`/console/catalog?verify_artifacts=true`, `/console/evaluations/{name}/{version}`).
#:
#: Named here rather than left as branches that quietly never fire. `attention_items()` still emits
#: all nine when a caller supplies the fields — that is what the catalog-backed path does — so the
#: vocabulary stays whole and this set records what the *polled route* can claim. Same discipline as
#: `predictions.DERIVABLE_SIGNALS`, and for the same reason: a rule that cannot fire reads exactly
#: like a rule that found nothing wrong.
POLLED_ATTENTION_KINDS = tuple(k for k in ATTENTION_KINDS
                               if k not in ("missing-artifact", "evaluation-gate-failure"))

#: Rank, not colour. The panel is ordered by this so the first row is the most consequential one,
#: independent of which source happened to answer first.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

#: PSI convention from `gateway/app/monitoring.py`: <0.1 none, 0.1–0.25 moderate, >0.25 significant.
#: Carried here as a constant rather than hard-coded at each comparison, and shipped to the console
#: in the drift payload (FR-405) so the interface never re-states it.
DRIFT_SIGNIFICANT = 0.25
DRIFT_MODERATE = 0.10

#: Unlabeled captures below this are a normal working backlog, not something to raise.
UNLABELED_BACKLOG = 50

#: An agent heartbeat older than this is reported as stale. Deliberately several multiples of the
#: console's own poll cadence — a heartbeat one poll old is not evidence of anything.
STALE_HEARTBEAT_S = 120.0

#: Engine states that mean the engine died rather than simply not being loaded. `cold` is absent on
#: purpose: an engine that has never loaded is the platform working as designed.
CRASHED_ENGINE_STATES = {"wedged", "error", "crashed", "failed"}


def _item(kind, severity, subject, detail, href, observed_at):
    return {"id": f"{kind}:{subject}", "kind": kind, "severity": severity, "subject": subject,
            "detail": detail, "href": href, "observedAt": observed_at}


def attention_items(*, now, agent=None, admission=None, jobs=None, drift=None, versions=None,
                    unlabeled=None, heartbeat_age_s=None):
    """Severity-ranked `AttentionItem[]` (FR-373).

    Every argument defaults to `None` = *source did not answer*, and a `None` source contributes
    nothing. The caller is responsible for naming it in `degraded`; this function will not invent an
    "all clear" on its behalf.

    `now` is the observation stamp every item carries. Passed in rather than read so a test can pin
    it, and so all items from one request share one stamp instead of drifting by microseconds.
    """
    items = []

    # -- engine-crash / stale-agent-heartbeat: the agent's own report ------------------------------
    if agent is not None:
        for engine_id, state in (agent.get("engines") or {}).items():
            if str(state).lower() in CRASHED_ENGINE_STATES:
                items.append(_item("engine-crash", "critical", engine_id,
                                   f"engine reports state {state!r}", "/runtime", now))
        if agent.get("wedged"):
            items.append(_item("engine-crash", "critical", "host",
                               "the agent reports a wedged engine", "/runtime", now))
        interrupted = agent.get("interrupted_since_start") or 0
        if interrupted:
            items.append(_item("failed-training-run", "warning", "restart",
                               f"{interrupted} job(s) were interrupted by an agent restart",
                               "/runtime", now))

    if heartbeat_age_s is not None and heartbeat_age_s > STALE_HEARTBEAT_S:
        # `warning`, not `critical`. Agent loss degrades: the CPU modalities keep serving, and
        # calling it critical would overstate an outage the operator can still work through
        # (data-model §2).
        items.append(_item("stale-agent-heartbeat", "warning", "agent",
                           f"last heartbeat {int(heartbeat_age_s)}s ago", "/runtime", now))

    # -- gpu-memory-pressure: refusals, from admission's own reasoning -----------------------------
    if admission is not None:
        for record in admission.get("records") or []:
            if record.get("decision") != "refused":
                continue
            # The server-composed explanation is reused verbatim (FR-378). Re-wording it here would
            # produce a second account of admission's reasoning that drifts from the real one.
            items.append(_item("gpu-memory-pressure", "warning",
                               record.get("model_key") or record.get("request_id") or "request",
                               record.get("explanation") or record.get("reason") or "refused",
                               "/runtime", record.get("at") or now))

    # -- failed-training-run -----------------------------------------------------------------------
    for job in jobs or []:
        if job.get("state") in ("failed", "interrupted"):
            items.append(_item("failed-training-run", "warning", job.get("job_id") or "job",
                               f"{job.get('kind') or 'job'} {job.get('state')}",
                               f"/training/jobs/{job.get('job_id')}", job.get("ended_at") or now))

    # -- drift-significant -------------------------------------------------------------------------
    for report in drift or []:
        score = report.get("max_psi")
        if score is None or score < DRIFT_MODERATE:
            continue
        significant = score >= DRIFT_SIGNIFICANT
        items.append(_item(
            "drift-significant", "warning" if significant else "info",
            report.get("model_name") or report.get("id") or "model",
            f"max PSI {score:.3f} ({'significant' if significant else 'moderate'}, "
            f"threshold {DRIFT_SIGNIFICANT})",
            "/evaluations/drift", report.get("created_at") or now))

    # -- version-unsigned / missing-artifact / evaluation-gate-failure -----------------------------
    for version in versions or []:
        subject = f"{version.get('name')}:{version.get('version')}"
        tags = version.get("tags") or {}
        if not version.get("signed", tags.get("signature")):
            items.append(_item("version-unsigned", "info", subject,
                               "no signature recorded for this version",
                               f"/models/{version.get('name')}/{version.get('version')}", now))
        if version.get("artifactPresent") is False:
            # `False`, explicitly — `None` means the object store was not reachable to check, and an
            # unchecked artifact is not a missing one.
            items.append(_item("missing-artifact", "critical", subject,
                               "the registry points at an object the store does not have",
                               f"/models/{version.get('name')}/{version.get('version')}", now))
        gate = version.get("gate") or {}
        if gate.get("verdict") in ("fail", "failed", "blocked"):
            items.append(_item("evaluation-gate-failure", "warning", subject,
                               gate.get("reason") or "the promotion gate failed",
                               "/evaluations/gates", now))

    # -- unlabeled-backlog --------------------------------------------------------------------------
    if unlabeled is not None and unlabeled > UNLABELED_BACKLOG:
        items.append(_item("unlabeled-backlog", "info", "review queue",
                           f"{unlabeled} captured predictions have no label",
                           "/inference/review-queue", now))

    items.sort(key=lambda i: (SEVERITY_RANK.get(i["severity"], 9), str(i["observedAt"])))
    return items


# -- summary cards -------------------------------------------------------------------------------

#: The eight Overview cards (FR-371), in the order the spec lists them.
#:
#: One deviation, and it is deliberate. FR-371 names a **pending admissions** card; there is no such
#: number. 026 established that admission is a synchronous decision, not a queue (research R1) —
#: `hostagent/admission.py` has no pending state and the decision ring has no queue-position field.
#: A "pending admissions" card would therefore read `0` forever, and a permanent zero is not a
#: harmless placeholder: it teaches an operator that requests never wait, which is the opposite of
#: what a refusal means. The slot instead reports the **recent admission decisions** the ring
#: actually holds, split into admitted and refused.
CARDS = ("activeEndpoints", "runningJobs", "gpuUtilization", "admissionDecisions", "failedJobs",
         "modelsRequiringReview", "unlabeledCaptures", "driftWarnings")


def summary_cards(*, agent=None, devices=None, admission=None, jobs=None, versions=None,
                  unlabeled=None, drift=None):
    """The eight card values, each `None` when its source did not answer.

    `None` here reaches the interface as the word "unknown". That is the entire point of computing
    these server-side: a card that falls back to `0` in the client is indistinguishable from a
    genuine zero, and "0 running jobs" during an agent outage is the specific falsehood SC-195
    exists to catch.
    """
    def count(rows, predicate):
        return None if rows is None else sum(1 for row in rows if predicate(row))

    utilization = None
    if devices:
        readings = [d.get("utilization_pct") for d in devices if d.get("utilization_pct") is not None]
        utilization = max(readings) if readings else None

    decisions = None
    if admission is not None:
        records = admission.get("records") or []
        decisions = {"admitted": sum(1 for r in records if r.get("decision") == "admitted"),
                     "refused": sum(1 for r in records if r.get("decision") == "refused")}

    return {
        "activeEndpoints": count(versions, lambda v: bool(v.get("serving"))),
        # The agent's own count is preferred over the job table's: the agent is the thing actually
        # running them, and during a store outage it is still right.
        "runningJobs": (agent or {}).get("jobs_active") if agent is not None else (
            count(jobs, lambda j: j.get("state") in ("running", "queued"))),
        "gpuUtilization": utilization,
        "admissionDecisions": decisions,
        "failedJobs": count(jobs, lambda j: j.get("state") in ("failed", "interrupted")),
        "modelsRequiringReview": count(
            versions,
            lambda v: v.get("artifactPresent") is False or not (v.get("tags") or {}).get("signature")),
        "unlabeledCaptures": unlabeled,
        "driftWarnings": count(drift, lambda r: (r.get("max_psi") or 0) >= DRIFT_MODERATE),
    }


# -- activity ----------------------------------------------------------------------------------------

#: The 021 loop, kept as a **visualization** now that it is no longer navigation (FR-363). The stages
#: still describe how work moves through the platform; they just no longer dictate where an operator
#: clicks.
ACTIVITY_STAGES = ("data", "train", "evaluate", "deploy", "infer", "monitor")


def _event(at, stage, kind, subject, detail, href):
    return {"at": at, "stage": stage, "kind": kind, "subject": subject, "detail": detail,
            "href": href}


def activity_events(*, jobs=None, versions=None, drift=None, activations=None, limit=50):
    """One normalized timeline across the lifecycle (FR-363).

    Normalized means every row has the same six fields regardless of which system recorded it — a
    timeline where a training job and a promotion carry different shapes forces the interface to
    special-case each source, and every new source then means a new special case.
    """
    events = []

    for job in jobs or []:
        stage = "train" if (job.get("kind") or "").startswith(("train", "finetune", "hpo")) else "infer"
        if job.get("submitted_at"):
            events.append(_event(job["submitted_at"], stage, "job-submitted",
                                 job.get("job_id"), job.get("kind") or "job",
                                 f"/training/jobs/{job.get('job_id')}"))
        if job.get("ended_at"):
            events.append(_event(job["ended_at"], stage, f"job-{job.get('state') or 'ended'}",
                                 job.get("job_id"), job.get("kind") or "job",
                                 f"/training/jobs/{job.get('job_id')}"))

    for version in versions or []:
        subject = f"{version.get('name')}:{version.get('version')}"
        if version.get("created_at"):
            events.append(_event(version["created_at"], "train", "version-registered", subject,
                                 version.get("modality") or "", f"/models/{version.get('name')}"))
        if version.get("serving"):
            events.append(_event(version.get("promoted_at") or version.get("created_at"),
                                 "deploy", "version-promoted", subject, "serving alias",
                                 "/deployments"))

    for report in drift or []:
        events.append(_event(report.get("created_at"), "monitor", "drift-report",
                             report.get("model_name") or report.get("id") or "model",
                             f"max PSI {report.get('max_psi')}", "/evaluations/drift"))

    for activation in activations or []:
        events.append(_event(activation.get("updated_at") or activation.get("created_at"),
                             "deploy", f"activation-{activation.get('state') or 'unknown'}",
                             activation.get("model_name") or activation.get("operation_id"),
                             activation.get("state") or "", "/deployments"))

    # Undated events are dropped rather than placed at an arbitrary end of the timeline: an event
    # rendered at the wrong time is worse than one not rendered, because the reader cannot tell.
    events = [e for e in events if e["at"] is not None]
    events.sort(key=lambda e: str(e["at"]), reverse=True)
    return events[:limit]


# -- search -------------------------------------------------------------------------------------------

#: The six kinds the resolver spans (FR-368). An operator who has an id from a log line should be
#: able to paste it here without first deciding what kind of thing it is.
SEARCH_KINDS = ("model", "run", "dataset", "job", "endpoint", "prediction")


def search_results(query, *, models=None, runs=None, datasets=None, jobs=None, endpoints=None,
                   predictions=None, limit=20):
    """Composed resolver across the six kinds (FR-368).

    Substring, case-insensitive, over id and name. Not fuzzy on purpose: an operator pasting a job
    id from a log wants that job, and a resolver that helpfully returns three near-matches makes
    them verify which one they got.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []

    def scan(kind, rows, id_key, name_key, href):
        out = []
        for row in rows or []:
            identifier = str(row.get(id_key) or "")
            name = str(row.get(name_key) or "") if name_key else ""
            if needle in identifier.lower() or (name and needle in name.lower()):
                out.append({"kind": kind, "id": identifier, "label": name or identifier,
                            "href": href(row),
                            # An exact id match is what someone pasting an id wanted; it sorts first.
                            "exact": identifier.lower() == needle})
        return out

    results = []
    results += scan("model", models, "name", "name", lambda r: f"/models/{r.get('name')}")
    results += scan("run", runs, "run_id", "name", lambda r: f"/training/runs/{r.get('run_id')}")
    results += scan("dataset", datasets, "name", "name", lambda r: f"/datasets/{r.get('name')}")
    results += scan("job", jobs, "job_id", "kind", lambda r: f"/training/jobs/{r.get('job_id')}")
    results += scan("endpoint", endpoints, "id", "name", lambda r: f"/deployments/{r.get('id')}")
    results += scan("prediction", predictions, "prediction_id", None,
                    lambda r: f"/inference/predictions/{r.get('prediction_id')}")

    results.sort(key=lambda r: (not r["exact"], SEARCH_KINDS.index(r["kind"]), r["label"]))
    return results[:limit]


def looks_like_id(value: str) -> bool:
    """Whether a query looks like an opaque identifier rather than a name fragment.

    Used to decide whether to search the prediction table at all — predictions are the largest
    table on the platform and a two-letter name fragment scanning it is how a console knocks over
    its own database (contract cross-cutting rule 3).
    """
    return bool(re.fullmatch(r"[0-9a-fA-F-]{8,}|[A-Za-z0-9_-]{12,}", value or ""))
