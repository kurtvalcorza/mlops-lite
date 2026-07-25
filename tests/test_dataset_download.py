"""025 US3 (T609) — dataset byte download is PROXIED, and no presigned URL reaches the browser (FR-355).

Two halves, both offline (fake S3, no live stack):

  - the proxy works: `open_dataset_bytes` streams the exact pinned bytes in bounded chunks, closes the
    body even on an abandoned download, and reports absence as None (→ 404);
  - nothing leaks: `get_dataset` returns NO `download_url` — not even when a manifest written by a
    pre-025 build has one stored — because that URL was signed against the internal store endpoint
    (`garage:3900`), unresolvable from a browser AND a handed-out object-store capability. The BFF
    allowlist has the download route, without which the browser would get a BFF 404 even though the
    gateway route works (closes 021 FR-215).
"""
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import datasets as ds  # noqa: E402

DATA = b'{"a": 1}\n{"a": 2}\n{"a": 3}\n'


class _TrackedBody(io.BytesIO):
    def __init__(self, blob):
        super().__init__(blob)
        self.closed_by_caller = False

    def close(self):
        self.closed_by_caller = True
        super().close()


class FakeS3:
    """Minimal S3: a manifest + a data object, and a presign that must never be reached."""

    def __init__(self, manifest, data=DATA, missing=()):
        self.manifest, self.data, self.missing = manifest, data, set(missing)
        self.bodies, self.presigns = [], 0

    def get_object(self, Bucket, Key):
        if Key in self.missing:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if Key.endswith("manifest.json"):
            return {"Body": io.BytesIO(json.dumps(self.manifest).encode())}
        body = _TrackedBody(self.data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(self.data)}

    def generate_presigned_url(self, *a, **kw):
        self.presigns += 1
        return "http://garage:3900/datasets/ds/v1/data?X-Amz-Signature=leak"


def _wire(monkeypatch, s3):
    monkeypatch.setattr(ds, "_s3", lambda: s3)
    return s3


MANIFEST = {"name": "ds", "version": "abc123", "size_bytes": len(DATA), "sha256": "deadbeef",
            "format": "jsonl"}


# --- nothing leaks ---------------------------------------------------------------------------------

def test_manifest_carries_no_presigned_download_url(monkeypatch):
    s3 = _wire(monkeypatch, FakeS3(MANIFEST))
    m = ds.get_dataset("ds", "abc123")
    assert m["sha256"] == "deadbeef" and m["size_bytes"] == len(DATA)
    assert "download_url" not in m          # the FR-355 guarantee
    assert s3.presigns == 0                 # …and we never even asked S3 to sign one


def test_a_stored_presigned_url_is_stripped_not_relayed(monkeypatch):
    """A manifest written by a pre-025 build may have one persisted — never hand it to a caller."""
    stale = {**MANIFEST, "download_url": "http://garage:3900/…?X-Amz-Signature=leak"}
    _wire(monkeypatch, FakeS3(stale))
    assert "download_url" not in ds.get_dataset("ds", "abc123")


def test_no_data_page_response_contains_a_signature(monkeypatch):
    """Belt-and-braces over the whole serialized manifest: no signed-URL shrapnel of any kind."""
    _wire(monkeypatch, FakeS3({**MANIFEST, "download_url": "http://x?X-Amz-Signature=leak"}))
    blob = json.dumps(ds.get_dataset("ds", "abc123"))
    for marker in ("X-Amz-Signature", "X-Amz-Credential", "garage:3900", "AWS_SECRET"):
        assert marker not in blob, marker


# --- the proxy works ------------------------------------------------------------------------------

def test_download_streams_the_exact_pinned_bytes(monkeypatch):
    _wire(monkeypatch, FakeS3(MANIFEST))
    chunks, size = ds.open_dataset_bytes("ds", "abc123")
    assert size == len(DATA)
    assert b"".join(chunks) == DATA          # byte-exact, reassembled from the chunk iterator


def test_download_is_chunked_not_read_whole(monkeypatch):
    _wire(monkeypatch, FakeS3(MANIFEST))
    chunks, _ = ds.open_dataset_bytes("ds", "abc123", chunk_size=4)
    parts = list(chunks)
    assert len(parts) > 1 and all(len(p) <= 4 for p in parts)   # bounded memory for a large dataset
    assert b"".join(parts) == DATA


def test_abandoned_download_closes_the_body(monkeypatch):
    """A client disconnecting mid-download must not leak the streaming connection."""
    s3 = _wire(monkeypatch, FakeS3(MANIFEST))
    chunks, _ = ds.open_dataset_bytes("ds", "abc123", chunk_size=4)
    next(chunks)                              # start, then abandon
    chunks.close()
    assert s3.bodies[0].closed_by_caller is True


def test_missing_data_object_is_absence_not_an_error(monkeypatch):
    _wire(monkeypatch, FakeS3(MANIFEST, missing={"ds/abc123/data"}))
    assert ds.open_dataset_bytes("ds", "abc123") is None       # the route maps this to 404


# --- the BFF allowlist ----------------------------------------------------------------------------

def test_bff_allowlist_has_the_download_route():
    """Without this entry the BFF rejects the route before injecting the key, so the browser gets a
    404 even though the gateway endpoint works."""
    src = open(os.path.join(REPO, "ui", "lib", "gw-allowlist.ts")).read()
    assert re.search(r"pattern:\s*'datasets/:name/:version/download'", src), \
        "the byte-download route must be allowlisted in the BFF"


# --- the download advertises the version's REAL format (round 8) ---------------------------------
#
# The route hardcoded `.jsonl` + application/x-ndjson for every version, so a csv/parquet dataset
# downloaded through the console arrived mislabeled. Resolved from the manifest via a whitelist —
# `format` is stored verbatim and unvalidated at registration, so interpolating it into
# Content-Disposition would let a crafted value inject header parameters.

from app.routers import datasets as ds_router  # noqa: E402


def _download(monkeypatch, fmt):
    manifest = {**MANIFEST, "format": fmt}
    monkeypatch.setattr(ds_router.datasets, "_s3", lambda: FakeS3(manifest))
    return ds_router.download_dataset_version("ds", "abc123")


def test_download_content_type_and_extension_follow_the_manifest_format(monkeypatch):
    for fmt, ext, ctype in [("csv", ".csv", "text/csv"),
                            ("jsonl", ".jsonl", "application/x-ndjson"),
                            ("parquet", ".parquet", "application/vnd.apache.parquet"),
                            ("json", ".json", "application/json")]:
        r = _download(monkeypatch, fmt)
        assert r.media_type == ctype, fmt
        assert r.headers["content-disposition"].endswith(f'{ext}"'), fmt


def test_unknown_or_missing_format_degrades_to_a_generic_binary_download(monkeypatch):
    for fmt in (None, "", "weird-proprietary-thing"):
        r = _download(monkeypatch, fmt)
        assert r.media_type == "application/octet-stream"
        assert r.headers["content-disposition"].endswith('.bin"')


def test_format_is_whitelisted_not_interpolated_into_the_header(monkeypatch):
    """A quote in the stored format must not escape the quoted Content-Disposition parameter."""
    r = _download(monkeypatch, 'jsonl"; filename*=UTF-8\'\'evil.exe')
    cd = r.headers["content-disposition"]
    assert cd == 'attachment; filename="ds-abc123.bin"'      # fell back, nothing injected
    assert "evil.exe" not in cd and "filename*" not in cd


def test_dataset_name_is_sanitized_in_the_filename():
    """`name` was already interpolated before the format work and is equally unvalidated."""
    assert ds_router._safe_filename_stem('a"b; x=', "v1") == "a_b__x_-v1"
    assert ds_router._safe_filename_stem("", "") == "dataset"       # never a separators-only name
    assert ds_router._safe_filename_stem("..", "..") == "dataset"   # nor a path-ish one
    assert '"' not in ds_router._safe_filename_stem('""""', "v")


def test_download_still_streams_the_exact_bytes_through_the_route(monkeypatch):
    """The identity work must not disturb the byte proxy itself."""
    import asyncio
    r = _download(monkeypatch, "csv")

    async def _drain():
        return b"".join([c async for c in r.body_iterator])   # Starlette wraps the sync generator

    assert asyncio.run(_drain()) == DATA
    assert r.headers["content-length"] == str(len(DATA))


def test_ui_download_extension_mirrors_the_gateway_whitelist():
    src = open(os.path.join(REPO, "ui", "app", "data", "page.tsx")).read()
    assert "downloadExt(detail.format)" in src          # not a hardcoded .jsonl
    assert ".jsonl`}" not in src
    for fmt in ("csv", "parquet", "jsonl"):             # same formats the gateway maps
        assert f"{fmt}:" in src, fmt


def test_the_data_page_links_the_proxy_route_not_a_presigned_url():
    src = open(os.path.join(REPO, "ui", "app", "data", "page.tsx")).read()
    assert "/download`" in src and "/api/gw/datasets/" in src   # goes through the key-injecting BFF
    assert "detail.download_url" not in src                     # never renders a signed URL
    assert "download_url?" not in src                           # and the field is gone from the type


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
