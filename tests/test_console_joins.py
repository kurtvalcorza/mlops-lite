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


def test_the_model_id_reads_the_key_the_job_record_actually_carries():
    """`training/run_flow.py` writes the completed job's model under `model`, journaled verbatim
    and spread back to the top level by `_job_row_to_record`. Nothing on that path ever emits
    `model_name`, so reading it blanked every completed fine-tune's modelId — silently, because
    null legitimately renders as unknown on this surface."""
    row = jobs_mod.platform_job(agent_job={"job_id": "j", "kind": "finetune",
                                           "state": "succeeded", "model": "distilbert-lora"})
    assert row["modelId"] == "distilbert-lora"


def test_the_job_type_is_derived_from_what_it_was_dispatched_as():
    assert jobs_mod.platform_job(agent_job={"job_id": "a", "kind": "finetune"})["type"] == "training"
    assert jobs_mod.platform_job(agent_job={"job_id": "b", "kind": "hpo"})["type"] == "hpo"
    assert jobs_mod.platform_job(agent_job={"job_id": "c", "kind": "batch"})["type"] == "batch"


# -- T720: the catalog join (data-model §5) --------------------------------------------------------

from gateway.app.console import catalog as catalog_mod  # noqa: E402


def test_a_version_with_no_artifact_is_marked_absent_not_dropped():
    """spec Edge Cases: the row with a missing side is precisely the one an operator is looking for.
    Filtering it out produces a list that looks complete and is not."""
    row = catalog_mod.platform_model({"name": "qwen", "version": "3", "source": "s3://m/qwen/3"},
                                     artifact_present=False)
    assert row["id"] == "qwen:3" and row["artifactPresent"] is False


def test_an_unchecked_artifact_is_null_and_never_defaulted_to_present():
    """Assuming presence from a registry URI is how a console shows a download that 404s."""
    row = catalog_mod.platform_model({"name": "qwen", "version": "3", "source": "s3://m/qwen/3"})
    assert row["artifactPresent"] is None
    assert row["artifactPresent"] is not True and row["artifactPresent"] is not False


def test_an_unrecognized_modality_renders_as_unknown_rather_than_being_filtered_out():
    """FR-385: a model the console cannot classify is still a model the operator has."""
    assert catalog_mod.platform_model({"name": "m", "version": "1",
                                       "tags": {"task": "protein-folding"}})["modality"] == "unknown"
    assert catalog_mod.platform_model({"name": "m", "version": "1"})["modality"] == "unknown"


def test_an_unevaluated_non_serving_version_is_not_evaluated_rather_than_an_error():
    """FR-402: the platform's documented refusal to score it, not a failure."""
    assert catalog_mod.evaluation_state(evaluation=None, is_serving=False) == "not-evaluated"


def test_an_unevaluated_serving_version_is_incomplete_not_merely_unscored():
    """'We chose not to evaluate this' and 'this got promoted without evidence' are different
    findings, and only the second one is a problem."""
    assert catalog_mod.evaluation_state(evaluation=None, is_serving=True) == "incomplete"


def test_a_registry_entry_with_no_evaluation_still_produces_a_catalog_row():
    rows = [catalog_mod.platform_model(v) for v in
            [{"name": "a", "version": "1"}, {"name": "b", "version": "2", "serving": True}]]
    assert [r["id"] for r in rows] == ["a:1", "b:2"]
    assert rows[1]["aliases"] == ["serving"] and rows[0]["aliases"] == []


# -- T722: the compatibility verdict (FR-388) -------------------------------------------------------

FITS = {"usable_budget_gb": 11.0, "accounted_resident_gb": 0.0, "reserved_gb": 0.0,
        "unmaterialized_gb": 0.0, "live_free_gb": 11.5, "headroom_gb": 0.5, "job_barrier": False,
        "active_job": None}


def test_an_unreachable_agent_is_unknown_never_incompatible():
    """An unreachable agent is not a compatibility fact. Reporting it as `incompatible` would send
    an operator to rebuild a model that was fine."""
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=None)
    assert verdict["verdict"] == "unknown"
    assert verdict["budgetCheck"] == "unknown" and verdict["liveVramCheck"] == "unknown"


def test_a_model_that_cannot_fit_an_empty_gpu_is_incompatible_not_transient():
    """Structural: neither eviction nor waiting can help, so calling it transient would tell an
    operator to wait for something that will never happen."""
    verdict = catalog_mod.compatibility(estimated_gb=40.0, admission=FITS)
    assert verdict["fitsAlone"] is False and verdict["verdict"] == "incompatible"
    assert any("cannot-fit-alone" in r for r in verdict["reasons"])


def test_a_budget_failure_on_a_model_that_fits_alone_is_transient():
    admission = {**FITS, "accounted_resident_gb": 9.0}
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=admission)
    assert verdict["fitsAlone"] is True
    assert verdict["budgetCheck"] == "fail" and verdict["verdict"] == "not-currently-eligible"


def test_the_two_vram_checks_are_reported_separately():
    """Never merged into one number: eviction fixes a budget failure, whereas a live-VRAM failure
    with headroom exhausted usually means a leaked or unaccounted allocation."""
    admission = {**FITS, "accounted_resident_gb": 9.0, "live_free_gb": 11.5}
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=admission)
    assert verdict["budgetCheck"] == "fail", "the accounted budget is exceeded"
    assert verdict["liveVramCheck"] == "pass", "but the GPU physically has the room"
    assert verdict["budgetCheck"] != verdict["liveVramCheck"]


def test_each_check_uses_its_own_reservation_term():
    """Invariant 1 counts EVERY outstanding reservation; invariant 2 deducts only the
    not-yet-materialized ones, because a reconciled reservation is already visible in live free and
    subtracting it twice reports a model as not fitting when admission would accept it."""
    admission = {**FITS, "reserved_gb": 8.0, "unmaterialized_gb": 0.0}
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=admission)
    assert verdict["budgetCheck"] == "fail", "4 + 0 accounted + 8 reserved > 11 usable budget"
    assert verdict["liveVramCheck"] == "pass", (
        "the reservation is already materialized, so it is already reflected in live free — "
        "deducting it again would report a model as not fitting when admission would accept it")


def test_a_job_holding_the_gpu_is_transient_and_says_a_job_is_never_preempted():
    verdict = catalog_mod.compatibility(estimated_gb=4.0,
                                        admission={**FITS, "active_job": {"job_id": "j-1"}})
    assert verdict["jobExclusive"] is True and verdict["verdict"] == "not-currently-eligible"
    assert any("never preempted" in r for r in verdict["reasons"])


def test_an_unresolvable_adapter_base_is_structural():
    """FR-389: the platform already refuses to promote it; the catalog says so rather than letting
    the operator discover it at promotion time."""
    verdict = catalog_mod.compatibility(estimated_gb=1.0, admission=FITS, base_resolvable=False)
    assert verdict["verdict"] == "incompatible"


def test_a_missing_artifact_is_structural():
    verdict = catalog_mod.compatibility(estimated_gb=1.0, admission=FITS, artifact_available=False)
    assert verdict["verdict"] == "incompatible"


def test_an_unchecked_artifact_does_not_make_a_model_incompatible():
    """`None` is not `False`. An unverified artifact is not a missing one."""
    verdict = catalog_mod.compatibility(estimated_gb=1.0, admission=FITS, artifact_available=None)
    assert verdict["verdict"] == "eligible"


def test_a_model_that_fits_every_check_is_eligible():
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=FITS)
    assert verdict["verdict"] == "eligible" and verdict["reasons"] == []


def test_unknown_is_never_collapsed_into_a_verdict_about_capacity():
    """The rule stated three ways, because collapsing it is the easy mistake: a partially-readable
    admission view still yields `unknown`, not a guess."""
    partial = {**FITS, "live_free_gb": None}
    verdict = catalog_mod.compatibility(estimated_gb=4.0, admission=partial)
    assert verdict["liveVramCheck"] == "unknown" and verdict["verdict"] == "unknown"
