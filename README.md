# Reel Factory

A low-cost script-to-Reel demo using a static interface, Cloudflare Worker/R2,
GitHub Actions, local ffmpeg and local word timing. No database or always-on server.

## Demo

https://content-pipeline.reelfactory-8798b777.workers.dev

The shared demo shows existing examples. Generation is currently disconnected.
Media and the gallery are public; do not submit confidential customer content.

## Spending protection

- Default daily allowance: $0.10, with at most **one app dispatch per UTC day**.
- An R2 conditional create reserves the allowance before contacting GitHub.
  Concurrent requests cannot both reserve the same day.
- Failed or ambiguous dispatches retain the reservation until the next UTC day.
  No automatic refunds or paid generation retries are triggered by the browser.
- Missing storage or invalid limit configuration stops generation.
- This is a dispatch allowance, **not a guaranteed provider billing cap**. A run's
  actual API charges may exceed the estimate. Direct workflow dispatches and manual
  workflow reruns bypass the app gate. Keep generation disconnected when only
  showcasing examples, and use provider-side limits where available.

`GET /api/spend` totals estimated usage from independent `spend/YYYY-MM-DD/`
records. Records are refreshed at stage boundaries and include workflow execution
and attempt IDs. A crash before the next checkpoint or unreported provider charge
can leave the estimate incomplete. Historical `spend.json` is not used as a ledger.

Gallery entries are written independently to `gallery/<run-id>.json`. The Worker
merges them with legacy examples from `gallery.json`; it does not replace another
run's gallery update. Keep the `MEDIA` R2 binding when deploying the Worker.

## Checks (no paid API calls)

```sh
python -m pip install pytest ruff pillow
python -m pytest -m "not integration" -q
python -m ruff check src scripts tests
node --test tests/frontend.test.cjs
```

## Remaining product work

- Private projects and explicit sharing before accepting confidential client work.
- Clip replacement, caption correction and saved brand settings.
- Complete job failure reporting for workflow setup failures and cancellations.
- Provider-aware per-call budget enforcement and reconciliation.

The current gallery is a demo, not an Instagram publishing queue. No automatic
Instagram publishing is implemented.
