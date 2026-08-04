"""027 US2 — the agent's runtime read surface (T707..T712).

Offline and web-free, over a fake NVML reader and a fake journal. The claims under test are about
*provenance and honesty* — that a fallback reading is labelled as one, that an unreadable GPU is a
200 rather than an error, that null is never zero, that a torn journal tail is shown as torn — and
none of them need a GPU to demonstrate. The `[HW]` legs (T718/T719) are the ones that genuinely do.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from hostagent import admissionlog, devices, journalread  # noqa: E402

GIB = 1024 ** 3


# -- T708: the device snapshot ---------------------------------------------------------------------

def _nvml_devices():
    return [{
        "index": 0, "name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "uuid": "GPU-1f2e",
        "compute_capability": "12.0", "total_vram_gb": 12.0, "free_vram_gb": 7.4,
        "used_vram_gb": 4.6, "utilization_pct": 61, "temperature_c": 58,
        "processes": [{"pid": 44121, "vram_gb": 4.6, "engine_id": None}],
    }]


def test_a_readable_gpu_reports_source_nvml_with_every_field():
    snap = devices.DeviceSnapshotter(nvml_fn=_nvml_devices).snapshot()
    assert snap["source"] == devices.NVML
    device = snap["devices"][0]
    assert device["name"].startswith("NVIDIA") and device["total_vram_gb"] == 12.0
    assert device["utilization_pct"] == 61 and device["temperature_c"] == 58
    assert "observed_at" in snap


def test_devices_is_always_a_list_even_with_one_device():
    """Multi-device and multi-host must need no contract change later (FR-382)."""
    snap = devices.DeviceSnapshotter(nvml_fn=_nvml_devices).snapshot()
    assert isinstance(snap["devices"], list) and len(snap["devices"]) == 1


def test_an_unreadable_gpu_yields_nulls_and_never_zeros():
    """FR-381's whole point: `0 GB free` is a FALSE reading an operator would act on, while `null`
    plus a labelled source is an honest 'unknown'."""
    snap = devices.DeviceSnapshotter(nvml_fn=lambda: None, smi_fn=lambda: None).snapshot()
    assert snap["source"] == devices.STATIC
    device = snap["devices"][0]
    assert device["index"] == 0, "the index is known even on the static path"
    for field in ("name", "total_vram_gb", "free_vram_gb", "used_vram_gb", "utilization_pct",
                  "temperature_c"):
        assert device[field] is None, f"{field} must be null, never zero"
        assert device[field] != 0


def test_an_unreadable_gpu_is_not_an_error():
    """A known operating state, not a request failure."""
    snap = devices.DeviceSnapshotter(nvml_fn=lambda: None, smi_fn=lambda: None).snapshot()
    assert snap["devices"], "the route still answers with a device list"


def test_the_smi_fallback_is_labelled_as_such():
    smi = [{"index": 0, "name": "GPU", "uuid": None, "compute_capability": None,
            "total_vram_gb": 12.0, "free_vram_gb": 7.0, "used_vram_gb": 5.0,
            "utilization_pct": 50, "temperature_c": 55, "processes": []}]
    snap = devices.DeviceSnapshotter(nvml_fn=lambda: None, smi_fn=lambda: smi).snapshot()
    assert snap["source"] == devices.SMI
    assert snap["devices"][0]["source"] == devices.SMI, \
        "the console labels a fallback-derived value from `source`, never by guessing"


def test_every_contract_field_is_present_even_when_unknown():
    """A missing key and a null key read very differently; only one says 'we looked'."""
    snap = devices.DeviceSnapshotter(nvml_fn=lambda: [{"index": 0}]).snapshot()
    for field in ("index", "name", "uuid", "compute_capability", "total_vram_gb", "free_vram_gb",
                  "used_vram_gb", "utilization_pct", "temperature_c", "processes", "source"):
        assert field in snap["devices"][0], field


def test_the_snapshot_is_cached_and_does_not_re_read_per_request():
    """The 018 regression NVML was introduced to remove: a console with ten live panels must not
    re-read the device on every poll."""
    reads = []

    def counting():
        reads.append(1)
        return _nvml_devices()

    clock = [0.0]
    snapper = devices.DeviceSnapshotter(ttl_s=1.0, clock=lambda: clock[0], nvml_fn=counting)
    for _ in range(10):
        snapper.snapshot()
    assert len(reads) == 1, "ten polls inside the TTL read the device once"

    clock[0] = 2.0
    snapper.snapshot()
    assert len(reads) == 2, "the cache expires"


def test_processes_are_attributed_to_their_engine():
    snapper = devices.DeviceSnapshotter(nvml_fn=_nvml_devices,
                                        engine_pids=lambda: {44121: "llm"})
    proc = snapper.snapshot()["devices"][0]["processes"][0]
    assert proc["pid"] == 44121 and proc["engine_id"] == "llm"


def test_a_failing_engine_attribution_does_not_lose_the_reading():
    def boom():
        raise RuntimeError("lifecycle unavailable")

    snap = devices.DeviceSnapshotter(nvml_fn=_nvml_devices, engine_pids=boom).snapshot()
    assert snap["devices"][0]["total_vram_gb"] == 12.0, "enrichment failure never loses the device"


# -- T709: the admission decision ring ----------------------------------------------------------------

def test_the_ring_is_bounded_and_newest_first():
    log = admissionlog.AdmissionLog(capacity=3)
    for i in range(5):
        log.record(decision="admitted", model_key=f"m{i}", requested_gb=1.0)
    records = log.records()
    assert len(records) == 3, "bounded"
    assert [r["model_key"] for r in records] == ["m4", "m3", "m2"], \
        "newest first — an operator investigating a refusal wants the refusal"


def test_a_record_carries_a_decision_time_not_a_queue_age():
    """Research R1: admission decides immediately, so there is no pending state and no queue."""
    log = admissionlog.AdmissionLog()
    record = log.record(decision="refused", reason="budget", model_key="m", requested_gb=1.0)
    assert "decided_at" in record
    assert "queued_at" not in record and "queue_position" not in record and "pending" not in record


def test_the_two_checks_are_recorded_separately_and_never_merged():
    log = admissionlog.AdmissionLog()
    record = log.record(decision="refused", reason="budget", model_key="m", requested_gb=3.0,
                        usable_budget_gb=11.0, accounted_resident_gb=9.0, reserved_gb=1.0,
                        unmaterialized_gb=1.0, live_free_gb=2.0, headroom_gb=0.5)
    # The budget check counts EVERY outstanding reservation; the live-VRAM check deducts only the
    # not-yet-materialized ones. Two different sums, both needed.
    assert record["reserved_gb"] == 1.0 and record["unmaterialized_gb"] == 1.0
    assert record["accounted_resident_gb"] == 9.0 and record["live_free_gb"] == 2.0
    assert "usable_budget_gb" in record and "headroom_gb" in record


@pytest.mark.parametrize("reason,expected", [
    ("job-exclusive", "never preempted"),
    ("budget", "usable budget"),
    ("live-vram", "live free VRAM"),
    ("cannot-fit-alone", "Evicting other models cannot help"),
    ("load-failed", "reserved capacity has been released"),
])
def test_each_refusal_reason_has_its_own_explanation(reason, expected):
    """FR-378: composed server-side from the values the decision used, so the interface cannot drift
    from admission's real reasoning."""
    log = admissionlog.AdmissionLog()
    record = log.record(decision="refused", reason=reason, model_key="clip", requested_gb=3.2,
                        usable_budget_gb=11.4, accounted_resident_gb=10.1, live_free_gb=1.6,
                        headroom_gb=0.5, blocking_tenant="job_3842", detail="OOM")
    assert expected in record["explanation"], record["explanation"]


def test_cannot_fit_alone_is_distinguished_from_budget():
    """The former is STRUCTURAL — no amount of eviction or waiting helps — and the latter transient.
    Filing one under the other sends an operator to the wrong remedy."""
    log = admissionlog.AdmissionLog()
    structural = log.record(decision="refused", reason="cannot-fit-alone", requested_gb=20.0,
                            usable_budget_gb=11.0, model_key="huge")
    transient = log.record(decision="refused", reason="budget", requested_gb=3.0,
                           usable_budget_gb=11.0, accounted_resident_gb=9.0, model_key="m")
    assert "cannot help" in structural["explanation"]
    assert "cannot help" not in transient["explanation"]
    assert structural["reason"] != transient["reason"]


def test_eviction_is_its_own_outcome_not_merged_with_the_admission_that_follows():
    """The protocol is evict-then-RECOMPUTE: the later attempt may still refuse, so a record
    claiming one event both evicted and admitted would assert a causal link the coordinator does not
    guarantee."""
    log = admissionlog.AdmissionLog()
    record = log.record(decision="evicted-retry", attempt=1, model_key="new",
                        evicted=[{"model_key": "old", "policy": "lru", "freed_gb": 2.0}])
    assert record["decision"] == "evicted-retry"
    assert "re-derived on the next attempt" in record["explanation"]
    assert "admitted" not in record["explanation"]


def test_an_admitted_record_names_the_co_resident_count():
    log = admissionlog.AdmissionLog()
    record = log.record(decision="admitted", model_key="m", requested_gb=2.0, device_index=0,
                        residents=[{"model_key": "a"}, {"model_key": "b"}])
    assert "2 resident model(s)" in record["explanation"]


def test_residents_are_keyed_by_model_instance_not_tenant():
    """Keying by tenant would render five tenants on one model as five residents, each apparently
    holding its own VRAM."""
    log = admissionlog.AdmissionLog()
    record = log.record(decision="admitted", model_key="qwen", requested_gb=5.0,
                        residents=[{"model_key": "qwen", "kind": "serving", "vram_gb": 5.0,
                                    "state": "resident", "active_requests": 5}])
    resident = record["residents"][0]
    assert resident["model_key"] == "qwen" and resident["active_requests"] == 5


# -- the coordinator writes real records --------------------------------------------------------------

def test_the_coordinator_records_a_job_exclusive_refusal_with_the_blocking_job():
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(sizes={"a": 2 * GIB})
    coord.admit_job("job-7")
    coord.admit_serving("a", 2 * GIB)

    record = coord.admission_log.records()[0]
    assert record["decision"] == "refused" and record["reason"] == "job-exclusive"
    assert "job-7" in record["explanation"] and "never preempted" in record["explanation"]


def test_the_coordinator_records_cannot_fit_alone_for_an_oversized_model():
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(total=12 * GIB)
    coord.admit_serving("huge", 20 * GIB)
    record = coord.admission_log.records()[0]
    assert record["reason"] == "cannot-fit-alone"
    assert "Evicting other models cannot help" in record["explanation"]


def test_the_coordinator_records_an_admission_with_both_bounds():
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(total=12 * GIB, sizes={"a": 4 * GIB})
    coord.admit_serving("a", 4 * GIB).claim.release()
    record = coord.admission_log.records()[0]
    assert record["decision"] == "admitted"
    assert record["usable_budget_gb"] == pytest.approx(11.0, abs=0.05)
    assert record["live_free_gb"] is not None and record["headroom_gb"] == pytest.approx(0.5, 0.05)


def test_the_coordinator_records_a_load_failure_as_its_own_reason():
    """Neither a budget nor a live-VRAM refusal — filing it under one would mislead the operator
    about the remedy."""
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(sizes={"a": 2 * GIB}, fail=("a",))
    coord.admit_serving("a", 2 * GIB)
    record = coord.admission_log.records()[0]
    assert record["reason"] == "load-failed"
    assert "released" in record["explanation"]


def test_recording_never_fails_an_admission():
    """An observability path that can refuse a request is worse than no observability path."""
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(sizes={"a": 2 * GIB})

    class Exploding:
        capacity = 0

        def record(self, **kw):
            raise RuntimeError("ring is broken")

        def records(self, limit=None):
            return []

    coord.admission_log = Exploding()
    result = coord.admit_serving("a", 2 * GIB)
    assert result.ok, "a broken ring must not refuse a request"
    result.claim.release()


def test_the_admission_snapshot_reports_both_reservation_terms():
    from hostagent import admissionlog as al
    from tests.test_agent_coordinator import make

    coord, gpu, life = make(total=12 * GIB, sizes={"a": 4 * GIB})
    claim = coord.admit_serving("a", 4 * GIB).claim
    snap = al.snapshot_from_coordinator(coord, coord.admission_log)
    for term in ("usable_budget_gb", "accounted_resident_gb", "reserved_gb", "unmaterialized_gb",
                 "live_free_gb", "headroom_gb"):
        assert term in snap, term
    assert snap["residents"][0]["model_key"] == "a"
    assert snap["residents"][0]["active_requests"] == 1
    claim.release()


# -- T710: the paged journal --------------------------------------------------------------------------

class FakeJournal:
    def __init__(self, records):
        self._records = records

    def jobs(self, kind=None):
        return list(self._records)


def _job(job_id, state="succeeded", submitted=1000.0, started=1001.0, ended=1002.0, modality="llm"):
    record = {"job_id": job_id, "state": state, "submitted_at": submitted, "modality": modality}
    if started is not None:
        record["started_at"] = started
    if ended is not None:
        record["ended_at"] = ended
    return record


def test_the_journal_pages_and_never_offers_an_all_mode():
    journal = FakeJournal([_job(f"j{i}", ended=1000.0 + i) for i in range(250)])
    page = journalread.read(journal, limit=10)
    assert len(page["entries"]) == 10 and page["has_more"] is True and page["next_cursor"]

    second = journalread.read(journal, limit=10, cursor=page["next_cursor"])
    assert len(second["entries"]) == 10
    assert not ({e["job_id"] for e in page["entries"]} & {e["job_id"] for e in second["entries"]}), \
        "pages do not overlap"


def test_the_limit_is_hard_capped():
    """The transport's 1 MiB JSON cap would fail an unbounded response, so the cap is enforced
    rather than defaulted."""
    journal = FakeJournal([_job(f"j{i}", ended=1000.0 + i) for i in range(900)])
    page = journalread.read(journal, limit=10_000)
    assert len(page["entries"]) == journalread.MAX_LIMIT


def test_entries_are_newest_first():
    journal = FakeJournal([_job("old", ended=1000.0), _job("new", ended=2000.0)])
    entries = journalread.read(journal)["entries"]
    assert [e["job_id"] for e in entries] == ["new", "old"]


def test_the_journal_filters_by_job_engine_and_time():
    journal = FakeJournal([_job("a", modality="llm", ended=1000.0),
                           _job("b", modality="asr", ended=2000.0)])
    assert [e["job_id"] for e in journalread.read(journal, job_id="a")["entries"]] == ["a"]
    assert [e["job_id"] for e in journalread.read(journal, engine_id="asr")["entries"]] == ["b"]
    assert [e["job_id"] for e in journalread.read(journal, since=1500.0)["entries"]] == ["b"]
    assert [e["job_id"] for e in journalread.read(journal, until=1500.0)["entries"]] == ["a"]


def test_filters_apply_before_paging():
    """`limit` counts MATCHING entries, not scanned ones — otherwise `has_more` is meaningless."""
    journal = FakeJournal([_job(f"j{i}", modality="llm" if i % 2 else "asr", ended=1000.0 + i)
                           for i in range(20)])
    page = journalread.read(journal, engine_id="asr", limit=5)
    assert len(page["entries"]) == 5
    assert all(e["engine_id"] == "asr" for e in page["entries"])


def test_a_torn_tail_entry_is_shown_as_torn_never_dropped():
    """A missing final transition is exactly what an operator investigating a crash needs."""
    journal = FakeJournal([_job("clean"), _job("torn", state="succeeded", started=None, ended=None)])
    entries = journalread.read(journal)["entries"]
    by_id = {e["job_id"]: e for e in entries}
    assert "torn" in by_id, "a torn entry is never silently dropped"
    assert by_id["torn"]["checksum_state"] == "torn"
    assert by_id["clean"]["checksum_state"] == "ok"


def test_a_malformed_cursor_reads_as_the_first_page():
    journal = FakeJournal([_job(f"j{i}", ended=1000.0 + i) for i in range(5)])
    page = journalread.read(journal, cursor="not-a-cursor")
    assert len(page["entries"]) == 5


def test_the_last_page_carries_no_cursor():
    journal = FakeJournal([_job("only")])
    page = journalread.read(journal, limit=10)
    assert page["has_more"] is False and page["next_cursor"] is None


# -- T712: the routes are registered and read-only -------------------------------------------------------

def test_the_three_runtime_routes_are_registered_on_the_agent():
    from hostagent import main as agent_main

    for path, handler in (("/runtime/devices", agent_main._get_runtime_devices),
                          ("/runtime/admission", agent_main._get_runtime_admission),
                          ("/journal", agent_main._get_journal)):
        matched = [h for m, h in agent_main._GET_ROUTES if m(path)]
        assert matched and matched[0] is handler, path


def test_the_admission_route_answers_503_without_a_coordinator():
    from types import SimpleNamespace

    from hostagent import main as agent_main

    status, payload, _ = agent_main._get_runtime_admission(
        "/runtime/admission", SimpleNamespace(query="", coordinator=None))
    assert status == 503 and "not enabled" in payload["error"]


def test_the_device_snapshotter_is_built_once():
    """A per-request instance would have an empty cache every time — the fork-per-poll regression in
    a new place."""
    from hostagent import main as agent_main

    agent_main._DEVICES = None
    first = agent_main._devices()
    assert agent_main._devices() is first


def test_the_runtime_routes_are_read_only():
    """Nothing here acquires, releases, or influences admission (Principle II)."""
    import inspect

    from hostagent import main as agent_main

    for fn in (agent_main._get_runtime_devices, agent_main._get_runtime_admission,
               agent_main._get_journal):
        src = inspect.getsource(fn)
        for mutator in ("acquire(", "release(", "admit_", "evict(", "end_job("):
            assert mutator not in src, f"{fn.__name__} calls {mutator}"


# -- T711: the EngineState extension is backward compatible ------------------------------------------------

def test_engine_state_enrichment_is_optional_only():
    """A listing that omits the new fields is still a valid EngineState — what makes this a
    backward-compatible extension rather than a version bump."""
    from platformlib.contracts import EngineState

    minimal = EngineState(engine_id="llm", state="ready", gpu=True)
    minimal.validate()
    assert minimal.pid is None and minimal.vram_gb is None and minimal.model_identity is None

    enriched = EngineState(engine_id="llm", state="ready", gpu=True, pid=44121, device_index=0,
                           vram_gb=4.6, model_identity="qwen2.5-7b", registry_version="3",
                           active_requests=2, residency_state="resident")
    enriched.validate()
    assert enriched.model_identity == "qwen2.5-7b"


def test_state_and_residency_state_are_distinct_fields():
    """`state` is the engine PROCESS's health; `residency_state` is the coordinator's view of the
    model's place in the resident set. Collapsing them loses 'the child is fine but its model is
    being evicted'."""
    from platformlib.contracts import EngineState

    engine = EngineState(engine_id="llm", state="ready", residency_state="draining")
    engine.validate()
    assert engine.state == "ready" and engine.residency_state == "draining"
