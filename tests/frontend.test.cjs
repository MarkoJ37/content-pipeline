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
        : url.endsWith('gallery.json') ? [] : status,
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
