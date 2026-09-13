/** Low-cost demo gate. R2 conditional writes enforce one dispatch per UTC day.
 * Reservations survive network errors and failed runs; they are never auto-refunded.
 * This limits dispatches, not a provider's final invoice. KV is only a secondary IP throttle.
 */

const RATE_HOURLY = 1; // per IP
const RATE_DAILY = 1; // per IP
const RESERVED_PER_RUN = 0.1; // conservative — typical run is ~$0.03-0.05

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return new Response(INDEX_HTML, {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    if (request.method === "GET" && url.pathname === "/api/health") {
      return json({ ok: true, dispatch_ready: Boolean(env.GITHUB_TOKEN && env.GITHUB_REPO && env.MEDIA),
        export_ready: Boolean(env.EDIT_GITHUB_TOKEN && env.EDIT_GITHUB_REPO && env.MEDIA) });
    }
    if (request.method === "GET" && url.pathname === "/api/gallery") {
      try { return json(await gallery(env)); }
      catch { return json({ error: "Gallery temporarily unavailable" }, 503); }
    }
    if (request.method === "GET" && url.pathname === "/api/spend") {
      try {
        const date = new Date().toISOString().slice(0, 10);
        const records = await readRecords(env.MEDIA, `spend/${date}/`);
        if (records.some(r => typeof r.total_usd !== "number" || !Number.isFinite(r.total_usd) || r.total_usd < 0)) {
          throw new Error("Invalid spending record");
        }
        const total = records.reduce((sum, record) => sum + record.total_usd, 0);
        if (!Number.isFinite(total) || total < 0) throw new Error("Invalid spending record");
        return json({ date, total_usd: total, estimated: true });
      } catch { return json({ error: "Spending records unavailable" }, 503); }
    }
    if (request.method === "POST" && url.pathname === "/api/assets") {
      try { return await uploadAsset(request, env); }
      catch { return json({ error: "Upload unavailable. Try again later." }, 503); }
    }
    if (request.method === "POST" && url.pathname === "/api/revise") {
      try { return await revise(request, env); }
      catch { return json({ error: "Export could not be confirmed. Do not retry automatically." }, 503); }
    }
    if (request.method === "POST" && url.pathname === "/api/generate") {
      try { return await handleGenerate(request, env); }
      catch { return json({ error: "Generation temporarily unavailable; no automatic retry." }, 503); }
    }
    return json({ error: "not found" }, 404);
  },
};

async function handleGenerate(request, env) {
  // 0. kill switch — checked before anything else costs a KV read or R2 fetch
  if ((await env.RATE_KV.get("pipeline:paused")) === "true") {
    return json({ error: "generation is paused right now — try again later" }, 503);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }

  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return json({ error: "Expected a JSON object" }, 400);
  }
  if (typeof body.script !== "string" || body.script.length > 12000) {
    return json({ error: "Script must be text, at most 12,000 characters" }, 400);
  }

  // 1. access code — compared against a Worker secret, rotated per demo
  if (!body.access_code || body.access_code !== env.ACCESS_CODE) {
    return json({ error: "wrong access code" }, 403);
  }

  const script = (body.script || "").trim();
  const words = script.split(/\s+/).filter(Boolean).length;
  if (words < 40 || words > 120) {
    return json({ error: `script is ${words} words; needs 40-120` }, 400);
  }

  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO || !env.MEDIA) {
    return json({ error: "Generation is not connected yet. You can view the examples below." }, 503);
  }
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const limited = await rateLimited(env.RATE_KV, ip);
  if (limited) return json({ error: `rate limit hit (${limited})` }, 429);

  const runId = crypto.randomUUID();
  try {
    if (!await reserveDay(env, runId)) {
      return json({ error: "Daily demo allowance used. Please try tomorrow (UTC)." }, 429);
    }
  } catch {
    return json({ error: "Cannot verify the spending allowance. Generation is paused." }, 503);
  }
  // Do not release a reservation on failure: a timed-out dispatch may have been accepted.
  try {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/generate.yml/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "content-pipeline-worker",
      },
      body: JSON.stringify({ ref: "main", inputs: { script, run_id: runId } }),
    },
  );
  if (resp.status !== 204) {
    const detail = await resp.text();
    return json({ error: `GitHub dispatch failed (${resp.status}): ${detail.slice(0, 200)}` }, 502);
  }
  return json({ run_id: runId });
  } catch {
    return json({ error: "Dispatch could not be confirmed. Today's allowance is held to avoid duplicate costs." }, 502);
  }
}

async function rateLimited(kv, ip) {
  const now = new Date();
  const hourKey = `rl:${ip}:${now.toISOString().slice(0, 13)}`; // per hour
  const dayKey = `rl:${ip}:${now.toISOString().slice(0, 10)}`; // per day
  const [hourly, daily] = await Promise.all([kv.get(hourKey), kv.get(dayKey)]);
  const hourCount = parseInt(hourly || "0", 10);
  const dayCount = parseInt(daily || "0", 10);
  if (hourCount >= RATE_HOURLY) return `${RATE_HOURLY}/hour`;
  if (dayCount >= RATE_DAILY) return `${RATE_DAILY}/day`;
  await Promise.all([
    kv.put(hourKey, String(hourCount + 1), { expirationTtl: 3700 }),
    kv.put(dayKey, String(dayCount + 1), { expirationTtl: 90000 }),
  ]);
  return null;
}

async function reserveDay(env, runId) {
  const cap = Number(env.DAILY_SPEND_CAP ?? "0.10");
  if (!Number.isFinite(cap) || cap < 0) throw new Error("Invalid spending limit");
  if (cap < RESERVED_PER_RUN) return false;
  const date = new Date().toISOString().slice(0, 10);
  const result = await env.MEDIA.put(`allowances/${date}.json`, JSON.stringify({
    run_id: runId, reserved_usd: RESERVED_PER_RUN, date,
  }), { onlyIf: { etagDoesNotMatch: "*" }, httpMetadata: { contentType: "application/json" } });
  return result !== null;
}

async function readRecords(bucket, prefix) {
  const records = [];
  let cursor;
  do {
    const page = await bucket.list({ prefix, cursor });
    for (const object of page.objects) {
      const value = await bucket.get(object.key);
      if (value) records.push(await value.json());
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return records;
}

async function gallery(env) {
  const records = await readRecords(env.MEDIA, "gallery/");
  records.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const old = await env.MEDIA.get("gallery.json");
  const legacy = old ? await old.json() : [];
  const seen = new Set();
  return [...records, ...legacy].filter(item => {
    if (seen.has(item.video_key)) return false;
    seen.add(item.video_key);
    return true;
  }).slice(0, 12);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}



/* Inlined into the Worker by deploy_frontend.py. Recipes contain no executable code. */
async function revise(request, env) {
  const raw = await request.text();
  if (raw.length > 24000) return json({ error: "Edit request too large" }, 400);
  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: "Invalid JSON" }, 400); }
  if (!body || !env.ACCESS_CODE || body.access_code !== env.ACCESS_CODE) {
    return json({ error: "Enter your access code to export" }, 403);
  }
  if (!env.EDIT_GITHUB_TOKEN || !env.EDIT_GITHUB_REPO || !env.MEDIA) {
    return json({ error: "Export is not connected yet. You can save your edit locally." }, 503);
  }
  if ((await env.RATE_KV.get("pipeline:paused")) === "true") {
    return json({ error: "The studio is paused" }, 503);
  }
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(body.project_id || "")) {
    return json({ error: "Invalid project" }, 400);
  }
  const object = await env.MEDIA.get(`projects/${body.project_id}/project.json`);
  if (!object) return json({ error: "This Reel has no editable source" }, 404);
  const project = await object.json();
  if (!validRecipe(project, body.recipe)) return json({ error: "Invalid edit settings" }, 400);
  const runId = crypto.randomUUID();
  const date = new Date().toISOString().slice(0, 10);
  // One CPU-only export per UTC day, separate from the paid generation allowance.
  const reserved = await env.MEDIA.put(`edit-allowances/${date}.json`, JSON.stringify({ run_id: runId }),
    { onlyIf: { etagDoesNotMatch: "*" } });
  if (!reserved) return json({ error: "Today's edit export allowance is used. Try tomorrow (UTC)." }, 429);
  await env.MEDIA.put(`revisions/${runId}.json`, JSON.stringify({ project_id: body.project_id, recipe: body.recipe }),
    { httpMetadata: { contentType: "application/json" } });
  const response = await fetch(`https://api.github.com/repos/${env.EDIT_GITHUB_REPO}/actions/workflows/revise.yml/dispatches`, {
    method: "POST", headers: { authorization: `Bearer ${env.EDIT_GITHUB_TOKEN}`,
      accept: "application/vnd.github+json", "user-agent": "reel-factory" },
    body: JSON.stringify({ ref: "main", inputs: { run_id: runId } }),
  });
  if (response.status !== 204) return json({ error: "Export could not be started. Allowance retained to avoid duplicates." }, 502);
  return json({ run_id: runId });
}

function validRecipe(project, recipe) {
  if (!recipe || !Array.isArray(recipe.words) || !Array.isArray(recipe.clips)) return false;
  if (recipe.words.length !== project.timings.length || recipe.clips.length !== project.segments.length) return false;
  if (recipe.words.some(w => typeof w !== "string" || !w || w.length > 40 || /\s/.test(w))) return false;
  const uploads = recipe.uploads || [];
  if (!Array.isArray(uploads) || uploads.length > 8 || uploads.some(a => !a
    || !/^projects\/uploads\/[a-f0-9-]{36}\.(mp4|png|jpg)$/.test(a.key)
    || !["image", "video"].includes(a.kind) || (a.kind === "video") !== a.key.endsWith(".mp4"))) return false;
  if (recipe.clips.some(i => !Number.isInteger(i) || i < 0 || i >= (project.sources || project.segments).length + uploads.length)) return false;
  if (recipe.logo_key && ![...(project.sources || []), ...uploads].some(a => a.key === recipe.logo_key && a.kind === "image")) return false;
  const b = recipe.brand;
  return Boolean(b && ["Arial", "DejaVu Sans", "DejaVu Serif"].includes(b.font)
    && ["background", "accent", "card_color"].every(k => b[k] === undefined || /^#[\da-f]{6}$/i.test(b[k]))
    && /^#[\da-f]{6}$/i.test(b.color) && [64, 76, 84].includes(b.size) && [360, 560, 800].includes(b.position));
}

async function uploadAsset(request, env) {
  if (!env.ACCESS_CODE || request.headers.get("x-access-code") !== env.ACCESS_CODE)
    return json({ error: "Enter your access code before uploading" }, 403);
  if (!env.MEDIA || !env.RATE_KV || await env.RATE_KV.get("pipeline:paused") === "true")
    return json({ error: "Uploads unavailable" }, 503);
  const type = request.headers.get("content-type");
  const ext = { "video/mp4": "mp4", "image/png": "png", "image/jpeg": "jpg" }[type];
  if (!ext) return json({ error: "Use MP4, PNG or JPEG" }, 400);
  const limit = 20 * 1024 * 1024;
  if (Number(request.headers.get("content-length")) > limit || !request.body)
    return json({ error: "Maximum file size is 20 MB" }, 413);
  const reader = request.body.getReader();
  const chunks = []; let size = 0;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    size += value.length;
    if (size > limit) { await reader.cancel(); return json({ error: "Maximum file size is 20 MB" }, 413); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  const valid = ext === "mp4" ? size > 12 && String.fromCharCode(...bytes.slice(4, 8)) === "ftyp"
    : ext === "png" ? [137,80,78,71,13,10,26,10].every((v,i) => bytes[i] === v)
    : bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  if (!valid) return json({ error: "File content does not match its format" }, 400);
  const day = new Date().toISOString().slice(0, 10);
  let reserved = false;
  for (let slot = 0; slot < 8; slot++) {
    if (await env.MEDIA.put(`upload-allowances/${day}/${slot}`, "reserved", { onlyIf: { etagDoesNotMatch: "*" } })) {
      reserved = true; break;
    }
  }
  if (!reserved) return json({ error: "Today's eight upload slots are used. Try tomorrow (UTC)." }, 429);
  const key = `projects/uploads/${crypto.randomUUID()}.${ext}`;
  await env.MEDIA.put(key, bytes, { httpMetadata: { contentType: type } });
  return json({ key, kind: ext === "mp4" ? "video" : "image", label: "Uploaded media" });
}

const INDEX_HTML = __INDEX_HTML_JSON__;
