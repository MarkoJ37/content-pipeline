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
    error.textContent = "Enter your access code to generate a Reel.";
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
    $("generate").disabled = Boolean(activeRun) || !backendReady;
  }
});

/* ---- progress polling (status.json written to R2 by the pipeline) ---- */
const STAGES = ["shotlist", "voice", "align", "footage", "assemble", "review"];
let pollTimer = null;
let activeRun = null;
let backendReady = true;
let exportReady = false;
const RUN_KEY = "reel-factory-run";
const MAX_WAIT_MS = 25 * 60 * 1000;

function remember(run) {
  try {
    if (run) localStorage.setItem(RUN_KEY, JSON.stringify(run));
    else localStorage.removeItem(RUN_KEY);
  } catch { /* Storage may be disabled; generation still works. */ }
}

function startPolling(runId, startedAt = Date.now()) {
  activeRun = { runId, startedAt };
  remember(activeRun);
  $("generate").disabled = true;
  $("export-edit").disabled = true;
  $("progress").hidden = false;
  $("result").hidden = true;
  $("resume").hidden = true;
  $("edit-result").hidden = true;
  $("cost").textContent = "$0.000";
  $("progress-note").textContent = "Waiting to start. Most Reels take 2-5 minutes. You can refresh this page safely.";
  setStages({});
  clearTimeout(pollTimer);
  poll(activeRun);
}

function finishRun() {
  activeRun = null;
  remember(null);
  $("generate").disabled = !backendReady;
  $("export-edit").disabled = !exportReady;
}

async function poll(run) {
  if (activeRun !== run) return;
  if (Date.now() - run.startedAt > MAX_WAIT_MS) {
    $("progress-note").textContent = "This is taking longer than expected. Check again before starting another Reel to avoid duplicate costs.";
    $("resume").hidden = false;
    return;
  }
  try {
    const resp = await fetch(`${R2_BASE}/runs/${run.runId}/status.json`, {
      cache: "no-store", signal: AbortSignal.timeout(15000),
    });
    if (resp.ok) {
      const status = await resp.json();
      if (activeRun !== run) return;
      setStages(status.stages || {});
      if (status.project_id) {
        $("edit-result").hidden = false;
        $("edit-result").onclick = () => openEditor(status.project_id);
      }
      $("cost").textContent = `$${Number(status.cost_usd || 0).toFixed(3)}`;
      if (status.state === "done") {
        $("final-cost").textContent = `$${Number(status.cost_usd || 0).toFixed(3)}`;
        $("player").src = `${R2_BASE}/${status.video_key}`;
        $("download").href = $("player").src;
        $("result").hidden = false;
        $("progress-note").textContent = "Your Reel is ready. Preview it before sharing.";
        finishRun();
        loadGallery();
        return;
      }
      if (status.state === "failed" || status.state === "needs_review") {
        $("progress-note").textContent = status.state === "needs_review"
          ? `Quality check needs attention: ${(status.issues || []).join("; ")}`
          : `Generation stopped at ${status.failed_stage || "setup"}. ${status.error || "Please try again later."}`;
        finishRun();
        return;
      }
      $("progress-note").textContent = "Creating your Reel. Progress updates after each step.";
    }
  } catch { /* Retry transient failures within the overall deadline. */ }
  if (activeRun === run) pollTimer = setTimeout(() => poll(run), POLL_MS);
}

$("resume").addEventListener("click", () => {
  if (activeRun) startPolling(activeRun.runId);
});

function setStages(stages) {
  for (const name of STAGES) {
    const li = document.querySelector(`#stages li[data-stage="${name}"]`);
    const info = stages[name];
    li.classList.toggle("done", Boolean(info && info.done));
    li.classList.toggle("active", Boolean(info && !info.done));
    const cost = li.querySelector(".stage-cost");
    if (cost) cost.textContent = info && info.cost_usd != null ? `$${info.cost_usd.toFixed(3)}` : "";
  }
}

/* ---- gallery ---- */
async function loadGallery() {
  let items = [];
  try {
    const resp = await fetch("/api/gallery", { cache: "no-store" });
    if (resp.ok) items = await resp.json();
  } catch {
    /* gallery is optional */
  }
  if (!Array.isArray(items)) items = [];
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
    cost.textContent = `EST. AI COST / $${(item.cost_usd || 0).toFixed(3)}`;
    meta.append(title, cost);
    if (item.project_id) {
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit this Reel";
      edit.addEventListener("click", () => openEditor(item.project_id));
      meta.append(edit);
    }
    div.append(video, meta);
    gallery.append(div);
  }
  $("gallery-note").textContent = items.length ? "" : "No Reels to show right now. Please check back soon.";
}

/* ---- backend health note ---- */
(async () => {
  try {
    const resp = await fetch("/api/health");
    const body = await resp.json();
    backendReady = Boolean(body.dispatch_ready);
    exportReady = Boolean(body.export_ready);
    $("export-status").textContent = exportReady
      ? "Export ready. One edit export per UTC day; no new AI generation."
      : "Preview mode: edit and save drafts locally. Export is not connected yet.";
    $("generate").disabled = Boolean(activeRun) || !backendReady;
    if (!backendReady) {
      $("backend-status").textContent = "Preview mode. Explore the examples below; generation is currently unavailable.";
    } else {
      $("backend-status").textContent = "Studio ready. One demo generation per day (UTC).";
    }
  } catch {
    $("backend-status").textContent = "Studio status unavailable. Please try again later.";
  }
})();

loadGallery();

try {
  const saved = JSON.parse(localStorage.getItem(RUN_KEY));
  if (saved && /^[A-Za-z0-9_-]{1,64}$/.test(saved.runId) && Number.isFinite(saved.startedAt)) {
    startPolling(saved.runId, saved.startedAt);
  }
} catch { /* Ignore unavailable or corrupt browser storage. */ }

$("example").addEventListener("click", () => {
  $("script").value = "Your best ideas deserve more than a place in your notes app. Start small. Pick one thing you learned this week. Explain it like you are talking to a friend. Give them one simple action to try today. You do not need a perfect studio or a complicated plan. Just a useful idea, a clear voice, and the courage to share it. What will you make today?";
  $("word-count").textContent = $("script").value.split(/\s+/).length;
  $("script").focus();
});
