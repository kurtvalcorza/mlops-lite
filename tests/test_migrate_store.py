"""020 T403 — scripts/migrate_store.py (the MinIO→Garage mirror, contracts/store-migration.md).

Two-FakeS3 seams (house style — the fakes model the boto3 client surface the tool actually
touches, including >1,000-key pagination and 404-shaped ClientErrors), so every guarantee is
pinned offline: full copy, idempotent re-run (`copied == 0`, SC-127), reverse direction,
MigrationReport shape, pagination past 1,000 keys, size-mismatch re-copy, and count+bytes
(never ETag) parity.
"""
import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))

import migrate_store as ms  # noqa: E402


class FakeClientError(Exception):
    def __init__(self, code="404"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Meta:
    endpoint_url = "http://fake:0"


class FakeS3:
    """In-memory two-sided seam: buckets of {key: (bytes, content_type)}. list_objects_v2
    paginates at `page_size` (default 1,000, shrinkable so the pagination test stays fast);
    upload_fileobj consumes a stream like the real client."""

    meta = _Meta()

    def __init__(self, buckets=("datasets", "models", "results", "mlflow"), page_size=1000):
        self.buckets = {b: {} for b in buckets}
        self.page_size = page_size

    # -- seeding helper (tests only) ------------------------------------------------------------
    def seed(self, bucket, key, body, content_type="application/octet-stream"):
        self.buckets[bucket][key] = (body, content_type)

    # -- the boto3 surface migrate_store touches -------------------------------------------------
    def list_objects_v2(self, Bucket, ContinuationToken=None, **kw):
        if Bucket not in self.buckets:
            raise FakeClientError("NoSuchBucket")
        keys = sorted(self.buckets[Bucket])
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start:start + self.page_size]
        out = {"Contents": [{"Key": k, "Size": len(self.buckets[Bucket][k][0])} for k in page]}
        if start + self.page_size < len(keys):
            out["IsTruncated"] = True
            out["NextContinuationToken"] = str(start + self.page_size)
        return out

    def get_object(self, Bucket, Key):
        if Key not in self.buckets.get(Bucket, {}):
            raise FakeClientError("NoSuchKey")
        body, ct = self.buckets[Bucket][Key]
        # ContentType rides on the GET (like real boto3) — the mirror reads it here, no HEAD.
        return {"Body": io.BytesIO(body), "ContentLength": len(body), "ContentType": ct}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        ct = (ExtraArgs or {}).get("ContentType", "application/octet-stream")
        self.buckets[Bucket][Key] = (Fileobj.read(), ct)


@pytest.fixture(autouse=True)
def _patch_client_error(monkeypatch):
    # migrate_store discriminates ClientError codes; the fakes raise FakeClientError.
    monkeypatch.setattr(ms, "ClientError", FakeClientError)


def _seed_standard(src):
    src.seed("datasets", "d/one.csv", b"a,b\n1,2\n", "text/csv")
    src.seed("models", "m/model.bin", b"\x00" * 2048)
    src.seed("results", "predictions/p1.json", b'{"x": 1}', "application/json")
    src.seed("mlflow", "1/run/artifacts/w.txt", b"hello")


# -- full copy + idempotent re-run (SC-127) -----------------------------------------------------

def test_forward_copies_all_then_rerun_copies_zero():
    src, dst = FakeS3(), FakeS3()
    _seed_standard(src)
    report = ms.run_migration(src, dst, list(ms.DEFAULT_BUCKETS), "forward")
    assert report["parity"] is True
    assert sum(b["copied"] for b in report["buckets"]) == 4
    assert dst.buckets["results"]["predictions/p1.json"][0] == b'{"x": 1}'
    # content type rides along
    assert dst.buckets["datasets"]["d/one.csv"][1] == "text/csv"

    rerun = ms.run_migration(src, dst, list(ms.DEFAULT_BUCKETS), "forward")
    assert rerun["parity"] is True
    assert all(b["copied"] == 0 for b in rerun["buckets"])
    assert sum(b["skipped"] for b in rerun["buckets"]) == 4


# -- reverse direction (rollback re-mirror) ------------------------------------------------------

def test_reverse_mirrors_post_cutover_writes_back():
    incumbent, replacement = FakeS3(), FakeS3()
    # a write that landed only on the replacement after cutover
    replacement.seed("results", "predictions/late.json", b'{"late": true}')
    report = ms.run_migration(replacement, incumbent, ["results"], "reverse")
    assert report["direction"] == "reverse"
    assert report["parity"] is True
    assert incumbent.buckets["results"]["predictions/late.json"][0] == b'{"late": true}'


# -- report shape (data-model.md MigrationReport) ------------------------------------------------

def test_report_shape_matches_data_model():
    src, dst = FakeS3(), FakeS3()
    _seed_standard(src)
    report = ms.run_migration(src, dst, list(ms.DEFAULT_BUCKETS), "forward")
    assert set(report) == {"direction", "started_at", "finished_at", "buckets", "parity"}
    assert report["direction"] in ("forward", "reverse")
    assert isinstance(report["started_at"], float) and isinstance(report["finished_at"], float)
    assert report["finished_at"] >= report["started_at"]
    for entry in report["buckets"]:
        assert set(entry) == {"name", "source", "dest", "copied", "skipped"}
        assert set(entry["source"]) == {"objects", "bytes"} == set(entry["dest"])
    # ETags are deliberately absent everywhere (multipart ETags aren't portable — R3)
    assert "etag" not in json.dumps(report).lower()


# -- pagination past 1,000 keys ------------------------------------------------------------------

def test_pagination_past_page_ceiling():
    src = FakeS3(buckets=("results",), page_size=100)  # 100-key pages, same truncation protocol
    dst = FakeS3(buckets=("results",), page_size=100)
    for i in range(257):
        src.seed("results", f"k/{i:05d}", bytes([i % 256]) * (i + 1))
    report = ms.run_migration(src, dst, ["results"], "forward")
    assert report["buckets"][0]["copied"] == 257
    assert report["buckets"][0]["source"]["objects"] == 257
    assert report["parity"] is True
    assert len(dst.buckets["results"]) == 257


# -- size mismatch => re-copy --------------------------------------------------------------------

def test_size_mismatch_recopied_equal_size_skipped():
    src, dst = FakeS3(), FakeS3()
    src.seed("results", "a.json", b"new-longer-body")
    dst.seed("results", "a.json", b"stale")  # present but wrong size
    src.seed("results", "b.json", b"same")
    dst.seed("results", "b.json", b"same")  # present, equal size -> skipped
    report = ms.run_migration(src, dst, ["results"], "forward")
    entry = report["buckets"][0]
    assert entry["copied"] == 1 and entry["skipped"] == 1
    assert dst.buckets["results"]["a.json"][0] == b"new-longer-body"


# -- parity is count+bytes, and a live-writer delta breaks it ------------------------------------

def test_parity_false_when_dest_diverges():
    src, dst = FakeS3(), FakeS3()
    src.seed("results", "a.json", b"aaaa")

    class RacingS3(FakeS3):
        """A writer lands a NEW source object after the copy pass (the un-quiesced case)."""
        def __init__(self):
            super().__init__()
            self.lists = 0

        def list_objects_v2(self, Bucket, **kw):
            self.lists += 1
            if self.lists == 2:  # after the copy pass, before the parity count
                self.seed("results", "late.json", b"zz")
            return super().list_objects_v2(Bucket, **kw)

    racing = RacingS3()
    racing.seed("results", "a.json", b"aaaa")
    report = ms.run_migration(racing, dst, ["results"], "forward")
    assert report["parity"] is False  # the delta shows up, never silent agreement


# -- CLI: exit code 0 iff parity, --report file, --reverse ---------------------------------------

def test_cli_exit_codes_and_report_file(tmp_path, monkeypatch):
    src, dst = FakeS3(), FakeS3()
    _seed_standard(src)
    clients = iter([src, dst])
    monkeypatch.setattr(ms, "make_client", lambda *a, **kw: next(clients))
    out = tmp_path / "report.json"
    rc = ms.main([
        "--source-endpoint", "http://a", "--source-key", "k", "--source-secret", "s",
        "--dest-endpoint", "http://b", "--dest-key", "k", "--dest-secret", "s",
        "--report", str(out),
    ])
    assert rc == 0
    report = json.loads(out.read_text())
    assert report["parity"] is True and report["direction"] == "forward"


def test_cli_reverse_swaps_roles(monkeypatch):
    incumbent, replacement = FakeS3(), FakeS3()
    replacement.seed("results", "late.json", b"zz")
    # args order is always --source=incumbent --dest=replacement; --reverse swaps the roles
    clients = iter([incumbent, replacement])
    monkeypatch.setattr(ms, "make_client", lambda *a, **kw: next(clients))
    rc = ms.main([
        "--source-endpoint", "http://a", "--source-key", "k", "--source-secret", "s",
        "--dest-endpoint", "http://b", "--dest-key", "k", "--dest-secret", "s",
        "--buckets", "results", "--reverse",
    ])
    assert rc == 0
    assert incumbent.buckets["results"]["late.json"][0] == b"zz"


def test_cli_nonzero_on_parity_failure(monkeypatch, capsys):
    src, dst = FakeS3(), FakeS3()
    src.seed("results", "a.json", b"aaaa")

    class WriteDropping(FakeS3):
        def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
            Fileobj.read()  # consume and drop — dest never converges

    dst = WriteDropping()
    clients = iter([src, dst])
    monkeypatch.setattr(ms, "make_client", lambda *a, **kw: next(clients))
    rc = ms.main([
        "--source-endpoint", "http://a", "--source-key", "k", "--source-secret", "s",
        "--dest-endpoint", "http://b", "--dest-key", "k", "--dest-secret", "s",
        "--buckets", "results",
    ])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["parity"] is False


# -- missing bucket fails loud (bootstrap owns creation) -----------------------------------------

def test_missing_bucket_fails_loud():
    src = FakeS3(buckets=("results",))
    dst = FakeS3(buckets=())  # dest side has no bucket at all
    src.seed("results", "a.json", b"x")
    with pytest.raises(SystemExit, match="bootstrap"):
        ms.run_migration(src, dst, ["results"], "forward")
