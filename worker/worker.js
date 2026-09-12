/**
 * The Worker is the gate (see CLAUDE.md "Frontend"): it serves the static app,
 * holds the GitHub token, and enforces protections so a leaked URL or access
 * code cannot burn the AI keys overnight while nobody's watching:
 *   0. kill switch          (KV "pipeline:paused" — flip by hand in the
 *                            Cloudflare dashboard for an instant, no-redeploy stop)
 *   1. shared access code   (env.ACCESS_CODE secret; 403 on mismatch)
 *   2. per-IP rate limit    (Workers KV: RATE_HOURLY/hr, RATE_DAILY/day; 429)
 *   3. global daily limit   (Workers KV: GLOBAL_DAILY — caps total dispatches
 *                            regardless of how many distinct IPs hit the gate)
 *   4. daily spend ceiling  (reads spend.json from R2; refuses once spend +
 *                            a conservative per-run reserve would exceed the
 *                            cap, not just once the cap is already blown —
 *                            spend.json only updates when a run finishes, so
 *                            checking the raw total alone would let several
 *                            runs slip through mid-flight before it catches up)
 *
 * Not an auth system — headers and counters, exactly as specified. KV isn't
 * atomic (read-then-write, not compare-and-swap), so a true simultaneous
 * burst can still let a handful of extra requests through a limit — accepted
 * as a bounded, low-probability gap rather than reached for Durable Objects.
 *
 * Bindings injected at deploy time (scripts/deploy_frontend.py):
 *   RATE_KV (KV), ACCESS_CODE (secret), R2_PUBLIC_BASE, DAILY_SPEND_CAP,
 *   GITHUB_TOKEN (secret, optional until repo exists), GITHUB_REPO (optional).
 * The static page is inlined as INDEX_HTML below the export.
 */

const RATE_HOURLY = 1; // per IP
const RATE_DAILY = 1; // per IP
const GLOBAL_DAILY = 1; // total dispatches per day, any IP
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
      return json({ ok: true, dispatch_ready: Boolean(env.GITHUB_TOKEN && env.GITHUB_REPO) });
    }
    if (request.method === "POST" && url.pathname === "/api/generate") {
      return handleGenerate(request, env);
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

  // 2. per-IP rate limit in KV
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const limited = await rateLimited(env.RATE_KV, ip);
  if (limited) {
    return json({ error: `rate limit hit (${limited})` }, 429);
  }

  // 3. global daily limit — bounds total exposure even if many different IPs
  // somehow have the access code (e.g. it leaked)
  const globalLimited = await globalDailyLimited(env.RATE_KV);
  if (globalLimited) {
    return json({ error: `global daily generation limit reached (${GLOBAL_DAILY}/day) — try tomorrow` }, 429);
  }

  // 4. daily spend ceiling, with a safety margin for the accounting lag —
  // spend.json only updates when a run *finishes*, so several runs could
  // dispatch in the gap between "spent" being read and it catching up
  const cap = parseFloat(env.DAILY_SPEND_CAP || "0.10");
  const spent = await todaysSpend(env);
  if (spent + RESERVED_PER_RUN > cap) {
    return json(
      { error: `daily spend ceiling reached ($${spent.toFixed(2)} of $${cap.toFixed(2)}) — try tomorrow` },
      503,
    );
  }

  // 5. dispatch the pipeline on GitHub Actions
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return json(
      { error: "pipeline dispatch not connected yet (GitHub repo pending)" },
      503,
    );
  }
  const runId = crypto.randomUUID();
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

async function globalDailyLimited(kv) {
  const key = `rl:global:${new Date().toISOString().slice(0, 10)}`;
  const count = parseInt((await kv.get(key)) || "0", 10);
  if (count >= GLOBAL_DAILY) return true;
  await kv.put(key, String(count + 1), { expirationTtl: 90000 });
  return false;
}

async function todaysSpend(env) {
  // spend.json: {"date": "YYYY-MM-DD", "total_usd": 0.12} — published to R2 by the pipeline
  try {
    const resp = await fetch(`${env.R2_PUBLIC_BASE}/spend.json`, {
      cf: { cacheTtl: 30 },
    });
    if (!resp.ok) return 0; // no spend recorded yet
    const spend = await resp.json();
    const today = new Date().toISOString().slice(0, 10);
    return spend.date === today ? Number(spend.total_usd) || 0 : 0;
  } catch {
    return 0; // fail open on read errors; the rate limit still applies
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const INDEX_HTML = __INDEX_HTML_JSON__;
