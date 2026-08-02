"""Job normalization and the three-way join (027 T726/T730/T731 — data-model §3).

One unit of work carries three identifiers on this platform: a broker job id (the gateway's lane), a
host-agent job id (the thing actually executing), and a tracking run id (where the metrics land). An
operator investigating a stuck fine-tune currently has to hold all three in their head and query
three systems. This module is the join that makes it one row.

Two rules are load-bearing:

  * **Every native state is preserved alongside the normalized one** (FR-392). The normalization is
    for scanning a list; the native strings are for debugging, and a projection that discarded them
    would force the operator back to the three systems the join exists to replace.

  * **`Unknown` is never inferred as stopped.** A source that did not answer leaves the job at its
    last known state marked `Unknown` — deciding that an unreachable agent means a finished job is
    the single most damaging inference this layer could make, because it reads as "the work is done"
    when the truth is "we cannot see the work".

`Orphaned` is the one state derived rather than read: the gateway says running and the agent has no
such process. It always carries a `StateConflict` and never stands alone (data-model §3).
"""

#: `JobState`, data-model §3.
JOB_STATES = ("Draft", "Queued", "AdmissionCheck", "Admitted", "Starting", "Running", "Completing",
              "Succeeded", "Failed", "Cancelled", "Rejected", "Orphaned", "Unknown")

#: Terminal states — a job here will not change again, so a conflict about it is a record-keeping
#: disagreement rather than something in flight.
TERMINAL = {"Succeeded", "Failed", "Cancelled", "Rejected"}

#: Observations further apart than this are not compared (data-model §4). A stale read disagreeing
#: with a fresh one is not evidence of inconsistency, and reporting it as one trains operators to
#: ignore the banner — which costs the real conflicts their audience.
SKEW_THRESHOLD_S = 30.0


def normalize(*, gateway=None, agent=None, tracking=None):
    """The normalization table, as a function. Returns one `JobState`.

    Arguments are the three native states, each `None` for "that source has no record" — which is
    different from `"unreachable"`, passed as the literal string when the source could not be read.
    The table's last row is that case: keep the last known state and mark `Unknown`.
    """
    if agent == "unreachable" or gateway == "unreachable":
        return "Unknown"

    if agent in ("failed", "error"):
        return "Failed"
    if agent == "cancelled" or gateway == "cancelled" or tracking == "KILLED":
        return "Cancelled"
    if agent == "refused":
        return "Rejected"
    if agent == "interrupted":
        # An agent restart killed it. `Failed` rather than `Cancelled`: nobody asked for it to stop.
        return "Failed"

    if gateway in (None, "draft"):
        if agent is None and tracking is None:
            return "Draft" if gateway == "draft" else "Unknown"

    if gateway == "queued" or (gateway == "accepted" and agent is None):
        return "Queued"

    if gateway in ("dispatched", "running"):
        if agent is None:
            # The gateway says this is out for execution and the agent has no record of it. Derived,
            # and always accompanied by a conflict — see `detect_conflict`.
            return "Orphaned" if gateway == "running" else "AdmissionCheck"
        if agent in ("admission", "pending"):
            return "AdmissionCheck"
        if agent == "admitted":
            return "Admitted"
        if agent in ("starting", "queued"):
            return "Starting"
        if agent == "running":
            return "Completing" if tracking in ("FINISHED", "FAILED") else "Running"
        if agent in ("done", "succeeded", "finished"):
            return "Completing" if tracking == "RUNNING" else "Succeeded"

    if gateway in ("done", "succeeded") and agent in ("done", "succeeded", "finished", None):
        return "Succeeded"
    if gateway == "failed":
        return "Failed"

    if agent in ("done", "succeeded", "finished"):
        return "Succeeded"
    if agent == "running":
        return "Running"

    return "Unknown"


def detect_conflict(*, entity, entity_id, sources, now=None, skew_threshold_s=SKEW_THRESHOLD_S):
    """A `StateConflict` when two sources disagree — or `None`.

    `sources` is a list of `{source, state, observedAt, observedEpoch}`. Only observations taken
    within the skew threshold of each other are compared; beyond it the conflict claim is
    **suppressed** and `skewExceeded` is set, because the disagreement is then explained by the
    clock rather than by the platform.
    """
    known = [s for s in sources if s.get("state") not in (None, "unreachable")]
    if len(known) < 2:
        return None

    epochs = [s.get("observedEpoch") for s in known if s.get("observedEpoch") is not None]
    skew_exceeded = bool(epochs) and (max(epochs) - min(epochs)) > skew_threshold_s

    states = {_comparable(s["state"]) for s in known}
    if len(states) < 2:
        return None
    if skew_exceeded:
        return {"entity": entity, "entityId": entity_id, "sources": known,
                "skewExceeded": True, "suggestedAction": "refresh",
                # No conflict is CLAIMED — the readings are too far apart to compare. The field is
                # reported so the interface can say why it is not showing a banner.
                "conflict": False}

    return {"entity": entity, "entityId": entity_id, "sources": known, "skewExceeded": False,
            "suggestedAction": "inspect-journal", "conflict": True,
            "lastConsistentAt": None}


#: Native strings that mean the same thing across the three systems. Normalizing before comparison
#: keeps "the agent says `done`, tracking says `FINISHED`" from being reported as a disagreement —
#: which would bury the real ones under vocabulary noise.
_EQUIVALENT = {
    "done": "finished", "succeeded": "finished", "finished": "finished",
    "failed": "failed", "error": "failed", "killed": "cancelled", "cancelled": "cancelled",
    "running": "running", "queued": "queued", "pending": "queued",
}


def _comparable(state):
    return _EQUIVALENT.get(str(state).lower(), str(state).lower())


def platform_job(*, gateway_job=None, agent_job=None, tracking_run=None, observed=None):
    """One `PlatformJob` row from up to three records (FR-391).

    `observed` maps source name -> `(iso, epoch)`, so the conflict detector can apply the skew rule
    against the times these particular readings were taken rather than against "now".
    """
    observed = observed or {}
    gateway_state = (gateway_job or {}).get("state")
    agent_state = (agent_job or {}).get("state")
    tracking_state = (tracking_run or {}).get("status")

    identifier = ((gateway_job or {}).get("job_id") or (agent_job or {}).get("job_id")
                  or (tracking_run or {}).get("run_id") or "unknown")
    state = normalize(gateway=gateway_state, agent=agent_state, tracking=tracking_state)

    conflict = None
    if state == "Orphaned":
        # Never stands alone (data-model §3). The disagreement IS the finding: the gateway believes
        # work is in flight that the agent has no process for.
        conflict = {"entity": "job", "entityId": identifier, "conflict": True,
                    "skewExceeded": False, "suggestedAction": "inspect-journal",
                    "sources": [{"source": "gateway", "state": gateway_state,
                                 "observedAt": observed.get("gateway", (None, None))[0]},
                                {"source": "agent", "state": "no record",
                                 "observedAt": observed.get("agent", (None, None))[0]}]}
    else:
        conflict = detect_conflict(
            entity="job", entity_id=identifier,
            sources=[{"source": name, "state": value,
                      "observedAt": observed.get(name, (None, None))[0],
                      "observedEpoch": observed.get(name, (None, None))[1]}
                     for name, value in (("gateway", gateway_state), ("agent", agent_state),
                                         ("tracking", tracking_state))])

    source = agent_job or gateway_job or {}
    return {
        "id": identifier,
        "type": _job_type(source.get("kind")),
        "normalizedState": state,
        # All three natives preserved (FR-392). The normalization is for scanning; these are for
        # debugging, and dropping them would send the operator back to the three systems the join
        # exists to replace.
        "gatewayState": gateway_state,
        "agentState": agent_state,
        "trackingRunState": tracking_state,
        "runId": source.get("run_id") or (tracking_run or {}).get("run_id"),
        "studyId": source.get("study_id"),
        # `model`, not `model_name`: the completed-job record's key is written by
        # `training/run_flow.py` (`out["model"]`), journaled verbatim, and spread back to the top
        # level by `_job_row_to_record`. Nothing on this path ever emits `model_name`.
        "modelId": source.get("model"),
        "assignedHost": source.get("host") or ("local" if agent_job else None),
        "assignedDevice": source.get("device_index"),
        "admissionReason": source.get("reason") if state == "Rejected" else None,
        "createdAt": source.get("submitted_at"),
        "startedAt": source.get("started_at"),
        "completedAt": source.get("ended_at"),
        "observed": {f"{name}At": value[0] for name, value in observed.items()},
        "conflict": conflict,
    }


#: `PlatformJob.type`, data-model §3. Derived from the agent's `kind` rather than declared, because
#: `kind` is what the job was actually dispatched as.
_TYPES = {"finetune": "training", "train": "training", "training": "training",
          "hpo": "hpo", "study": "hpo", "batch": "batch", "shadow": "shadow",
          "evaluate": "evaluation", "evaluation": "evaluation"}


def _job_type(kind):
    return _TYPES.get(str(kind or "").lower(), "inference")


def join(*, gateway_jobs=None, agent_jobs=None, tracking_runs=None, observed=None):
    """The unified active-work list (FR-372): one normalized row shape across three sources.

    A job present in only one source still produces a row. Dropping it would make the list silently
    incomplete in exactly the situation an operator is investigating — an id that appears on one
    side and not the other is a finding, not a reason to hide it.
    """
    by_id = {}
    for job in gateway_jobs or []:
        by_id.setdefault(job.get("job_id"), {})["gateway"] = job
    for job in agent_jobs or []:
        by_id.setdefault(job.get("job_id"), {})["agent"] = job
    for run in tracking_runs or []:
        key = run.get("job_id") or run.get("run_id")
        by_id.setdefault(key, {})["tracking"] = run

    rows = [platform_job(gateway_job=parts.get("gateway"), agent_job=parts.get("agent"),
                         tracking_run=parts.get("tracking"), observed=observed)
            for parts in by_id.values()]
    rows.sort(key=lambda r: (r["normalizedState"] in TERMINAL, -(r["createdAt"] or 0)))
    return rows
