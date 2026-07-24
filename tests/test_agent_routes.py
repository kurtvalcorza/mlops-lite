"""024 US3 (T584) — the agent dispatcher route-table, exercised WITHOUT the HTTP server.

The GET/POST if-ladders became ordered `(matcher, handler)` tables (`hostagent/main.py`); each handler
is a plain `(path, ctx)` function, so the dispatch unit-tests with fake admission/journal/manager/jobs
and no socket. Pins the public route SET (contracts/preservation.md §C3) — the exact paths that route,
the strays that fall through to 404 (the Codex-round-4 parsed-path fix), and a handful of handler
outcomes — all byte-identical to the pre-refactor branches (FR-340).
"""
import os
import sys
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import main  # noqa: E402


class FakeAdmission:
    def __init__(self, holder=None, free=8.0):
        self._holder, self._free = holder, free

    def holder(self):
        return self._holder

    def free_gb(self):
        return self._free


class FakeJournal:
    def __init__(self, jobs=None, recs=None, active=0):
        self._jobs, self._recs, self._active = jobs or [], recs or {}, active

    def active_count(self):
        return self._active

    def get(self, jid):
        return self._recs.get(jid)

    def jobs(self, kind=None):
        return [j for j in self._jobs if kind is None or j.get("kind") == kind]


class FakeManager:
    def __init__(self, states=None, rows=None, runtimes=None):
        self._states, self._rows, self.runtimes = states or {}, rows or [], runtimes or {}

    def engine_states(self):
        return self._states

    def engine_rows(self):
        return self._rows


class FakeJobs:
    def __init__(self, health=None, submit=(202, {"status": "queued"}), cancel=None):
        self._health = health or {"gpu_batch_active": False, "jobs_active": 0}
        self._submit, self._cancel = submit, cancel or {"status": "cancelled"}

    def health_fields(self):
        return dict(self._health)

    def submit(self, kind, req):
        return self._submit

    def cancel(self, jid):
        return self._cancel


def _get(path, query="", **over):
    deps = dict(admission=FakeAdmission(), journal=FakeJournal(), manager=FakeManager(),
                jobs=FakeJobs())
    deps.update(over)
    return main.handle_get(path, query, **deps)


def _post(path, *, query="", content_type="application/json", control_header="", raw_body=b"{}",
          **over):
    deps = dict(admission=FakeAdmission(), journal=FakeJournal(), manager=FakeManager(),
                jobs=FakeJobs())
    deps.update(over)
    return main.handle_post(path, query, content_type, control_header, raw_body, **deps)


# --- the public route SET (C3): exactly what routes, and the strays that don't --------------------

def test_get_route_table_covers_the_public_surface():
    def matched(p):
        return any(m(p) for m, _ in main._GET_ROUTES)
    for p in ("/healthz", "/readyz", "/health", "/metrics", "/engines",
              "/engines/llm/health", "/engines/vision/readyz", "/engines/tabular/healthz",
              "/jobs", "/jobs/abc"):
        assert matched(p), f"{p!r} should route"
    # strays fall through to the legacy fallback / 404 — the parsed-path fix (Codex round 4).
    assert not matched("/nope")
    assert not matched("/jobsxyz")       # not "/jobs" and not "/jobs/..." → not the jobs listing


def test_post_route_table_covers_the_public_surface():
    def matched(p):
        return any(m(p) for m, _ in main._POST_ROUTES)
    for p in ("/control/unload", "/control/reload", "/jobs", "/jobs/abc/cancel",
              "/engines/llm/infer", "/engines/vision/classify", "/engines/llm/infer/stream"):
        assert matched(p), f"{p!r} should route"
    alias = next(iter(main.jobs_mod.ROUTE_TO_KIND))     # a legacy trainer alias routes
    assert matched("/" + alias)
    assert not matched("/nope")


# --- handlers are callable with fakes, no HTTP server ---------------------------------------------

def test_healthz_is_open_and_minimal():
    assert main._get_healthz("/healthz", SimpleNamespace()) == \
        (200, {"ok": True, "service": "hostagent"}, "application/json")
    # and via the dispatcher
    assert _get("/healthz")[0] == 200


def test_health_reports_holder_and_engine_state():
    mgr = FakeManager(states={"llm": {"state": "resident"}})
    status, payload, ct = _get("/health", admission=FakeAdmission(holder={"tenant": "llm",
                                                                          "kind": "serving"}),
                               manager=mgr)
    assert status == 200 and ct == "application/json"
    assert payload["ok"] is True and payload["holder"] == "llm" and payload["holder_kind"] == "serving"
    assert payload["engines"] == {"llm": "resident"} and payload["gpu_batch_active"] is False


def test_engines_listing_and_jobs_listing_and_single_and_unknown():
    assert _get("/engines", manager=FakeManager(rows=[{"engine_id": "llm"}])) == \
        (200, {"engines": [{"engine_id": "llm"}]}, "application/json")
    j = FakeJournal(jobs=[{"job_id": "a", "kind": "train"}], recs={"a": {"job_id": "a"}})
    assert _get("/jobs", journal=j)[1] == {"jobs": [{"job_id": "a", "kind": "train"}]}
    assert _get("/jobs/a", journal=j) == (200, {"job_id": "a"}, "application/json")
    assert _get("/jobs/zzz", journal=j) == (404, {"error": "unknown job"}, "application/json")


def test_unknown_get_path_is_404():
    assert _get("/nope") == (404, {"error": "unknown path"}, "application/json")


def test_post_jobs_submits_and_unknown_engine_unload_404():
    assert _post("/jobs", jobs=FakeJobs(submit=(202, {"status": "queued"}))) == \
        ("json", 202, {"status": "queued"})
    # /control/unload for an unknown engine → 404 (no CONTROL_SECRET set in the test env).
    assert _post("/control/unload", raw_body=b'{"engine": "ghost"}') == \
        ("json", 404, {"error": "unknown engine 'ghost'"})


def test_post_job_cancel_maps_unknown_to_404():
    assert _post("/jobs/x/cancel", jobs=FakeJobs(cancel={"status": "unknown"}))[1] == 404
    assert _post("/jobs/x/cancel", jobs=FakeJobs(cancel={"status": "cancelled"}))[1] == 200


def test_unknown_post_path_is_404():
    assert _post("/nope") == ("json", 404, {"error": "unknown path"})


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
