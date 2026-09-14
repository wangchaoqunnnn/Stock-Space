/**
 * 交互测试：用 DevTools Protocol 派发**真实鼠标事件**，验证导航是否可点击，
 * 并诊断"是什么元素盖在了目标上方"。
 *
 * 这类问题（透明遮罩挡住点击、z-index 压住、pointer-events 误设）
 * 单靠读代码很难确认，必须真的点一下。
 *
 * 用法：node tools/interaction_test.mjs [url]
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

const PORT = 9346;
const profileDir = mkdtempSync(join(tmpdir(), 'ss-int-'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(BROWSER, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--force-color-profile=srgb',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profileDir}`,
  '--window-size=1440,900', 'about:blank',
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
const pageErrors = [];
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
  if (m.method === 'Runtime.exceptionThrown') {
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
    pageErrors.push((m.params.args || []).map((a) => a.value ?? a.description).join(' '));
  }
});
const send = (method, params = {}) => new Promise((res) => {
  const id = nextId++;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return { __error: r.exceptionDetails.exception?.description || r.exceptionDetails.text };
  return r.result.value;
};

/** 在指定坐标派发一次真实点击（按下 + 抬起） */
async function realClick(x, y) {
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  await sleep(40);
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
}

await send('Runtime.enable');
await send('Page.enable');
await send('Page.navigate', { url: BASE_URL });
await sleep(11000);

console.log('='.repeat(76));
console.log('1) 关键元素的可点击性（elementFromPoint 命中测试）');
console.log('='.repeat(76));
const hits = await evaluate(`(() => {
  const describe = (el) => {
    if (!el) return '(null)';
    const cs = getComputedStyle(el);
    return el.tagName.toLowerCase()
      + (el.id ? '#' + el.id : '')
      + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : '')
      + ' [pointer-events:' + cs.pointerEvents + ' z-index:' + (cs.zIndex || 'auto') + ']';
  };
  const out = {};
  // 侧边栏几个导航项的几何中心
  document.querySelectorAll('.sidenav .nav-item').forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2);
    const cy = Math.round(r.top + r.height / 2);
    const at = document.elementFromPoint(cx, cy);
    out['nav-item[' + i + '] ' + (el.getAttribute('data-nav') || '')] = {
      rect: Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height),
      hit: describe(at),
      isSelfOrChild: !!(at && (at === el || el.contains(at))),
    };
  });
  // 顶部栏与内容区也测一下
  const extra = [['topbar', '.topbar'], ['content', '.content'], ['refreshBtn', '#refreshBtn']];
  extra.forEach(([name, sel]) => {
    const el = document.querySelector(sel);
    if (!el) { out[name] = 'missing'; return; }
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.left + Math.min(r.width / 2, 200));
    const cy = Math.round(r.top + r.height / 2);
    const at = document.elementFromPoint(cx, cy);
    out[name] = { hit: describe(at), isSelfOrChild: !!(at && (el === at || el.contains(at))) };
  });
  return JSON.stringify(out, null, 1);
})()`);
console.log(typeof hits === 'string' ? hits : JSON.stringify(hits));

console.log('');
console.log('='.repeat(76));
console.log('2) 覆盖层状态（可能挡住点击的元素）');
console.log('='.repeat(76));
const overlays = await evaluate(`(() => {
  const check = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return 'missing';
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      hiddenAttr: el.hasAttribute('hidden'),
      display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
      pointerEvents: cs.pointerEvents, zIndex: cs.zIndex, position: cs.position,
      rect: Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height),
      covers: r.width > 0 && r.height > 0 && r.left <= 100 && r.top <= 400 && r.right >= 100 && r.bottom >= 400,
    };
  };
  return JSON.stringify({
    '#modalRoot': check('#modalRoot'),
    '#navBackdrop': check('#navBackdrop'),
    '#loadBar': check('#loadBar'),
    '#toast': check('#toast'),
    '#searchPanel': check('#searchPanel'),
    '#sidenav': check('#sidenav'),
  }, null, 1);
})()`);
console.log(typeof overlays === 'string' ? overlays : JSON.stringify(overlays));

console.log('');
console.log('='.repeat(76));
console.log('3) 真实点击测试：逐个点侧边栏导航项，看 hash 是否变化');
console.log('='.repeat(76));
const targets = await evaluate(`JSON.stringify(Array.from(document.querySelectorAll('.sidenav .nav-item')).map(el => {
  const r = el.getBoundingClientRect();
  return { nav: el.getAttribute('data-nav'), href: el.getAttribute('href'),
           x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) };
}))`);
const list = JSON.parse(targets);
let passed = 0;
let checked = 0;
for (const t of list) {
  const before = await evaluate('location.hash');
  // "部署自检"是独立页面（selfcheck.html），点击后会离开单页应用，
  // 因此它的期望结果不是 hash 变化，而是 location 变化。
  const isExternalPage = String(t.href || '').indexOf('.html') >= 0;
  const beforeUrl = await evaluate('location.href');
  await realClick(t.x, t.y);
  await sleep(1200);
  const after = await evaluate('location.hash');
  const afterUrl = await evaluate('location.href');
  const activeNav = await evaluate(`(() => { const el = document.querySelector('[data-nav].active'); return el ? el.getAttribute('data-nav') : '(none)'; })()`);

  let ok;
  if (isExternalPage) {
    ok = beforeUrl !== afterUrl;
  } else if (before === `#/${t.nav}`) {
    // 已经在该页，点击不应报错；只要仍在同一路由即视为正常
    ok = after === before && activeNav === t.nav;
  } else {
    ok = after === `#/${t.nav}` && activeNav === t.nav;
  }
  checked += 1;
  if (ok) passed += 1;
  const detail = isExternalPage
    ? `跳转独立页面: ${beforeUrl.split('/').pop()} -> ${afterUrl.split('/').pop()}`
    : `hash: ${before} -> ${after}  激活项: ${activeNav}`;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} 点击 ${String(t.nav).padEnd(12)} 期望 #/${t.nav}  ${detail}`);
}

// 点过"部署自检"后已经离开单页应用，用导航回到主页面再测顶部栏按钮
await send('Page.navigate', { url: BASE_URL });
await sleep(11000);

console.log('');
console.log('='.repeat(76));
console.log('4) 顶部栏按钮真实点击');
console.log('='.repeat(76));
for (const [name, sel] of [['刷新', '#refreshBtn'], ['主题', '#themeBtn']]) {
  const g = await evaluate(`(() => { const el = document.querySelector('${sel}');
    if (!el) return null; const r = el.getBoundingClientRect();
    return JSON.stringify({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) }); })()`);
  if (!g) { console.log(`  [FAIL] ${name}: 元素不存在`); continue; }
  const pos = JSON.parse(g);
  const themeBefore = await evaluate('document.documentElement.getAttribute("data-theme")');
  await realClick(pos.x, pos.y);
  await sleep(1500);
  const themeAfter = await evaluate('document.documentElement.getAttribute("data-theme")');
  const changed = name === '主题' ? themeBefore !== themeAfter : true;
  console.log(`  ${changed ? '[OK]  ' : '[FAIL]'} ${name}按钮（${pos.x},${pos.y}）` +
    (name === '主题' ? `  主题: ${themeBefore} -> ${themeAfter}` : ''));
}
// 切回浅色便于观察
await evaluate(`try { localStorage.setItem('ss_theme','light'); document.documentElement.setAttribute('data-theme','light'); } catch(e) {}`);

console.log('');
console.log('='.repeat(76));
console.log(`5) 页面 JS 错误 (${pageErrors.length})`);
console.log('='.repeat(76));
if (!pageErrors.length) console.log('  无');
for (const e of pageErrors.slice(0, 8)) console.log('  ✗', String(e).split('\n').slice(0, 4).join('\n    '));

console.log('');
console.log(`结论: 导航点击 ${passed}/${checked} 项生效` +
  (passed === checked ? '（全部正常）' : '（存在不可点击项，见上）'));

ws.close();
child.kill();
await sleep(400);
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
process.exit(passed === checked ? 0 : 1);
