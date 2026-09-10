const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const requestSource = source.slice(source.indexOf('async function request('), source.indexOf('window.argusRequest = request;'));

async function connect(pageUrl, needsToken = false) {
  const calls = [], stored = new Map();
  let prompts = 0;
  const context = vm.createContext({
    URL, Headers, window: {location: new URL(pageUrl)},
    sessionStorage: {
      getItem: key => stored.get(key),
      setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key)
    },
    askAccessToken: async () => { prompts++; return 'test-token'; },
    fetch: async (url, options) => {
      calls.push(options.headers);
      const ok = !needsToken || options.headers.get('X-Argus-Token') === 'test-token';
      return {ok, status: ok ? 200 : 401, json: async () => ok ? {status: 'AVAILABLE'} : {error: 'authentication required'}};
    }
  });
  vm.runInContext(requestSource, context);
  const result = await context.request('/api/quant/decision');
  return {calls, prompts, result};
}

(async () => {
  for (const host of ['127.0.0.1', 'localhost', '[::1]']) {
    const {calls, prompts, result} = await connect(`http://${host}:8765/?view=harness`);
    assert.equal(calls[0].get('X-Argus-Local'), '1');
    assert.equal(calls[0].get('X-Argus-Token'), null);
    assert.equal(prompts, 0);
    assert.equal(result.status, 'AVAILABLE');
  }
  for (const host of ['192.168.1.2', 'example.com', '127.0.0.1.attacker.test']) {
    const {calls, prompts} = await connect(`http://${host}:8765/`, true);
    assert.equal(calls[0].get('X-Argus-Local'), null);
    assert.equal(prompts, 1);
    assert.equal(calls[1].get('X-Argus-Token'), 'test-token');
  }
  console.log('Local automatic connection and remote token login passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
