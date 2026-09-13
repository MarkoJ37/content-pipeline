const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function browser(status, saved = null) {
  const nodes = new Map();
  function element(tag = "div") {
    return {
      tag, children: [], style: {}, hidden: true, disabled: false, textContent: '', value: '', listeners: {},
      setAttribute() {}, scrollIntoView() {}, focus() {},
      querySelectorAll(tag) { return this.children.flatMap(child => [
        ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); },
      classList: { toggle() {} },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      querySelector() { return this.cost ||= element(); },
      replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); },
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
  vm.runInContext(fs.readFileSync('frontend/editor.js', 'utf8'), context);
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


const editProject = {
  id: 'demo', audio_key: 'projects/demo/audio.wav',
  timings: [{ word: 'Hello', start: 0, end: 0.5 }, { word: 'world', start: 0.5, end: 1 }],
  segments: [{ key: 'projects/demo/one.mp4', start: 0, end: 0.5, label: 'one' },
             { key: 'projects/demo/two.mp4', start: 0.5, end: 1, label: 'two' }],
  brand: { font: 'Arial', color: '#ffffff', size: 76, position: 560 },
};

test('editor corrects words, replaces clips, and saves and restores a draft', async () => {
  const app = browser({}); await flush();
  app.context.fetch = async () => ({ ok: true, json: async () => editProject });
  await app.context.openEditor('demo');
  app.get('caption-editor').querySelectorAll('input')[1].value = 'friend';
  app.get('scene-editor').querySelectorAll('select')[0].value = '1';
  app.get('brand-color').value = '#d1ee8a';
  app.get('save-edit').listeners.click();
  const draft = JSON.parse(app.stored.get('reel-edit:demo'));
  assert.deepEqual(draft.words, ['Hello', 'friend']);
  assert.deepEqual(draft.clips.map(Number), [1, 1]);
  assert.equal(draft.brand.color, '#d1ee8a');
  await app.context.openEditor('demo');
  assert.equal(app.get('caption-editor').querySelectorAll('input')[1].value, 'friend');
  assert.equal(editProject.timings[1].word, 'world');
});

test('brand presets roundtrip in browser storage', async () => {
  const app = browser({}); await flush();
  app.context.showBrand(editProject.brand);
  app.get('brand-font').value = 'DejaVu Serif';
  app.get('save-brand').listeners.click();
  app.get('brand-font').value = 'Arial';
  app.get('load-brand').listeners.click();
  assert.equal(app.get('brand-font').value, 'DejaVu Serif');
});

test('worker revision validation rejects invalid words and clip indices', () => {
  const context = workerContext();
  const recipe = { words: ['Hello', 'friend'], clips: [1, 0], brand: editProject.brand };
  assert.equal(context.validRecipe(editProject, recipe), true);
  assert.equal(context.validRecipe(editProject, { ...recipe, clips: [99, 0] }), false);
  assert.equal(context.validRecipe(editProject, { ...recipe, words: ['two words', 'friend'] }), false);
});

test('edit exports have a separate atomic allowance and use the revision workflow', async () => {
  const context = workerContext();
  let writes = new Map(), calls = [];
  context.fetch = async (url, options) => { calls.push([url, JSON.parse(options.body)]); return { status: 204 }; };
  const env = { ACCESS_CODE: 'test', EDIT_GITHUB_TOKEN: 'edit-only', EDIT_GITHUB_REPO: 'test/repo',
    RATE_KV: { get: async () => null }, MEDIA: {
      get: async () => ({ json: async () => editProject }),
      put: async (key, value, options) => {
        if (options.onlyIf && writes.has(key)) return null;
        writes.set(key, value); return {};
      },
    } };
  const request = { text: async () => JSON.stringify({ access_code: 'test', project_id: 'demo',
    recipe: { words: ['Hello', 'friend'], clips: [1, 0], brand: editProject.brand } }) };
  assert.equal((await context.revise(request, env)).status, 200);
  assert.equal((await context.revise(request, env)).status, 429);
  assert.equal(calls.length, 1);
  assert.match(calls[0][0], /revise.yml/);
  assert.equal([...writes.keys()].some(key => key.startsWith('allowances/')), false);
  const stored = [...writes].find(([key]) => key.startsWith('revisions/'))[1];
  assert.equal(stored.includes('access_code'), false);
});


test('uploads require authorization and a supported file signature', async () => {
  const context = workerContext();
  const env = { ACCESS_CODE: 'test', RATE_KV: {get: async () => null}, MEDIA: {put: async () => {throw Error('must not write');}} };
  assert.equal((await context.uploadAsset(new Request('https://example.test/api/assets', {method: 'POST', body: 'invalid'}), env)).status, 403);
  assert.equal((await context.uploadAsset(new Request('https://example.test/api/assets', {method: 'POST', headers: {'x-access-code':'test','content-type':'image/png'}, body: 'invalid'}), env)).status, 400);
});

test('uploads have an atomic eight-file daily allowance', async () => {
  const context = workerContext(); const writes = new Map();
  const env = {ACCESS_CODE: 'test', RATE_KV: {get: async () => null}, MEDIA: {put: async (key, data, options) => {
    if (options.onlyIf && writes.has(key)) return null;
    writes.set(key, data); return {};
  }}};
  const request = () => new Request('https://example.test/api/assets', {method:'POST', headers:{'x-access-code':'test','content-type':'image/png'}, body: new Uint8Array([137,80,78,71,13,10,26,10,0])});
  const results = await Promise.all(Array.from({length:10}, () => context.uploadAsset(request(), env)));
  assert.equal(results.filter(r => r.status === 200).length, 8);
  assert.equal(results.filter(r => r.status === 429).length, 2);
});
