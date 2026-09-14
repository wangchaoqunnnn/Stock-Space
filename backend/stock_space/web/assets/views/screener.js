/* ============================================================================
   views/screener.js —— 策略选股
   策略切换 / 参数调节 / 一键扫描 / 结果排序与逐条依据展开 / 导出 / 推送
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = {
      key: ctx.params.strategy || 'trend',
      meta: null,
      catalog: {},
      params: {},
      overrides: {},
      items: [],
      expanded: null,
      busy: false
    };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>策略选股</h1>' +
        '<div class="ph-sub" id="scrSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="scrExport">导出 CSV</button>' +
        '<button class="btn" id="scrParams">参数设置</button>' +
        '<button class="btn" id="scrRefresh" title="强制重新拉取上游数据">强制刷新数据</button>' +
        '<button class="btn primary" id="scrRun">运行扫描</button>' +
        '</div></div>' +
        '<div class="tabs" id="scrTabs"></div>' +
        '<div class="card" id="scrMeta"></div>' +
        '<div class="card"><div class="card-head"><h3>筛选结果</h3>' +
        '<span class="ch-sub" id="scrCount"></span></div>' +
        '<div class="btn-row" style="margin-bottom:8px">' +
        '<button class="btn sm" id="scrOnlyPassed">只看入选</button>' +
        '<button class="btn sm" id="scrAll">显示全部</button>' +
        '<label class="switch"><input type="checkbox" id="scrAuto">自动刷新(60s)</label>' +
        '</div><div id="scrResult"></div></div>';
    }

    function loadCatalog() {
      return api.strategies().then(function (data) {
        (data.items || []).forEach(function (item) { state.catalog[item.key] = item; });
        paintTabs();
        return selectStrategy(state.key);
      });
    }

    function paintTabs() {
      var tabs = util.$('#scrTabs');
      if (!tabs) return;
      tabs.innerHTML = SS.STRATEGY_ORDER.map(function (key) {
        var item = state.catalog[key] || {};
        return '<button class="tab' + (key === state.key ? ' active' : '') + '" data-strategy="' +
          util.esc(key) + '">' + util.esc(item.name || SS.STRATEGY_LABELS[key] || key) + '</button>';
      }).join('');
    }

    function selectStrategy(key) {
      state.key = key;
      var meta = state.catalog[key];
      if (!meta) return Promise.resolve();
      state.meta = meta;
      state.params = Object.assign({}, meta.param_defaults || {});
      state.overrides = Object.assign({}, meta.user_overrides || {});
      paintTabs();
      paintMeta();

      var sub = util.$('#scrSub');
      if (sub) sub.textContent = meta.name + ' · ' + (meta.source || '');

      var resultBox = util.$('#scrResult');
      if (resultBox) resultBox.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载最近一次扫描结果…</p></div>';
      return api.lastScan(key).then(function (data) {
        state.items = data.items || [];
        state.lastDate = data.trade_date || '';
        state.expanded = null;
        paintResults();
      }).catch(function () {
        state.items = [];
        paintResults();
      });
    }

    function paintMeta() {
      var meta = state.meta || {};
      var box = util.$('#scrMeta');
      if (!box) return;
      var desc = meta.description || '';
      var regime = meta.regime || '';
      var ruleChips = (meta.rules || []).map(function (rule) {
        return '<span class="chip" title="' + util.esc(rule.threshold || '') + '">' +
          util.esc(rule.name) + (rule.weight ? ' ' + util.fixed(rule.weight * 100, 0) + '%' : '') + '</span>';
      }).join('');
      var bt = meta.backtest || {};
      var customKeys = Object.keys(state.overrides || {});
      box.innerHTML = '<div class="card-head"><h2>' + util.esc(meta.name || '') + '</h2>' +
        '<span class="ch-sub">' + util.esc(meta.category || '') + ' · 需 ≥' +
        (meta.min_bars || 0) + ' 根日线' + (customKeys.length
          ? ' · <span class="badge info">已自定义 ' + customKeys.length + ' 项参数</span>' : '') +
        '</span></div>' +
        '<p>' + util.esc(desc) + '</p>' +
        (regime ? '<p class="small muted">适用行情：' + util.esc(regime) + '</p>' : '') +
        '<p class="small muted">出处：' + util.esc(meta.source || '') + '</p>' +
        '<div class="chip-list" style="margin:8px 0">' + ruleChips + '</div>' +
        '<div class="small muted">回测口径：止损 ' + util.fixed(bt.stop_loss_pct, 1) + '%' +
        (bt.take_profit_pct ? ' · 止盈 ' + util.fixed(bt.take_profit_pct, 1) + '%' : ' · 无止盈目标') +
        (bt.max_hold_days ? ' · 最长持有 ' + bt.max_hold_days + ' 个交易日' : '') +
        (bt.break_ma ? ' · 收盘跌破 MA' + bt.break_ma + ' 离场' : '') +
        (bt.use_atr_stop ? ' · ATR 动态止损(' + bt.stop_atr + ' 倍)' : '') + '</div>';
    }

    function paintResults() {
      var box = util.$('#scrResult');
      var countBox = util.$('#scrCount');
      if (!box) return;
      var items = state.filterPassed ? state.items.filter(function (i) { return i.passed; }) : state.items;
      if (countBox) {
        countBox.textContent = '共 ' + state.items.length + ' 条' +
          (state.filterPassed ? '(仅显示入选 ' + items.length + ' 条)' : '') +
          (state.lastDate ? ' · 扫描日 ' + state.lastDate : '') +
          ' · 点击行展开逐条依据';
      }
      if (!items.length) {
        box.innerHTML = util.emptyState('暂无结果', '点右上角「运行扫描」执行一次');
        return;
      }
      var cols = [
        { label: '代码', key: 'code' },
        { label: '名称', key: 'name' },
        { label: '评分', num: true, sort: function (i) { return i.score; } },
        { label: '入选', center: true, sort: function (i) { return i.passed ? 1 : 0; } },
        { label: '通过', num: true, sort: function (i) { return i.passed_count; } },
        { label: '通过率', num: true, sort: function (i) { return i.pass_ratio; } },
        { label: '止损价', num: true, sort: function (i) { return i.stop_loss; } },
        { label: '止盈价', num: true, sort: function (i) { return i.take_profit; } },
        { label: '买区', num: true, sort: function (i) { return i.entry_low; } },
        { label: '标签', key: 'tags' },
        { label: '关键指标', key: 'metrics' }
      ];
      util.sortableTable(box, cols, items, function (item) {
        var metrics = item.metrics || {};
        var highlights = [];
        if (util.isNum(metrics.change_20d)) highlights.push('20日 ' + util.fixed(metrics.change_20d, 2) + '%');
        if (util.isNum(metrics.vol_ratio_5_20)) highlights.push('量比 ' + util.fixed(metrics.vol_ratio_5_20, 2));
        if (util.isNum(metrics.atr_pct)) highlights.push('ATR ' + util.fixed(metrics.atr_pct, 2) + '%');
        if (util.isNum(metrics.excess_ret)) highlights.push('超额 ' + util.fixed(metrics.excess_ret, 2) + '%');
        if (metrics.pattern_name) highlights.push(metrics.pattern_name);
        if (util.isNum(metrics.limit_days_ago)) highlights.push(metrics.limit_days_ago + '日前涨停');
        return [
          '<span class="mono">' + util.esc(item.code) + '</span>',
          util.esc(item.name),
          '<span class="' + util.scoreTone(item.score) + '">' + util.fixed(item.score, 1) + '</span>',
          item.passed ? util.badge('入选', 'ok') : util.badge('未入选'),
          item.passed_count + '/' + item.total_count,
          util.fixed((item.pass_ratio || 0) * 100, 0) + '%',
          item.stop_loss ? util.num(item.stop_loss, 2) : '--',
          item.take_profit ? util.num(item.take_profit, 2) : '--',
          item.entry_low ? util.num(item.entry_low, 2) + ' ~ ' + util.num(item.entry_high, 2) : '--',
          (item.tags || []).map(function (tag) { return util.badge(tag, 'info'); }).join(' '),
          '<span class="small muted">' + util.esc(highlights.join(' · ')) + '</span>'
        ];
      }, {
        initialSort: 2, initialAsc: false,
        onRowClick: function (item) { showDetail(item); }
      });

      if (state.expanded) {
        var detail = items.filter(function (i) { return i.code === state.expanded; })[0];
        if (detail) showDetail(detail, true);
      }
    }

    function showDetail(item, inline) {
      var meta = state.meta || {};
      var html = '<div class="grid kpi">' +
        util.kpi('评分', '<span class="' + util.scoreTone(item.score) + '">' + util.fixed(item.score, 1) + '</span>',
          '入选 ' + (item.passed ? '是' : '否')) +
        util.kpi('通过条件', item.passed_count + ' / ' + item.total_count,
          '通过率 ' + util.fixed((item.pass_ratio || 0) * 100, 0) + '%') +
        util.kpi('止损价', util.num(item.stop_loss, 2), '回测口径 ' +
          util.fixed((meta.backtest || {}).stop_loss_pct, 1) + '%') +
        util.kpi('止盈价', item.take_profit ? util.num(item.take_profit, 2) : '不设',
          '买区 ' + util.num(item.entry_low, 2) + ' ~ ' + util.num(item.entry_high, 2)) +
        '</div>' +
        (item.note ? util.notice('warn', '否决/提示', util.esc(item.note)) : '') +
        '<h4>逐条依据</h4>' + util.reasonList(item.reasons) +
        '<h4 style="margin-top:12px">关键指标</h4>' +
        util.kvList(Object.keys(item.metrics || {}).filter(function (k) {
          var v = item.metrics[k];
          return v !== null && v !== undefined && typeof v !== 'object';
        }).map(function (k) {
          var v = item.metrics[k];
          return [k, util.isNum(v) ? util.fixed(v, 3) : String(v)];
        }));

      if (inline && state.expanded) return;

      var body = util.modal(util.esc(item.name) + '（' + util.esc(item.code) + '） · ' +
        util.esc(meta.name || ''), html, [
        util.el('button', {
          class: 'btn', text: '查看个股详情',
          onclick: function () { util.closeModal(); SS.app.navigate('#/stock?code=' + item.code); }
        }),
        util.el('button', { class: 'btn primary', text: '关闭', onclick: util.closeModal })
      ]);
      // 同时渲染 K 线缩略图
      var chartBox = util.el('div', { class: 'card', style: { marginTop: '12px' } });
      chartBox.innerHTML = '<div class="card-head"><h3>近 120 日走势</h3></div>' +
        '<div class="chart-box" style="min-height:260px"><canvas></canvas></div>';
      body.appendChild(chartBox);
      api.kline(item.code, 120).then(function (data) {
        var canvas = chartBox.querySelector('canvas');
        if (canvas) {
          charts.kline(canvas, data.bars || [], { height: 260, indicators: data.indicators });
        }
      }).catch(function () {
        var canvas = chartBox.querySelector('canvas');
        if (canvas) charts.empty(canvas, 'K线数据不可用');
      });
    }

    function runScan(force) {
      if (state.busy) return;
      state.busy = true;
      var btn = util.$('#scrRun');
      if (btn) { btn.disabled = true; btn.textContent = '扫描中…'; }
      SS.app.setLoading(true);
      var resultBox = util.$('#scrResult');

      //: 先画"排队中"，随后每次轮询刷新进度 —— 分钟级任务必须让用户看到在动，
      //: 否则和卡死无法区分。
      function paintProgress(job) {
        var pct = job.percent || 0;
        var label = job.status === 'queued' ? '排队中…'
          : ('正在扫描全市场…' + (job.current ? '（' + job.current + '）' : ''));
        var detail = job.detail ? '<p class="muted small">' + util.esc(job.detail) + '</p>' : '';
        resultBox.innerHTML =
          '<div class="boot-placeholder"><div class="spinner"></div>' +
          '<p>' + util.esc(label) + '</p>' +
          '<div style="width:min(420px,80%);margin:10px auto 0">' +
          util.progress(pct, 100, 'lg') + '</div>' +
          detail +
          '<p class="muted small">进度 ' + pct + '% · 已用 ' + util.secs(job.elapsed_seconds) +
          ' · 任务 ' + util.esc(job.id) + '</p>' +
          '<p class="muted small">首次执行需要拉取全市场日线，通常 2~3 分钟（后台执行，可随时切走）</p></div>';
      }

      paintProgress({ status: 'queued', percent: 0, elapsed_seconds: 0, id: '…' });

      return api.createScanJob(state.key, { refresh: !!force, limit: 200, perStrategy: 200 })
        .then(function (created) {
          var job = (created && created.job) || {};
          if (!job.id) throw new Error('后端未返回任务 ID');
          return util.pollScanJob(job.id, { onProgress: paintProgress });
        })
        .then(function (job) {
          var data = job.result || {};
          state.items = data.items || [];
          state.lastDate = data.trade_date || '';
          paintResults();
          var fetched = data.fetcher || {};
          util.toast('扫描完成：评估 ' + data.total_evaluated + ' 只，入选 ' +
            data.passed_count + ' 只（网络取数 ' + (fetched.network_hits || 0) +
            ' / 磁盘复用 ' + (fetched.disk_hits || 0) + '）', 'ok', 6000);
          if ((data.warnings || []).length) {
            util.toast(data.warnings[0], 'error', 7000);
          }
        })
        .catch(function (error) {
          resultBox.innerHTML = util.notice('bad', '扫描失败', util.esc(error.message) +
            '<div class="small muted" style="margin-top:6px">若提示数据源不可用，' +
            '请到「数据源」页面查看健康度并手动切换。</div>');
          util.toast('扫描失败: ' + error.message, 'error', 6000);
        })
        .then(function () {
          state.busy = false;
          if (btn) { btn.disabled = false; btn.textContent = '运行扫描'; }
          SS.app.setLoading(false);
        });
    }

    function openParams() {
      var meta = state.meta || {};
      var params = state.meta.param_defaults || {};
      var hints = meta.param_hints || {};
      var fields = Object.keys(params).map(function (name) {
        var value = state.overrides[name] !== undefined ? state.overrides[name] : params[name];
        var hint = hints[name] || {};
        var type = typeof params[name];
        if (name === 'pattern') {
          var options = (meta.patterns || []).map(function (p) {
            return '<option value="' + util.esc(p.key) + '"' +
              (String(value) === p.key ? ' selected' : '') + '>' + util.esc(p.name) + '</option>';
          }).join('');
          return '<div class="field"><label>' + util.esc(hint.label || name) +
            '</label><select data-param="' + util.esc(name) + '">' + options + '</select></div>';
        }
        if (type === 'boolean') {
          return '<div class="field"><label class="switch"><input type="checkbox" data-param="' +
            util.esc(name) + '"' + (value ? ' checked' : '') + '>' +
            util.esc(hint.label || name) + '</label></div>';
        }
        return '<div class="field"><label>' + util.esc(hint.label || name) +
          (hint.unit ? ' <span class="unit">' + util.esc(hint.unit) + '</span>' : '') +
          '</label><input type="number" step="any" data-param="' + util.esc(name) +
          '" value="' + util.esc(String(value)) + '"></div>';
      }).join('');

      var body = util.modal('参数设置 · ' + util.esc(meta.name || ''), 
        '<p class="small muted">只显示当前策略声明的参数；留空或恢复默认可点「恢复默认」。</p>' +
        '<div class="form-grid">' + fields + '</div>',
        [
          util.el('button', {
            class: 'btn', text: '恢复默认',
            onclick: function () {
              api.resetStrategyParams(state.key).then(function (data) {
                state.overrides = {};
                state.params = data.params || {};
                util.closeModal();
                paintMeta();
                util.toast('已恢复默认参数', 'ok');
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          }),
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '保存',
            onclick: function () {
              var patch = {};
              util.$$('[data-param]', body).forEach(function (input) {
                var name = input.dataset.param;
                if (input.type === 'checkbox') patch[name] = input.checked;
                else if (input.tagName === 'SELECT') patch[name] = input.value;
                else {
                  var num = Number(input.value);
                  patch[name] = isFinite(num) && input.value !== '' ? num : input.value;
                }
              });
              api.saveStrategyParams(state.key, patch).then(function (data) {
                state.overrides = patch;
                util.closeModal();
                paintMeta();
                util.toast('参数已保存，重新扫描后生效', 'ok');
              }).catch(function (e) { util.toast('保存失败: ' + e.message, 'error'); });
            }
          })
        ]);
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var tab = event.target.closest ? event.target.closest('[data-strategy]') : null;
        if (tab) { selectStrategy(tab.dataset.strategy); return; }
        var target = event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        if (target.id === 'scrRun') { runScan(false); return; }
        if (target.id === 'scrRefresh') { runScan(true); return; }
        if (target.id === 'scrParams') { openParams(); return; }
        if (target.id === 'scrOnlyPassed') { state.filterPassed = true; paintResults(); return; }
        if (target.id === 'scrAll') { state.filterPassed = false; paintResults(); return; }
        if (target.id === 'scrExport') { exportCsv(); return; }
      });
      var auto = util.$('#scrAuto');
      ctx.setInterval(function () {
        if (auto && auto.checked) runScan(false);
      }, 60000);
    }

    function exportCsv() {
      if (!state.items.length) { util.toast('暂无可导出的结果', 'error'); return; }
      var rows = state.items.map(function (item) {
        var passed = (item.reasons || []).filter(function (r) { return r.passed; })
          .map(function (r) { return r.name; }).join(' / ');
        var failed = (item.reasons || []).filter(function (r) { return !r.passed; })
          .map(function (r) { return r.name; }).join(' / ');
        return [item.code, item.name, item.score, item.passed ? '是' : '否',
          item.passed_count, item.total_count, passed, failed,
          item.stop_loss, item.take_profit, item.entry_low, item.entry_high];
      });
      var csv = util.toCsv(['代码', '名称', '评分', '入选', '通过数', '总条件数',
        '通过的条件', '未通过的条件', '止损', '止盈', '买区下沿', '买区上沿'], rows);
      util.download('策略选股_' + state.key + '_' + (state.lastDate || '') + '.csv', csv);
      util.toast('已导出 CSV', 'ok');
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function (force) { return force ? runScan(true) : loadCatalog(); });
    return loadCatalog();
  }

  SS.views.screener = { title: '策略选股', render: render };
})(window);
