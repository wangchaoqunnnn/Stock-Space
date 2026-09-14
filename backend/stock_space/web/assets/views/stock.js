/* ============================================================================
   views/stock.js —— 个股详情
   行情卡片 / K线与均线(可切换周期) / 分时 / 技术指标 / 五策略逐条评估 / 自选
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = {
      code: ctx.params.code || '',
      period: 'day',
      days: 250,
      quote: null,
      kline: null,
      evaluations: {}
    };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1 id="stkTitle">个股详情</h1>' +
        '<div class="ph-sub" id="stkSub">输入 6 位代码或在上方搜索</div></div>' +
        '<div class="page-actions">' +
        '<input class="input" id="stkCode" placeholder="6 位代码" style="width:120px" value="' +
        util.esc(state.code) + '" inputmode="numeric">' +
        '<button class="btn" id="stkLoad">查询</button>' +
        '<button class="btn" id="stkWatch">加入自选</button>' +
        '<button class="btn" id="stkSim">记入模拟持仓</button>' +
        '</div></div>' +
        '<div id="stkBody"></div>';
    }

    function load(code) {
      var target = code || state.code || (util.$('#stkCode') || {}).value || '';
      target = String(target).trim();
      var body = util.$('#stkBody');
      if (!target) {
        body.innerHTML = util.emptyState('请输入股票代码',
          '例如 600519（贵州茅台）、300750（宁德时代）、688981（中芯国际）、920001（北交所）');
        return Promise.resolve();
      }
      state.code = target;
      body.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载 ' +
        util.esc(target) + ' …</p></div>';

      return Promise.all([
        api.stock(target),
        api.kline(target, state.days, state.period)
      ]).then(function (results) {
        state.detail = results[0];
        state.kline = results[1];
        state.quote = results[0].quote;
        util.$('#stkSub').textContent = '数据时间 ' + (results[0].as_of || '--') +
          ' · K线 ' + (state.kline.count || 0) + ' 根（' + (state.kline.origin === 'disk' ? '磁盘缓存' : '实时拉取') + '）' +
          ' · 源 ' + (state.kline.source || '--');
        util.$('#stkTitle').textContent = (state.quote && state.quote.name) ?
          state.quote.name + '（' + state.code + '）' : state.code;
        paint();
        return loadEvaluations(target);
      }).catch(function (error) {
        body.innerHTML = util.notice('bad', '加载失败', util.esc(error.message) +
          '<div class="small muted" style="margin-top:6px">请确认代码正确（6 位数字），' +
          '或在「数据源」页面检查 K 线源是否可用。</div>');
      });
    }

    function quoteCard(quote) {
      if (!quote) return util.notice('warn', '未获取到实时行情', 'K 线仍可用，可参考下方走势。');
      var cls = util.dirClass(quote.change_pct);
      return '<div class="card"><div class="card-head">' +
        '<div><h2 style="margin:0">' + util.esc(quote.name) + ' <span class="mono muted">' +
        util.esc(quote.code) + '</span></h2>' +
        '<div class="small muted">' + util.esc(quote.board || '') + ' · ' +
        util.esc(quote.market || '') + (quote.industry ? ' · ' + util.esc(quote.industry) : '') +
        (quote.is_st ? ' · <span class="badge bad">ST</span>' : '') + '</div></div>' +
        '<div class="right"><div class="' + cls + '" style="font-size:26px;font-weight:750">' +
        util.num(quote.price, 2) + '</div>' +
        '<div class="' + cls + '">' + util.num(quote.change, 2, { sign: true }) + ' ' +
        util.pct(quote.change_pct) + '</div></div></div>' +
        '<div class="grid cols-4" style="margin-top:6px">' +
        util.kpi('今开', util.num(quote.open, 2), '昨收 ' + util.num(quote.prev_close, 2)) +
        util.kpi('最高 / 最低', util.num(quote.high, 2) + ' / ' + util.num(quote.low, 2),
          '振幅 ' + util.fixed(quote.amplitude, 2) + '%') +
        util.kpi('成交量', util.volume(quote.volume), '量比 ' + util.fixed(quote.volume_ratio, 2)) +
        util.kpi('成交额', util.money(quote.amount), '换手 ' + util.fixed(quote.turnover_rate, 2) + '%') +
        util.kpi('总市值', util.money(quote.total_mv), '流通 ' + util.money(quote.float_mv)) +
        util.kpi('PE(TTM)', util.fixed(quote.pe_ttm, 2), 'PB ' + util.fixed(quote.pb, 2)) +
        util.kpi('涨停价', util.num(quote.limit_up, 2), '跌停 ' + util.num(quote.limit_down, 2)) +
        util.kpi('数据源', util.esc(quote.source || '--'), '行情快照') +
        '</div></div>';
    }

    function paint() {
      var body = util.$('#stkBody');
      var html = quoteCard(state.quote);

      html += '<div class="card"><div class="card-head">' +
        '<h3>K 线与均线</h3>' +
        '<div class="btn-row">' +
        ['day', 'week', 'month'].map(function (p) {
          return '<button class="btn sm' + (state.period === p ? ' primary' : '') +
            '" data-period="' + p + '">' + ({ day: '日K', week: '周K', month: '月K' })[p] + '</button>';
        }).join('') +
        ['120', '250', '500'].map(function (d) {
          return '<button class="btn sm' + (String(state.days) === d ? ' primary' : '') +
            '" data-days="' + d + '">' + d + ' 根</button>';
        }).join('') +
        '</div></div>' +
        '<div class="chart-box"><canvas id="stkKline"></canvas></div>' +
        '<div class="chart-legend">' +
        '<span><i style="background:' + charts.colors.ma5 + '"></i>MA5</span>' +
        '<span><i style="background:' + charts.colors.ma10 + '"></i>MA10</span>' +
        '<span><i style="background:' + charts.colors.ma20 + '"></i>MA20</span>' +
        '<span><i style="background:' + charts.colors.ma60 + '"></i>MA60</span>' +
        '<span class="faint">阳线红边空心 / 阴线绿实心（A 股习惯）</span>' +
        '</div></div>';

      html += indicatorCard(state.kline);

      html += '<div class="card"><div class="card-head"><h3>策略评估</h3>' +
        '<span class="ch-sub">逐条列出每个策略的判定条件，解释"为什么入选/未入选"</span>' +
        '<button class="btn sm" id="stkEval">重新评估</button></div>' +
        '<div id="stkEvalBox"></div></div>';

      html += '<div class="card"><div class="card-head"><h3>当日分时</h3>' +
        '<button class="btn sm" id="stkMinute">加载分时</button></div>' +
        '<div id="stkMinuteBox" class="chart-empty">点击「加载分时」拉取当日分时数据</div></div>';

      body.innerHTML = html;
      drawChart();
      paintEvalBox();
    }

    function indicatorCard(kline) {
      var ind = (kline && kline.indicators) || {};
      function last(key) {
        var arr = ind[key];
        if (!arr || !arr.length) return null;
        for (var i = arr.length - 1; i >= 0; i--) {
          if (arr[i] !== null && arr[i] !== undefined) return arr[i];
        }
        return null;
      }
      var rows = [
        ['MA5', last('ma5')], ['MA10', last('ma10')], ['MA20', last('ma20')], ['MA60', last('ma60')],
        ['DIF', last('dif')], ['DEA', last('dea')], ['MACD 柱', last('macd')],
        ['KDJ-K', last('k')], ['KDJ-D', last('d')], ['KDJ-J', last('j')],
        ['RSI14', last('rsi14')], ['ATR14', last('atr14')],
        ['BOLL 上轨', last('boll_upper')], ['BOLL 中轨', last('boll_mid')], ['BOLL 下轨', last('boll_lower')]
      ];
      return '<div class="card"><div class="card-head"><h3>技术指标</h3>' +
        '<span class="ch-sub">基于前复权日线本地计算</span></div>' +
        util.kvList(rows.map(function (row) {
          return [row[0], row[1] === null ? '--' : util.fixed(row[1], 3)];
        })) + '</div>';
    }

    function drawChart() {
      var canvas = util.$('#stkKline');
      if (!canvas) return;
      if (!state.kline || !(state.kline.bars || []).length) {
        charts.empty(canvas, 'K线数据不可用');
        return;
      }
      charts.kline(canvas, state.kline.bars, { height: 340, indicators: state.kline.indicators });
    }

    function paintEvalBox() {
      var box = util.$('#stkEvalBox');
      if (!box) return;
      var keys = Object.keys(state.evaluations);
      if (!keys.length) {
        box.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>正在评估各策略…</p></div>';
        return;
      }
      box.innerHTML = '<div class="tabs">' + keys.map(function (key, index) {
        var label = SS.STRATEGY_LABELS[key] || key;
        var data = state.evaluations[key] || {};
        return '<button class="tab' + (index === 0 ? ' active' : '') + '" data-eval="' + util.esc(key) + '">' +
          util.esc(label) + ' <span class="' + util.scoreTone(data.score) + '">' +
          util.fixed(data.score, 1) + '</span></button>';
      }).join('') + '</div><div id="stkEvalDetail"></div>';
      paintEvalDetail(keys[0]);
    }

    function paintEvalDetail(key) {
      var box = util.$('#stkEvalDetail');
      var data = state.evaluations[key];
      if (!box) return;
      if (!data) { box.innerHTML = util.emptyState('该策略无结果'); return; }
      if (data.error) {
        box.innerHTML = util.notice('warn', '评估失败', util.esc(data.error.message || data.error));
        return;
      }
      box.innerHTML = '<div class="grid kpi">' +
        util.kpi('评分', '<span class="' + util.scoreTone(data.score) + '">' + util.fixed(data.score, 1) + '</span>',
          data.passed ? '满足入选条件' : '未满足入选条件') +
        util.kpi('通过条件', data.passed_count + ' / ' + data.total_count,
          '通过率 ' + util.fixed((data.pass_ratio || 0) * 100, 0) + '%') +
        util.kpi('止损价', util.num(data.stop_loss, 2), '算法给出') +
        util.kpi('止盈/目标', data.take_profit ? util.num(data.take_profit, 2) : '不设', '算法给出') +
        '</div>' +
        (data.note ? util.notice('warn', '否决/提示', util.esc(data.note)) : '') +
        util.reasonList(data.reasons);
    }

    function loadEvaluations(code) {
      var keys = SS.STRATEGY_ORDER.slice();
      var chain = Promise.resolve();
      keys.forEach(function (key) {
        chain = chain.then(function () {
          return api.evaluate(key, code).then(function (data) {
            state.evaluations[key] = data;
            paintEvalBox();
          }).catch(function (error) {
            state.evaluations[key] = { error: { message: error.message } };
            paintEvalBox();
          });
        });
      });
      return chain;
    }

    function loadMinute() {
      var box = util.$('#stkMinuteBox');
      box.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载分时…</p></div>';
      api.minute(state.code).then(function (data) {
        var points = data.points || [];
        if (!points.length) { box.innerHTML = util.emptyState('暂无分时数据'); return; }
        box.innerHTML = '<div class="chart-box"><canvas id="stkMinuteChart"></canvas></div>' +
          '<p class="small muted">昨收 ' + util.num(data.prev_close, 2) +
          ' · 数据点 ' + points.length + ' · 源 ' + util.esc(data.source || '--') + '</p>';
        var canvas = util.$('#stkMinuteChart');
        var series = [{ name: '价格', color: charts.themeColors().accent, fill: true,
          data: points.map(function (p, i) { return { x: p.time || i, y: p.price }; }) }];
        if (points[0] && points[0].avg !== undefined) {
          series.push({ name: '均价', color: charts.themeColors().gold,
            data: points.map(function (p, i) { return { x: p.time || i, y: p.avg }; }) });
        }
        charts.line(canvas, {
          series: series, height: 260,
          baseline: data.prev_close,
          xFormat: function (v) { return String(v).slice(-4); },
          yFormat: function (v) { return v.toFixed(2); }
        });
      }).catch(function (error) {
        box.innerHTML = util.notice('warn', '分时不可用', util.esc(error.message) +
          '<div class="small muted">部分数据源不提供分时接口，不影响其它功能。</div>');
      });
    }

    function addWatch() {
      if (!state.code) return;
      api.addWatch({
        code: state.code,
        name: (state.quote && state.quote.name) || '',
        note: ''
      }).then(function () {
        //: 明确告知去哪个页面看自选池 —— 此前自选只能在「用户配置」页底部看到，
        //: 用户加完根本不知道上哪儿找。
        util.toast('已加入自选 · 在「行情中枢 → 自选股池」查看', 'ok', 6000);
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function simPosition() {
      if (!state.quote || !state.quote.price) { util.toast('无有效行情，无法记录', 'error'); return; }
      var body = util.modal('记入模拟持仓',
        util.kvList([
          ['代码', state.code], ['名称', state.quote.name || '--'],
          ['现价', util.num(state.quote.price, 2)]
        ]) +
        '<div class="form-grid" style="margin-top:10px">' +
        '<div class="field"><label>买入价</label><input type="number" step="0.01" id="simPrice" value="' +
        util.fixed(state.quote.price, 2) + '"></div>' +
        '<div class="field"><label>股数</label><input type="number" step="100" id="simShares" value="100"></div>' +
        '<div class="field"><label>理由</label><input type="text" id="simReason" placeholder="例如：潜涨评分 82"></div>' +
        '</div>' +
        '<p class="small muted">模拟持仓仅记录在本平台数据库，不涉及任何真实交易。</p>',
        [
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '保存',
            onclick: function () {
              var price = Number(util.$('#simPrice').value);
              var shares = Number(util.$('#simShares').value);
              var reason = util.$('#simReason').value;
              api.openPosition({
                code: state.code, name: (state.quote && state.quote.name) || '',
                price: price, shares: shares, reason: reason
              }).then(function () {
                util.closeModal();
                util.toast('已记入模拟持仓', 'ok');
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          })
        ]);
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var target = event.target.closest ? event.target.closest('button') : null;
        var periodBtn = event.target.closest ? event.target.closest('[data-period]') : null;
        if (periodBtn) {
          state.period = periodBtn.dataset.period;
          load(state.code);
          return;
        }
        var daysBtn = event.target.closest ? event.target.closest('[data-days]') : null;
        if (daysBtn) {
          state.days = Number(daysBtn.dataset.days);
          load(state.code);
          return;
        }
        var evalTab = event.target.closest ? event.target.closest('[data-eval]') : null;
        if (evalTab) {
          util.$$('[data-eval]').forEach(function (node) {
            node.classList.toggle('active', node.dataset.eval === evalTab.dataset.eval);
          });
          paintEvalDetail(evalTab.dataset.eval);
          return;
        }
        if (!target) return;
        if (target.id === 'stkLoad') { load(util.$('#stkCode').value); return; }
        if (target.id === 'stkWatch') { addWatch(); return; }
        if (target.id === 'stkSim') { simPosition(); return; }
        if (target.id === 'stkMinute') { loadMinute(); return; }
        if (target.id === 'stkEval') {
          state.evaluations = {};
          paintEvalBox();
          loadEvaluations(state.code);
          return;
        }
      });
      var input = util.$('#stkCode');
      if (input) {
        input.addEventListener('keydown', function (event) {
          if (event.key === 'Enter') load(input.value);
        });
      }
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(state.code); });
    return load(state.code);
  }

  SS.views.stock = { title: '个股详情', render: render };
})(window);
