"""Observability and administration (027 T756/T757/T758 — data-model §13).

Three rules, each protecting against a different way an interface can imply capability it lacks.

**The alert honesty rule (FR-424) is the sharpest.** `AlertRule` carries **no** delivery,
notification, recipient, or acknowledgement field, and none may be added. This platform has no
Alertmanager and no notification channel — 023 US7 shipped rules deliberately without one. A
delivery field would invite the interface to imply that someone was told, which is the same
fake-semantics failure as an admission queue over a path that never queues, and considerably more
dangerous: an operator who believes a page went out will not send one.

**Ranges are bounded server-side** (FR-423). A console panel must not be able to issue an unbounded
query against the metrics store; the bound lives here rather than in the client so a hand-written
URL cannot exceed it either.

**Embeddability is resolved server-side** (FR-425), from the configured frame policy rather than
discovered by the browser failing to render a frame. `externalUrl` is always populated, so the
fallback is a designed state and not an error path.

**No credential material leaves these routes** (FR-426). `apiAccess` reports *whether* a key is
configured and whether the gateway is fail-closed. `integrations[].endpoint` is a host identity,
never a credentialed URL.
"""
import os

#: `MetricPanel.key`, data-model §13 — the curated set (FR-423). Curated rather than "every metric
#: the store has": a panel wall an operator has to search is a worse answer than eleven panels that
#: answer the questions this platform actually raises.
PANEL_KEYS = ("request-rate", "error-rate", "latency-percentiles", "active-jobs", "queue-depth",
              "gpu-utilization", "gpu-vram", "engine-restarts", "tracking-health",
              "objectstore-health", "database-health")

#: `AlertRule.state`. `unknown` is a first-class member: a rule whose evaluation could not be read
#: is not "inactive", and rendering it as such would be the reassuring falsehood.
ALERT_STATES = ("inactive", "pending", "firing", "unknown")

#: Bounds for `GET /console/metrics/series`. Server-side, so a hand-written URL cannot exceed them.
MAX_WINDOW_S = 24 * 3600
MIN_STEP_S = 15
MAX_POINTS = 1500

#: Fields that must never appear on an alert. Asserted in the test suite as well — the ban is worth
#: more as an executable check than as a comment, because the field would be added by someone who
#: never read this docstring.
FORBIDDEN_ALERT_FIELDS = ("delivery", "notification", "notified", "recipient", "recipients",
                          "acknowledged", "acknowledgement", "channel", "webhook", "email",
                          "silenced")


def bound_range(*, window_seconds, step_seconds):
    """Clamp a range query. Returns `(window, step)`.

    The step is widened rather than the window truncated when a request would produce too many
    points: an operator asking for a day of data wants the day, and a coarser resolution answers
    their question where a silently shortened window would answer a different one.
    """
    window = max(MIN_STEP_S, min(int(window_seconds or 3600), MAX_WINDOW_S))
    step = max(MIN_STEP_S, int(step_seconds or 60))
    if window / step > MAX_POINTS:
        step = int(window / MAX_POINTS) + 1
    return window, step


def panel(key, *, series=None, unit=None, window_seconds=3600, observed_at=None, degraded=False):
    """One `MetricPanel`.

    A degraded panel carries **no points** rather than zero points. A flat line at zero is a
    measurement claim — "the request rate was zero" — and an unreachable metrics store has not made
    it.
    """
    return {
        "key": key,
        "series": [] if degraded else (series or []),
        "unit": unit,
        "windowSeconds": window_seconds,
        "observedAt": observed_at,
        "degraded": bool(degraded),
    }


def alert_rule(rule):
    """One `AlertRule` — rule state only.

    Every field is copied explicitly rather than by spreading the source dict. That is deliberate:
    a spread would silently carry through any delivery-shaped field a future rules file happens to
    add, which is exactly the leak FR-424 forbids.
    """
    state = str(rule.get("state") or "unknown").lower()
    return {
        "name": rule.get("name") or rule.get("alert"),
        "severity": (rule.get("labels") or {}).get("severity") or rule.get("severity"),
        "expression": rule.get("expr") or rule.get("expression"),
        "state": state if state in ALERT_STATES else "unknown",
        "activeSince": rule.get("activeAt") or rule.get("active_since"),
        # The 023 US7 runbooks. A link to what to DO is the honest substitute for a notification
        # channel: it does not claim anyone was told, and it is what the operator needs once they
        # are here reading the rule.
        "runbookUrl": (rule.get("annotations") or {}).get("runbook_url") or rule.get("runbookUrl"),
    }


#: Stated on the surface next to the rules. The platform not having a notification channel is a
#: fact about the deployment, and a surface that showed rule state without it would leave every
#: reader to assume the usual thing.
NO_DELIVERY_NOTICE = (
    "No notification was sent. This platform has no alert delivery channel — these are rule states "
    "only, and nobody is paged when one fires.")


def dashboard_embed(*, id, title, external_url, embeddable, reason=None, embed_url=None):
    """One `DashboardEmbed` (FR-425).

    `externalUrl` is always present so the fallback is structural. `embedUrl` is **omitted** when
    embedding is unavailable, rather than present-and-empty — an empty frame source is how a page
    renders a blank rectangle instead of a working link.
    """
    embed = {"id": id, "title": title, "externalUrl": external_url,
             "embeddable": bool(embeddable), "reason": reason}
    if embeddable:
        embed["embedUrl"] = embed_url or external_url
    # The embed carries no administrative controls and is labelled external by the surface: the
    # dashboard tool runs anonymous and read-only behind a CSP scoped to the console origin, and
    # presenting it as a native surface would misdescribe both its authority and its trust boundary.
    return embed


def api_access():
    """Whether a key is configured and whether the gateway is fail-closed — **never** the key.

    Returns booleans only. There is no code path here that reads the key's value, which is stronger
    than a redaction step: a redaction can be forgotten, an absent read cannot leak.
    """
    keys = os.getenv("GATEWAY_API_KEYS") or os.getenv("GATEWAY_API_KEY") or ""
    return {"keyConfigured": bool(keys.strip()), "failClosed": bool(keys.strip())}


def integration(name, *, endpoint=None, reachable=None, version=None):
    """One backing service. `endpoint` is a **host identity**, never a credentialed URL."""
    return {"name": name, "endpoint": _strip_credentials(endpoint), "reachable": reachable,
            "version": version}


def _strip_credentials(url):
    """Remove any `user:password@` component before the value ever leaves the process.

    Configuration URLs routinely carry credentials, and an admin page is exactly where someone would
    paste one into a screenshot.
    """
    if not url or "@" not in url:
        return url
    scheme, _, rest = str(url).partition("://")
    if not rest:
        return url
    _credentials, _, host = rest.rpartition("@")
    return f"{scheme}://{host}" if scheme else host
