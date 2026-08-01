import io
import json
from pathlib import Path

import pytest

from scripts.deploy_frontend import build_worker_source, ensure_kv_namespace
from src.lib import storage

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def cf_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(result=None):
    return FakeResponse(json.dumps({"success": True, "result": result or {}}).encode())


# -- api plumbing ---------------------------------------------------------------


def test_api_request_sends_bearer_and_account_path(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _ok({"id": "x"})

    monkeypatch.setattr(storage.urllib.request, "urlopen", fake_urlopen)
    result = storage.api_request("/r2/buckets")

    assert captured["url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/r2/buckets"
    assert captured["auth"] == "Bearer test-token"
    assert result == {"id": "x"}


def test_api_request_fails_loudly_on_api_error(monkeypatch):
    payload = {"success": False, "errors": [{"code": 10000, "message": "denied"}]}
    monkeypatch.setattr(
        storage.urllib.request,
        "urlopen",
        lambda req, timeout: FakeResponse(json.dumps(payload).encode()),
    )
    with pytest.raises(storage.StorageError, match="denied"):
        storage.api_request("/r2/buckets")


def test_missing_credentials_fail_before_network(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    with pytest.raises(storage.StorageError, match="must be set"):
        storage.api_request("/anything")


def test_ensure_bucket_tolerates_already_exists(monkeypatch):
    def fake(path, method="GET", body=None, content_type="application/json"):
        raise storage.StorageError("POST /r2/buckets -> [{'code': 10004, ...}] 10004")

    monkeypatch.setattr(storage, "api_request", fake)
    storage.ensure_bucket()  # must not raise


def test_ensure_bucket_tolerates_403_missing_admin_scope(monkeypatch):
    # dashboard-issued R2 tokens can't create/admin buckets via the API even
    # when the bucket already exists and object read/write works fine
    def fake(path, method="GET", body=None, content_type="application/json"):
        raise storage.StorageError("POST /r2/buckets -> HTTP 403: forbidden")

    monkeypatch.setattr(storage, "api_request", fake)
    storage.ensure_bucket()  # must not raise


def test_ensure_bucket_raises_on_unrelated_error(monkeypatch):
    def fake(path, method="GET", body=None, content_type="application/json"):
        raise storage.StorageError("POST /r2/buckets -> HTTP 500: internal error")

    monkeypatch.setattr(storage, "api_request", fake)
    with pytest.raises(storage.StorageError, match="500"):
        storage.ensure_bucket()


def test_upload_guesses_content_type(monkeypatch, tmp_path):
    captured = {}

    def fake(path, method="GET", body=None, content_type="application/json"):
        captured["path"] = path
        captured["content_type"] = content_type
        return {}

    monkeypatch.setattr(storage, "api_request", fake)
    mp4 = tmp_path / "reel.mp4"
    mp4.write_bytes(b"vid")

    storage.upload("videos/reel.mp4", mp4)

    assert captured["path"] == "/r2/buckets/content-pipeline-media/objects/videos/reel.mp4"
    assert captured["content_type"] == "video/mp4"


def test_download_public_returns_bytes(monkeypatch):
    monkeypatch.setattr(
        storage.urllib.request, "urlopen", lambda url, timeout: _ok_bytes(b'{"a":1}')
    )
    assert storage.download_public("pub-x.r2.dev", "gallery.json") == b'{"a":1}'


def test_download_public_returns_none_on_404(monkeypatch):
    def raise_404(url, timeout):
        raise storage.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(storage.urllib.request, "urlopen", raise_404)
    assert storage.download_public("pub-x.r2.dev", "gallery.json") is None


def test_download_public_raises_on_other_errors(monkeypatch):
    def raise_500(url, timeout):
        raise storage.urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    monkeypatch.setattr(storage.urllib.request, "urlopen", raise_500)
    with pytest.raises(storage.StorageError, match="500"):
        storage.download_public("pub-x.r2.dev", "gallery.json")


def _ok_bytes(data: bytes):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(data)


# -- KV namespace lookup (regression: Cloudflare list endpoints return a bare
#    list for `result`, not {"items": [...]}) ------------------------------------


def test_ensure_kv_namespace_finds_existing_bare_list(monkeypatch):
    calls = []

    def fake(path, method="GET", body=None, content_type="application/json"):
        calls.append(path)
        return [{"id": "ns-1", "title": "content-pipeline-rate-limit"}]

    monkeypatch.setattr(storage, "api_request", fake)
    assert ensure_kv_namespace() == "ns-1"
    assert len(calls) == 1  # found it in the list — no create call needed


def test_ensure_kv_namespace_creates_when_empty_list(monkeypatch):
    calls = []

    def fake(path, method="GET", body=None, content_type="application/json"):
        calls.append((path, method))
        if method == "GET":
            return []
        return {"id": "ns-new"}

    monkeypatch.setattr(storage, "api_request", fake)
    assert ensure_kv_namespace() == "ns-new"
    assert calls[-1][1] == "POST"


def test_ensure_kv_namespace_creates_when_dict_result(monkeypatch):
    # defensive: tolerate a dict-shaped result too, not just bare list/empty
    def fake(path, method="GET", body=None, content_type="application/json"):
        if method == "GET":
            return {}
        return {"id": "ns-new"}

    monkeypatch.setattr(storage, "api_request", fake)
    assert ensure_kv_namespace() == "ns-new"


def test_public_url_quotes_key():
    assert (
        storage.public_url("pub-x.r2.dev", "videos/my reel.mp4")
        == "https://pub-x.r2.dev/videos/my%20reel.mp4"
    )


# -- worker build ----------------------------------------------------------------


def test_build_worker_inlines_everything():
    source = build_worker_source(
        ROOT / "frontend", ROOT / "worker" / "worker.js", "https://pub-test.r2.dev"
    )

    # no unexpanded placeholders remain
    assert "__INLINE_CSS__" not in source
    assert "__INLINE_JS__" not in source
    assert "__INDEX_HTML_JSON__" not in source
    assert "__R2_PUBLIC_BASE__" not in source
    # the page and config made it in
    assert "Reel Factory" in source
    assert "https://pub-test.r2.dev" in source
    # the inlined HTML is a syntactically valid JS string literal
    marker = "const INDEX_HTML = "
    literal = source[source.index(marker) + len(marker):].strip().rstrip(";")
    html = json.loads(literal)
    assert html.startswith("<!doctype html>")
    assert "cost-value" in html
