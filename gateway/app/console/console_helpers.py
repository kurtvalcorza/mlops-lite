"""Frame-policy resolution for dashboard embeds (027 T757 — FR-425).

Separate from `observability.py` because it is the one piece of that surface that reads deployment
configuration rather than shaping data, and mixing the two would make the pure part untestable
without an environment.

Resolved **server-side**. The alternative — letting the browser try the frame and fail — turns a
configuration fact into a rendering accident: the user sees a blank rectangle, the console has no
idea anything went wrong, and there is no way to offer the external link that would have worked.
"""
import os


def frame_policy_allows():
    """`(embeddable, reason)`.

    Default **false**. 004 US1 set the console's CSP to `frame-ancestors 'none'`, and the dashboard
    tool ships with its own framing restrictions; assuming embedding works would make the fallback
    an error path instead of the designed state it is.
    """
    allowed = os.getenv("CONSOLE_DASHBOARD_EMBED", "0").lower() in ("1", "true", "yes", "on")
    if allowed:
        return True, None
    return False, ("embedding is disabled by the frame policy — the dashboard opens externally, "
                   "where it runs anonymous and read-only")
