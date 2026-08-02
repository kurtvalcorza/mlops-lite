"""027 T753/T757/T758/T760/T768/T769 — integrity, alert honesty, credentials, and the deferrals.

Four guarantees, and three of them are guarantees about what the console must **not** say:

  * `not-verified` must not collapse into `verification-unavailable` — "we did not check" and
    "there is nothing to check against" are different facts (T753).
  * an alert must carry no delivery field, because this platform pages nobody and a field implying
    otherwise would stop an operator from paging themselves (T757).
  * an admin surface must never emit credential material, including inside a configuration URL
    (T758).

And one guarantee about the deferrals: US11 and US12 are specified in 027 but built in 028/029.
Deferral is not the absence of work — 027 owes each a proof that it holds, and that no half-built
affordance misleads anyone in the meantime (T768/T769).
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tests import _gwimport  # noqa: E402

with _gwimport.isolated_metrics():
    from gateway.app.console import datasets as datasets_mod  # noqa: E402
    from gateway.app.console import observability  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    yield from _gwimport.isolate_module_metrics()


# -- T753: the four integrity states ---------------------------------------------------------------

def test_not_verified_never_collapses_into_verification_unavailable():
    """FR-420, and the reason this module exists as more than a passthrough. 'We did not check' is a
    button away from an answer; 'no checksum was ever recorded' means no answer exists. An operator
    deciding whether to trust an artifact needs to know which one they have."""
    not_verified = datasets_mod.integrity_of(recorded_digest="abc123", verified=False)
    unavailable = datasets_mod.integrity_of(recorded_digest=None, verified=False)
    assert not_verified == "not-verified"
    assert unavailable == "verification-unavailable"
    assert not_verified != unavailable


def test_a_matching_recomputed_digest_is_verified():
    assert datasets_mod.integrity_of(recorded_digest="abc", computed_digest="abc",
                                     verified=True) == "verified"


def test_a_mismatching_recomputed_digest_is_a_verification_failure():
    """A data-integrity incident — it surfaces in the attention panel rather than sitting quietly in
    a detail view."""
    assert datasets_mod.integrity_of(recorded_digest="abc", computed_digest="def",
                                     verified=True) == "verification-failed"


def test_verification_unavailable_wins_over_not_verified_when_there_is_no_digest():
    """The order of the checks matters: with no recorded checksum there is nothing verification
    could have told us, and `not-verified` there would imply a check is available that is not."""
    assert datasets_mod.integrity_of(recorded_digest=None, computed_digest="anything",
                                     verified=True) == "verification-unavailable"


def test_every_integrity_verdict_is_in_the_declared_vocabulary():
    for recorded in (None, "abc"):
        for computed in (None, "abc", "def"):
            for verified in (True, False):
                assert datasets_mod.integrity_of(
                    recorded_digest=recorded, computed_digest=computed,
                    verified=verified) in datasets_mod.INTEGRITY_STATES


def test_an_artifact_uri_is_logical_and_never_presigned():
    """FR-421/422: no presigned URL is minted and no object-store credential reaches the browser.
    025 US3 removed presigned URLs precisely because they were signed against the internal store
    endpoint and constituted a leaked capability."""
    artifact = datasets_mod.artifact({"uri": "s3://models/qwen/3/model.gguf", "kind": "model"})
    assert artifact["uri"] == "s3://models/qwen/3/model.gguf"
    for forbidden in ("X-Amz-Signature", "Signature=", "AWSAccessKeyId", "?X-Amz"):
        assert forbidden not in (artifact["uri"] or "")


# -- T757: alert honesty ----------------------------------------------------------------------------

def test_an_alert_rule_carries_no_delivery_or_notification_field():
    """FR-424. There is no Alertmanager. A delivery field would invite the console to imply someone
    was told — and an operator who believes a page went out will not send one themselves."""
    rule = observability.alert_rule({
        "alert": "GpuWedged", "expr": "up == 0", "state": "firing",
        # A rules file that grew a delivery-shaped key must not leak it through.
        "notification": "pagerduty", "recipients": ["ops@example.com"], "acknowledged": True,
    })
    for field in observability.FORBIDDEN_ALERT_FIELDS:
        assert field not in rule, f"{field} leaked into the alert projection"


def test_the_alert_projection_copies_fields_explicitly_rather_than_spreading():
    """The mechanism behind the test above: a spread would silently carry through whatever a future
    rules file adds. This pins the field set rather than trusting the implementation's shape."""
    rule = observability.alert_rule({"alert": "X", "expr": "1", "state": "firing"})
    assert set(rule) == {"name", "severity", "expression", "state", "activeSince", "runbookUrl"}


def test_an_unreadable_rule_state_is_unknown_rather_than_inactive():
    """`unknown` is a first-class member of the vocabulary. A rule whose evaluation could not be
    read is not 'inactive', and rendering it as such would be the reassuring falsehood."""
    assert observability.alert_rule({"alert": "X"})["state"] == "unknown"
    assert observability.alert_rule({"alert": "X", "state": "nonsense"})["state"] == "unknown"


def test_the_surface_states_that_no_notification_was_sent():
    """quickstart §2.9. The absence of a delivery field is necessary but not sufficient — a reader
    seeing rule states and no disclaimer will assume the usual thing."""
    notice = observability.NO_DELIVERY_NOTICE.lower()
    assert "no notification" in notice
    assert "no alert delivery channel" in notice or "nobody is paged" in notice


# -- T757: the embed --------------------------------------------------------------------------------

def test_an_unembeddable_dashboard_still_carries_an_external_url():
    """FR-425: the fallback is structural, not an error path."""
    embed = observability.dashboard_embed(
        id="d", title="Platform", external_url="http://localhost:3001/d", embeddable=False,
        reason="frame policy")
    assert embed["externalUrl"] == "http://localhost:3001/d"
    assert embed["embeddable"] is False and embed["reason"] == "frame policy"


def test_an_unembeddable_dashboard_omits_embed_url_rather_than_emptying_it():
    """An empty frame source renders a blank rectangle instead of a working link."""
    embed = observability.dashboard_embed(
        id="d", title="Platform", external_url="http://x/d", embeddable=False)
    assert "embedUrl" not in embed


def test_embeddability_is_resolved_from_the_frame_policy_not_the_browser(monkeypatch):
    """Letting the browser discover it turns a configuration fact into a rendering accident: the
    user sees a blank rectangle and the console has no idea anything went wrong."""
    from gateway.app.console import console_helpers

    monkeypatch.delenv("CONSOLE_DASHBOARD_EMBED", raising=False)
    embeddable, reason = console_helpers.frame_policy_allows()
    assert embeddable is False and reason, "the default is closed, and it says why"

    monkeypatch.setenv("CONSOLE_DASHBOARD_EMBED", "1")
    assert console_helpers.frame_policy_allows()[0] is True


# -- T758: never credential material -----------------------------------------------------------------

def test_api_access_reports_only_whether_a_key_is_configured(monkeypatch):
    """FR-426. There is no code path here that reads the key's value, which is stronger than
    redaction: a redaction step can be forgotten, an absent read cannot leak."""
    monkeypatch.setenv("GATEWAY_API_KEYS", "super-secret-operator-key")
    access = observability.api_access()
    assert access == {"keyConfigured": True, "failClosed": True}
    assert "super-secret-operator-key" not in str(access)


def test_api_access_reports_fail_open_honestly(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    assert observability.api_access() == {"keyConfigured": False, "failClosed": False}


def test_an_integration_endpoint_is_a_host_identity_not_a_credentialed_url():
    """Configuration URLs routinely carry credentials, and an admin page is exactly where someone
    pastes a screenshot into a ticket."""
    integration = observability.integration(
        "database", endpoint="postgresql://mlops:hunter2@postgres:5432/mlops")
    assert "hunter2" not in integration["endpoint"]
    assert "mlops:" not in integration["endpoint"].split("://")[1]
    assert integration["endpoint"] == "postgresql://postgres:5432/mlops"


def test_a_credential_free_endpoint_is_left_alone():
    assert observability.integration(
        "agent", endpoint="http://127.0.0.1:8100")["endpoint"] == "http://127.0.0.1:8100"


# -- T756: bounded ranges ----------------------------------------------------------------------------

def test_a_range_query_is_bounded_server_side():
    """A console panel must not be able to issue an unbounded query. The bound lives on the server
    so a hand-written URL cannot exceed it either."""
    window, step = observability.bound_range(window_seconds=10 ** 9, step_seconds=1)
    assert window <= observability.MAX_WINDOW_S
    assert window / step <= observability.MAX_POINTS


def test_an_over_long_range_widens_the_step_rather_than_truncating_the_window():
    """An operator asking for a day of data wants the day. A coarser resolution answers their
    question; a silently shortened window answers a different one."""
    window, step = observability.bound_range(window_seconds=86400, step_seconds=1)
    assert window == 86400, "the window they asked for"
    assert step > 1, "at a resolution the store can serve"


def test_a_degraded_panel_carries_no_points_rather_than_zero_points():
    """A flat line at zero is a measurement claim, and an unreachable metrics store has not made
    it. The temptation is stronger here than elsewhere: an empty chart looks broken."""
    panel = observability.panel("request-rate", series=[{"label": "x", "points": [[0, 5]]}],
                                degraded=True)
    assert panel["series"] == [] and panel["degraded"] is True


# -- T768: the MVP 2 read-only guarantee --------------------------------------------------------------

CONSOLE_ROUTER = os.path.join(REPO, "gateway", "app", "routers", "console.py")


def test_no_console_route_mutates_state():
    """FR-435/436. The guarantee, asserted against the source rather than trusted: a future MVP 2
    write path must land through the sanctioned gated route in 028, not here by accident."""
    source = open(CONSOLE_ROUTER, encoding="utf-8").read()
    decorators = re.findall(r"@router\.(get|post|put|patch|delete)\(\"([^\"]+)\"", source)
    non_get = [(verb, path) for verb, path in decorators if verb != "get"]
    assert non_get == [("post", "/console/predictions/{prediction_id}/payload")], (
        "the payload reveal is the only non-GET, and it mutates nothing: " + str(non_get))


def test_the_console_modules_contain_no_write_statements():
    """The projection layer reads. A write here would bypass every gate the platform has, because
    nothing downstream of a read route expects one."""
    console_dir = os.path.join(REPO, "gateway", "app", "console")
    for name in sorted(os.listdir(console_dir)):
        if not name.endswith(".py"):
            continue
        source = open(os.path.join(console_dir, name), encoding="utf-8").read()
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER "):
            assert statement not in source.upper(), f"{name} contains a {statement.strip()}"


def test_the_payload_reveal_only_reads():
    """It is write-SHAPED, not a write. The distinction is the whole reason it was allowed."""
    source = open(CONSOLE_ROUTER, encoding="utf-8").read()
    reveal = source.split("async def console_reveal_payload")[1].split("\n@router")[0]
    for statement in ("INSERT", "UPDATE", "DELETE", "put_object", "delete_object"):
        assert statement not in reveal, f"the reveal path contains {statement}"


# -- T769: MVP 3 affordances are inert and labelled ---------------------------------------------------

BANNER = os.path.join(REPO, "ui", "components", "ConflictBanner.tsx")


def test_the_reconcile_affordance_is_inert_and_says_so():
    """FR-437. An affordance that looks actionable but does nothing is worse than its absence — it
    teaches operators that the console lies, and that lesson generalizes to everything else on the
    screen."""
    source = open(BANNER, encoding="utf-8").read()
    assert "reconcile" in source, "the eventual answer is named rather than hidden"
    assert "not available in this release" in source, "and it is labelled as unavailable"
    # Not a button, not a link — nothing that invites a click.
    reconcile_line = next(line for line in source.splitlines() if "reconcile (" in line)
    assert "<button" not in reconcile_line and "href" not in reconcile_line


def test_no_suggestion_is_auto_applied_anywhere_in_the_console():
    """FR-437: MVP 3 owns automated reconciliation and auto-acceptance. 027 surfaces neither."""
    import glob

    for path in glob.glob(os.path.join(REPO, "ui", "app", "**", "*.tsx"), recursive=True) + \
            glob.glob(os.path.join(REPO, "ui", "components", "**", "*.tsx"), recursive=True):
        source = open(path, encoding="utf-8").read()
        for forbidden in ("autoAccept", "auto_accept", "autoApply", "auto_apply",
                          "autoReconcile", "auto_reconcile"):
            assert forbidden not in source, f"{os.path.basename(path)} contains {forbidden}"


# -- T764: the dependency floor (SC-198 / FR-434) -----------------------------------------------------

def test_the_console_adds_no_runtime_dependency():
    """SC-198. Six chart shapes are not worth a charting library, and Principle III applied to the
    console means owning them is cheaper than owning the integration."""
    manifest = json.load(open(os.path.join(REPO, "ui", "package.json"), encoding="utf-8"))
    assert set(manifest["dependencies"]) == {"next", "react", "react-dom"}


def test_no_broker_scheduler_or_analytics_store_was_introduced():
    """FR-434: 027 is a read layer. A new backing service here would mean the console had grown a
    system of record, which is the opposite of what a projection is for."""
    import glob

    compose = open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8").read().lower()
    for service in ("kafka", "rabbitmq", "redis", "airflow", "clickhouse", "elasticsearch",
                    "celery"):
        assert service not in compose, f"{service} appeared in the compose model"

    # And no new Python runtime dependency for the console's own modules.
    for path in glob.glob(os.path.join(REPO, "gateway", "app", "console", "*.py")):
        source = open(path, encoding="utf-8").read()
        for forbidden in ("import pandas", "import numpy", "import scipy", "import optuna",
                          "import plotly", "import evidently"):
            assert forbidden not in source, f"{os.path.basename(path)} has {forbidden}"


def test_the_chart_primitives_import_nothing_but_react():
    """The one place a charting dependency would sneak in."""
    source = open(os.path.join(REPO, "ui", "lib", "charts", "index.tsx"), encoding="utf-8").read()
    imports = re.findall(r"^import .*? from '([^']+)';", source, re.MULTILINE)
    assert imports == ["react"], imports


# -- the projections read the field names the sources actually emit -------------------------------
#
# A class of bug this surface makes unusually quiet: everywhere else in the console, `null` renders
# as "unknown", which is a legitimate value. So a projection reading a field name its source does
# not emit produces a screen that looks correct and is simply blank — no error, no warning, and
# nothing to notice unless you happen to know the value exists. These pin the mappings against the
# vocabulary of the modules that produce them.

def test_a_dataset_version_reads_the_manifests_own_digest_field():
    """`gateway/app/datasets.py` writes `sha256`. Reading only a generic `digest` reported every
    dataset's digest as unknown."""
    manifest = {"name": "iris", "version": "1", "sha256": "abc123", "size_bytes": 4096,
                "format": "csv"}
    row = datasets_mod.dataset_version(manifest)
    assert row["contentDigest"] == "abc123"
    assert row["sizeBytes"] == 4096 and row["format"] == "csv"


def test_a_dataset_version_survives_a_manifest_that_only_has_a_version():
    """`_versions()` falls back to `{"version": ver}` when a manifest cannot be read. That row must
    still render, with its unknowns honest rather than absent."""
    row = datasets_mod.dataset_version({"name": "iris", "version": "2"})
    assert row["version"] == "2" and row["contentDigest"] is None
    assert row["validation"]["status"] == "not-validated"
    assert row["referencedBy"] == {"runIds": [], "modelVersions": []}


# -- the alert rules are actually found -------------------------------------------------------------

def test_the_shipped_alert_rules_are_discovered():
    """The reader globbed `monitoring/**/*rules*.y*ml`, which matched nothing: the rules are at
    `infra/prometheus/rules/mlops-lite.yml` — wrong directory, and the filename contains no "rules"
    either, so the right directory would have missed it too.

    It returned `[]` **successfully**, so the console reported "no rules configured" with a clean
    observation timestamp instead of degrading. A confident empty list is worse than an error here:
    nothing about it looks wrong, and this is the surface whose entire premise is that it does not
    claim things it cannot see.
    """
    from gateway.app.console import sources

    rules = sources.alert_rules()
    assert rules, "the shipped rule set must be found"
    names = {r["name"] for r in rules}
    assert "WedgedEngine" in names, f"expected the 023 US7 rules, got {sorted(names)}"


def test_every_discovered_rule_carries_its_runbook():
    """The runbook link is the honest substitute for having no notification channel: it says what to
    DO without claiming anyone was told. The parser accepted only `runbook_url:` while the shipped
    rules write `runbook:`, so every link was dropped."""
    from gateway.app.console import sources

    for rule in sources.alert_rules():
        url = (rule.get("annotations") or {}).get("runbook_url")
        assert url, f"{rule['name']} has no runbook"
        assert not url.startswith('"'), "YAML quoting must be stripped, or the href resolves nowhere"


def test_an_unreadable_rules_directory_degrades_rather_than_reporting_no_rules(monkeypatch):
    """The whole point of the fix: 'we could not read the rules' and 'there are no rules' are
    different facts, and only the second is safe to render as an empty list."""
    from gateway.app.console import sources

    monkeypatch.setattr(sources, "RULES_DIR", "/nonexistent/rules")
    with pytest.raises(Exception):
        sources.alert_rules()
