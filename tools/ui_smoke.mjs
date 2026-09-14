/**
 * UI 几何检查：逐个页面、逐个组件验证"图形元素真的有尺寸"。
 * ============================================================================
 * 为什么需要它：有一类前端缺陷**接口测试和 DOM 存在性检查都发现不了** ——
 * 元素确实在 DOM 里、文本也对，但渲染尺寸是 0x0，于是界面上什么都没有。
 *
 * 实测踩到的例子：进度条填充是 `<i>` 元素，CSS 漏写 `display:block`，
 * 浏览器按 `display:inline` 渲染，而 **inline 元素不接受 width/height**，
 * 于是 `width:27.9%` 与 `height:100%` 全被忽略 → 0x0 →
 * "情绪分拆解 / 板块强弱 / 涨停板块聚集"三处进度条完全不显示。
 *
 * 本脚本对每个页面检查这些"必须有尺寸"的元素：
 *   .bar-fill     进度条填充
 *   .bar-track    进度条轨道
 *   .progress > i 细进度条
 *   .stack-bar > i 堆叠占比条
 *   .spinner      加载指示
 *   canvas        图表（必须已被绘制）
 *
 * 用法：node tools/ui_smoke.mjs [baseUrl]
 * 退出码：0 = 全部通过；1 = 存在零尺寸元素
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = (process.argv[2] || 'http://127.0.0.1:8770/').replace(/\/$/, '');
const BROWSER = [
  process.env.SS_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/microsoft-edge', '/usr/bin/google-chrome', '/usr/bin/chromium',
].filter(Boolean).find((p) => existsSync(p));
if (!BROWSER) { console.error('找不到浏览器'); process.exit(2); }

const PORT = 9349;
const profileDir = mkdtempSync(join(tmpdir(), 'ss-ui-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

//: 需要检查的页面（路由 + 等待时间，首次加载需要拉数据所以给久一点）
const PAGES = [
  { name: '仪表盘', hash: '#/dashboard', wait: 14000 },
  { name: '情绪周期', hash: '#/emotion', wait: 14000 },
  { name: '行情中枢', hash: '#/market', wait: 14000 },
  { name: '策略选股', hash: '#/screener', wait: 14000 },
  { name: '回测分析', hash: '#/backtest', wait: 9000 },
  { name: '内存监控', hash: '#/memory', wait: 10000 },
  { name: '数据源', hash: '#/datasources', wait: 10000 },
  { name: '系统状态', hash: '#/system', wait: 12000 },
];

const GEOMETRY_PROBE = `(() => {
  const specs = [
    ['.bar-fill', '进度条填充', 1, 4],
    ['.bar-track', '进度条轨道', 60, 6],
    ['.progress > i', '细进度条', 1, 3],
    ['.stack-bar > i', '堆叠占比条', 1, 8],
    ['.spinner', '加载指示', 8, 8],
  ];
  const problems = [];
  const summary = [];
  for (const [sel, label, minW, minH] of specs) {
    const nodes = Array.from(document.querySelectorAll(sel));
    // 加载中的 spinner 是允许存在的；其余元素只检查"已渲染出来"的那些
    const visible = nodes.filter(n => {
      const r = n.getBoundingClientRect();
      return r.width > 0 || r.height > 0 || n.offsetParent !== null;
    });
    const zero = nodes.filter(n => {
      const r = n.getBoundingClientRect();
      return r.width < minW || r.height < minH;
    });
    if (nodes.length) {
      summary.push(label + ' x' + nodes.length + ' (零尺寸 ' + zero.length + ')');
      if (zero.length) {
        const z = zero[0];
        const cs = getComputedStyle(z);
        problems.push({
          selector: sel, label,
          count: zero.length, total: nodes.length,
          size: Math.round(z.getBoundingClientRect().width) + 'x' +
                Math.round(z.getBoundingClientRect().height),
          display: cs.display, width: cs.width, height: cs.height,
          parent: z.parentElement ? z.parentElement.className : '',
        });
      }
    }
  }
  const canvases = Array.from(document.querySelectorAll('canvas'));
  const blank = canvases.filter(c => c.width === 0 || c.height === 0);
  if (canvases.length) summary.push('canvas x' + canvases.length + ' (空 ' + blank.length + ')');
  if (blank.length) {
    problems.push({ selector: 'canvas', label: '图表画布', count: blank.length,
      total: canvases.length, size: '0x0', display: '', width: '', height: '', parent: '' });
  }
  return JSON.stringify({ summary, problems });
})()`;

const child = spawn(BROWSER, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', `--remote-debugging-port=${PORT}`,
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
  if (r.exceptionDetails) return JSON.stringify({ error: r.exceptionDetails.text });
  return r.result.value;
};

await send('Runtime.enable');
await send('Page.enable');

console.log('='.repeat(76));
console.log(`UI 几何检查（${BASE}）`);
console.log(`浏览器: ${BROWSER}`);
console.log('='.repeat(76));

let totalProblems = 0;

// 先在主入口加载一次，让前端脚本就绪
await send('Page.navigate', { url: BASE + '/' });
await sleep(9000);

for (const p of PAGES) {
  // 用 hash 切换路由（应用是单页哈希路由，无需重新加载脚本）
  await evaluate(`location.hash = '${p.hash}'`);
  await sleep(p.wait);
  const raw = await evaluate(GEOMETRY_PROBE);
  let result;
  try { result = JSON.parse(raw); } catch { result = { summary: [], problems: [{ label: '探测失败', size: raw }] }; }

  const bad = (result.problems || []).length;
  totalProblems += bad;
  console.log('');
  console.log(`  ${bad === 0 ? '[OK]  ' : '[FAIL]'} ${p.name}  ${(result.summary || []).join(' · ') || '(无图形元素)'}`);
  for (const problem of result.problems || []) {
    console.log(`          ✗ ${problem.label} ${problem.count}/${problem.total} 个零尺寸` +
      `  实测 ${problem.size}  display=${problem.display}  width=${problem.width}  height=${problem.height}` +
      (problem.parent ? `  父元素=${problem.parent}` : ''));
  }
}

console.log('');
console.log(totalProblems === 0
  ? '结论: 所有图形元素均已正常渲染'
  : `结论: 发现 ${totalProblems} 处零尺寸图形元素（详见上方）`);

ws.close();
child.kill();
await sleep(400);
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
process.exit(totalProblems === 0 ? 0 : 1);
