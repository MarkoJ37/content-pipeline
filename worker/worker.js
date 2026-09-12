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
      return json({ ok: true, dispatch_ready: Boolean(env.GITHUB_TOKEN && env.GITHUB_REPO && env.MEDIA) });
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

const INDEX_HTML = __INDEX_HTML_JSON__;
