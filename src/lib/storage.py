"""Cloudflare R2 via the Cloudflare REST API (free tier, zero egress fees).

Uses the account API token (CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID) for
everything — bucket admin and object uploads — so no S3 access keys are
needed. The Graph API and the demo app both fetch media by public URL, which
is why every bucket here gets its managed r2.dev public domain enabled.

Object uploads via this endpoint cap at ~300 MB; our Reels are ~10 MB.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_BUCKET = "content-pipeline-media"


class StorageError(Exception):
    """Cloudflare API refused or credentials are missing."""


def _credentials() -> tuple[str, str]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        raise StorageError("CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be set")
    return token, account


def api_request(
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str = "application/json",
) -> dict | list:
    """One Cloudflare API call. `path` is relative to /accounts/{account_id}.

    Returns whatever shape `result` is: a dict for create/get-one endpoints,
    a bare list for list endpoints (Cloudflare does not wrap lists in
    {"items": [...]}) — check `isinstance` at the call site rather than
    assuming dict.
    """
    token, account = _credentials()
    req = urllib.request.Request(
        f"{API_BASE}/accounts/{account}{path}",
        method=method,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise StorageError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
    if not payload.get("success", False):
        raise StorageError(f"{method} {path} -> {payload.get('errors')}")
    return payload.get("result", {})


def ensure_bucket(name: str = DEFAULT_BUCKET) -> None:
    """Create the bucket if it doesn't exist (idempotent).

    Bucket administration (create/delete, public-domain toggling) needs a
    Cloudflare permission scope that dashboard-issued R2 tokens don't grant
    even with "Edit" on Workers R2 Storage — that permission covers object
    read/write only. A 403 here means the bucket already exists (set up once
    by hand in the dashboard, which is the only way to do it with this token
    class); object uploads still work fine, so we don't fail the deploy.
    """
    try:
        api_request("/r2/buckets", "POST", json.dumps({"name": name}).encode())
    except StorageError as e:
        if "10004" not in str(e) and "already exists" not in str(e).lower() and (
            "403" not in str(e)
        ):
            raise


def set_cors(bucket: str = DEFAULT_BUCKET, origins: list[str] | None = None) -> None:
    """Allow the static app (a different origin than the R2 bucket) to fetch
    gallery.json/status.json/videos with the browser's fetch() API.

    Unlike bucket create/domain-toggle, this endpoint works with dashboard-
    issued R2 object read/write tokens — no admin scope needed. Body must be
    wrapped in {"rules": [...]}; a bare array 400s.
    """
    body = {
        "rules": [
            {
                "allowed": {"origins": origins or ["*"], "methods": ["GET", "HEAD"]},
                "exposeHeaders": ["content-length", "content-type"],
                "maxAgeSeconds": 3600,
            }
        ]
    }
    api_request(f"/r2/buckets/{bucket}/cors", "PUT", json.dumps(body).encode())


def upload(
    key: str,
    source: str | Path | bytes,
    bucket: str = DEFAULT_BUCKET,
    content_type: str | None = None,
) -> str:
    """Upload a file or bytes to the bucket. Returns the object key."""
    if isinstance(source, str | Path):
        data = Path(source).read_bytes()
        content_type = content_type or mimetypes.guess_type(str(source))[0]
    else:
        data = source
    api_request(
        f"/r2/buckets/{bucket}/objects/{urllib.parse.quote(key)}",
        "PUT",
        data,
        content_type=content_type or "application/octet-stream",
    )
    return key


def public_url(domain: str, key: str) -> str:
    return f"https://{domain}/{urllib.parse.quote(key)}"


def download_public(domain: str, key: str, timeout: int = 30) -> bytes | None:
    """GET a public object straight from its r2.dev URL (no auth needed).

    Used to read back gallery.json before merging in a new entry. Returns
    None on 404 (e.g. the very first run, before any gallery.json exists).
    """
    try:
        with urllib.request.urlopen(public_url(domain, key), timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise StorageError(f"GET {key} -> HTTP {e.code}") from e
