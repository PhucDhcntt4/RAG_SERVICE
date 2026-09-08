// DOM unit harness for automatic connection; no browser or real API calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
class Element {
  constructor() {
    this.listeners = {};
    this.style = {};
    this.classList = { toggle() {}, add() {}, remove() {} };
    this.value = '';
    this.children = [];
    this.hidden = false;
    this.open = false;
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute() {}
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  close() { this.open = false; }
  showModal() { this.open = true; }
  focus() {}
}
const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
const calls = [];
const context = {
  document: { getElementById: id => elements[id], createElement: () => new Element(), querySelectorAll: () => [] },
  window: { location: { hostname: '127.0.0.1' }, addEventListener() {} },
  fetch: async (url, options) => {
    calls.push({url, options});
    return {ok: true, json: async () => url.includes('/chat') ? {
      status: 'answered', answer: 'Synthetic response [S1]', sources: [], elapsed_ms: 1,
      context: 'Synthetic context', retrieval_query: 'Test'
    } : {documents: []}};
  },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'app/static/admin.js'), 'utf8'), context);
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await settle();
  assert.equal(calls[0].url, '/admin-api/v1/documents?limit=21&offset=0');
  assert.equal(calls[0].options.headers['X-RAG-Local-UI'], '1');
  assert.equal(calls[0].options.headers.Authorization, undefined);
  assert.equal(elements['login-panel'].hidden, true);
  assert.equal(elements['disconnect'].hidden, true);
  assert.equal(elements['add-document'].disabled, false);
  assert.equal(elements['test-rag'].disabled, false);
  elements['test-rag'].listeners.click();
  elements['chat-input'].value = 'Test';
  elements['chat-form'].listeners.submit({preventDefault() {}});
  await settle();
  assert.equal(calls[1].url, '/admin-api/v1/chat');
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.equal(elements['chat-error'].textContent, '');
  console.log('Local UI auto-connect and chat without an API key: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
