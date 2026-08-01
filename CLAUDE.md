# CLAUDE.md

Project context for Claude Code. Read this fully before making changes.

## What this is

An automated Instagram content pipeline. It takes raw input (images, video, a written
script, or a product URL) and produces finished, scheduled Instagram posts: single images,
carousels, and faceless Reels.

Two modes:
- **Curate** — group existing images into carousels, trim raw footage into Reels, write captions.
- **Generate** — build new creative from scratch (AI ad images, faceless script-to-video).

Clients are both physical-product and digital-product (SaaS, courses, services), so the
pipeline must never assume a photographable product exists.

## Guiding principles

These drive every design decision. When in doubt, re-read them.

1. **Cost is the primary constraint.** This is a possibilities demo on a shoestring budget.
   Always reach for the free option first. Never add a paid service where a free one is
   adequate. Never introduce a paid tier, subscription, or hosted service without flagging
   it explicitly and explaining why the free path fails.
2. **Free compute over paid API.** If a job can run on the GitHub Actions CPU runner for
   free (ffmpeg, forced alignment, image resizing), run it there. Do not call a paid API
   for something a local library can do.
3. **No servers.** Everything runs on GitHub Actions cron. No VPS, no always-on process,
   no Docker host, no Kubernetes. If a task needs compute, it runs inside a workflow.
4. **Stock-first for video.** AI video generation is the single most expensive line item.
   Use Pexels stock for anything generic. Only generate AI clips when stock genuinely
   cannot cover a shot, and cap it at 1–2 generated clips per Reel.
5. **Human approval before publish.** Nothing posts to Instagram without an approval step.
   Never bypass this, even for testing. Use a dry-run flag instead.
6. **Fail loudly, cheaply.** Validate before spending. Check the script is well-formed
   before calling TTS; check the shot list before calling a video model. Never retry a
   paid API call in a loop without a hard cap.

## Stack

| Layer | Choice | Cost | Notes |
|---|---|---|---|
| Publishing | Instagram Graph API | Free | 50 API posts / 24h. Needs a Business/Creator account linked to a FB Page. |
| Scheduling / compute | GitHub Actions (cron) | Free | All jobs run here. 2,000 min/mo private, unlimited public. |
| Media hosting | Cloudflare R2 (free tier) | Free | Graph API fetches media by **public URL** — it does not accept uploads. |
| Orchestration / reasoning | Claude API | ~$0.05/Reel | Scripts, shot lists, captions, carousel grouping, output QC. |
| Voice | **Gemini Flash TTS** | ~$0.012/1K chars | MVP choice. Keep behind an interface — ElevenLabs is the upgrade path. |
| **Caption timing** | **faster-whisper (local)** | **Free** | See "The timestamp problem" below. Non-obvious, read it. |
| Stock media | Pexels API | Free | Commercial use, no attribution required. |
| Image generation | Flux / Nano Banana via fal.ai | ~$0.01–0.04/img | |
| Video generation | Kling / Wan via fal.ai | ~$0.30–1.00 / 10s | **Use sparingly.** |
| Editing | ffmpeg | Free | Cutting, 9:16 crop, caption burn-in, audio ducking. |

Target: **~$0.06 per stock-only Reel**, up to ~$1.06 if AI clips are used.

## The timestamp problem (IMPORTANT — verified, do not "fix" this)

Gemini TTS returns **audio only**. It does not return word-level timestamps, character
offsets, or any timing metadata. This was verified against Google's docs; do not assume
otherwise or waste time looking for a `timestamps=True` flag. It does not exist.

We need per-word timing for two things: burned-in caption sync, and knowing where to cut
between shots.

**Solution: forced alignment locally, for free.**

We already know the exact transcript, so this is alignment, not transcription. Run
`faster-whisper` with `word_timestamps=True` on the audio inside the GitHub Actions runner:

- Use the `base` or `small` model on CPU. `tiny` is too inaccurate; `medium`+ is too slow.
- ~10–20 seconds of runner time for a 30-second clip. Costs nothing.
- Pass the known script as `initial_prompt` to improve alignment accuracy.
- Output: `[{word, start, end}, ...]` → drives both captions and ffmpeg cut points.

**Do not** call a paid transcription API for this. **Do not** estimate timing from
character counts — it drifts and looks broken. **Do not** add a Whisper API call; run it
locally.

If we later swap to ElevenLabs, it returns timestamps natively and this step can be
skipped — which is why the TTS layer must stay behind an interface that returns
`(audio_bytes, word_timings)` regardless of provider.

## Gemini TTS gotchas (from the docs — handle these)

- **Random 500s.** The model occasionally returns text tokens instead of audio, failing the
  request. This is documented and expected. Implement retry with backoff, max 3 attempts.
- **Prompt classifier rejections.** Vague prompts can get rejected (`PROHIBITED_CONTENT`),
  or worse, the model reads your style directions aloud. Always use an explicit preamble
  instructing it to synthesize speech, and clearly label where the spoken transcript begins.
- **Quality drift on long output.** Split anything longer than ~1 minute into chunks and
  concatenate. Reels are short, so this rarely bites — but scripts for longer formats will.
- **Size limits.** Text field caps around 4,000 bytes per request. Chunk accordingly.

## Full flow (end to end)

```
┌─ INPUT ────────────────────────────────────────────────────────┐
│  input/scripts/*.md   — a written script or a brief            │
│  input/images/*       — raw photos                             │
│  input/video/*        — raw footage or screen recordings       │
│  input/briefs/*.json  — {product_url, tone, cta, goal}         │
└────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                CURATE                GENERATE
                    │                     │
                    │            ┌────────┴────────┐
                    │        has script?      no script?
                    │            │                 │
                    │            │        Claude: brief/URL → script
                    │            └────────┬────────┘
                    │                     │
                    │        ┌────────────▼─────────────┐
                    │        │ 1. SHOT LIST             │
                    │        │ Claude splits script into│
                    │        │ 2–3s shots. Each tagged: │
                    │        │   STOCK   (+ keywords)   │
                    │        │   TEXT_CARD (+ copy)     │
                    │        │   SCREEN_REC (+ tmrange) │
                    │        │   GENERATE (+ prompt)    │
                    │        │ 30s Reel → 10–15 shots.  │
                    │        │ Fewer = reject, retry.   │
                    │        └────────────┬─────────────┘
                    │                     │
                    │        ┌────────────▼─────────────┐
                    │        │ 2. VOICE                 │
                    │        │ Gemini Flash TTS         │
                    │        │ → voiceover.wav          │
                    │        │ (retry on 500, max 3)    │
                    │        └────────────┬─────────────┘
                    │                     │
                    │        ┌────────────▼─────────────┐
                    │        │ 3. ALIGN  (local, free)  │
                    │        │ faster-whisper base      │
                    │        │ word_timestamps=True     │
                    │        │ initial_prompt = script  │
                    │        │ → [{word, start, end}]   │
                    │        └────────────┬─────────────┘
                    │                     │
                    │        ┌────────────▼─────────────┐
                    │        │ 4. FOOTAGE               │
                    │        │ STOCK   → Pexels search; │
                    │        │   Claude vision picks    │
                    │        │   best of top 5          │
                    │        │ TEXT_CARD → ffmpeg render│
                    │        │ SCREEN_REC → crop + Ken  │
                    │        │   Burns zoom/pan         │
                    │        │ GENERATE → fal.ai (max 2)│
                    │        └────────────┬─────────────┘
                    │                     │
                    ▼                     ▼
        ┌───────────────────────────────────────────┐
        │ 5. ASSEMBLE (ffmpeg, free)                │
        │   • cut each shot to its word-timed slot  │
        │   • crop/pad to 1080x1920 (9:16)          │
        │   • burn captions from word timings       │
        │   • mix VO + music, duck music under VO   │
        │   • encode H.264 / AAC                    │
        └───────────────────┬───────────────────────┘
                            │
        ┌───────────────────▼───────────────────────┐
        │ 6. REVIEW (Claude)                        │
        │   sample frames → check for artifacts,    │
        │   caption overflow, black frames, audio   │
        │   desync. Reject → retry once, then flag. │
        └───────────────────┬───────────────────────┘
                            │
        ┌───────────────────▼───────────────────────┐
        │ 7. STAGE                                  │
        │   upload media → Cloudflare R2 (public)   │
        │   append entry to queue.json (status=     │
        │   pending_approval) → open a GitHub PR    │
        └───────────────────┬───────────────────────┘
                            │
                    ── HUMAN APPROVES (merges PR) ──
                            │
        ┌───────────────────▼───────────────────────┐
        │ 8. PUBLISH (cron workflow)                │
        │   pick next approved item from queue.json │
        │   Graph API 3-step container flow         │
        │   poll status_code until FINISHED         │
        │   media_publish → mark posted             │
        │   commit queue.json back                  │
        └───────────────────────────────────────────┘
```

## Repo layout

```
input/                  raw assets + briefs dropped here
queue.json              single source of truth for post state
src/
  lib/
    tts.py              provider-agnostic: returns (audio, word_timings)
    align.py            faster-whisper forced alignment
    video.py            provider-agnostic AI video wrapper
    stock.py            Pexels client
    storage.py          Cloudflare R2 upload
    spend.py            cost-logging wrapper — ALL paid calls go through this
  curate/               carousel grouping, footage trimming
  generate/             script → shot list → TTS → align → footage → assemble
  publish/              Graph API container flow
  review/               Claude QC pass
.github/workflows/
  generate.yml          on push to input/
  publish.yml           cron
```

## Instagram Graph API gotchas

- Publishing is a **three-step container flow**, not one call:
  1. `POST /{ig-user-id}/media` → returns a creation ID (one per item)
  2. Carousels: create a child container per item, then a parent with `children=[...]`
  3. `POST /{ig-user-id}/media_publish` with the creation ID
- Reels containers process asynchronously. **Poll `status_code` until `FINISHED`** before
  publishing. Do not sleep-and-hope.
- Carousels: 2–10 items. Hard limit.
- Media must be at a **public URL** — R2 first, then pass the URL.
- Tokens expire. Use a long-lived token, stored in GitHub Secrets. Never in the repo.

## Frontend (the showcase app)

The demo is delivered **through an app**, not a terminal. It must be usable by someone who
is not the developer. It stays free by having no always-on process.

```
Static app  (GitHub Pages / Cloudflare Pages, $0)
   │  user pastes a script or product URL → clicks Generate
   │  sends: { script, access_code }
   ▼
Cloudflare Worker  ($0 — 100k req/day free)
   │  1. check access_code against a Worker secret → 403 if wrong
   │  2. rate-limit per IP via Workers KV → 429 if exceeded
   │  3. fire GitHub workflow_dispatch using a token the Worker holds
   │  returns a run_id
   ▼
GitHub Actions  ($0)
   │  the existing pipeline: shot list → TTS → align → Pexels → ffmpeg
   │  writes status.json to R2 after each stage
   ▼
Cloudflare R2  ($0)
   │  status.json + finished .mp4
   ▲
Static app polls status.json → renders progress → plays the Reel
```

**Why a Worker at all.** GitHub Pages serves static files; it cannot run code. Triggering
the pipeline requires a GitHub token with repo write access. That token must never reach
the browser. The Worker holds it. It is not a server — it is a ~50ms function with a URL,
dormant when unused.

**The Worker is also the gate.** Hiding the token is not security on its own: without a
gate, anyone with the URL can spam Generate and burn the Anthropic / Gemini / fal keys.
The Worker therefore enforces:
- a **shared access code** (compared against a Worker secret; rotate after each demo)
- a **per-IP rate limit** in Workers KV (e.g. 5 runs/hour, 20/day)
- a **hard daily spend ceiling** — read the spend log; refuse to dispatch past the cap

This is not an auth system. No accounts, no login, no user database. A header and a counter.

**What the app shows:**
- Script / product-URL input → Generate
- Live progress per stage (shot list → voice → footage → assembly → done)
- **Running cost counter** — the core selling point. Watching "$0.06" tick up while it
  works *is* the pitch. Surface it prominently.
- The finished Reel, playing inline
- A gallery of previous Reels, each with its script, shot list, and cost

**Constraints:**
- Vanilla HTML/CSS/JS. No React, no build step, no bundler, no framework.
- No backend beyond the Worker. No job queue, no database, no auth provider.
- Generation is async (2–5 min). The app polls; it never blocks on a request.
- Keep the repo **public** — public repos get unlimited free Actions minutes; private repos
  get 2,000/month, and ffmpeg is CPU-heavy. All secrets live in GitHub Secrets and Worker
  secrets, never in the repo.

## Conventions

- Python. `ruff` for lint, `pytest` for tests.
- All keys via environment variables / GitHub Secrets. **Never commit a key.**
- Every paid API call goes through `src/lib/spend.py`, which logs estimated cost. This is
  non-negotiable — cost visibility is the entire point of the project.
- `queue.json` is the source of truth for post state. Commit it back after each run.
- TTS and video providers stay behind thin interfaces so they can be swapped without
  touching the pipeline.
- Add a `--dry-run` flag to every command that spends money or publishes.

## Do not

- Do not add a database. `queue.json` in the repo is sufficient at this scale.
- Do not add a web framework, a backend server, a job queue, or an auth provider. The
  static app + Cloudflare Worker described above is the *only* sanctioned frontend
  architecture. (An earlier version of this file banned frontends outright — that was
  wrong and has been corrected.)
- Do not put any API key or GitHub token in frontend JavaScript. Ever.
- Do not auto-publish without approval.
- Do not generate AI video where stock would do.
- Do not call a paid API for transcription/alignment — run faster-whisper locally.
- Do not use Pexels clips with identifiable faces in anything implying endorsement; the
  license prohibits it and those clips may lack model releases. Prefer faceless /
  hands-only / object b-roll.
- Do not commit generated media to the repo. It goes to R2.

## Current status

**Done:** `src/lib/spend.py`, `src/lib/tts.py` (Gemini + SAPI offline fallback),
`src/lib/align.py`. `src/generate/` produces a finished 9:16 Reel end-to-end with word-timed
captions. 47 tests passing.

**Known gap:** the demo Reel is built entirely from TEXT_CARD shots. `STOCK` raises
`NotImplementedError`. The central assumption of the whole project — that keyword-matched
Pexels b-roll cut at 2–3s intervals reads as a real ad — is **still unvalidated**. That is
the biggest risk in the project and the next thing to prove.

**Build order:**
1. `src/lib/stock.py` — Pexels search (portrait, ≥1080), download + cache by video ID,
   Claude vision picks best-of-5 per shot. Rewrite the demo shot list to ~70% STOCK /
   30% TEXT_CARD and re-run. **Look at the output before building anything else.**
2. `src/generate/shots.py` — the Claude call that turns a script into a shot list
   (currently only validates a hardcoded list).
3. Frontend: static app + Worker (see above). This is the demo vehicle.
4. `src/review/`, `src/publish/`, R2 upload, GitHub Actions workflows.

Meta/Instagram app + long-lived token setup is slow and bureaucratic — start it in the
background early, even though it isn't needed until step 4.

Start with a single hardcoded script and get one Reel out the door before generalizing.
