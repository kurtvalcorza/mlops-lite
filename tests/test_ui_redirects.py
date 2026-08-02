"""027 T700 — every retired 021 path resolves (FR-364 / SC-186).

One case per row of `specs/027-unified-lifecycle-console/data-model.md` §10. The property under test
is that **nothing 404s**: a console that renames its areas and leaves the old URLs dead breaks every
bookmark, every runbook link, and every reference in an incident write-up — and it breaks them at
exactly the moment someone is following a link under pressure.

Checked against the route sources rather than a running server, so the guarantee holds in the offline
suite. `tests/test_ui_smoke.py` exercises the same paths against a live console when one is up.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

APP = os.path.join(REPO, "ui", "app")

#: data-model.md §10, verbatim. `/models` and `/training` are kept rather than redirected — their
#: 021 names survived the IA change because they were already areas of concern, not loop stages.
REDIRECTS = {
    "/serving": "/deployments",
    "/data": "/datasets",
    "/monitoring": "/observability",
    "/monitor": "/observability",
    "/retraining": "/evaluations/drift",
    "/infer": "/inference",
    "/runs": "/training",
    "/health": "/observability/health",
}

KEPT = ["/models", "/training"]

#: The ten areas. Every one must have a route, or the nav renders a link to nothing.
AREAS = ["overview", "models", "training", "evaluations", "deployments", "inference",
         "datasets", "runtime", "observability", "administration"]


def _page(path: str) -> str:
    """The route source for a console path, or '' when the route does not exist."""
    candidate = os.path.join(APP, path.strip("/"), "page.tsx")
    return open(candidate, encoding="utf-8").read() if os.path.isfile(candidate) else ""


@pytest.mark.parametrize("old,new", sorted(REDIRECTS.items()))
def test_every_retired_path_resolves_to_its_successor(old, new):
    source = _page(old)
    assert source, f"{old} has no route — a retired path must resolve, not 404"
    assert "redirect(" in source, f"{old} exists but does not redirect"
    assert f"redirect('{new}')" in source, f"{old} does not redirect to {new}"


@pytest.mark.parametrize("path", KEPT)
def test_kept_paths_still_render_rather_than_redirect(path):
    """`/models` and `/training` were already areas of concern; renaming them would have been churn."""
    source = _page(path)
    assert source and "redirect(" not in source, f"{path} should still be a real page"


@pytest.mark.parametrize("area", AREAS)
def test_every_area_has_a_route(area):
    assert _page("/" + area), f"the nav links to /{area}, which has no route"


def test_the_root_lands_on_overview():
    """021 landed on `/serving` — right when the IA *was* the loop, wrong now."""
    assert "redirect('/overview')" in _page("/")


def test_the_probe_endpoints_are_not_redirected():
    """`/healthz` and `/readyz` are probes, not navigation. Moving them would break liveness checks
    that have nothing to do with the console's IA."""
    for probe in ("healthz", "readyz"):
        route = os.path.join(APP, probe, "route.ts")
        assert os.path.isfile(route), f"/{probe} must remain a route handler"
        assert "redirect(" not in open(route, encoding="utf-8").read()


def test_no_retired_path_redirects_to_another_redirect():
    """A redirect chain is a slower 404: each hop is a chance to land somewhere unintended, and the
    second hop is invisible in a bookmark."""
    for old, new in REDIRECTS.items():
        target = _page(new)
        assert target, f"{old} redirects to {new}, which does not exist"
        assert "redirect(" not in target, f"{old} -> {new} -> another redirect (a chain)"


def test_the_area_list_and_the_routes_agree():
    """The nav is generated from `AREAS`; a slug there with no route is a link to nothing."""
    source = open(os.path.join(REPO, "ui", "lib", "areas.ts"), encoding="utf-8").read()
    slugs = re.findall(r"slug:\s*'([a-z-]+)'", source)
    top_level = [s for s in slugs if s in AREAS]
    assert set(top_level) == set(AREAS), f"areas.ts and the expected ten disagree: {set(top_level) ^ set(AREAS)}"


def test_the_redirect_map_in_code_matches_the_data_model():
    """`areas.ts` publishes the same map this test pins, so the two cannot drift apart silently."""
    source = open(os.path.join(REPO, "ui", "lib", "areas.ts"), encoding="utf-8").read()
    declared = dict(re.findall(r"'(/[a-z]+)':\s*'(/[a-z/]+)'", source))
    for old, new in REDIRECTS.items():
        assert declared.get(old) == new, f"areas.ts maps {old} -> {declared.get(old)}, expected {new}"


def test_no_loop_nav_remains_in_the_shell():
    """The loop nav encoded ORDER with directional connectors; ten areas of concern have none, and
    keeping the arrows would make them mean nothing."""
    layout = open(os.path.join(APP, "layout.tsx"), encoding="utf-8").read()
    assert "LoopNav" not in layout and "AreaNav" in layout


def test_no_area_is_named_after_a_backing_service():
    """FR-365: an operator should not have to know which process answers a question in order to find
    where to ask it."""
    source = open(os.path.join(REPO, "ui", "lib", "areas.ts"), encoding="utf-8").read()
    labels = re.findall(r"label:\s*'([^']+)'", source)
    for forbidden in ("agent", "mlflow", "postgres", "garage", "prometheus", "gateway",
                      "supervisor", "bentoml", "llama"):
        for label in labels:
            assert forbidden not in label.lower(), f"area {label!r} is named after a service"
