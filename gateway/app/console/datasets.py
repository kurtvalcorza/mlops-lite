"""Datasets and artifacts (027 T752/T753 — data-model §12).

**The four integrity states are genuinely distinct and must not collapse.** This is the module's
whole reason for existing as more than a passthrough:

  * `verified` — a recorded checksum exists and was recomputed to match.
  * `verification-failed` — a recorded checksum exists and did **not** match. This is a
    data-integrity incident and surfaces in the attention panel; it does not sit quietly here.
  * `not-verified` — a checksum exists but was not recomputed for this response.
  * `verification-unavailable` — no checksum was ever recorded for this object.

Collapsing the last two into a single "unverified" would conflate *"we did not check"* with *"there
is nothing to check against"*. Those are materially different facts when an operator is deciding
whether to trust an artifact: the first is a button away from an answer, the second means no answer
exists and the object's integrity is simply unknowable.

**Verification is opt-in per request.** Rehashing multi-gigabyte objects on every page render is not
viable on this hardware, so the default is `not-verified` — honest about not having checked rather
than silently claiming it did.

**Bytes never flow through here.** `uri` is a *logical* reference. Downloads continue to use the
existing gateway-proxied dataset route, which validates against permitted prefixes before any
upstream call. No presigned URL is minted — 025 US3 removed them precisely because they were signed
against the internal store endpoint (unresolvable from a browser) and constituted a leaked
object-store capability.
"""

INTEGRITY_STATES = ("verified", "verification-failed", "not-verified", "verification-unavailable")

SCHEMA_STATES = ("known", "unknown", "mismatch")
VALIDATION_STATES = ("passed", "failed", "warning", "not-validated")

ARTIFACT_KINDS = ("model", "dataset", "eval-result", "capture", "other")


def integrity_of(*, recorded_digest=None, computed_digest=None, verified=False):
    """The four-state integrity verdict.

    Note the order of the checks: "was a checksum ever recorded" comes **first**, because without
    one there is nothing verification could have told us, and reporting `not-verified` there would
    imply a check is available that is not.
    """
    if not recorded_digest:
        return "verification-unavailable"
    if not verified:
        return "not-verified"
    return "verified" if computed_digest == recorded_digest else "verification-failed"


def dataset_version(record, *, referenced_by=None, validation=None):
    """One `DatasetVersion` (FR-419)."""
    return {
        "name": record.get("name"),
        "version": str(record.get("version")),
        # The platform's own content-addressed identity — not an object-store ETag, which changes
        # with multipart chunking and would make the same bytes look like different data.
        "contentDigest": record.get("digest") or record.get("content_digest"),
        "sizeBytes": record.get("size_bytes"),
        "objectCount": record.get("object_count"),
        "format": record.get("format"),
        "schemaStatus": record.get("schema_status") or "unknown",
        "validation": validation or {"status": "not-validated", "checks": None,
                                     "validatedAt": None},
        "createdAt": record.get("created_at"),
        # Empty lists here are honest: a dataset genuinely referenced by nothing is a real and
        # useful finding. `None` is reserved for the sources that did not answer, and the envelope
        # names those.
        "referencedBy": referenced_by or {"runIds": [], "modelVersions": []},
    }


def artifact(record, *, verified=False, computed_digest=None, present=None):
    """One `Artifact` (FR-420).

    `present` is an actual existence check, never inferred from the URI — the same rule the catalog
    follows, for the same reason: a registry row is a pointer, and pointers outlive what they point
    at.
    """
    return {
        # A LOGICAL reference. Never presigned, never credentialed.
        "uri": record.get("uri"),
        "kind": record.get("kind") if record.get("kind") in ARTIFACT_KINDS else "other",
        "sizeBytes": record.get("size_bytes"),
        "digest": record.get("digest"),
        "integrity": integrity_of(recorded_digest=record.get("digest"),
                                  computed_digest=computed_digest, verified=verified),
        "present": present,
        "observedAt": record.get("observed_at"),
    }
