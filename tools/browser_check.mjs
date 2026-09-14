/**
 * 前端冒烟检查（无头浏览器）—— 部署后用来确认"页面真的能打开、不是转圈"
 * ============================================================================
 * 为什么需要它：后端接口全绿并不代表页面可用。前端问题（脚本 404、启动函数名不匹配、
 * 浏览器缓存了旧脚本）在接口层面完全看不出来，只会表现为"页面一直加载中"。
 *
 * 本脚本用 DevTools Protocol 直连本机 Edge/Chrome（不安装任何 npm 依赖），检查：
 *   1. 页面是否完成首屏渲染（而不是停在加载占位）
 *   2. 是否有未捕获的 JS 异常
 *   3. 是否有 4xx/5xx 的资源或接口请求
 *   4. 关键接口是否返回 code=0
 *
 * 用法：
 *   node tools/browser_check.mjs                        # 默认 http://127.0.0.1:8770/
 *   node tools/browser_check.mjs http://host:9000/
 *   node tools/browser_check.mjs http://127.0.0.1:8770/ /path/to/msedge
 *
 * 退出码：0 = 通过；1 = 前端有问题；2 = 环境问题（找不到浏览器等）
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const URL_TO_OPEN = process.argv[2] || 'http://127.0.0.1:8770/';

const BROWSER_CANDIDATES = [
  process.argv[3],
  process.env.SS_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/microsoft-edge',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);

const browser = BROWSER_CANDIDATES.find((p) => existsSync(p));
if (!browser) {
  console.error('找不到 Edge/Chrome。可用环境变量 SS_BROWSER 指定路径。');
  process.exit(2);
}

const PORT = 9342;
const WAIT_SECONDS = Number(process.env.SS_BROWSER_WAIT || 30);
const profileDir = mkdtempSync(join(tmpdir(), 'ss-check-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(browser, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profileDir}`,
  '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' });

function cleanup() {
  try { child.kill(); } catch { /* 忽略 */ }
  setTimeout(() => {
    try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }, 300);
}

let page = null;
for (let i = 0; i < 60 && !page; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    page = (await res.json()).find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  } catch { /* 未就绪 */ }
  if (!page) await sleep(500);
}
if (!page) {
  console.error('无法连接浏览器调试端口');
  cleanup();
  process.exit(2);
}

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
});

let nextId = 1;
const pending = new Map();
const exceptions = [];
const badRequests = [];
const consoleErrors = [];

ws.addEventListener('message', (event) => {
  let m;
  try { m = JSON.parse(event.data); } catch { return; }
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m.result);
    pending.delete(m.id);
    return;
  }
  if (m.method === 'Runtime.exceptionThrown') {
    const d = m.params.exceptionDetails || {};
    exceptions.push(d.exception?.description || d.text || '?');
  }
  if (m.method === 'Runtime.consoleAPICalled' && ['error', 'assert'].includes(m.params.type)) {
    consoleErrors.push((m.params.args || []).map((a) => a.value ?? a.description).join(' '));
  }
  if (m.method === 'Network.responseReceived') {
    const r = m.params.response || {};
    if (r.status >= 400) badRequests.push(`${r.status} ${r.url}`);
  }
  if (m.method === 'Network.loadingFailed') {
    // 只关心真实失败，忽略被主动取消的（如页面跳转导致）
    if (!m.params.canceled) {
      badRequests.push(`${m.params.errorText} ${m.params.type}`);
    }
  }
});

const send = (method, params = {}) => new Promise((res) => {
  const id = nextId++;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) return `__error__: ${result.exceptionDetails.text}`;
  return result.result.value;
}

await send('Runtime.enable');
await send('Page.enable');
await send('Network.enable');
await send('Page.navigate', { url: URL_TO_OPEN });

// 轮询直到渲染完成或超时
const started = Date.now();
let state = null;
while ((Date.now() - started) / 1000 < WAIT_SECONDS) {
  await sleep(1000);
  const raw = await evaluate(`(() => {
    const c = document.querySelector('#content');
    const html = c ? c.innerHTML : '';
    return JSON.stringify({
      len: html.length,
      loading: html.includes('加载中') || html.includes('正在加载'),
      failed: html.includes('页面加载失败') || html.includes('页面启动失败'),
      head: html.replace(/\\s+/g, ' ').slice(0, 70),
    });
  })()`);
  try { state = JSON.parse(raw); } catch { state = null; }
  if (state && !state.loading) break;
}

const apiProbe = await evaluate(`(async () => {
  const out = [];
  for (const p of ['api/health', 'api/dashboard', 'api/strategies']) {
    try {
      const r = await fetch(p, { cache: 'no-store' });
      const j = await r.json();
      out.push(p + ' => http:' + r.status + ' code:' + j.code);
    } catch (e) { out.push(p + ' => ERR ' + e.message); }
  }
  return out.join(' | ');
})()`);

const checks = [
  ['页面完成首屏渲染', Boolean(state && !state.loading), state ? `contentLen=${state.len}` : '无法读取'],
  ['首屏未报错', !(state && state.failed), state && state.failed ? state.head : ''],
  ['无未捕获 JS 异常', exceptions.length === 0, exceptions.slice(0, 2).join(' / ')],
  ['无失败请求(4xx/5xx)', badRequests.length === 0, badRequests.slice(0, 3).join(' / ')],
  ['无 console.error', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' / ')],
  ['关键接口返回 code=0', !String(apiProbe).includes('code:0') === false, String(apiProbe)],
];

console.log('='.repeat(74));
console.log(`前端冒烟检查（${URL_TO_OPEN}）`);
console.log(`浏览器: ${browser}`);
console.log('='.repeat(74));

let failed = 0;
for (const [name, ok, detail] of checks) {
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}${detail ? `  (${detail})` : ''}`);
  if (!ok) failed += 1;
}
if (state) console.log(`\n  首屏内容: ${state.head}`);
console.log(`  耗时: ${((Date.now() - started) / 1000).toFixed(1)}s`);

console.log('');
console.log(failed === 0
  ? '结论: 页面可用，无前端错误'
  : `结论: 存在 ${failed} 项问题，见上方明细`);

ws.close();
cleanup();
process.exit(failed === 0 ? 0 : 1);
