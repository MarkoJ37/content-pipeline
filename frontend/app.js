"use strict";

/* Injected at deploy time by scripts/deploy_frontend.py */
const R2_BASE = "__R2_PUBLIC_BASE__"; // e.g. https://pub-xxxx.r2.dev
const POLL_MS = 4000;

const $ = (id) => document.getElementById(id);

/* ---- word count ---- */
$("script").addEventListener("input", () => {
  const words = $("script").value.trim().split(/\s+/).filter(Boolean).length;
  $("word-count").textContent = words;
});

/* ---- generate ---- */
$("generate").addEventListener("click", async () => {
  const script = $("script").value.trim();
  const words = script.split(/\s+/).filter(Boolean).length;
  const error = $("form-error");
  error.hidden = true;

  if (words < 40 || words > 120) {
    error.textContent = `Script is ${words} words — aim for 60–90 (about 30 seconds spoken).`;
    error.hidden = false;
    return;
  }
  if (!$("access-code").value) {
    error.textContent = "Access code required (this demo is gated to stop key-burning).";
    error.hidden = false;
    return;
  }

  $("generate").disabled = true;
  try {
    const resp = await fetch("/api/generate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ script, access_code: $("access-code").value }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      error.textContent = body.error || `Request failed (${resp.status})`;
      error.hidden = false;
      return;
    }
    startPolling(body.run_id);
  } catch (e) {
    error.textContent = "Network error — try again.";
    error.hidden = false;
  } finally {
    $("generate").disabled = false;
  }
});

/* ---- progress polling (status.json written to R2 by the pipeline) ---- */
const STAGES = ["shotlist", "voice", "align", "footage", "assemble", "review"];
let pollTimer = null;

function startPolling(runId) {
  $("progress").hidden = false;
  $("result").hidden = true;
  $("progress-note").textContent = "Generation runs on GitHub Actions — typically 2–5 minutes.";
  setStages({});
  clearInterval(pollTimer);
  pollTimer = setInterval(() => poll(runId), POLL_MS);
  poll(runId);
}

async function poll(runId) {
  let status;
  try {
    const resp = await fetch(`${R2_BASE}/runs/${runId}/status.json`, { cache: "no-store" });
    if (!resp.ok) return; // status.json not written yet
    status = await resp.json();
  } catch {
    return; // transient network error; next tick retries
  }
  setStages(status.stages || {});
  $("cost").textContent = `$${(status.cost_usd || 0).toFixed(3)}`;

  if (status.state === "done") {
    clearInterval(pollTimer);
    $("final-cost").textContent = `$${(status.cost_usd || 0).toFixed(3)}`;
    $("player").src = `${R2_BASE}/${status.video_key}`;
    $("result").hidden = false;
    loadGallery();
  } else if (status.state === "failed") {
    clearInterval(pollTimer);
    $("progress-note").textContent = `Failed at ${status.failed_stage || "?"}: ${status.error || "unknown error"}`;
  }
}

function setStages(stages) {
  for (const name of STAGES) {
    const li = document.querySelector(`#stages li[data-stage="${name}"]`);
    const info = stages[name];
    li.classList.toggle("done", Boolean(info && info.done));
    li.classList.toggle("active", Boolean(info && !info.done));
    if (info && info.cost_usd != null) {
      li.querySelector(".stage-cost").textContent = `$${info.cost_usd.toFixed(3)}`;
    }
  }
}

/* ---- gallery ---- */
async function loadGallery() {
  let items = [];
  try {
    const resp = await fetch(`${R2_BASE}/gallery.json`, { cache: "no-store" });
    if (resp.ok) items = await resp.json();
  } catch {
    /* gallery is optional */
  }
  const gallery = $("gallery");
  gallery.replaceChildren();
  for (const item of items) {
    const div = document.createElement("div");
    div.className = "gallery-item";
    const video = document.createElement("video");
    video.src = `${R2_BASE}/${item.video_key}`;
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    const meta = document.createElement("div");
    meta.className = "gallery-meta";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = item.title || "untitled";
    const cost = document.createElement("span");
    cost.className = "cost";
    cost.textContent = `$${(item.cost_usd || 0).toFixed(3)}`;
    meta.append(title, cost);
    div.append(video, meta);
    gallery.append(div);
  }
  document.getElementById("gallery-section").hidden = items.length === 0;
}

/* ---- backend health note ---- */
(async () => {
  try {
    const resp = await fetch("/api/health");
    const body = await resp.json();
    if (!body.dispatch_ready) {
      $("backend-status").textContent =
        "(demo note: GitHub dispatch not connected yet — Generate is disabled.)";
    }
  } catch {
    /* worker not reachable; leave footer as-is */
  }
})();

loadGallery();
