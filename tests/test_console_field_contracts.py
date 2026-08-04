"""027 — the composers may only read field names their sources actually emit.

This suite exists because the same bug happened three times, and each time it was invisible.

A console composer reads `row.get("some_key")`. If the source never emits that key, the result is
`None` — and `None` is a **legitimate, meaningful value** everywhere in this console: it renders as
"unknown". So a wrong field name produces a screen that looks correct and is simply blank. No error,
no warning, no failing test, and nothing to notice unless you already knew the value existed.

The three that got through review by hand:

  * dataset digests read `digest`; the manifest writes `sha256` — every digest read "unknown";
  * the activity timeline read `created_at` and `modality` off registry versions, which emit
    neither — so the entire model half of the lifecycle timeline was silently absent, because the
    timeline drops undated events on purpose;
  * the review queue keyed on five columns the schema does not have — five of seven priority
    signals could never fire.

So the check is mechanical rather than a matter of care: parse what each composer reads, compare it
against what its source emits, and fail on a name that appears in one and not the other.
"""
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

CONSOLE = os.path.join(REPO, "gateway", "app", "console")


def _keys_read(path, function, variable):
    """Every literal key `variable.get("...")` inside `function`."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return {call.args[0].value
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                    and getattr(call.func.value, "id", None) == variable
                    and call.args and isinstance(call.args[0], ast.Constant)}
    raise AssertionError(f"{function} not found in {path}")


def _registry_version_keys():
    """What `registry.list_versions()` emits, plus what `sources.model_versions()` adds."""
    source = open(os.path.join(REPO, "gateway", "app", "registry.py"), encoding="utf-8").read()
    block = source[source.index("def list_versions"):source.index("def list_models")]
    emitted = set(re.findall(r'^\s+"(\w+)":', block, re.M))

    source = open(os.path.join(CONSOLE, "sources.py"), encoding="utf-8").read()
    block = source[source.index("def model_versions"):source.index("def unlabeled_count")]
    emitted |= set(re.findall(r'^\s+"(\w+)":', block, re.M))
    return emitted


#: Keys a composer may read that its source legitimately does not emit, each with the reason.
#: An entry here is a claim that the field arrives from a DIFFERENT caller — the catalog-backed
#: path supplies these — not a licence to read a name nothing produces.
ENRICHED = {
    # `attention_items` emits all nine kinds when a caller supplies these; the polled route cannot
    # afford them and declares so via `POLLED_ATTENTION_KINDS`.
    "artifactPresent": "supplied by the catalog projection, not the polled registry read",
    "gate": "supplied by the catalog projection, not the polled registry read",
    "signed": "optional override; falls back to tags['signature']",
    "promoted_at": "optional; falls back to created_at",
}


@pytest.mark.parametrize("function", ["attention_items", "activity_events", "summary_cards"])
def test_every_version_field_a_composer_reads_is_actually_emitted(function):
    """The check that would have caught `created_at` and `modality` the day they were written."""
    variable = {"attention_items": "version", "activity_events": "version",
                "summary_cards": "v"}[function]
    read = _keys_read(os.path.join(CONSOLE, "overview.py"), function, variable)
    emitted = _registry_version_keys()

    unexplained = sorted(read - emitted - set(ENRICHED))
    assert not unexplained, (
        f"{function} reads {unexplained} off a registry version, which never emits them — the "
        f"composer will silently see `None`, which this console renders as 'unknown'")


def test_the_activity_timeline_can_actually_produce_model_events():
    """The concrete consequence, asserted end to end rather than by field name.

    The timeline drops undated events deliberately — one shown at the wrong time is worse than one
    not shown — so a missing `created_at` removed every model event instead of misplacing it.
    """
    from gateway.app.console import overview

    version = {"name": "qwen", "version": "3", "created_at": 1_700_000_000.0, "serving": True,
               "modality": "text-generation"}
    events = overview.activity_events(versions=[version])
    kinds = {e["kind"] for e in events}
    assert "version-registered" in kinds, "a registered version must appear on the timeline"
    assert "version-promoted" in kinds, "so must a promotion"
    assert all(e["at"] is not None for e in events)


def test_a_version_with_no_timestamp_still_produces_no_dated_event():
    """The dropping behaviour itself is correct and stays: the fix was to supply the date, not to
    invent one."""
    from gateway.app.console import overview

    assert overview.activity_events(versions=[{"name": "m", "version": "1"}]) == []


def test_the_polled_attention_route_declares_which_kinds_it_checks():
    """A kind that cannot fire is indistinguishable from a kind that found nothing wrong — and
    'nothing needs attention' is the most consequential sentence this console prints."""
    from gateway.app.console import overview

    assert set(overview.POLLED_ATTENTION_KINDS) < set(overview.ATTENTION_KINDS)
    assert set(overview.ATTENTION_KINDS) - set(overview.POLLED_ATTENTION_KINDS) == {
        "missing-artifact", "evaluation-gate-failure"}


def test_the_composer_still_emits_every_kind_when_the_fields_are_supplied():
    """The vocabulary stays whole: the catalog-backed path can produce all nine."""
    from gateway.app.console import overview

    items = overview.attention_items(
        now="2026-07-31T09:00:00Z",
        agent={"engines": {"llm": "wedged"}, "interrupted_since_start": 1},
        admission={"records": [{"decision": "refused", "model_key": "q", "explanation": "x"}]},
        jobs=[{"job_id": "j", "state": "failed"}],
        drift=[{"model_name": "d", "max_psi": 0.4}],
        versions=[{"name": "m", "version": "1", "tags": {}, "artifactPresent": False,
                   "gate": {"verdict": "fail"}}],
        unlabeled=999, heartbeat_age_s=10_000)
    assert {i["kind"] for i in items} == set(overview.ATTENTION_KINDS)
