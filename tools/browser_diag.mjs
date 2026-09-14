/**
 * 浏览器端诊断脚本（用 DevTools Protocol 直连 Edge/Chrome，不装任何依赖）。
 *
 * 用途：在不打开可见窗口的情况下加载页面，抓取
 *   - 控制台日志与异常
 *   - 未捕获错误
 *   - 失败的网络请求（4xx/5xx）
 *   - 页面关键 DOM 状态
 * 用来定位"页面卡在加载中"这类纯前端问题。
 *
 * 用法：node tools/browser_diag.mjs [url] [edgePath]
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const URL_TO_OPEN = process.argv[2] || 'http://127.0.0.1:8770/';
const EDGE = process.argv[3]
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9333;

const profileDir = mkdtempSync(join(tmpdir(), 'ss-diag-'));

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForTarget() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const targets = await response.json();
      const page = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* 还没起来 */
    }
    await sleep(500);
  }
  throw new Error('无法连接到浏览器的调试端口');
}

const child = spawn(EDGE, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`,
  '--window-size=1440,900',
  'about:blank',
], { stdio: 'ignore' });

let socket;
let nextId = 1;
const pending = new Map();
const logs = [];
const exceptions = [];
const failedRequests = [];

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function main() {
  const target = await waitForTarget();
  socket = new WebSocket(target.webSocketDebuggerUrl);

  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve);
    socket.addEventListener('error', reject);
  });

  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
      return;
    }
    if (message.method === 'Runtime.consoleAPICalled') {
      logs.push({
        type: message.params.type,
        text: (message.params.args || [])
          .map((a) => a.value ?? a.description ?? a.type)
          .join(' '),
      });
    }
    if (message.method === 'Runtime.exceptionThrown') {
      const d = message.params.exceptionDetails || {};
      exceptions.push({
        text: d.text,
        description: d.exception?.description || '',
        url: d.url,
        line: d.lineNumber,
        column: d.columnNumber,
      });
    }
    if (message.method === 'Log.entryAdded') {
      const entry = message.params.entry || {};
      if (entry.level === 'error') {
        logs.push({ type: 'log-error', text: `${entry.text} ${entry.url || ''}` });
      }
    }
    if (message.method === 'Network.loadingFailed') {
      failedRequests.push({
        url: message.params.documentURL || '',
        error: message.params.errorText,
        type: message.params.type,
      });
    }
    if (message.method === 'Network.responseReceived') {
      const r = message.params.response || {};
      if (r.status >= 400) {
        failedRequests.push({ url: r.url, status: r.status, type: message.params.type });
      }
    }
  });

  await send('Runtime.enable');
  await send('Log.enable');
  await send('Network.enable');
  await send('Page.enable');

  await send('Page.navigate', { url: URL_TO_OPEN });
  // 给页面足够时间完成首屏渲染与首轮数据请求
  await sleep(9000);

  const state = await send('Runtime.evaluate', {
    expression: `(() => {
      const content = document.querySelector('#content');
      const html = content ? content.innerHTML : '(无 #content)';
      return JSON.stringify({
        title: document.title,
        readyState: document.readyState,
        nsPresent: typeof window.StockSpace,
        views: window.StockSpace && window.StockSpace.views
          ? Object.keys(window.StockSpace.views) : null,
        appPresent: !!(window.StockSpace && window.StockSpace.app),
        contentLength: html.length,
        contentHead: html.slice(0, 400),
        hasBootPlaceholder: html.includes('boot-placeholder'),
        hasLoadingText: html.includes('加载中'),
        navActive: Array.from(document.querySelectorAll('[data-nav].active'))
          .map(n => n.getAttribute('data-nav')),
        sessionPill: (document.querySelector('#sessionPill') || {}).textContent,
        sourcePill: (document.querySelector('#sourcePill') || {}).textContent,
        loadBarHidden: (document.querySelector('#loadBar') || {}).hidden,
      });
    })()`,
    returnByValue: true,
  });

  console.log('='.repeat(72));
  console.log('页面状态');
  console.log('='.repeat(72));
  try {
    const parsed = JSON.parse(state.result.value);
    for (const [key, value] of Object.entries(parsed)) {
      console.log(`  ${key}: ${typeof value === 'string' ? value.slice(0, 300) : JSON.stringify(value)}`);
    }
  } catch {
    console.log('  无法解析:', state.result.value);
  }

  console.log('');
  console.log('='.repeat(72));
  console.log(`未捕获异常 (${exceptions.length})`);
  console.log('='.repeat(72));
  if (!exceptions.length) console.log('  无');
  for (const e of exceptions.slice(0, 10)) {
    console.log(`  ✗ ${e.text} @ ${e.url || '?'}:${(e.line ?? 0) + 1}:${(e.column ?? 0) + 1}`);
    if (e.description) console.log(`    ${e.description.split('\n').slice(0, 6).join('\n    ')}`);
  }

  console.log('');
  console.log('='.repeat(72));
  console.log(`控制台输出 (${logs.length})`);
  console.log('='.repeat(72));
  for (const l of logs.slice(0, 25)) {
    console.log(`  [${l.type}] ${l.text.slice(0, 240)}`);
  }
  if (!logs.length) console.log('  无');

  console.log('');
  console.log('='.repeat(72));
  console.log(`失败的网络请求 (${failedRequests.length})`);
  console.log('='.repeat(72));
  for (const r of failedRequests.slice(0, 25)) {
    console.log(`  ${r.status || r.error}  ${r.url}`);
  }
  if (!failedRequests.length) console.log('  无');

  const ok = exceptions.length === 0 && !failedRequests.some((r) => r.status >= 400);
  console.log('');
  console.log(ok ? '结论: 页面无 JS 异常、无失败请求' : '结论: 存在前端错误，见上方明细');
  return ok ? 0 : 1;
}

let exitCode = 0;
try {
  exitCode = await main();
} catch (error) {
  console.error('诊断脚本失败:', error.message);
  exitCode = 2;
} finally {
  try { socket?.close(); } catch { /* 忽略 */ }
  child.kill();
  await sleep(600);
  try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
}
process.exit(exitCode);
