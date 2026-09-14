/* ============================================================================
   views/dashboard.js —— 仪表盘
   指数速览 / 市场宽度 / 情绪温度计 / 涨跌榜 / 板块强弱 / 涨停梯队 / 策略入选概览
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function renderIndices(items) {
    if (!items || !items.length) return util.emptyState('暂无指数数据');
    return '<div class="grid cols-4">' + items.map(function (item) {
      if (item.missing) {
        return util.kpi(item.name, '<span class="faint">--</span>', '快照未返回该指数');
      }
      return util.kpi(
        item.name,
        '<span class="' + util.dirClass(item.change_pct) + '">' + util.num(item.price, 2) + '</span>',
        util.pct(item.change_pct) + ' · 额 ' + util.money(item.amount)
      );
    }).join('') + '</div>';
  }

  function renderBreadth(breadth) {
    if (!breadth) return util.emptyState('涨跌家数不可用');
    var segments = [
      { label: '上涨', value: breadth.up, color: 'var(--up)' },
      { label: '平盘', value: breadth.flat, color: 'var(--faint)' },
      { label: '下跌', value: breadth.down, color: 'var(--down)' }
    ];
    return '<div class="grid cols-2">' +
      '<div>' + util.stackBar(segments) +
      '<div class="kv-list" style="margin-top:10px">' +
      '<div class="kv"><span class="k">上涨占比</span><span class="v">' +
      util.fixed((breadth.up_ratio || 0) * 100, 1) + '%</span></div>' +
      '<div class="kv"><span class="k">涨停 / 跌停</span><span class="v">' +
      '<span class="up">' + util.count(breadth.limit_up) + '</span> / ' +
      '<span class="down">' + util.count(breadth.limit_down) + '</span></span></div>' +
      '<div class="kv"><span class="k">两市成交额</span><span class="v">' +
      util.money(breadth.total_amount) + '</span></div>' +
      '<div class="kv"><span class="k">数据源</span><span class="v">' +
      util.esc(breadth.source || '--') + '</span></div>' +
      '</div></div>' +
      '<div><div class="chart-box" style="min-height:180px"><canvas id="dashDonut"></canvas></div></div>' +
      '</div>';
  }

  function renderEmotion(emotion) {
    if (!emotion) return util.emptyState('情绪数据不可用');
    var cycle = emotion.cycle || {};
    var emo = emotion.emotion || {};
    var tone = emo.score >= 60 ? 'up' : (emo.score >= 40 ? 'warn' : 'down');
    var parts = util.barList((emo.parts || []).map(function (p) {
      return {
        label: p.name, value: p.earned, text: util.fixed(p.earned, 1) + ' / ' + util.fixed(p.weight, 0),
        color: p.earned / (p.weight || 1) >= 0.6 ? 'var(--up)' : 'var(--accent)'
      };
    }), { digits: 1 });
    return '<div class="gauge-wrap">' +
      '<div class="gauge-text"><div class="g-value ' + util.dirClass(emo.score - 50) + '">' +
      util.fixed(emo.score, 1) + '</div><div class="g-label">' + util.esc(emo.level || '') +
      ' · 加权情绪分(0~100)</div></div>' +
      '<div><span class="badge info">周期：' + util.esc(cycle.label || '--') + '</span> ' +
      '<span class="badge">置信度 ' + util.fixed((cycle.confidence || 0) * 100, 0) + '%</span></div>' +
      '</div>' +
      '<p class="small muted" style="margin-top:8px">' + util.esc(cycle.advice || '') + '</p>' +
      '<div style="margin-top:10px">' + parts + '</div>';
  }

  function rankTable(items, valueKey, title) {
    if (!items || !items.length) return util.emptyState('暂无数据');
    return '<div class="table-wrap"><table class="grid"><thead><tr>' +
      '<th class="col-no">No</th><th>代码</th><th>名称</th><th class="n">现价</th>' +
      '<th class="n">涨跌幅</th><th class="n">' + util.esc(title || '成交额') + '</th>' +
      '</tr></thead><tbody>' + items.map(function (item, index) {
        var value = valueKey === 'amount' ? util.money(item.amount)
          : valueKey === 'turnover_rate' ? util.fixed(item.turnover_rate, 2) + '%'
          : util.pct(item.change_pct);
        return '<tr class="clickable" data-code="' + util.esc(item.code) + '">' +
          '<td class="col-no">' + (index + 1) + '</td>' +
          '<td>' + util.esc(item.code) + '</td>' +
          '<td>' + util.esc(item.name) + '</td>' +
          '<td class="n">' + util.num(item.price, 2) + '</td>' +
          '<td class="n">' + util.pct(item.change_pct) + '</td>' +
          '<td class="n">' + value + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function bindRowClicks(root) {
    root.addEventListener('click', function (event) {
      var row = event.target.closest ? event.target.closest('tr[data-code]') : null;
      if (row) api && SS.app.navigate('#/stock?code=' + row.dataset.code);
    });
  }

  function render(content, ctx) {
    var state = { rankKind: 'gainers', sectorKind: 'industry', data: {} };

    var loggedOnce = false;

    function load(force) {
      var startedAt = Date.now();
      // 只记录**首次**加载的分路耗时：首屏慢时能一眼看出是哪个接口拖慢的，
      // 又不会因为后续自动轮询把控制台刷满。
      // 需要持续观察时在 URL 上加 ?debug=1。
      var verbose = loggedOnce ? /[?&]debug=1/.test(location.search) : true;
      loggedOnce = true;

      function timed(label, promise, fallback) {
        if (!verbose) {
          return fallback === undefined
            ? promise
            : promise.catch(function () { return fallback; });
        }
        var t0 = Date.now();
        return promise.then(function (data) {
          if (window.console && console.debug) {
            console.debug('[dashboard] ' + label + ' ' + (Date.now() - t0) + 'ms');
          }
          return data;
        }).catch(function (error) {
          if (window.console && console.warn) {
            console.warn('[dashboard] ' + label + ' 失败(' + (Date.now() - t0) + 'ms): ' + error.message);
          }
          return fallback;
        });
      }
      return Promise.all([
        timed('api/dashboard', api.dashboard()),
        timed('api/market/sectors', api.sectors(state.sectorKind), { items: [] }),
        timed('api/strategies', api.strategies(), { items: [] })
      ]).then(function (results) {
        if (verbose && window.console && console.debug) {
          console.debug('[dashboard] 全部就绪 ' + (Date.now() - startedAt) + 'ms');
        }
        paint(results[0], (results[1] && results[1].items) || [], (results[2] && results[2].items) || []);
      });
    }

    function paint(data, sectors, strategies) {
      state.data = data;
      var rankKind = state.rankKind;
      var sectorKind = state.sectorKind;
      var degraded = data.degraded || [];
      var html = '';

      html += '<div class="page-head"><div class="ph-left">' +
        '<h1>仪表盘</h1>' +
        '<div class="ph-sub">数据时间 ' + util.esc(data.as_of || '--') +
        ' · 交易日 ' + util.esc(data.trade_date || '--') +
        ' · 样本 ' + util.count(data.universe_size) + ' 只' +
        (data.sample_limited ? ' <span class="badge warn">样本受限</span>' : '') +
        ' · 时段 ' + util.esc(util.pick(data, 'clock.label', '--')) +
        '</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="dashScanAll">一键全策略扫描</button>' +
        '<button class="btn primary" id="dashRefresh">手动刷新</button>' +
        '</div></div>';

      if (degraded.length) {
        html += util.notice('warn', '部分数据块降级',
          degraded.map(function (item) {
            return util.esc(item.block) + '：' + util.esc(item.reason);
          }).join('<br>') +
          '<div class="small muted" style="margin-top:6px">可到「数据源」页面查看各源健康度并手动切换。</div>');
      }
      if (data.sample_limited) {
        html += util.notice('info', '当前按配额采样',
          '配置项 <code>quotas.universe_size</code> 限制了扫描范围，' +
          '市场级统计为样本口径。可在「用户配置」页调为 0 表示覆盖全部 A 股。');
      }

      var breadth = data.breadth || {};
      var emo = data.emotion || {};
      var metrics = util.pick(emo, 'emotion.metrics', {}) || {};
      var cycle = emo.cycle || {};

      html += '<div class="grid kpi">' +
        util.kpi('上涨 / 下跌', '<span class="up">' + util.count(breadth.up) + '</span> / ' +
          '<span class="down">' + util.count(breadth.down) + '</span>',
          '平盘 ' + util.count(breadth.flat)) +
        util.kpi('涨停 / 跌停', '<span class="up">' + util.count(breadth.limit_up) + '</span> / ' +
          '<span class="down">' + util.count(breadth.limit_down) + '</span>',
          '炸板 ' + util.count(metrics.broken) + ' 家') +
        util.kpi('情绪分', util.fixed(util.pick(emo, 'emotion.score'), 1),
          util.esc(util.pick(emo, 'emotion.level', '--')) +
          ' · 炸板率 ' + util.fixed(metrics.broken_rate, 1) + '%') +
        util.kpi('情绪周期', util.esc(cycle.label || '--'),
          '置信度 ' + util.fixed((cycle.confidence || 0) * 100, 0) + '%') +
        util.kpi('最高连板', util.count(metrics.max_consecutive) + ' 板',
          '竞价涨停 ' + util.count(metrics.auction_limit_up) + ' 家') +
        util.kpi('两市成交额', util.money(breadth.total_amount),
          '数据源 ' + util.esc(breadth.source || '--')) +
        '</div>';

      html += '<div class="card"><div class="card-head"><h2>主要指数</h2>' +
        '<span class="ch-sub">来自当前生效数据源的快照</span></div>' +
        renderIndices(data.index_quotes) + '</div>';

      html += '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>市场宽度</h3></div>' +
        '<div id="breadthBox">' + renderBreadth(data.breadth) + '</div></div>' +
        '<div class="card"><div class="card-head"><h3>情绪温度计</h3>' +
        '<span class="ch-sub">加权情绪分 + 逐项贡献</span></div>' +
        '<div id="emotionBox">' + renderEmotion(emo) + '</div></div>' +
        '</div>';

      // 涨跌 / 成交 / 换手 榜
      var leaders = data.leaders || {};
      html += '<div class="card"><div class="card-head"><h3>榜单速览</h3>' +
        '<div class="btn-row">' +
        ['gainers', 'losers', 'amount', 'turnover'].map(function (kind) {
          return '<button class="btn sm' + (kind === rankKind ? ' primary' : '') +
            '" data-rank="' + kind + '">' + util.esc(SS.RANK_LABELS[kind] || kind) + '</button>';
        }).join('') + '</div></div>' +
        '<div id="rankBox">' + rankTable(leaders[rankKind], rankKind) + '</div></div>';

      // 板块 + 龙头分层标签
      //: 涨停池 20 行按默认行高要 740px。原先它挤在右侧窄栏(473px)里、
      //: 又被 max-height:280px 截住 —— 实测只露 7 行、藏了 460px，还得内部滚动。
      //: 现在把标签留在窄栏、整表移到下方通栏：6 列都有足够宽度，高度也够，
      //: 因此不需要任何 max-height，20 行一次性铺开。
      html += '<div class="grid cols-2 asym-lead">' +
        '<div class="card"><div class="card-head"><h3>板块强弱</h3>' +
        '<div class="btn-row">' +
        ['industry', 'concept'].map(function (kind) {
          return '<button class="btn sm' + (kind === sectorKind ? ' primary' : '') +
            '" data-sector="' + kind + '">' + (kind === 'industry' ? '行业' : '概念') + '</button>';
        }).join('') + '</div></div><div id="sectorBox">' +
        sectorList(sectors) + '</div></div>' +
        '<div class="card"><div class="card-head"><h3>龙头/高标分层</h3>' +
        '<span class="ch-sub">按连板高度归层</span></div>' +
        '<div id="ladderBox">' + leaderList(emo.leaders, emo.cycle) + '</div></div>' +
        '</div>';

      // 涨停梯队：通栏整表，不限制高度
      html += '<div class="card"><div class="card-head"><h3>涨停梯队</h3>' +
        '<span class="ch-sub">涨停池全量 ' + ((emo.leaders || []).length) +
        ' 只 · 按连板高度排序 · 点击行查看个股</span></div>' +
        '<div id="ladderTableBox">' + leaderTable(emo.leaders) + '</div></div>';

      // 策略入选概览
      html += '<div class="card"><div class="card-head"><h3>策略入选概览</h3>' +
        '<span class="ch-sub">展示最近一次扫描结果；点击「一键全策略扫描」立即重跑</span></div>' +
        '<div id="strategyBox">' + strategyOverview(data.latest_scans, strategies) + '</div></div>';

      content.innerHTML = html;

      // 环形图
      var donut = util.$('#dashDonut');
      if (donut && data.breadth) {
        charts.donut(donut, [
          { label: '上涨', value: breadth.up, color: charts.themeColors().up },
          { label: '平盘', value: breadth.flat, color: charts.themeColors().muted },
          { label: '下跌', value: breadth.down, color: charts.themeColors().down }
        ], { centerText: util.count(breadth.total || (breadth.up + breadth.down + breadth.flat)),
             centerSub: '合计家数', legendSide: true, height: 190 });
      }

      bind(content, ctx, sectors);
    }

    function sectorList(items) {
      if (!items || !items.length) return util.emptyState('板块数据不可用');
      //: 20 行：接口返回 100 条，多列一些让"板块强弱"这张卡更饱满，
      //: 同时与右侧阶梯表(20 行 + 分层标签)的高度大致齐平。
      return util.barList(items.slice(0, 20).map(function (item) {
        // 上涨家数 / 有交易成分股数（涨+跌，平盘不计），比"成分股总数"更能反映当下强弱
        var traded = (item.up_count || 0) + (item.down_count || 0);
        var breadth = traded > 0
          ? ' <span class="faint small">' + (item.up_count || 0) + '/' + traded + '</span>'
          : '';
        var leader = item.leader_name
          ? ' <span class="faint small">领涨 ' + util.esc(item.leader_name) + '</span>'
          : '';
        return {
          label: item.name, value: item.change_pct,
          text: util.pct(item.change_pct) + breadth + leader
        };
      }));
    }

    function leaderList(leaders, cycle) {
      if (!leaders || !leaders.length) {
        return util.emptyState('暂无涨停样本', '非交易日或涨停池数据源不可用');
      }
      var tiers = {};
      leaders.forEach(function (item) { tiers[item.tier] = (tiers[item.tier] || 0) + 1; });
      //: 各层最高连板，用来说明"这层到底有多硬"，比只报家数有信息量
      var maxCons = {};
      leaders.forEach(function (item) {
        var c = item.consecutive || 1;
        if (!maxCons[item.tier] || c > maxCons[item.tier]) maxCons[item.tier] = c;
      });
      var rows = Object.keys(tiers).map(function (tier) {
        return {
          label: tier, value: tiers[tier],
          text: tiers[tier] + ' 只 · 最高 ' + maxCons[tier] + ' 板',
          color: tier === '龙头' ? 'var(--up)' : 'var(--accent)'
        };
      });
      var best = leaders.reduce(function (m, i) { return Math.max(m, i.consecutive || 1); }, 1);
      return util.barList(rows) +
        '<p class="small muted" style="margin-top:8px">共 ' + leaders.length +
        ' 只涨停样本 · 最高 ' + best + ' 连板；完整明细见下方「涨停梯队」。</p>';
    }

    /**
     * 涨停梯队全量表（通栏）。
     * 不加 max-height：20 行紧凑行高约 571px，整表铺开比"280px 滚动区 + 藏 460px"可用得多。
     */
    function leaderTable(leaders) {
      if (!leaders || !leaders.length) {
        return util.emptyState('暂无涨停样本', '非交易日或涨停池数据源不可用');
      }
      return '<div class="table-wrap"><table class="grid compact"><thead><tr>' +
        '<th class="col-no">#</th><th>分层</th><th>名称</th><th class="n">连板</th>' +
        '<th class="n">涨跌幅</th><th class="n">成交额</th>' +
        '<th class="n hide-narrow">换手</th>' +
        '<th class="n hide-narrow">首封</th>' +
        '<th class="n hide-narrow">开板</th>' +
        '<th class="hide-narrow">行业</th></tr></thead><tbody>' +
        leaders.map(function (item, index) {
          return '<tr class="clickable" data-code="' + util.esc(item.code) + '">' +
            '<td class="col-no">' + (index + 1) + '</td>' +
            '<td><span class="badge' + (item.tier === '龙头' ? ' up' : '') + '">' +
            util.esc(item.tier) + '</span></td>' +
            '<td>' + util.esc(item.name) + ' <span class="faint small">' +
            util.esc(item.code) + '</span></td>' +
            '<td class="n">' + (item.consecutive || 1) + '</td>' +
            '<td class="n">' + util.pct(item.change_pct) + '</td>' +
            '<td class="n">' + util.money(item.amount) + '</td>' +
            '<td class="n hide-narrow">' + util.fixed(item.turnover_rate, 2) + '%</td>' +
            '<td class="n small muted hide-narrow">' + util.esc(item.first_limit_time || '--') + '</td>' +
            '<td class="n hide-narrow">' + util.count(item.open_times) + '</td>' +
            '<td class="small muted hide-narrow">' + util.esc(item.industry || '--') + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }

    function strategyOverview(latestScans, strategies) {
      if (!strategies || !strategies.length) return util.emptyState('策略目录不可用');
      var byKey = {};
      strategies.forEach(function (s) { byKey[s.key] = s; });
      var rows = SS.STRATEGY_ORDER.map(function (key) {
        var meta = byKey[key] || {};
        var scan = (latestScans || {})[key];
        var items = (scan && scan.items) || [];
        var top = items.slice(0, 4).map(function (item) {
          return '<span class="chip">' + util.esc(item.name) + ' ' +
            '<span class="' + util.scoreTone(item.score) + '">' + util.fixed(item.score, 1) + '</span></span>';
        }).join(' ');
        return '<tr>' +
          '<td><strong>' + util.esc(meta.name || key) + '</strong>' +
          '<div class="faint small">' + util.esc(meta.source || '') + '</div></td>' +
          '<td>' + (scan ? util.esc(scan.trade_date || '') : '<span class="faint">尚未扫描</span>') + '</td>' +
          '<td class="n">' + (items.length ? items.length : '--') + '</td>' +
          '<td>' + (top || '<span class="faint small">无入选</span>') + '</td>' +
          '<td><button class="btn sm" data-goto-strategy="' + util.esc(key) + '">查看</button></td>' +
          '</tr>';
      }).join('');
      return '<div class="table-wrap"><table class="grid"><thead><tr>' +
        '<th>策略</th><th>最近扫描日</th><th class="n">入选数</th><th>评分靠前标的</th><th></th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    function bind(root, ctx, sectors) {
      var refreshBtn = util.$('#dashRefresh');
      if (refreshBtn) refreshBtn.addEventListener('click', function () {
        SS.app.setLoading(true);
        load(true).catch(function (e) { util.toast(e.message, 'error'); })
          .then(function () { SS.app.setLoading(false); });
      });
      var scanAll = util.$('#dashScanAll');
      if (scanAll) scanAll.addEventListener('click', function () {
        scanAll.disabled = true;
        scanAll.textContent = '扫描中…';
        util.toast('已开始全策略扫描（后台执行，可随时切走页面）', 'ok', 5000);
        //: 走异步任务 + 轮询：5 个策略逐个跑，分钟级。同步请求会被代理/浏览器
        //: 的空闲超时掐断，前端只能报"无法连接到后端服务"。
        api.createScanJob('all', { perStrategy: 20 }).then(function (created) {
          var job = (created && created.job) || {};
          if (!job.id) throw new Error('后端未返回任务 ID');
          return util.pollScanJob(job.id, {
            interval: 3000,
            onProgress: function (j) {
              scanAll.textContent = '扫描中 ' + (j.percent || 0) + '%';
            }
          });
        }).then(function (job) {
          var results = (job.result && job.result.results) || {};
          var failed = Object.keys(results).filter(function (k) { return results[k].error; });
          util.toast('扫描完成' + (failed.length ? '（' + failed.length + ' 个策略失败）' : ''),
            failed.length ? 'error' : 'ok');
          return load(true);
        }).catch(function (error) {
          util.toast('扫描失败: ' + error.message, 'error', 6000);
        }).then(function () {
          scanAll.disabled = false;
          scanAll.textContent = '一键全策略扫描';
        });
      });

      root.addEventListener('click', function (event) {
        var row = event.target.closest ? event.target.closest('tr[data-code]') : null;
        if (row) { SS.app.navigate('#/stock?code=' + row.dataset.code); return; }
        var rankBtn = event.target.closest ? event.target.closest('[data-rank]') : null;
        if (rankBtn) {
          state.rankKind = rankBtn.dataset.rank;
          var leaders = state.data.leaders || {};
          util.$('#rankBox').innerHTML = rankTable(leaders[state.rankKind], state.rankKind);
          util.$$('[data-rank]').forEach(function (b) {
            b.classList.toggle('primary', b.dataset.rank === state.rankKind);
          });
          return;
        }
        var sectorBtn = event.target.closest ? event.target.closest('[data-sector]') : null;
        if (sectorBtn) {
          state.sectorKind = sectorBtn.dataset.sector;
          util.$$('[data-sector]').forEach(function (b) {
            b.classList.toggle('primary', b.dataset.sector === state.sectorKind);
          });
          util.$('#sectorBox').innerHTML = util.emptyState('加载中…');
          api.sectors(state.sectorKind).then(function (data) {
            util.$('#sectorBox').innerHTML = sectorList(data.items || []);
          }).catch(function (error) {
            util.$('#sectorBox').innerHTML = util.notice('bad', '板块数据不可用', util.esc(error.message));
          });
          return;
        }
        var goBtn = event.target.closest ? event.target.closest('[data-goto-strategy]') : null;
        if (goBtn) {
          SS.app.navigate('#/screener?strategy=' + goBtn.dataset.gotoStrategy);
        }
      });
    }

    ctx.setRefresh(function (force) { return load(!!force); });
    ctx.setInterval(function () { load(false); }, 45000);

    return load(false);
  }

  SS.views.dashboard = { title: '仪表盘', render: render };
})(window);
