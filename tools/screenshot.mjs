/**
 * 页面截图（无头浏览器）—— 用于目视检查配色/可读性。
 * 用法：node tools/screenshot.mjs [url] [输出文件] [宽x高]
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const URL_TO_OPEN = process.argv[2] || 'http://127.0.0.1:8770/';
const OUT = resolve(process.argv[3] || 'screenshot.png');
const SIZE = (process.argv[4] || '1440x900').split('x').map(Number);
const DELAY = Number(process.env.SS_SHOT_DELAY || 12000);

const BROWSER = [
  process.env.SS_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/microsoft-edge', '/usr/bin/google-chrome', '/usr/bin/chromium',
].filter(Boolean).find((p) => existsSync(p));
if (!BROWSER) { console.error('找不到浏览器'); process.exit(2); }

const PORT = 9343;
const profileDir = mkdtempSync(join(tmpdir(), 'ss-shot-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(BROWSER, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--force-device-scale-factor=1',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profileDir}`,
  `--window-size=${SIZE[0]},${SIZE[1]}`, 'about:blank',
], { stdio: 'ignore' });

let page = null;
for (let i = 0; i < 60 && !page; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    page = (await res.json()).find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  } catch { /* 未就绪 */ }
  if (!page) await sleep(500);
}
if (!page) { child.kill(); console.error('无法连接调试端口'); process.exit(2); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
});

let nextId = 1;
const pending = new Map();
ws.addEventListener('message', (event) => {
  const m = JSON.parse(event.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((res) => {
  const id = nextId++;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});

await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: SIZE[0], height: SIZE[1], deviceScaleFactor: 1, mobile: false,
});
await send('Page.navigate', { url: URL_TO_OPEN });
await sleep(DELAY);

const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
console.log(`截图已保存: ${OUT}  (${SIZE[0]}x${SIZE[1]})`);

ws.close();
child.kill();
await sleep(400);
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
process.exit(0);
