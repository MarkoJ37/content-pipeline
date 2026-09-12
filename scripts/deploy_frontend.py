"""Deploy the showcase app: R2 bucket + KV + Worker, all free tier.

    python -m scripts.deploy_frontend [--access-code CODE] [--spend-cap 1.00]

Needs CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID, and the bucket's public
r2.dev domain (--r2-domain, or R2_PUBLIC_DOMAIN env) since dashboard-issued R2
tokens can't toggle the public domain via the API — that's a one-time manual
step (R2 > bucket > Settings > Public Development URL). Optionally
GITHUB_TOKEN + GITHUB_REPO (owner/repo) to wire the Generate button to
Actions — without them the Worker deploys with dispatch disabled and the app
says so.

Idempotent: re-running redeploys the Worker and refreshes the gallery.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import uuid
from pathlib import Path

from src.lib import storage

ROOT = Path(__file__).resolve().parent.parent
WORKER_NAME = "content-pipeline"
KV_TITLE = "content-pipeline-rate-limit"


def build_worker_source(
    frontend_dir: Path, worker_template: Path, r2_public_base: str
) -> str:
    """Inline css+js into index.html, then the whole page into the Worker."""
    html = (frontend_dir / "index.html").read_text(encoding="utf-8")
    css = (frontend_dir / "style.css").read_text(encoding="utf-8")
    js = (frontend_dir / "app.js").read_text(encoding="utf-8")
    js = js.replace("__R2_PUBLIC_BASE__", r2_public_base)
    html = html.replace("__INLINE_CSS__", css).replace("__INLINE_JS__", js)
    worker = worker_template.read_text(encoding="utf-8")
    # json.dumps produces a valid JS string literal, whatever the HTML contains
    return worker.replace("__INDEX_HTML_JSON__", json.dumps(html))


def ensure_kv_namespace() -> str:
    # Cloudflare list endpoints return `result` as a bare array, not
    # {"items": [...]} — unlike the dict-shaped `result` most other endpoints
    # (create, get-one) return.
    listing = storage.api_request("/storage/kv/namespaces?per_page=100")
    namespaces = listing if isinstance(listing, list) else []
    for ns in namespaces:
        if ns.get("title") == KV_TITLE:
            return ns["id"]
    result = storage.api_request(
        "/storage/kv/namespaces", "POST", json.dumps({"title": KV_TITLE}).encode()
    )
    return result["id"]


def upload_worker(source: str, bindings: list[dict]) -> None:
    boundary = f"----boundary{uuid.uuid4().hex}"
    metadata = json.dumps(
        {
            "main_module": "worker.js",
            "compatibility_date": "2025-01-01",
            "bindings": bindings,
            "keep_bindings": ["secret_text"],
        }
    )
    parts = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        "Content-Type: application/javascript+module\r\n\r\n"
        f"{source}\r\n"
        f"--{boundary}--\r\n"
    )
    storage.api_request(
        f"/workers/scripts/{WORKER_NAME}",
        "PUT",
        parts.encode("utf-8"),
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def ensure_workers_subdomain(account_id: str) -> str:
    try:
        result = storage.api_request("/workers/subdomain")
        if result.get("subdomain"):
            return result["subdomain"]
    except storage.StorageError:
        pass
    name = f"reelfactory-{account_id[:8]}"
    storage.api_request("/workers/subdomain", "PUT", json.dumps({"subdomain": name}).encode())
    return name


def enable_script_subdomain() -> None:
    storage.api_request(
        f"/workers/scripts/{WORKER_NAME}/subdomain",
        "POST",
        json.dumps({"enabled": True, "previews_enabled": False}).encode(),
    )


def upload_gallery(domain: str) -> None:
    items = []
    for mp4, title, cost in [
        (ROOT / "output" / "focus-app.mp4", "Deep Work Mode (SaaS demo)", 0.081),
        (ROOT / "output" / "demo_reel_v2.mp4", "Growth tips (stock demo)", 0.033),
    ]:
        if not mp4.exists():
            continue
        key = f"videos/{mp4.stem}.mp4"
        print(f"  uploading {mp4.name} ({mp4.stat().st_size / 1e6:.1f} MB)...")
        storage.upload(key, mp4, content_type="video/mp4")
        items.append({"title": title, "video_key": key, "cost_usd": cost})
    storage.upload(
        "gallery.json", json.dumps(items).encode(), content_type="application/json"
    )
    print(f"  gallery: {len(items)} reels at https://{domain}/gallery.json")


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-code", default=None)
    parser.add_argument("--spend-cap", default="0.10")
    parser.add_argument(
        "--r2-domain",
        default=os.environ.get("R2_PUBLIC_DOMAIN"),
        help="e.g. pub-xxxx.r2.dev (set once by hand; see module docstring)",
    )
    args = parser.parse_args(argv)

    if not args.r2_domain:
        print(
            "ERROR: --r2-domain (or R2_PUBLIC_DOMAIN) is required.\n"
            "Get it from R2 > your bucket > Settings > Public Development URL."
        )
        return 1

    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    access_code = args.access_code or f"reel-{secrets.token_hex(3)}"

    print("1/5 R2 bucket (assumed to exist already; domain given explicitly)")
    storage.ensure_bucket()
    storage.set_cors()  # the app (workers.dev) and bucket (r2.dev) are different origins
    domain = args.r2_domain
    r2_base = f"https://{domain}"
    print(f"  {r2_base}")

    print("2/5 KV namespace (rate limiting)")
    kv_id = ensure_kv_namespace()

    print("3/5 gallery upload")
    upload_gallery(domain)

    print("4/5 worker deploy")
    source = build_worker_source(ROOT / "frontend", ROOT / "worker" / "worker.js", r2_base)
    bindings = [
        {"type": "kv_namespace", "name": "RATE_KV", "namespace_id": kv_id},
        {"type": "secret_text", "name": "ACCESS_CODE", "text": access_code},
        {"type": "plain_text", "name": "R2_PUBLIC_BASE", "text": r2_base},
        {"type": "plain_text", "name": "DAILY_SPEND_CAP", "text": args.spend_cap},
    ]
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        bindings.append(
            {"type": "secret_text", "name": "GITHUB_TOKEN", "text": os.environ["GITHUB_TOKEN"]}
        )
        bindings.append(
            {"type": "plain_text", "name": "GITHUB_REPO", "text": os.environ["GITHUB_REPO"]}
        )
        dispatch = "connected"
    else:
        dispatch = "NOT connected (set GITHUB_TOKEN + GITHUB_REPO and redeploy)"
    upload_worker(source, bindings)

    print("5/5 workers.dev subdomain")
    subdomain = ensure_workers_subdomain(account)
    enable_script_subdomain()

    print()
    print(f"app:         https://{WORKER_NAME}.{subdomain}.workers.dev")
    print(f"access code: {access_code}")
    print(f"spend cap:   ${args.spend_cap}/day")
    print(f"dispatch:    {dispatch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
