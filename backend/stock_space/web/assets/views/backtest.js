/* ============================================================================
   views/backtest.js —— 回测分析
   策略与参数 / 标的范围 / 成交口径 / 绩效指标 / 资金曲线 / 交易明细 / 离场归因
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = {
      key: ctx.params.strategy || 'trend',
      result: null,
      catalog: {},
      codes: ''
    };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>回测分析</h1>' +
        '<div class="ph-sub">保守成本口径：佣金 0.023% + 印花税 0.05%(卖出) + 滑点 0.15%；' +
        '默认次日开盘成交，规避未来函数</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="btExport">导出明细 CSV</button>' +
        '<button class="btn primary" id="btRun">开始回测</button>' +
        '</div></div>' +
        '<div class="card"><div class="card-head"><h3>回测设置</h3>' +
        '<span class="ch-sub">标的留空 = 按成交额取前 N 只（受配额限制）</span></div>' +
        '<div class="form-grid">' +
        '<div class="field"><label>策略</label><select id="btStrategy"></select></div>' +
        '<div class="field"><label>回看天数 <span class="unit">交易日</span></label>' +
        '<input type="number" id="btDays" value="250" min="120" max="800"></div>' +
        '<div class="field"><label>标的数量上限</label>' +
        '<input type="number" id="btLimit" value="120" min="1" max="800"></div>' +
        '<div class="field"><label>成交口径</label><select id="btFill">' +
        '<option value="next_open">次日开盘（推荐，无未来函数）</option>' +
        '<option value="close">信号日收盘（存在轻微前视）</option></select></div>' +
        '<div class="field"><label>最大持仓数</label>' +
        '<input type="number" id="btMaxPos" value="5" min="1" max="30"></div>' +
        '<div class="field"><label>单票仓位 <span class="unit">0~1</span></label>' +
        '<input type="number" id="btPosPct" value="0.2" step="0.05" min="0.01" max="1"></div>' +
        '<div class="field" style="grid-column:1/-1"><label>指定标的 <span class="unit">逗号分隔，可留空</span></label>' +
        '<input type="text" id="btCodes" placeholder="例如 600519,300750,688981"></div>' +
        '</div>' +
        '<div class="btn-row" style="margin-top:10px">' +
        '<button class="btn primary" id="btRun2">开始回测</button>' +
        '<span class="small muted">首次回测需要拉取日线，样本越大越慢；结果会显示失败/成功标的数。</span>' +
        '</div></div>' +
        '<div id="btBody"></div>';
    }

    function fillStrategies() {
      return api.strategies().then(function (data) {
        (data.items || []).forEach(function (item) { state.catalog[item.key] = item; });
        var select = util.$('#btStrategy');
        if (!select) return;
        select.innerHTML = (data.order || SS.STRATEGY_ORDER).map(function (key) {
          var item = state.catalog[key] || {};
          return '<option value="' + util.esc(key) + '"' + (key === state.key ? ' selected' : '') + '>' +
            util.esc(item.name || key) + '</option>';
        }).join('');
      });
    }

    function run() {
      var key = util.$('#btStrategy').value;
      state.key = key;
      var payload = {
        strategy: key,
        days: Number(util.$('#btDays').value) || 250,
        limit: Number(util.$('#btLimit').value) || 120,
        fill: util.$('#btFill').value,
        max_positions: Number(util.$('#btMaxPos').value) || 5,
        position_pct: Number(util.$('#btPosPct').value) || 0.2
      };
      var codesRaw = util.$('#btCodes').value.trim();
      if (codesRaw) {
        payload.codes = codesRaw.split(/[,，\s]+/).filter(Boolean);
      }
      var body = util.$('#btBody');
      body.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div>' +
        '<p>回测运行中…</p><p class="muted small">正在拉取日线并逐日重放，请耐心等待（大样本可能数分钟）</p></div>';
      var runBtns = [util.$('#btRun'), util.$('#btRun2')];
      runBtns.forEach(function (b) { if (b) b.disabled = true; });
      SS.app.setLoading(true);

      return api.backtest(payload).then(function (data) {
        state.result = data;
        paint(data);
        util.toast('回测完成：' + (data.metrics.closed_count || 0) + ' 笔已平仓交易', 'ok', 5000);
      }).catch(function (error) {
        body.innerHTML = util.notice('bad', '回测失败', util.esc(error.message) +
          '<div class="small muted" style="margin-top:6px">若提示日线不可用，请到「数据源」页面检查 K 线源，' +
          '或减少标的数量重试。</div>');
      }).then(function () {
        runBtns.forEach(function (b) { if (b) b.disabled = false; });
        SS.app.setLoading(false);
      });
    }

    function paint(data) {
      var m = data.metrics || {};
      var body = util.$('#btBody');
      var html = '<div class="grid kpi">' +
        util.kpi('交易笔数', util.count(m.trade_count) + ' / 平仓 ' + util.count(m.closed_count),
          '可用 ' + data.available + ' 只 · 失败 ' + data.failed + ' 只') +
        util.kpi('胜率', util.fixed((m.win_rate || 0) * 100, 1) + '%',
          util.count(m.win_count) + ' 胜 / ' + util.count(m.loss_count) + ' 负',
          (m.win_rate || 0) >= 0.5 ? 'up' : 'down') +
        util.kpi('盈亏比', m.payoff_ratio === null ? '∞' : util.fixed(m.payoff_ratio, 2),
          '平均盈 ' + util.fixed(m.avg_win_pct, 2) + '% / 亏 ' + util.fixed(m.avg_loss_pct, 2) + '%') +
        util.kpi('利润因子', m.profit_factor === null ? '∞' : util.fixed(m.profit_factor, 2),
          '毛利润 ' + util.money(m.gross_profit) + ' / 毛亏损 ' + util.money(m.gross_loss)) +
        util.kpi('单笔期望', util.fixed(m.expectancy_pct, 3) + '%', '每笔交易的平均收益') +
        util.kpi('总收益', util.fixed(m.total_return_pct, 2) + '%',
          '年化 ' + util.fixed(m.annualized_return_pct, 2) + '%',
          (m.total_return_pct || 0) >= 0 ? 'up' : 'down') +
        util.kpi('最大回撤', util.fixed(m.max_drawdown_pct, 2) + '%',
          'Calmar ' + util.fixed(m.calmar, 2), 'down') +
        util.kpi('夏普 / 索提诺', util.fixed(m.sharpe, 2) + ' / ' + util.fixed(m.sortino, 2),
          '平均持有 ' + util.fixed(m.avg_hold_days, 1) + ' 个交易日') +
        '</div>';

      html += '<div class="card"><div class="card-head"><h3>回测口径说明</h3></div>' +
        '<div class="small">' +
        '<p>区间 <strong>' + util.esc(data.start_date || '--') + ' ~ ' + util.esc(data.end_date || '--') +
        '</strong>；成交口径 <strong>' + (data.fill_mode === 'next_open' ? '次日开盘' : '信号日收盘') +
        '</strong>；成本：佣金 ' + util.fixed((data.cost_model || {}).commission_rate * 100, 3) +
        '% + 印花税 ' + util.fixed((data.cost_model || {}).stamp_duty_rate * 100, 3) +
        '%（仅卖出）+ 滑点 ' + util.fixed((data.cost_model || {}).slippage_rate * 100, 2) + '%。</p>' +
        '<p class="muted">买入当日不可卖出（T+1）；同一根 K 线内止损与止盈同时触发时一律按止损成交（保守）。' +
        '绩效按 ' + util.count(244) + ' 个交易日年化。</p>' +
        ((data.warnings || []).length ? '<p class="warn">' +
          data.warnings.map(util.esc).join('<br>') + '</p>' : '') +
        '</div></div>';

      html += '<div class="card"><div class="card-head"><h3>资金曲线</h3>' +
        '<span class="ch-sub">等权名义仓位逐笔累乘（初始 100 万）</span></div>' +
        '<div class="chart-box"><canvas id="btEquity"></canvas></div></div>';

      html += '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>离场原因归因</h3></div>' +
        '<div id="btReasons"></div></div>' +
        '<div class="card"><div class="card-head"><h3>收益分布</h3></div>' +
        '<div class="chart-box"><canvas id="btDist"></canvas></div></div>' +
        '</div>';

      html += '<div class="card"><div class="card-head"><h3>交易明细</h3>' +
        '<span class="ch-sub">共 ' + data.trade_count + ' 笔，显示最近 ' +
        Math.min(300, (data.trades || []).length) + ' 笔 · 点击表头排序</span></div>' +
        '<div id="btTrades"></div></div>';

      body.innerHTML = html;

      // 资金曲线
      var eq = data.equity_curve || [];
      var canvas = util.$('#btEquity');
      if (canvas && eq.length) {
        charts.line(canvas, {
          height: 280,
          series: [{
            name: '净值', color: charts.themeColors().accent, fill: true,
            data: eq.map(function (point) { return { x: point.date, y: point.equity }; })
          }],
          baseline: eq.length ? eq[0].equity : undefined,
          xFormat: function (v) { return String(v).slice(5); },
          yFormat: function (v) { return (v / 10000).toFixed(0) + '万'; }
        });
      } else if (canvas) {
        charts.empty(canvas, '无净值数据');
      }

      // 离场归因
      var reasons = m.by_exit_reason || {};
      var keys = Object.keys(reasons);
      util.$('#btReasons').innerHTML = keys.length
        ? '<div class="table-wrap"><table class="grid"><thead><tr><th>离场原因</th>' +
          '<th class="n">笔数</th><th class="n">胜率</th><th class="n">平均收益</th>' +
          '</tr></thead><tbody>' + keys.map(function (key) {
            var row = reasons[key];
            return '<tr><td>' + util.esc(key) + '</td><td class="n">' + row.count +
              '</td><td class="n">' + util.fixed((row.win_rate || 0) * 100, 1) + '%</td>' +
              '<td class="n">' + util.pct(row.avg_pnl_pct) + '</td></tr>';
          }).join('') + '</tbody></table></div>'
        : util.emptyState('无已平仓交易');

      // 收益分布直方图
      var dist = util.$('#btDist');
      var trades = data.trades || [];
      if (dist && trades.length) {
        var buckets = [
          { label: '<-15%', min: -999, max: -15, count: 0 },
          { label: '-15~-8', min: -15, max: -8, count: 0 },
          { label: '-8~-3', min: -8, max: -3, count: 0 },
          { label: '-3~0', min: -3, max: 0, count: 0 },
          { label: '0~3', min: 0, max: 3, count: 0 },
          { label: '3~8', min: 3, max: 8, count: 0 },
          { label: '8~15', min: 8, max: 15, count: 0 },
          { label: '>15%', min: 15, max: 999, count: 0 }
        ];
        trades.forEach(function (t) {
          var pnl = Number(t.pnl_pct);
          for (var i = 0; i < buckets.length; i++) {
            if (pnl >= buckets[i].min && pnl < buckets[i].max) { buckets[i].count++; break; }
          }
        });
        charts.bars(dist, buckets.map(function (b) {
          return {
            label: b.label, value: b.count,
            color: b.max <= 0 ? charts.themeColors().down : charts.themeColors().up
          };
        }), { height: 240 });
      } else if (dist) {
        charts.empty(dist, '无交易样本');
      }

      // 交易明细
      var cols = [
        { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
        { label: '买入日', key: 'entry_date' },
        { label: '买入价', num: true, sort: function (t) { return t.entry_price; } },
        { label: '卖出日', key: 'exit_date' },
        { label: '卖出价', num: true, sort: function (t) { return t.exit_price; } },
        { label: '收益', num: true, sort: function (t) { return t.pnl_pct; } },
        { label: '盈亏额', num: true, sort: function (t) { return t.pnl; } },
        { label: '持有', num: true, sort: function (t) { return t.hold_days; } },
        { label: '最大浮盈', num: true, sort: function (t) { return t.max_gain_pct; } },
        { label: '最大浮亏', num: true, sort: function (t) { return t.max_loss_pct; } },
        { label: '离场原因', key: 'exit_reason' },
        { label: '入场评分', num: true, sort: function (t) { return t.score; } }
      ];
      util.sortableTable(util.$('#btTrades'), cols, trades, function (t) {
        return [
          '<span class="mono">' + util.esc(t.code) + '</span>', util.esc(t.name),
          util.esc(t.entry_date), util.num(t.entry_price, 2),
          util.esc(t.exit_date), util.num(t.exit_price, 2),
          util.pct(t.pnl_pct), '<span class="' + util.dirClass(t.pnl) + '">' + util.num(t.pnl, 0) + '</span>',
          t.hold_days + ' 日', util.pct(t.max_gain_pct), util.pct(t.max_loss_pct),
          util.esc(t.exit_reason), util.fixed(t.score, 1)
        ];
      }, { initialSort: 6, initialAsc: false, onRowClick: function (t) { SS.app.navigate('#/stock?code=' + t.code); } });
    }

    function exportCsv() {
      var data = state.result;
      if (!data || !data.trades || !data.trades.length) { util.toast('暂无回测结果', 'error'); return; }
      var m = data.metrics || {};
      var header = ['代码', '名称', '买入日', '买入价', '卖出日', '卖出价', '收益%', '盈亏额',
        '持有日', '最大浮盈%', '最大浮亏%', '离场原因', '入场评分'];
      var rows = data.trades.map(function (t) {
        return [t.code, t.name, t.entry_date, t.entry_price, t.exit_date, t.exit_price,
          t.pnl_pct, t.pnl, t.hold_days, t.max_gain_pct, t.max_loss_pct, t.exit_reason, t.score];
      });
      var summary = [
        ['策略', state.key, '区间', data.start_date + ' ~ ' + data.end_date, '成交口径', data.fill_mode],
        ['胜率', util.fixed((m.win_rate || 0) * 100, 2) + '%', '盈亏比',
          m.payoff_ratio === null ? 'inf' : util.fixed(m.payoff_ratio, 2),
          '最大回撤', util.fixed(m.max_drawdown_pct, 2) + '%'],
        ['总收益', util.fixed(m.total_return_pct, 2) + '%', '年化',
          util.fixed(m.annualized_return_pct, 2) + '%', '交易笔数', m.closed_count]
      ];
      var csv = util.toCsv(['回测汇总'], summary) + '\r\n\r\n' + util.toCsv(header, rows);
      util.download('回测_' + state.key + '_' + data.start_date + '_' + data.end_date + '.csv', csv);
      util.toast('已导出回测明细', 'ok');
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var target = event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        if (target.id === 'btRun' || target.id === 'btRun2') run();
        if (target.id === 'btExport') exportCsv();
      });
    }

    content.innerHTML = shell();
    bind();
    util.$('#btBody').innerHTML = util.notice('info', '如何使用回测',
      '① 选择策略 → ② 设定区间与标的范围 → ③ 点「开始回测」。' +
      '回测使用与实盘扫描完全相同的规则与参数，便于对照；' +
      '结果仅用于研究，历史表现不代表未来收益。');
    ctx.setRefresh(function () { return fillStrategies(); });
    return fillStrategies();
  }

  SS.views.backtest = { title: '回测分析', render: render };
})(window);
