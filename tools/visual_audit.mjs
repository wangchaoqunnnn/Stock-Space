/**
 * 逐像素分析当前页面的视觉问题：把截图重绘到 canvas，按网格统计亮度，
 * 并输出对比度与"可疑遮罩区域"。
 *
 * 覆盖深/浅两种主题，避免因浏览器主题偏好不同而误判。
 * 用法：node tools/visual_audit.mjs [url]
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE_URL = process.argv[2] || 'http://127.0.0.1:8770/';
const BROWSER = [
  process.env.SS_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean).find((p) => existsSync(p));
if (!BROWSER) { console.error('找不到浏览器'); process.exit(2); }

const PORT = 9345;
const profileDir = mkdtempSync(join(tmpdir(), 'ss-vis-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(BROWSER, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--force-color-profile=srgb', '--force-device-scale-factor=1',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' });

let page = null;
for (let i = 0; i < 60 && !page; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    page = (await res.json()).find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  } catch { /* 未就绪 */ }
  if (!page) await sleep(500);
}
if (!page) { child.kill(); console.error('无法连接'); process.exit(2); }

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
  if (r.exceptionDetails) return `__error__: ${r.exceptionDetails.text}`;
  return r.result.value;
};

await send('Runtime.enable');
await send('Page.enable');

/** 在页面里对整屏做亮度网格分析（用 canvas 重绘截图，逐像素读取） */
const ANALYSIS_SCRIPT = (base64) => `(async () => {
  const img = new Image();
  img.src = 'data:image/png;base64,${base64}';
  await img.decode();
  const W = img.width, H = img.height;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, W, H).data;
  const at = (x, y) => { const i = (y * W + x) * 4; return [px[i], px[i+1], px[i+2]]; };
  const luma = (p) => 0.2126*p[0] + 0.7152*p[1] + 0.0722*p[2];

  // 网格亮度（8x6）
  const GX = 8, GY = 6, grid = [];
  for (let gy = 0; gy < GY; gy++) {
    const row = [];
    for (let gx = 0; gx < GX; gx++) {
      let sum = 0, n = 0;
      for (let y = Math.floor(H*gy/GY); y < Math.floor(H*(gy+1)/GY); y += 5)
        for (let x = Math.floor(W*gx/GX); x < Math.floor(W*(gx+1)/GX); x += 5) { sum += luma(at(x,y)); n++; }
      row.push(sum/n);
    }
    grid.push(row);
  }

  // 顶部栏三个高度采样（看是否有一条横向"带"）
  const band = (yf) => {
    let sum = 0, n = 0;
    for (let x = Math.floor(W*0.25); x < W*0.9; x += 4) { sum += luma(at(Math.floor(x), Math.floor(H*yf))); n++; }
    return sum/n;
  };

  // 颜色多样性：统计不同颜色的数量（判断是否"糊成一片"）
  const seen = new Set();
  for (let y = 0; y < H; y += 3) for (let x = 0; x < W; x += 3) {
    const p = at(x,y); seen.add(((p[0]>>3)<<10)|((p[1]>>3)<<5)|(p[2]>>3));
  }

  return JSON.stringify({
    size: W + 'x' + H,
    grid: grid.map(r => r.map(v => Math.round(v))),
    gridRange: Math.round(Math.max(...grid.flat()) - Math.min(...grid.flat())),
    gridMin: Math.round(Math.min(...grid.flat())),
    gridMax: Math.round(Math.max(...grid.flat())),
    bandTop2: Math.round(band(0.02)),
    bandTop5: Math.round(band(0.05)),
    bandTop8: Math.round(band(0.08)),
    bandMid: Math.round(band(0.5)),
    distinctColors: seen.size,
  });
})()`;

async function audit(theme) {
  await send('Page.navigate', { url: BASE_URL });
  await sleep(1500);
  // 设定主题（应用会读取 localStorage）
  await evaluate(`try { localStorage.setItem('ss_theme', '${theme}'); } catch(e) {} document.documentElement.setAttribute('data-theme', '${theme}');`);
  await send('Page.reload');
  await sleep(11000);

  const styles = JSON.parse(await evaluate(`(() => {
    const cs = (sel, props) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const s = getComputedStyle(el);
      const o = {};
      props.forEach(p => o[p] = s.getPropertyValue(p));
      return o;
    };
    return JSON.stringify({
      theme: document.documentElement.getAttribute('data-theme'),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      bodyImage: getComputedStyle(document.body).backgroundImage.slice(0, 90),
      topbar: cs('.topbar', ['background-color','backdrop-filter']),
      card: cs('.card', ['background-color','box-shadow']),
      button: cs('.btn', ['background-color','border-color']),
    });
  })()`));

  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const analysis = JSON.parse(await evaluate(ANALYSIS_SCRIPT(shot.data)));

  console.log('='.repeat(76));
  console.log(`主题: ${theme}`);
  console.log('='.repeat(76));
  console.log(`  body 背景        : ${styles.bodyBg}`);
  console.log(`  body 背景图      : ${styles.bodyImage || 'none'}`);
  console.log(`  顶部栏            : ${JSON.stringify(styles.topbar)}`);
  console.log(`  卡片              : ${JSON.stringify(styles.card)}`);
  console.log(`  按钮              : ${JSON.stringify(styles.button)}`);
  console.log('');
  console.log(`  截图尺寸          : ${analysis.size}`);
  console.log(`  网格亮度极差      : ${analysis.gridRange}  (min ${analysis.gridMin} / max ${analysis.gridMax})`);
  console.log(`  颜色多样性        : ${analysis.distinctColors} 种（每 3px 采样）`);
  console.log(`  横向亮度带        : 顶部2%=${analysis.bandTop2}  5%=${analysis.bandTop5}  8%=${analysis.bandTop8}  中部=${analysis.bandMid}`);
  console.log('  网格亮度图:');
  for (const row of analysis.grid) console.log('    ' + row.map((v) => String(v).padStart(4)).join(''));
  console.log('');
}

await audit('light');
await audit('dark');

ws.close();
child.kill();
await sleep(400);
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
process.exit(0);
