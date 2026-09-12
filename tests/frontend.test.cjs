const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function browser(status, saved = null) {
  const nodes = new Map();
  function element() {
    return {
      hidden: true, disabled: false, textContent: '', value: '', listeners: {},
      classList: { toggle() {} },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      querySelector() { return this.cost ||= element(); },
      replaceChildren() {}, append() {},
    };
  }
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const stored = new Map(saved ? [['reel-factory-run', JSON.stringify(saved)]] : []);
  const timers = [];
  const context = vm.createContext({
    document: { getElementById: get, querySelector: get, createElement: element },
    localStorage: {
      getItem: key => stored.get(key) || null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key),
    },
    AbortSignal, Date,
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    fetch: async url => ({
      ok: true,
      json: async () => url === '/api/health' ? { dispatch_ready: true }
        : url === '/api/gallery' ? [] : status,
    }),
  });
  vm.runInContext(fs.readFileSync('frontend/app.js', 'utf8'), context);
  return { context, get, stored, timers };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('refresh resumes a job and completion restores the generate button', async () => {
  const app = browser({ state: 'done', cost_usd: 0.04, video_key: 'videos/a.mp4' },
    { runId: 'abc123', startedAt: Date.now() });
  await flush();
  assert.equal(app.get('result').hidden, false);
  assert.equal(app.get('generate').disabled, false);
  assert.equal(app.stored.has('reel-factory-run'), false);
  assert.match(app.get('download').href, /videos\/a.mp4$/);
});

test('quality failure shows issues without displaying a successful result', async () => {
  const app = browser({ state: 'needs_review', issues: ['black frames'] },
    { runId: 'abc123', startedAt: Date.now() });
  await flush();
  assert.match(app.get('progress-note').textContent, /black frames/);
  assert.equal(app.get('result').hidden, true);
  assert.equal(app.get('generate').disabled, false);
});

test('timeout preserves job and allows checking it without another generation', async () => {
  const app = browser({ state: 'running' }, { runId: 'abc123', startedAt: 1 });
  await flush();
  assert.equal(app.get('resume').hidden, false);
  assert.equal(app.get('generate').disabled, true);
  assert.equal(app.stored.has('reel-factory-run'), true);
  app.get('resume').listeners.click();
  await flush();
  assert.equal(app.get('resume').hidden, true);
  assert.equal(app.timers.length, 1);
});

test('free stages without a cost element do not break status rendering', async () => {
  const app = browser({});
  app.get('#stages li[data-stage="align"]').querySelector = () => null;
  assert.doesNotThrow(() => vm.runInContext('setStages({align: {done: true, cost_usd: 0}})', app.context));
  await flush();
});

test('worker rejects malformed script types before dispatch', async () => {
  const source = fs.readFileSync('worker/worker.js', 'utf8')
    .replace('export default {', 'globalThis.worker = {')
    .replace('__INDEX_HTML_JSON__', '""');
  const context = vm.createContext({ Response, URL });
  vm.runInContext(source, context);
  for (const body of [null, [], { script: 123 }, { script: 'x'.repeat(12001) }]) {
    const response = await context.worker.fetch({
      url: 'https://example.test/api/generate', method: 'POST', json: async () => body,
    }, { RATE_KV: { get: async () => null } });
    assert.equal(response.status, 400);
  }
});


function workerContext() {
  const source = fs.readFileSync('worker/worker.js', 'utf8')
    .replace('export default {', 'globalThis.worker = {')
    .replace('__INDEX_HTML_JSON__', '\"\"');
  const context = vm.createContext({ Response, URL, Date, crypto: require('node:crypto').webcrypto });
  vm.runInContext(source, context);
  return context;
}

test('simultaneous reservations admit exactly one run', async () => {
  const context = workerContext();
  const objects = new Map();
  const env = { DAILY_SPEND_CAP: '0.10', MEDIA: {
    async put(key, value, options) {
      assert.equal(options.onlyIf.etagDoesNotMatch, '*');
      if (objects.has(key)) return null;
      objects.set(key, value);
      return { key };
    },
  } };
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) => context.reserveDay(env, String(i))));
  assert.equal(results.filter(Boolean).length, 1);
  assert.equal(objects.size, 1);
});

test('zero or invalid budget cannot reserve a run', async () => {
  const context = workerContext();
  const MEDIA = { put() { throw Error('must not write'); } };
  assert.equal(await context.reserveDay({ MEDIA, DAILY_SPEND_CAP: '0' }, 'x'), false);
  await assert.rejects(context.reserveDay({ MEDIA, DAILY_SPEND_CAP: 'bad' }, 'x'));
});

test('gallery merges all pages and legacy examples without duplicates', async () => {
  const context = workerContext();
  const values = {
    'gallery/a.json': { video_key: 'a.mp4', created_at: '2026-09-12' },
    'gallery/b.json': { video_key: 'b.mp4', created_at: '2026-09-13' },
    'gallery.json': [{ video_key: 'a.mp4' }, { video_key: 'demo.mp4' }],
  };
  const MEDIA = {
    async list({ cursor }) { return cursor
      ? { objects: [{ key: 'gallery/b.json' }], truncated: false }
      : { objects: [{ key: 'gallery/a.json' }], truncated: true, cursor: 'next' }; },
    async get(key) { return { json: async () => values[key] }; },
  };
  const result = await context.gallery({ MEDIA });
  assert.deepEqual(Array.from(result, x => x.video_key), ['b.mp4', 'a.mp4', 'demo.mp4']);
});
const validRequest = () => ({
  url: 'https://example.test/api/generate', method: 'POST',
  headers: new Headers({ 'cf-connecting-ip': '127.0.0.1' }),
  json: async () => ({ script: 'word '.repeat(60), access_code: 'test' }),
});

test('ambiguous dispatch retains the reservation and does not retry GitHub', async () => {
  const context = workerContext();
  let reserved = false;
  let dispatches = 0;
  context.fetch = async () => { dispatches++; throw Error('network timeout'); };
  const env = {
    ACCESS_CODE: 'test', GITHUB_TOKEN: 'test', GITHUB_REPO: 'test/repo',
    RATE_KV: { get: async () => null, put: async () => {} },
    MEDIA: { put: async () => {
      if (reserved) return null;
      reserved = true;
      return {};
    } },
  };
  assert.equal((await context.worker.fetch(validRequest(), env)).status, 502);
  assert.equal((await context.worker.fetch(validRequest(), env)).status, 429);
  assert.equal(dispatches, 1);
});

test('storage failure blocks paid dispatch', async () => {
  const context = workerContext();
  context.fetch = async () => { throw Error('must not dispatch'); };
  const response = await context.worker.fetch(validRequest(), {
    ACCESS_CODE: 'test', GITHUB_TOKEN: 'test', GITHUB_REPO: 'test/repo',
    RATE_KV: { get: async () => null, put: async () => {} },
    MEDIA: { put: async () => { throw Error('storage unavailable'); } },
  });
  assert.equal(response.status, 503);
});
