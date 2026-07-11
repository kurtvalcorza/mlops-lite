"""In-memory stand-ins for the 022 serving-LLM seams (research R8 — offline, fake registry/store).

`FakeRegistry` covers exactly the duck-typed MLflow client surface the resolution walk touches
(`get_model_version_by_alias` / `get_model_version` / `search_model_versions` /
`search_registered_models` / tag + alias writes), with the not-found shape `llmresolve` classifies
(`error_code == "RESOURCE_DOES_NOT_EXIST"`). `FakeLLMStore` mirrors `platformlib.store`'s
serving-LLM pointer surface, StoreError included, so `hostagent.serving_llm.active_model_name` and
the gateway pointer accessors run the real code paths with only the SQL/socket faked (the same
stance as tests/_agentstore.py).
"""
from platformlib import store as _store

try:  # the gateway registry code catches MlflowException; the agent code duck-types error_code —
    # subclassing the real exception satisfies BOTH consumers of this one fake.
    from mlflow.exceptions import MlflowException as _NotFoundBase
except Exception:  # noqa: BLE001 — mlflow-less interpreter: agent-side tests still work
    _NotFoundBase = Exception


class NotFoundError(_NotFoundBase):
    def __init__(self, msg):
        Exception.__init__(self, msg)  # skip MlflowException.__init__ (proto error-code plumbing)
        self.error_code = "RESOURCE_DOES_NOT_EXIST"


class BadNameError(_NotFoundBase):
    """MLflow rejects a model NAME containing '/' or ':' with INVALID_PARAMETER_VALUE — a DIFFERENT
    code from RESOURCE_DOES_NOT_EXIST, so `llmresolve._is_not_found` does NOT swallow it. The raw HF
    base id an adapter stamps (`Qwen/Qwen2.5-0.5B-Instruct`) hits exactly this on real MLflow; the
    fake now models it so resolve_base_version's slash-guard is regression-covered (on-HW finding)."""
    def __init__(self, name):
        Exception.__init__(self, f"Invalid model name '{name}'. Names cannot contain '/' or ':'.")
        self.error_code = "INVALID_PARAMETER_VALUE"


def _check_name(name):
    if "/" in str(name) or ":" in str(name):
        raise BadNameError(name)


class FakeMV:
    """A model-version row (the attribute surface mlflow's ModelVersion exposes to our code)."""

    def __init__(self, name, version, source, tags=None, run_id=None):
        self.name, self.version = name, str(version)
        self.source, self.tags, self.run_id = source, dict(tags or {}), run_id
        self.status = "READY"


class FakeRM:
    """A registered-model row for search_registered_models (name + aliases)."""

    def __init__(self, name, aliases):
        self.name, self.aliases = name, dict(aliases or {})


class FakeRegistry:
    def __init__(self):
        self.versions = []   # [FakeMV]
        self.aliases = {}    # model name -> the @serving version (str)
        self.tag_writes = []  # (name, version, key, value) — asserts non-clobber/idempotency

    def add(self, name, version, source, tags=None, serving=False) -> FakeMV:
        mv = FakeMV(name, version, source, tags)
        self.versions.append(mv)
        if serving:
            self.aliases[name] = str(version)
        return mv

    # -- the duck-typed client surface ------------------------------------------------------------
    def get_model_version_by_alias(self, name, alias):
        _check_name(name)  # MLflow validates the model name first (rejects '/' and ':')
        v = self.aliases.get(name)
        if alias != "serving" or v is None:
            raise NotFoundError(f"RESOURCE_DOES_NOT_EXIST: no @{alias} for {name}")
        return self.get_model_version(name, v)

    def get_model_version(self, name, version):
        _check_name(name)
        for mv in self.versions:
            if mv.name == name and mv.version == str(version):
                return mv
        raise NotFoundError(f"RESOURCE_DOES_NOT_EXIST: {name} v{version}")

    def search_model_versions(self, flt):
        if flt.startswith("name='") and flt.endswith("'"):
            val = flt[len("name='"):-1].replace("''", "'")
            return [mv for mv in self.versions if mv.name == val]
        if flt.startswith("tags."):
            key, _, val = flt[len("tags."):].partition("=")
            val = val.strip("'").replace("''", "'")
            return [mv for mv in self.versions if mv.tags.get(key) == val]
        raise ValueError(f"fake registry cannot parse filter {flt!r}")

    def search_registered_models(self):
        names = sorted({mv.name for mv in self.versions})
        return [FakeRM(n, {"serving": self.aliases[n]} if n in self.aliases else {})
                for n in names]

    def set_model_version_tag(self, name, version, key, value):
        self.get_model_version(name, version).tags[key] = str(value)
        self.tag_writes.append((name, str(version), key, str(value)))

    def set_registered_model_alias(self, name, alias, version):
        self.aliases[name] = str(version)

    def create_registered_model(self, name):
        return FakeRM(name, {})

    def create_model_version(self, name, source, run_id=None, tags=None):
        nxt = max([int(mv.version) for mv in self.versions if mv.name == name] or [0]) + 1
        return self.add(name, nxt, source, tags)


class FakeLLMStore:
    """platformlib.store stand-in for the ActiveServingLLM pointer surface (T461)."""

    StoreError = _store.StoreError

    def __init__(self):
        self.row = None         # the singleton pointer row, or None (unset ⇒ default base)
        self.fail = False       # flip on to simulate a store outage
        self.writes = 0
        self.bootstrapped = 0   # how many times bootstrap() ran (asserts the write bootstraps DDL)

    def connect(self, dsn=None, *, autocommit=True):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        return self

    def close(self):
        pass

    def bootstrap(self, conn=None):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        self.bootstrapped += 1

    def get_serving_llm(self, conn):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        return dict(self.row) if self.row else None

    def set_serving_llm(self, conn, model_name, selected_at, selected_by):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        self.row = {"model_name": model_name, "selected_at": selected_at,
                    "selected_by": selected_by}
        self.writes += 1

    def clear_serving_llm(self, conn):
        self.row = None
