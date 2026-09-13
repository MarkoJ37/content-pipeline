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
- Uploading new replacement footage and editing text-card designs.
- Complete job failure reporting for workflow setup failures and cancellations.
- Provider-aware per-call budget enforcement and reconciliation.

The current gallery is a demo, not an Instagram publishing queue. No automatic
Instagram publishing is implemented.


## Editing a Reel

Use **Edit this Reel** on a gallery entry with saved project media. You can:

- Replace a scene using another saved clip from that project. Short clips loop;
  scene lengths and voiceover do not change.
- Correct one word per timed caption field. This changes captions, not speech.
- Set caption font, text color, size and position; save a brand preset in this browser.
- Save an edit draft locally, then export a new MP4 without altering the original.

Caption presets do not change text-card backgrounds or text baked into saved footage.
Browser previews show individual clips and a style sample; export creates the final composite.

Exports run `revise.yml` with Pillow and ffmpeg, with **no AI credentials**. They use
an independent R2 allowance of one export per UTC day. Set `EDIT_GITHUB_TOKEN` and
`EDIT_GITHUB_REPO` in the Worker to enable exports separately from paid generation.
The shared demo access code is required to export. Storage and runner usage still
apply; no paid generation, voice synthesis, stock picking or AI quality review runs.
The export receives technical checks only: preview it before sharing.

Editable media lives under `projects/<id>/`; these assets are public like the demo.
New generation runs save source segments and word timings automatically. Older Reels
need their original segments and audio restored before they can be edited.


## Reel quality and funnel integration

The editor accepts MP4, PNG and JPEG replacements (20 MB/file; eight authenticated
uploads per UTC day across the studio). Demo uploads are public. Saved revisions
retain their source pool. Uploaded images can also serve as an optional logo.
Brand presets cover caption styling and card background, accent and text colors.
New projects retain card copy so revised exports can rebuild branded cards.
Older bundles with only rendered clips cannot restyle their baked card text.

Local alignment rejects missing transcript words and invalid timestamps instead of
scaling cut points by word count. Cuts prefer nearby phrase boundaries. Short stock
clips hold their final frame rather than loop. Captions are measured for fit and
stop at text-card boundaries. Local QC flags silent audio, long internal pauses,
caption timing errors, scene gaps and unusually short or long scenes. These checks
add no paid AI calls. Existing voice settings and paid visual review are unchanged.

The September showcase contains two manually curated examples with reused stock and
one Gemini voiceover request each. Logged total estimate: $0.008772. They were
rendered and reviewed locally; no paid footage picker or visual review was used.

The ZeroToStore funnel links to the studio from
https://markoj37.github.io/zerotostore/tools/reel-studio.html .
The studio's generation and online export dispatch remain disconnected until
separate workflow credentials are configured. Editing drafts is available.
