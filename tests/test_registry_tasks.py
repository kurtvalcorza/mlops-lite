"""009 US1 registry task/serving-engine routing test (T160 → SC-049, SC-051).

Exercises the registry routing metadata through the gateway:
  - register a model version with `task` + `serving_engine` version tags and confirm both are
    surfaced by `/models/{name}` (FR-074),
  - promote it and confirm it appears in `/serving/tasks` with the resolved task + engine (FR-075),
  - register a LoRA adapter carrying a `base_model` tag and confirm it **inherits the base's
    serving_engine** in `/serving/tasks` (FR-076 / SC-051),
  - a serving version with NO `task` tag is reported as task=null (legacy tolerance → the UI's "no
    renderer" placeholder, FR-077).

Requires the stack up + a key. Robust to re-runs (captures the version numbers MLflow assigns).
Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
# Unique-ish names so re-runs don't collide with the seeded models; the registry keeps history.
BASE = os.getenv("TASKS_TEST_MODEL", "test-tasks-llm")
ADAPTER = f"{BASE}-lora"
LEGACY = os.getenv("TASKS_TEST_LEGACY", "test-tasks-legacy")


def _req(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _register(name, source, tags):
    s, body = _req("POST", "/models", {"name": name, "source": source, "tags": tags})
    if s != 201:
        print(f"[FAIL] register {name} -> {s} {body}")
        return None
    return body["version"]


def _promote(name, version):
    s, _ = _req("POST", f"/models/{name}/promote", {"version": version})
    return s == 200


def _tasks_entry(model):
    s, body = _req("GET", "/serving/tasks")
    if s != 200:
        print(f"[FAIL] /serving/tasks -> {s} {body}")
        return None, None
    for t in body.get("tasks", []):
        if t.get("model") == model:
            return body, t
    return body, None


def main() -> int:
    # 1. Register a base LLM version with task + serving_engine tags; confirm /models surfaces them.
    v = _register(BASE, "s3://models/test/base", {"task": "text-generation", "serving_engine": "llama.cpp"})
    if v is None:
        return 1
    s, model = _req("GET", f"/models/{BASE}")
    tags = next((mv["tags"] for mv in model.get("versions", []) if mv["version"] == v), {})
    if tags.get("task") != "text-generation" or tags.get("serving_engine") != "llama.cpp":
        print(f"[FAIL] /models/{BASE} did not surface task/serving_engine tags: {tags}")
        return 1
    print(f"[OK] registered {BASE} v{v} with task=text-generation, serving_engine=llama.cpp (surfaced)")

    # 2. Promote and confirm it appears in /serving/tasks with the resolved task + engine.
    if not _promote(BASE, v):
        print(f"[FAIL] promote {BASE} v{v}")
        return 1
    _, entry = _tasks_entry(BASE)
    if not entry or entry.get("task") != "text-generation" or entry.get("serving_engine") != "llama.cpp":
        print(f"[FAIL] /serving/tasks entry for {BASE}: {entry}")
        return 1
    print(f"[OK] /serving/tasks resolves {BASE} → task=text-generation, engine=llama.cpp")

    # 3. LoRA adapter inherits the base's serving_engine (FR-076 / SC-051): tag base_model only, NO
    #    serving_engine of its own, and confirm the resolved engine is the base's.
    va = _register(ADAPTER, "s3://models/test/adapter",
                   {"task": "text-generation", "base_model": BASE})
    if va is None:
        return 1
    if not _promote(ADAPTER, va):
        print(f"[FAIL] promote {ADAPTER} v{va}")
        return 1
    _, aentry = _tasks_entry(ADAPTER)
    if not aentry or aentry.get("serving_engine") != "llama.cpp":
        print(f"[FAIL] LoRA adapter did not inherit base engine: {aentry}")
        return 1
    print(f"[OK] LoRA adapter {ADAPTER} inherits base serving_engine=llama.cpp (no own engine tag)")

    # 4. Legacy tolerance (FR-077): a serving version with no task tag reports task=null.
    vl = _register(LEGACY, "s3://models/test/legacy", {"kind": "legacy"})
    if vl is None:
        return 1
    if not _promote(LEGACY, vl):
        print(f"[FAIL] promote {LEGACY} v{vl}")
        return 1
    _, lentry = _tasks_entry(LEGACY)
    if not lentry or lentry.get("task") is not None:
        print(f"[FAIL] legacy untagged version should report task=null: {lentry}")
        return 1
    print(f"[OK] legacy untagged {LEGACY} reports task=null (→ UI 'no renderer' placeholder)")

    print("\nT160 PASS — registry task/serving_engine routing + LoRA inheritance + legacy tolerance")
    return 0


def test_registry_tasks(require_gateway, require_key):
    """Pytest wrapper (005 US5): skip if the stack is down / no key."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
