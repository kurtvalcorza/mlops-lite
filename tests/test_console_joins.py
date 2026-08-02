"""027 — the console's multi-source joins (T720 catalog half, T725 job half).

Two joins, one property: **a missing side is marked absent, never dropped**. A join that silently
omits rows whose other side is missing produces a list that looks complete and is not, and the
omitted rows are precisely the interesting ones — a registry version with no artifact, a gateway job
the agent has no process for. Those are findings, not noise to filter out.

Offline and web-free: both joins are pure functions over plain dicts, which is what lets every row of
the data-model's normalization table be pinned without a platform behind it.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tests import _gwimport  # noqa: E402

with _gwimport.isolated_metrics():
    from gateway.app.console import jobs as jobs_mod  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    yield from _gwimport.isolate_module_metrics()


# -- T725: the normalization table (data-model §3, FR-392 / SC-190) -------------------------------
#
# Every row of the table, verbatim, in the table's order. SC-190 requires BOTH halves: total mapping
# coverage, and each source's native state remaining inspectable alongside the normalized one.

TABLE = [
    # (gateway, agent, tracking, expected)
    ("accepted", None, None, "Queued"),
    ("dispatched", "admission", None, "AdmissionCheck"),
    ("dispatched", "refused", None, "Rejected"),
    ("dispatched", "admitted", None, "Admitted"),
    ("dispatched", "starting", None, "Starting"),
    ("running", "running", "RUNNING", "Running"),
    ("running", "finished", "RUNNING", "Completing"),
    ("done", "done", "FINISHED", "Succeeded"),
    ("failed", "failed", "FAILED", "Failed"),
    ("cancelled", "cancelled", "KILLED", "Cancelled"),
    ("running", None, "RUNNING", "Orphaned"),
    ("running", "unreachable", "RUNNING", "Unknown"),
]


@pytest.mark.parametrize("gateway,agent,tracking,expected", TABLE)
def test_every_row_of_the_normalization_table_maps_to_exactly_one_state(gateway, agent, tracking,
                                                                       expected):
    assert jobs_mod.normalize(gateway=gateway, agent=agent, tracking=tracking) == expected


def test_every_normalized_state_is_one_the_data_model_declares():
    for gateway, agent, tracking, _ in TABLE:
        assert jobs_mod.normalize(gateway=gateway, agent=agent,
                                  tracking=tracking) in jobs_mod.JOB_STATES


def test_an_unreachable_source_is_never_inferred_as_stopped():
    """The single most damaging inference this layer could make. 'We cannot see the work' rendered
    as 'the work is done' is a false all-clear during exactly the outage an operator is chasing."""
    state = jobs_mod.normalize(gateway="running", agent="unreachable", tracking="RUNNING")
    assert state == "Unknown"
    assert state not in ("Succeeded", "Failed", "Cancelled")


def test_the_native_states_survive_normalization():
    """The normalization is for scanning a list; the natives are for debugging. Dropping them sends
    the operator back to the three systems the join exists to replace."""
    row = jobs_mod.platform_job(
        gateway_job={"job_id": "j-1", "state": "running", "kind": "finetune"},
        agent_job={"job_id": "j-1", "state": "running", "kind": "finetune"},
        tracking_run={"run_id": "r-1", "status": "RUNNING"})
    assert row["normalizedState"] == "Running"
    assert row["gatewayState"] == "running"
    assert row["agentState"] == "running"
    assert row["trackingRunState"] == "RUNNING"


def test_orphaned_always_carries_a_conflict_and_never_stands_alone():
    """data-model §3: `Orphaned` is the one state the console DERIVES, and the disagreement IS the
    finding — the gateway believes work is in flight the agent has no process for."""
    row = jobs_mod.platform_job(gateway_job={"job_id": "j-1", "state": "running"}, agent_job=None)
    assert row["normalizedState"] == "Orphaned"
    assert row["conflict"] is not None and row["conflict"]["conflict"] is True
    assert {s["source"] for s in row["conflict"]["sources"]} == {"gateway", "agent"}


def test_a_rejected_job_carries_the_admission_reason():
    row = jobs_mod.platform_job(
        gateway_job={"job_id": "j-1", "state": "dispatched"},
        agent_job={"job_id": "j-1", "state": "refused", "reason": "cannot-fit-alone"})
    assert row["normalizedState"] == "Rejected" and row["admissionReason"] == "cannot-fit-alone"


# -- T730/T735: conflict detection ----------------------------------------------------------------

def _source(name, state, epoch):
    return {"source": name, "state": state, "observedAt": "2026-07-31T09:00:00Z",
            "observedEpoch": epoch}


def test_a_disagreement_within_the_skew_threshold_is_reported():
    conflict = jobs_mod.detect_conflict(
        entity="job", entity_id="j-1",
        sources=[_source("gateway", "running", 1000.0), _source("agent", "failed", 1001.0)])
    assert conflict["conflict"] is True and conflict["skewExceeded"] is False
    assert conflict["suggestedAction"] == "inspect-journal"


def test_a_disagreement_beyond_the_skew_threshold_suppresses_the_claim():
    """data-model §4: a stale read disagreeing with a fresh one is not evidence of inconsistency,
    and reporting it as one trains operators to ignore the banner — which costs the real conflicts
    their audience."""
    conflict = jobs_mod.detect_conflict(
        entity="job", entity_id="j-1",
        sources=[_source("gateway", "running", 1000.0),
                 _source("agent", "failed", 1000.0 + jobs_mod.SKEW_THRESHOLD_S + 1)])
    assert conflict["skewExceeded"] is True
    assert conflict["conflict"] is False, "the claim is suppressed, not merely annotated"


def test_vocabulary_differences_are_not_reported_as_disagreements():
    """The agent says `done`, tracking says `FINISHED`. Reporting that as a conflict would bury the
    real ones under vocabulary noise."""
    assert jobs_mod.detect_conflict(
        entity="job", entity_id="j-1",
        sources=[_source("agent", "done", 1000.0), _source("tracking", "FINISHED", 1000.0)]) is None


def test_one_known_source_cannot_produce_a_conflict():
    """A conflict is a statement about two readings. With one, there is nothing to disagree with."""
    assert jobs_mod.detect_conflict(
        entity="job", entity_id="j-1",
        sources=[_source("agent", "running", 1000.0),
                 _source("gateway", "unreachable", 1000.0)]) is None


# -- T726: the three-way join ----------------------------------------------------------------------

def test_a_job_present_in_only_one_source_still_produces_a_row():
    """Dropping it would make the list silently incomplete in exactly the situation an operator is
    investigating: an id on one side and not the other is a finding, not a reason to hide it."""
    rows = jobs_mod.join(
        gateway_jobs=[{"job_id": "only-gateway", "state": "queued"}],
        agent_jobs=[{"job_id": "only-agent", "state": "running", "kind": "finetune"}],
        tracking_runs=[{"run_id": "only-tracking", "status": "RUNNING"}])
    assert {row["id"] for row in rows} == {"only-gateway", "only-agent", "only-tracking"}


def test_the_join_keys_the_three_sources_onto_one_row():
    rows = jobs_mod.join(
        gateway_jobs=[{"job_id": "j-1", "state": "running"}],
        agent_jobs=[{"job_id": "j-1", "state": "running", "kind": "finetune"}],
        tracking_runs=[{"job_id": "j-1", "run_id": "r-1", "status": "RUNNING"}])
    assert len(rows) == 1
    assert rows[0]["normalizedState"] == "Running" and rows[0]["runId"] == "r-1"


def test_terminal_jobs_sort_below_work_still_in_flight():
    rows = jobs_mod.join(agent_jobs=[
        {"job_id": "old", "state": "done", "kind": "finetune", "submitted_at": 200.0},
        {"job_id": "live", "state": "running", "kind": "finetune", "submitted_at": 100.0}])
    assert [row["id"] for row in rows] == ["live", "old"]


def test_the_job_type_is_derived_from_what_it_was_dispatched_as():
    assert jobs_mod.platform_job(agent_job={"job_id": "a", "kind": "finetune"})["type"] == "training"
    assert jobs_mod.platform_job(agent_job={"job_id": "b", "kind": "hpo"})["type"] == "hpo"
    assert jobs_mod.platform_job(agent_job={"job_id": "c", "kind": "batch"})["type"] == "batch"
