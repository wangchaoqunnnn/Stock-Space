/* ============================================================================
   util.js —— 基础工具(无依赖)
   包含: 命名空间、DOM 助手、数值/日期格式化、可排序表格、提示与弹窗。
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace = global.StockSpace || {};

  /* ------------------------------------------------------------------ 常量 */
  SS.STRATEGY_LABELS = {
    trend: '趋势狙击',
    quiet_rise: '潜涨雷达',
    limit_up_pullback: '涨停回调低吸',
    n_pattern: 'N 字战法',
    pattern: '形态扫描'
  };
  SS.STRATEGY_ORDER = ['trend', 'quiet_rise', 'limit_up_pullback', 'n_pattern', 'pattern'];
  SS.RANK_LABELS = {
    gainers: '涨幅榜', losers: '跌幅榜', amount: '成交额榜',
    turnover: '换手率榜', speed: '涨速榜', amplitude: '振幅榜', volume_ratio: '量比榜'
  };
  SS.CHANNEL_LABELS = {
    news: '资讯', flash: '快讯', announcement: '公告', rumor: '传闻'
  };
  SS.PHASE_LABELS = {
    ice: '冰点', start: '启动', ferment: '发酵', climax: '高潮', ebb: '退潮'
  };
  SS.CAP_LABELS = {
    snapshot: '全市场快照', quote: '批量实时行情', kline: '历史K线', minute: '当日分时',
    rank: '榜单', sector: '板块列表', sector_members: '板块成分股', money_flow: '个股资金流',
    sector_flow: '板块资金流', limit_up_pool: '涨停/炸板池', breadth: '市场涨跌家数',
    code_list: '全市场代码表', attention: '人气/关注度', news_flash: '财经快讯',
    announcement: '公司公告', finance: '财务指标', health: '连通性探测'
  };

  /* ------------------------------------------------------------------ DOM */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'html') node.innerHTML = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
        else if (key.slice(0, 2) === 'on' && typeof value === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(value));
      });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        node.appendChild(typeof child === 'string' || typeof child === 'number'
          ? document.createTextNode(String(child)) : child);
      });
    }
    return node;
  }

  /* -------------------------------------------------------------- 数值格式 */
  function isNum(value) {
    return value !== null && value !== undefined && value !== '' && isFinite(Number(value));
  }
  function toNum(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  function fixed(value, digits) {
    if (!isNum(value)) return '--';
    return toNum(value).toFixed(digits === undefined ? 2 : digits);
  }

  /** 涨跌方向 class: 正=红、负=绿 */
  function dirClass(value, zeroAsFlat) {
    if (!isNum(value)) return 'flat';
    var n = toNum(value);
    if (n > 0) return 'up';
    if (n < 0) return 'down';
    return zeroAsFlat === false ? '' : 'flat';
  }

  /** 带正负号的涨跌值(可指定颜色) */
  function pct(value, digits, opts) {
    opts = opts || {};
    if (!isNum(value)) return '<span class="flat">--</span>';
    var n = toNum(value);
    var text = (n > 0 ? '+' : '') + n.toFixed(digits === undefined ? 2 : digits) + '%';
    return '<span class="' + dirClass(n) + '">' + text + '</span>';
  }

  function num(value, digits, opts) {
    opts = opts || {};
    if (!isNum(value)) return '<span class="flat">--</span>';
    var n = toNum(value);
    var text = n.toFixed(digits === undefined ? 2 : digits);
    if (opts.sign && n > 0) text = '+' + text;
    return opts.colored ? '<span class="' + dirClass(n) + '">' + text + '</span>' : text;
  }

  /** 金额/成交额自动单位 */
  function money(value, digits) {
    if (!isNum(value)) return '--';
    var n = toNum(value);
    var abs = Math.abs(n);
    if (abs >= 1e12) return (n / 1e12).toFixed(digits === undefined ? 2 : digits) + ' 万亿';
    if (abs >= 1e8) return (n / 1e8).toFixed(digits === undefined ? 2 : digits) + ' 亿';
    if (abs >= 1e4) return (n / 1e4).toFixed(digits === undefined ? 2 : digits) + ' 万';
    return n.toFixed(0);
  }

  /** 成交量(股)自动单位 */
  function volume(value) {
    if (!isNum(value)) return '--';
    var n = toNum(value);
    var abs = Math.abs(n);
    if (abs >= 1e8) return (n / 1e8).toFixed(2) + ' 亿股';
    if (abs >= 1e4) return (n / 1e4).toFixed(2) + ' 万股';
    return n.toFixed(0) + ' 股';
  }

  function count(value) {
    if (!isNum(value)) return '--';
    return toNum(value).toLocaleString('zh-CN');
  }

  function secs(value) {
    if (!isNum(value)) return '--';
    var n = Math.max(0, Math.round(toNum(value)));
    if (n < 60) return n + ' 秒';
    if (n < 3600) return Math.floor(n / 60) + ' 分 ' + (n % 60) + ' 秒';
    if (n < 86400) return Math.floor(n / 3600) + ' 小时 ' + Math.floor((n % 3600) / 60) + ' 分';
    return Math.floor(n / 86400) + ' 天 ' + Math.floor((n % 86400) / 3600) + ' 小时';
  }

  function timeText(ts) {
    if (!isNum(ts)) return '--';
    var d = new Date(toNum(ts) * 1000);
    if (isNaN(d.getTime())) return '--';
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
           pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function ago(seconds) {
    if (!isNum(seconds)) return '--';
    var n = Math.max(0, toNum(seconds));
    if (n < 60) return Math.round(n) + ' 秒前';
    if (n < 3600) return Math.round(n / 60) + ' 分钟前';
    if (n < 86400) return Math.round(n / 3600) + ' 小时前';
    return Math.round(n / 86400) + ' 天前';
  }

  /* -------------------------------------------------------------- 结构片段 */
  function kpi(label, value, foot, tone) {
    return '<div class="kpi-card' + (tone ? ' tone-' + tone : '') + '">' +
      '<div class="k-label">' + esc(label) + '</div>' +
      '<div class="k-value">' + (value === null || value === undefined ? '--' : value) + '</div>' +
      (foot ? '<div class="k-foot">' + foot + '</div>' : '') +
      '</div>';
  }

  function notice(kind, title, body) {
    return '<div class="notice ' + (kind || 'info') + '">' +
      '<div class="n-body"><strong>' + esc(title) + '</strong>' +
      (body ? '<div>' + body + '</div>' : '') + '</div></div>';
  }

  function badge(text, kind) {
    return '<span class="badge' + (kind ? ' ' + kind : '') + '">' + esc(text) + '</span>';
  }

  function emptyState(text, hint) {
    return '<div class="empty-state"><div><p>' + esc(text || '暂无数据') + '</p>' +
      (hint ? '<p class="small muted">' + esc(hint) + '</p>' : '') + '</div></div>';
  }

  /**
   * 细进度条。
   * 值为 0 时**不渲染填充元素**（只留轨道）—— 配合 CSS 的 min-width，
   * 这样"空"与"接近 0"在视觉上可以区分，也避免出现 0 宽度的无效元素。
   */
  function progress(value, max, cls) {
    var ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
    var fill = ratio > 0
      ? '<i style="width:' + (ratio * 100).toFixed(1) + '%"></i>'
      : '';
    return '<div class="progress' + (cls ? ' ' + cls : '') + '">' + fill + '</div>';
  }

  function stackBar(segments) {
    var total = segments.reduce(function (sum, seg) { return sum + Math.max(0, toNum(seg.value)); }, 0);
    if (total <= 0) return '<div class="stack-bar"></div>';
    var html = '<div class="stack-bar">';
    segments.forEach(function (seg) {
      var w = Math.max(0, toNum(seg.value)) / total * 100;
      if (w <= 0) return;
      html += '<i style="width:' + w.toFixed(2) + '%;background:' + esc(seg.color) + '" title="' +
        esc(seg.label + ' ' + seg.value) + '"></i>';
    });
    html += '</div>';
    html += '<div class="stack-legend">' + segments.map(function (seg) {
      return '<span><i style="background:' + esc(seg.color) + '"></i>' + esc(seg.label) +
        ' ' + count(seg.value) + '</span>';
    }).join('') + '</div>';
    return html;
  }

  function barList(rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return emptyState('暂无数据');
    var max = rows.reduce(function (m, r) { return Math.max(m, Math.abs(toNum(r.value))); }, 0) || 1;
    //: 外层 .bar-list 是这三列（名称/轨道/数值）的**共享网格**，
    //: 每个 .bar-row 用 `grid-template-columns: subgrid` 继承同一套列宽。
    //: 这样列宽由整列最宽的一行决定 —— 数值既不会被挤到第二行，
    //: 各行轨道的起点与终点也严格对齐（此前逐行独立 flex，宽度随文字长短浮动）。
    var html = rows.map(function (row) {
      var ratio = Math.abs(toNum(row.value)) / max * 100;
      var color = row.color || (toNum(row.value) >= 0 ? 'var(--up)' : 'var(--down)');
      var text = row.text !== undefined ? row.text
        : (opts.digits !== undefined ? toNum(row.value).toFixed(opts.digits) : num(row.value, 2));
      return '<div class="bar-row">' +
        '<span class="bar-label" title="' + esc(row.label) + '">' + esc(row.label) + '</span>' +
        '<span class="bar-track' + (opts.thin ? ' thin' : '') + '">' +
        '<i class="bar-fill" style="width:' + ratio.toFixed(1) + '%;background:' + color + '"></i></span>' +
        '<span class="bar-value">' + text + '</span></div>';
    }).join('');
    return '<div class="bar-list">' + html + '</div>';
  }

  /* ---------------------------------------------------- 可排序表格(事件委托) */
  var SORT_STATE = new WeakMap();

  /**
   * 渲染可排序表格。
   * @param {HTMLElement} container 容器
   * @param {Array} cols 列定义 [{label, num, sort(item), key, noSort}]
   * @param {Array} items 数据
   * @param {Function} cellFn (item, index) => [html, html, ...]
   * @param {Object} opts {empty, onRowClick, initialSort}
   */
  function sortableTable(container, cols, items, cellFn, opts) {
    opts = opts || {};
    var state = SORT_STATE.get(container) || { si: null, asc: true };
    if (state.si === null && opts.initialSort !== undefined) {
      state.si = opts.initialSort;
      state.asc = opts.initialAsc !== false;
    }
    SORT_STATE.set(container, state);

    function rawValue(item, index) {
      var col = cols[index];
      if (!col) return null;
      if (typeof col.sort === 'function') return col.sort(item);
      return col.key !== undefined ? item[col.key] : null;
    }

    function compare(a, b) {
      var x = rawValue(a, state.si);
      var y = rawValue(b, state.si);
      var nx = typeof x === 'number' ? x : parseFloat(String(x === null || x === undefined ? '' : x).replace(/[%+,，\s]/g, ''));
      var ny = typeof y === 'number' ? y : parseFloat(String(y === null || y === undefined ? '' : y).replace(/[%+,，\s]/g, ''));
      var xn = isFinite(nx), yn = isFinite(ny);
      if (xn && yn) return nx - ny;
      if (xn !== yn) return xn ? 1 : -1;   // 数字优先在前
      return String(x === null || x === undefined ? '' : x)
        .localeCompare(String(y === null || y === undefined ? '' : y), 'zh-Hans-CN');
    }

    function render() {
      var arr = items.slice();
      if (state.si !== null) {
        arr.sort(compare);
        if (!state.asc) arr.reverse();
      }
      if (!arr.length) {
        container.innerHTML = emptyState(opts.empty || '暂无数据', opts.emptyHint);
        return;
      }
      var head = '<tr><th class="col-no">No</th>' + cols.map(function (col, index) {
        var cls = (col.num ? 'n' : '') + (col.center ? ' c' : '') + (col.noSort ? ' no-sort' : '');
        var arrow = state.si === index ? (state.asc ? ' <span class="arrow">▲</span>' : ' <span class="arrow">▼</span>') : '';
        return '<th class="' + cls.trim() + '" data-i="' + index + '">' + esc(col.label) + arrow + '</th>';
      }).join('') + '</tr>';

      var body = arr.map(function (item, index) {
        var cells = cellFn(item, index) || [];
        var tds = cols.map(function (col, ci) {
          var cls = (col.num ? 'n' : '') + (col.center ? ' c' : '');
          return '<td class="' + cls.trim() + '">' + (cells[ci] === undefined || cells[ci] === null ? '' : cells[ci]) + '</td>';
        }).join('');
        var code = item && (item.code || item.key) ? esc(item.code || item.key) : '';
        return '<tr class="' + (opts.onRowClick ? 'clickable' : '') + '"' +
          (code ? ' data-code="' + code + '"' : '') + '>' +
          '<td class="col-no">' + (index + 1) + '</td>' + tds + '</tr>';
      }).join('');

      container.innerHTML = '<div class="table-wrap"><table class="grid">' +
        '<thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
    }

    if (!container._ssBound) {
      container._ssBound = true;
      container.addEventListener('click', function (event) {
        var th = event.target.closest ? event.target.closest('th[data-i]') : null;
        if (th && !th.classList.contains('no-sort') && container.contains(th)) {
          var index = Number(th.dataset.i);
          state.asc = state.si === index ? !state.asc : true;
          state.si = index;
          render();
          return;
        }
        var row = event.target.closest ? event.target.closest('tr[data-code]') : null;
        if (row && opts.onRowClick) {
          var code = row.dataset.code;
          var item = items.filter(function (it) { return (it.code || it.key) === code; })[0];
          if (item) opts.onRowClick(item, event);
        }
      });
    }
    render();
    return container;
  }

  /* -------------------------------------------------------------- Toast/Modal */
  var toastTimer = null;
  function toast(message, kind, ms) {
    var node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.className = 'toast show' + (kind ? ' ' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.className = 'toast'; }, ms || 3400);
  }

  function modal(title, bodyHtml, footNodes) {
    var root = $('#modalRoot');
    if (!root) return;
    root.hidden = false;
    root.innerHTML = '';
    var box = el('div', { class: 'modal' });
    var head = el('div', { class: 'modal-head' }, [
      el('h3', { text: title }),
      el('button', { class: 'icon-btn', title: '关闭', html: '✕', onclick: closeModal })
    ]);
    var body = el('div', { class: 'modal-body', html: bodyHtml });
    box.appendChild(head);
    box.appendChild(body);
    if (footNodes && footNodes.length) {
      var foot = el('div', { class: 'modal-foot' });
      footNodes.forEach(function (n) { foot.appendChild(n); });
      box.appendChild(foot);
    }
    root.appendChild(box);
    root.onclick = function (event) { if (event.target === root) closeModal(); };
    return body;
  }
  function closeModal() {
    var root = $('#modalRoot');
    if (!root) return;
    root.hidden = true;
    root.innerHTML = '';
  }

  function confirmDialog(title, message) {
    return new Promise(function (resolve) {
      var okBtn = el('button', { class: 'btn primary', text: '确认', onclick: function () { closeModal(); resolve(true); } });
      var cancelBtn = el('button', { class: 'btn', text: '取消', onclick: function () { closeModal(); resolve(false); } });
      modal(title, '<p>' + esc(message) + '</p>', [cancelBtn, okBtn]);
    });
  }

  /* -------------------------------------------------------------- 杂项 */
  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait || 250);
    };
  }

  function download(filename, content, mime) {
    var blob = new Blob([content], { type: mime || 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  function toCsv(header, rows) {
    function cell(value) {
      var text = value === null || value === undefined ? '' : String(value);
      return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    }
    return '\ufeff' + [header.map(cell).join(',')]
      .concat(rows.map(function (row) { return row.map(cell).join(','); }))
      .join('\r\n');
  }

  function pick(obj, path, fallback) {
    var cursor = obj;
    var parts = String(path).split('.');
    for (var i = 0; i < parts.length; i++) {
      if (cursor === null || cursor === undefined) return fallback;
      cursor = cursor[parts[i]];
    }
    return cursor === undefined ? fallback : cursor;
  }

  function scoreTone(score) {
    if (!isNum(score)) return '';
    var n = toNum(score);
    if (n >= 85) return 'ok';
    if (n >= 70) return 'info';
    if (n >= 55) return 'warn';
    return '';
  }

  function reasonList(reasons) {
    if (!reasons || !reasons.length) return '<p class="muted small">无条件明细</p>';
    return '<div class="reason-list">' + reasons.map(function (r) {
      var cls = r.passed ? 'pass' : 'fail';
      var value = r.value === null || r.value === undefined ? '' :
        (typeof r.value === 'number' ? num(r.value, 2) : esc(String(r.value)));
      return '<div class="reason-item ' + cls + '">' +
        '<span class="r-name">' + (r.passed ? '✔' : '✘') + ' ' + esc(r.name) + '</span>' +
        '<span class="r-detail">' + esc(r.threshold || '') +
        (r.detail ? ' · ' + esc(r.detail) : '') + '</span>' +
        '<span class="r-val">' + value + '</span></div>';
    }).join('') + '</div>';
  }

  function kvList(pairs) {
    return '<div class="kv-list">' + pairs.filter(Boolean).map(function (pair) {
      return '<div class="kv"><span class="k">' + esc(pair[0]) + '</span>' +
        '<span class="v">' + (pair[2] ? pair[2] : esc(pair[1])) + '</span></div>';
    }).join('') + '</div>';
  }

  /**
   * 轮询异步扫描任务直到结束。
   *
   * 背景：扫描是分钟级的（单策略实测 158 秒）。同步请求会被代理/浏览器空闲超时
   * 掐断，用户看到的是"无法连接到后端服务"。改成本函数后每个 HTTP 请求都是毫秒级，
   * 顺便还能把进度回调出去画进度条。
   *
   * @param {string} jobId
   * @param {Object} [opts]
   *   onProgress(job) 每次轮询回调（含 percent/done/total/current）
   *   interval        轮询间隔毫秒，默认 2000
   *   timeout         总超时毫秒，默认 30 分钟（远大于任何单次扫描）
   * @returns {Promise<Object>} 终态任务对象（含 result）
   */
  function pollScanJob(jobId, opts) {
    opts = opts || {};
    var api = SS.api;
    var interval = opts.interval || 2000;
    var timeout = opts.timeout || 30 * 60 * 1000;
    var startedAt = Date.now();
    return new Promise(function (resolve, reject) {
      function tick() {
        api.scanJob(jobId).then(function (job) {
          if (opts.onProgress) { try { opts.onProgress(job); } catch (e) { /* 忽略回调异常 */ } }
          if (!job) { reject(new Error('任务状态为空')); return; }
          if (job.status === 'succeeded') { resolve(job); return; }
          if (job.status === 'failed' || job.status === 'cancelled') {
            reject(new Error(job.error || '扫描任务' + (job.status === 'cancelled' ? '已取消' : '失败')));
            return;
          }
          if (Date.now() - startedAt > timeout) {
            reject(new Error('扫描超时（已等待 ' + Math.round(timeout / 60000) + ' 分钟），' +
              '可稍后在「系统状态 → 任务」查看是否完成。'));
            return;
          }
          setTimeout(tick, interval);
        }).catch(function (error) {
          //: 单次轮询失败不致命（网络抖动/页面切后台），继续重试直到总超时
          if (Date.now() - startedAt > timeout) { reject(error); return; }
          setTimeout(tick, Math.max(interval, 3000));
        });
      }
      tick();
    });
  }

  SS.util = {
    $: $, $$: $$, esc: esc, el: el,
    isNum: isNum, toNum: toNum, fixed: fixed, dirClass: dirClass,
    pct: pct, num: num, money: money, volume: volume, count: count,
    secs: secs, timeText: timeText, ago: ago, pad: pad,
    kpi: kpi, notice: notice, badge: badge, emptyState: emptyState,
    progress: progress, stackBar: stackBar, barList: barList,
    pollScanJob: pollScanJob,
    sortableTable: sortableTable, toast: toast, modal: modal, closeModal: closeModal,
    confirmDialog: confirmDialog, debounce: debounce, download: download, toCsv: toCsv,
    pick: pick, scoreTone: scoreTone, reasonList: reasonList, kvList: kvList
  };
})(window);
