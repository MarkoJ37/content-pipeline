import json

import pytest

from src.lib import storage


@pytest.fixture(autouse=True)
def cf_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")


def test_set_cors_wraps_rules_and_defaults_to_wildcard_origin(monkeypatch):
    captured = {}

    def fake(path, method="GET", body=None, content_type="application/json"):
        captured["path"] = path
        captured["method"] = method
        captured["body"] = json.loads(body)
        return {}

    monkeypatch.setattr(storage, "api_request", fake)
    storage.set_cors()

    assert captured["path"] == "/r2/buckets/content-pipeline-media/cors"
    assert captured["method"] == "PUT"
    rules = captured["body"]["rules"]
    assert rules[0]["allowed"]["origins"] == ["*"]
    assert rules[0]["allowed"]["methods"] == ["GET", "HEAD"]


def test_set_cors_accepts_explicit_origins(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        storage,
        "api_request",
        lambda path, method="GET", body=None, content_type="application/json": captured.update(
            body=json.loads(body)
        ),
    )
    storage.set_cors(origins=["https://example.com"])

    assert captured["body"]["rules"][0]["allowed"]["origins"] == ["https://example.com"]
