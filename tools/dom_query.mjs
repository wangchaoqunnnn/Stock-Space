/**
 * DOM 查询工具：加载页面后执行一段表达式并打印结果（无头浏览器）。
 * 用于确认"某个面板到底渲染出了什么"，比读代码可靠。
 *
 * 用法：
 *   node tools/dom_query.mjs <url> <selector>          # 打印匹配元素的文本
 *   node tools/dom_query.mjs <url> --js "<表达式>"      # 执行自定义表达式
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [, , URL_TO_OPEN = 'http://127.0.0.1:8770/', ARG2 = 'body', ARG3] = process.argv;
const isJs = ARG2 === '--js';
//: 表达式较长时（含引号/反斜杠/中文）经 shell 传递极易被转义搞坏，
//: 因此支持 `--js @路径` 从文件读取。
const EXPRESSION = isJs
  ? (ARG3 && ARG3.startsWith('@') ? readFileSync(ARG3.slice(1), 'utf8') : ARG3)
  : null;
const SELECTOR = isJs ? null : ARG2;
const WAIT = Number(process.env.SS_WAIT || 12000);
//: 默认桌面视口；设 SS_WIN=360,780 可验证移动端断点下的真实布局。
const WINDOW_SIZE = process.env.SS_WIN || '1440,900';

const BROWSER = [
  process.env.SS_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/microsoft-edge', '/usr/bin/google-chrome', '/usr/bin/chromium',
].filter(Boolean).find((p) => existsSync(p));
if (!BROWSER) { console.error('找不到浏览器'); process.exit(2); }

const PORT = 9348;
const profileDir = mkdtempSync(join(tmpdir(), 'ss-domq-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(BROWSER, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, `--window-size=${WINDOW_SIZE}`, 'about:blank',
], { stdio: 'ignore' });

let page = null;
for (let i = 0; i < 60 && !page; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    page = (await res.json()).find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  } catch { /* 未就绪 */ }
  if (!page) await sleep(500);
}
if (!page) { child.kill(); console.error('无法连接浏览器'); process.exit(2); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
});
let nextId = 1;
const pending = new Map();
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((res) => {
  const id = nextId++;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return `__ERROR__: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`;
  return r.result.value;
};

await send('Runtime.enable');
await send('Page.enable');
//: --window-size 在 headless=new 下不改变 CSS 视口宽度，必须用设备指标覆盖才能
//: 真正触发移动端媒体查询（否则 innerWidth 恒为 550，测不出断点布局）。
await send('Emulation.setDeviceMetricsOverride', {
  width: Number(WINDOW_SIZE.split(',')[0]), height: Number(WINDOW_SIZE.split(',')[1]),
  deviceScaleFactor: 1, mobile: false,
});
await send('Page.navigate', { url: URL_TO_OPEN });
await sleep(WAIT);

if (isJs) {
  const result = await evaluate(EXPRESSION);
  console.log(typeof result === 'string' ? result : JSON.stringify(result, null, 2));
} else {
  const result = await evaluate(`(() => {
    const nodes = Array.from(document.querySelectorAll(${JSON.stringify(SELECTOR)}));
    if (!nodes.length) return JSON.stringify({ matched: 0, note: '没有匹配到元素' });
    return JSON.stringify({
      matched: nodes.length,
      items: nodes.slice(0, 30).map(n => ({
        tag: n.tagName.toLowerCase(),
        cls: typeof n.className === 'string' ? n.className : '',
        text: (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
        children: n.children.length,
        htmlLen: n.innerHTML.length,
      })),
    }, null, 2);
  })()`);
  console.log(result);
}

ws.close();
child.kill();
await sleep(400);
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
process.exit(0);
