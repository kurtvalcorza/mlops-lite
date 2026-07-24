"""025 US2 — tabular admitted as a CPU/off-lease trainable modality (T603/T605 plumbing).

Tabular gains a fine-tune flow, so it must be admissible as a `finetune` job, a policy modality, and a
retrain target — WITHOUT ever taking the single GPU lease and WITHOUT leaking into HPO. Offline, no GPU:

  - `finetune` accepts tabular and takes **no** lease; `hpo` still REFUSES it (no search space — a
    tabular study would hold the GPU and fail every trial);
  - a GPU modality still takes (and releases) the lease exactly as before — no regression;
  - `ModelPolicy` + `RetrainSpec` accept tabular (they'd otherwise 400/422 before the scheduler runs);
  - `scheduler.MODALITY_TASK` has a `tabular` entry, so a tabular policy tick resolves its task instead
    of raising `KeyError` (which the tick swallows as `check_error` → breach never detected).
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hostagent import jobs as jobs_mod  # noqa: E402
from platformlib.topology import (  # noqa: E402
    CPU_TRAINABLE_MODALITIES,
    FINETUNE_MODALITIES,
    TRAINABLE_MODALITIES,
)

# --- the modality sets -----------------------------------------------------------------------------

def test_tabular_is_finetune_trainable_but_not_gpu_trainable():
    assert "tabular" in FINETUNE_MODALITIES and "tabular" in CPU_TRAINABLE_MODALITIES
    # NOT in the GPU/HPO-capable set — this separation is the whole point (no tabular search space).
    assert "tabular" not in TRAINABLE_MODALITIES
    assert set(TRAINABLE_MODALITIES) < set(FINETUNE_MODALITIES)


def test_finetune_admits_tabular_hpo_does_not():
    assert "tabular" in jobs_mod.KINDS["finetune"].modality_set
    assert "tabular" not in jobs_mod.KINDS["hpo"].modality_set     # would take the lease then fail


def test_needs_gpu_is_false_for_a_cpu_modality_on_a_gpu_kind():
    ft = jobs_mod.KINDS["finetune"]
    assert ft.gpu is True                                   # the kind is still a GPU kind…
    assert jobs_mod._needs_gpu(ft, "llm") is True           # …for GPU modalities
    assert jobs_mod._needs_gpu(ft, "tabular") is False      # …but tabular trains off-lease
    assert jobs_mod._needs_gpu(jobs_mod.KINDS["batch"], "llm") is False   # batch never takes it


# --- end-to-end through JobManager.submit (fake admission + journal) ------------------------------

class FakeAdmission:
    def __init__(self):
        self.acquired, self.released = [], []

    def acquire(self, tenant, kind, est_gb):
        self.acquired.append((tenant, kind))

    def release(self, tenant):
        self.released.append(tenant)

    def free_gb(self):
        return 8.0

    def holder(self):
        return None


class FakeJournal:
    def __init__(self):
        self.records = {}

    def submit(self, rec):
        self.records[rec["job_id"]] = dict(rec)

    def transition(self, jid, state, **kw):
        self.records[jid].update(state=state, **kw)

    def get(self, jid):
        return self.records.get(jid)

    def jobs(self, kind=None):
        return list(self.records.values())

    def active_count(self):
        return sum(1 for r in self.records.values() if r["state"] in ("queued", "running"))


def _manager(adm):
    # A no-op runner: we assert the ADMISSION decision, not the training itself.
    return jobs_mod.JobManager(adm, FakeJournal(),
                               runners={k: (lambda m, jid, req: {}) for k in jobs_mod.KINDS})


def _req(**over):
    r = {"dataset_name": "ds", "dataset_version": "v1", "output_name": "out"}
    r.update(over)
    return r


def test_tabular_finetune_submits_and_takes_no_gpu_lease():
    adm = FakeAdmission()
    mgr = _manager(adm)
    code, payload = mgr.submit("finetune", _req(modality="tabular"))
    assert code == 202 and payload["run_id"]
    assert adm.acquired == []          # CPU/off-lease — the lease was never taken (FR-354)
    assert mgr._lease_held is False


def test_llm_finetune_still_takes_and_releases_the_lease():
    adm = FakeAdmission()
    mgr = _manager(adm)
    code, _ = mgr.submit("finetune", _req(modality="llm"))
    assert code == 202
    assert adm.acquired == [(mgr.tenant, "job")]     # unchanged GPU behavior
    # The no-op runner finishes in its worker thread and `_release` frees the lease — poll for it
    # rather than asserting on `_lease_held`, which would race that thread.
    for _ in range(200):
        if adm.released:
            break
        time.sleep(0.01)
    assert adm.released == [mgr.tenant] and mgr._lease_held is False


def test_release_after_a_tabular_job_does_not_free_a_lease_it_never_held():
    adm = FakeAdmission()
    mgr = _manager(adm)
    mgr.submit("finetune", _req(modality="tabular"))
    mgr._release("finetune", jobs_mod.KINDS["finetune"])
    assert adm.released == []          # would otherwise free a real GPU tenant's lease


def test_tabular_hpo_is_refused_at_submission():
    adm = FakeAdmission()
    code, payload = _manager(adm).submit("hpo", _req(modality="tabular"))
    assert code == 400 and "modality" in payload["error"]
    assert adm.acquired == []          # refused BEFORE any lease acquisition


# --- policy / retrain / scheduler plumbing --------------------------------------------------------

def test_scheduler_maps_the_tabular_modality_to_a_task():
    from app import scheduler
    assert scheduler.MODALITY_TASK["tabular"] == "tabular"
    # every fine-tunable modality resolves — a direct index can no longer KeyError
    for m in FINETUNE_MODALITIES:
        assert scheduler.MODALITY_TASK[m]


def test_retrain_spec_accepts_tabular():
    from app.routers.monitor import RetrainSpec
    spec = RetrainSpec(dataset_name="ds", dataset_version="latest", output_name="o",
                       modality="tabular")
    assert spec.modality == "tabular"


def test_retrain_spec_still_rejects_a_non_modality():
    from app.routers.monitor import RetrainSpec
    try:
        RetrainSpec(dataset_name="ds", dataset_version="latest", output_name="o", modality="banana")
    except Exception as e:
        assert "modality" in str(e)
    else:
        raise AssertionError("expected a validation error for an unknown modality")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
