/* ============================================================================
   views/market.js —— 行情中枢
   指数 / 榜单 / 板块与成分股 / 涨停池与连板梯队 / 市场宽度 / 资金流
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var params = (ctx && ctx.params) || {};
    //: 支持 #/market?tab=watch 直接定位标签页（自选池的入口链接用它）
    var initial = params.tab || 'watch';
    var state = { tab: initial, rankKind: 'gainers', sectorKind: 'industry', limit: 50 };

    var TABS = [
      { key: 'watch', label: '自选股池' },
      { key: 'rank', label: '榜单' },
      { key: 'sectors', label: '板块' },
      { key: 'limit', label: '涨停池与梯队' },
      { key: 'breadth', label: '市场宽度' },
      { key: 'indices', label: '指数速览' },
      { key: 'flow', label: '板块资金流' }
    ];

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>行情中枢</h1>' +
        '<div class="ph-sub" id="mktSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<label class="switch"><input type="checkbox" id="mktAuto" checked>自动刷新</label>' +
        '<button class="btn" id="mktReload">手动刷新数据源</button>' +
        '</div></div>' +
        '<div class="tabs">' + TABS.map(function (t) {
          return '<button class="tab' + (t.key === state.tab ? ' active' : '') +
            '" data-tab="' + t.key + '">' + util.esc(t.label) + '</button>';
        }).join('') + '</div>' +
        '<div id="mktBody"></div>';
    }

    function load() {
      var body = util.$('#mktBody');
      if (!body) return Promise.resolve();
      body.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载中…</p></div>';
      var sub = util.$('#mktSub');

      if (state.tab === 'watch') return loadWatch(body, sub);
      if (state.tab === 'rank') return loadRank(body, sub);
      if (state.tab === 'sectors') return loadSectors(body, sub);
      if (state.tab === 'limit') return loadLimit(body, sub);
      if (state.tab === 'breadth') return loadBreadth(body, sub);
      if (state.tab === 'indices') return loadIndices(body, sub);
      if (state.tab === 'flow') return loadFlow(body, sub);
      return Promise.resolve();
    }

    /**
     * 自选股池 —— 带实时行情的自选列表。
     *
     * 背景：自选原先只能在「用户配置」页底部看到，而且只有 代码/名称/备注/移除，
     * **没有行情**，算不上"股池"。这里把它放进行情中枢作为第一个标签页：
     * 自选本身就是"我要盯的票"，与榜单/板块同属看盘动作，放一起比塞进设置页合理。
     */
    function loadWatch(body, sub) {
      return api.watchlist().then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          if (sub) sub.textContent = '自选股池 · 0 只';
          body.innerHTML = '<div class="card"><div class="card-head"><h3>自选股池</h3></div>' +
            util.emptyState('自选池还是空的',
              '到「个股详情」页点「加入自选」，或点右侧按钮批量添加') +
            '<div class="btn-row" style="margin-top:10px">' +
            '<button class="btn sm primary" id="watchAddBatch">批量添加代码</button>' +
            '<button class="btn sm" id="watchGoScreener">去选股</button></div></div>';
          return;
        }
        var codes = items.map(function (i) { return i.code; });
        return api.quotes(codes).then(function (quotes) {
          var byCode = {};
          (quotes.items || []).forEach(function (q) { byCode[q.code] = q; });
          //: 以自选表为基准合并行情 —— 某只票上游取不到时仍要出现在池子里（显示 --），
          //: 而不是静默消失
          var rows = items.map(function (it) {
            var q = byCode[it.code] || {};
            return {
              code: it.code, name: q.name || it.name || '--', note: it.note || '',
              price: q.price, change_pct: q.change_pct, change: q.change,
              amount: q.amount, turnover_rate: q.turnover_rate,
              volume_ratio: q.volume_ratio, amplitude: q.amplitude,
              total_mv: q.total_mv, board: q.board, missing: !byCode[it.code]
            };
          });
          var up = rows.filter(function (r) { return util.toNum(r.change_pct) > 0; }).length;
          var down = rows.filter(function (r) { return util.toNum(r.change_pct) < 0; }).length;
          if (sub) {
            sub.textContent = '自选股池 · ' + rows.length + ' 只 · 涨 ' + up + ' / 跌 ' + down +
              ' · 源 ' + ((quotes && quotes.source) || '--') + ' · ' + ((quotes && quotes.as_of) || '');
          }
          body.innerHTML = '<div class="card"><div class="card-head"><h3>自选股池</h3>' +
            '<div class="btn-row">' +
            '<button class="btn sm" id="watchAddBatch">批量添加</button>' +
            '<button class="btn sm" id="watchExport">导出 CSV</button>' +
            '</div></div>' +
            '<div class="ch-sub" style="margin-bottom:8px">点击行查看个股 · 「移除」仅从自选池删除</div>' +
            '<div id="watchTable"></div></div>';

          var cols = [
            { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
            { label: '板块', key: 'board' },
            { label: '现价', num: true, sort: function (i) { return i.price; } },
            { label: '涨跌幅', num: true, sort: function (i) { return i.change_pct; } },
            { label: '涨跌额', num: true, sort: function (i) { return i.change; } },
            { label: '成交额', num: true, sort: function (i) { return i.amount; } },
            { label: '换手率', num: true, sort: function (i) { return i.turnover_rate; } },
            { label: '量比', num: true, sort: function (i) { return i.volume_ratio; } },
            { label: '振幅', num: true, sort: function (i) { return i.amplitude; } },
            { label: '总市值', num: true, sort: function (i) { return i.total_mv; } },
            { label: '备注', key: 'note' },
            { label: '', sort: null }
          ];
          util.sortableTable(util.$('#watchTable'), cols, rows, function (item) {
            var price = item.missing ? '<span class="faint">--</span>' : util.num(item.price, 2);
            return [
              '<span class="mono">' + util.esc(item.code) + '</span>',
              util.esc(item.name),
              '<span class="faint small">' + util.esc(item.board || '') + '</span>',
              price,
              item.missing ? '<span class="faint">--</span>' : util.pct(item.change_pct),
              item.missing ? '<span class="faint">--</span>'
                : util.num(item.change, 2, { colored: true, sign: true }),
              item.missing ? '<span class="faint">--</span>' : util.money(item.amount),
              item.missing ? '<span class="faint">--</span>' : util.fixed(item.turnover_rate, 2) + '%',
              item.missing ? '<span class="faint">--</span>' : util.fixed(item.volume_ratio, 2),
              item.missing ? '<span class="faint">--</span>' : util.fixed(item.amplitude, 2) + '%',
              item.missing ? '<span class="faint">--</span>' : util.money(item.total_mv),
              '<span class="small muted">' + util.esc(item.note) + '</span>',
              '<button class="btn sm" data-unwatch="' + util.esc(item.code) + '">移除</button>'
            ];
          }, { initialSort: 4, initialAsc: false, onRowClick: openStock });
        });
      }).catch(function (error) { fail(body, '自选股池不可用', error); });
    }

    function loadRank(body, sub) {
      return api.rank(state.rankKind, state.limit).then(function (data) {
        var items = data.items || [];
        if (sub) sub.textContent = '榜单：' + (SS.RANK_LABELS[state.rankKind] || state.rankKind) +
          ' · ' + items.length + ' 条 · 源 ' + (data.source || '--') + ' · ' + (data.as_of || '');
        body.innerHTML = '<div class="card"><div class="card-head">' +
          '<div class="btn-row">' + Object.keys(SS.RANK_LABELS).map(function (kind) {
            return '<button class="btn sm' + (kind === state.rankKind ? ' primary' : '') +
              '" data-rank="' + kind + '">' + util.esc(SS.RANK_LABELS[kind]) + '</button>';
          }).join('') + '</div>' +
          '<span class="ch-sub">点击表头排序 · 点击行查看个股</span></div>' +
          '<div id="rankTable"></div></div>';
        var cols = [
          { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
          { label: '板块', key: 'board' },
          { label: '现价', num: true, sort: function (i) { return i.price; } },
          { label: '涨跌幅', num: true, sort: function (i) { return i.change_pct; } },
          { label: '涨跌额', num: true, sort: function (i) { return i.change; } },
          { label: '成交额', num: true, sort: function (i) { return i.amount; } },
          { label: '换手率', num: true, sort: function (i) { return i.turnover_rate; } },
          { label: '量比', num: true, sort: function (i) { return i.volume_ratio; } },
          { label: '振幅', num: true, sort: function (i) { return i.amplitude; } },
          { label: '总市值', num: true, sort: function (i) { return i.total_mv; } }
        ];
        util.sortableTable(util.$('#rankTable'), cols, items, function (item) {
          return [
            '<span class="mono">' + util.esc(item.code) + '</span>', util.esc(item.name),
            '<span class="faint small">' + util.esc(item.board || '') + '</span>',
            util.num(item.price, 2), util.pct(item.change_pct), util.num(item.change, 2, { colored: true, sign: true }),
            util.money(item.amount), util.fixed(item.turnover_rate, 2) + '%',
            util.fixed(item.volume_ratio, 2), util.fixed(item.amplitude, 2) + '%',
            util.money(item.total_mv)
          ];
        }, { initialSort: 4, initialAsc: false, onRowClick: openStock });
      }).catch(function (error) { fail(body, '榜单不可用', error); });
    }

    function loadSectors(body, sub) {
      return api.sectors(state.sectorKind).then(function (data) {
        var items = data.items || [];
        if (sub) sub.textContent = '板块 · ' + items.length + ' 个 · 源 ' + (data.source || '--');
        body.innerHTML = '<div class="card"><div class="card-head">' +
          '<div class="btn-row">' +
          '<button class="btn sm' + (state.sectorKind === 'industry' ? ' primary' : '') + '" data-sector="industry">行业板块</button>' +
          '<button class="btn sm' + (state.sectorKind === 'concept' ? ' primary' : '') + '" data-sector="concept">概念板块</button>' +
          '</div><span class="ch-sub">点击板块查看成分股</span></div>' +
          '<div class="grid cols-2"><div id="sectorBars"></div><div id="sectorTable"></div></div></div>';
        util.$('#sectorBars').innerHTML = util.barList(items.slice(0, 18).map(function (item) {
          return { label: item.name, value: item.change_pct, text: util.pct(item.change_pct) };
        }));
        var cols = [
          { label: '板块', key: 'name' },
          { label: '涨跌幅', num: true, sort: function (i) { return i.change_pct; } },
          { label: '上涨家数', num: true, sort: function (i) { return i.up_count; } },
          { label: '成交额', num: true, sort: function (i) { return i.amount; } },
          { label: '领涨股', key: 'leader_name' },
          { label: '主力净流入', num: true, sort: function (i) { return i.main_net_inflow; } }
        ];
        util.sortableTable(util.$('#sectorTable'), cols, items, function (item) {
          return [
            util.esc(item.name), util.pct(item.change_pct), util.count(item.up_count),
            util.money(item.amount),
            util.esc(item.leader_name || '--') + ' ' + util.pct(item.leader_change_pct),
            item.main_net_inflow ? util.money(item.main_net_inflow) : '--'
          ];
        }, {
          initialSort: 1, initialAsc: false,
          onRowClick: function (item) { showSector(item); }
        });
      }).catch(function (error) { fail(body, '板块不可用', error); });
    }

    function showSector(item) {
      var body = util.modal(item.name + ' · 成分股', '<div class="boot-placeholder"><div class="spinner"></div><p>加载中…</p></div>');
      api.sectorDetail(item.code, item.name).then(function (data) {
        var members = data.members || [];
        body.innerHTML = util.kvList([
          ['板块代码', data.code], ['成分股数', data.member_count],
          ['上涨家数', data.up_count + ' (' + util.fixed((data.up_ratio || 0) * 100, 1) + '%)'],
          ['平均涨幅', util.pct(data.avg_change_pct)], ['合计成交额', util.money(data.amount)]
        ]) + '<div class="table-wrap" style="max-height:52vh;margin-top:8px">' +
          '<table class="grid"><thead><tr><th class="col-no">#</th><th>代码</th><th>名称</th>' +
          '<th class="n">现价</th><th class="n">涨跌幅</th><th class="n">成交额</th></tr></thead><tbody>' +
          members.slice(0, 120).map(function (m, index) {
            return '<tr class="clickable" data-code="' + util.esc(m.code) + '">' +
              '<td class="col-no">' + (index + 1) + '</td><td class="mono">' + util.esc(m.code) + '</td>' +
              '<td>' + util.esc(m.name) + '</td><td class="n">' + util.num(m.price, 2) + '</td>' +
              '<td class="n">' + util.pct(m.change_pct) + '</td><td class="n">' + util.money(m.amount) + '</td></tr>';
          }).join('') + '</tbody></table></div>' +
          '<p class="small muted" style="margin-top:8px">数据源：' + util.esc(data.source || '--') + '</p>';
        body.addEventListener('click', function (event) {
          var row = event.target.closest ? event.target.closest('tr[data-code]') : null;
          if (row) { util.closeModal(); openStock({ code: row.dataset.code }); }
        });
      }).catch(function (error) {
        body.innerHTML = util.notice('bad', '成分股不可用', util.esc(error.message));
      });
    }

    function loadLimit(body, sub) {
      return api.limitUp().then(function (data) {
        if (sub) sub.textContent = '涨停池 · 涨停 ' + data.limit_up_count + ' 家 · 封板率 ' +
          util.fixed(100 - (data.broken_rate || 0), 1) + '% · 源 ' + (data.source || '--');
        var ladder = data.ladder || {};
        var ladderRows = Object.keys(ladder).map(function (level) {
          return { label: level + ' 板', value: ladder[level], text: ladder[level] + ' 家' };
        }).sort(function (a, b) { return parseInt(b.label) - parseInt(a.label); });
        var tone = data.broken_rate >= 40 ? 'down' : (data.broken_rate >= 25 ? '' : 'up');
        body.innerHTML = '<div class="grid kpi">' +
          util.kpi('涨停家数', util.count(data.limit_up_count), '封板率 ' +
            util.fixed(100 - (data.broken_rate || 0), 1) + '%') +
          util.kpi('炸板家数', util.count(data.broken_count), '炸板率 ' +
            util.fixed(data.broken_rate, 1) + '%', data.broken_rate >= 30 ? 'down' : '') +
          util.kpi('最高连板', util.count(data.max_consecutive) + ' 板', '梯队档位 ' +
            Object.keys(ladder).length) +
          util.kpi('日期', util.esc(data.date || '--'), '源 ' + util.esc(data.source || '--')) +
          '</div>' +
          '<div class="card"><div class="card-head"><h3>连板梯队</h3></div>' +
          (ladderRows.length ? util.barList(ladderRows, { digits: 0 })
            : util.emptyState('暂无连板样本')) + '</div>' +
          '<div class="card"><div class="card-head"><h3>涨停 / 炸板明细</h3>' +
          '<span class="ch-sub">点击表头排序</span></div><div id="limitTable"></div></div>';
        var rows = (data.limit_up || []).concat((data.broken || []).map(function (b) {
          var copy = Object.assign({}, b);
          copy.is_broken = true;
          return copy;
        }));
        var cols = [
          { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
          { label: '连板', num: true, sort: function (i) { return i.consecutive; } },
          { label: '现价', num: true, sort: function (i) { return i.price; } },
          { label: '涨跌幅', num: true, sort: function (i) { return i.change_pct; } },
          { label: '成交额', num: true, sort: function (i) { return i.amount; } },
          { label: '换手率', num: true, sort: function (i) { return i.turnover_rate; } },
          { label: '首封时间', key: 'first_limit_time' },
          { label: '开板次数', num: true, sort: function (i) { return i.open_times; } },
          { label: '状态', key: 'is_broken' },
          { label: '行业', key: 'industry' }
        ];
        util.sortableTable(util.$('#limitTable'), cols, rows, function (item) {
          return [
            '<span class="mono">' + util.esc(item.code) + '</span>', util.esc(item.name),
            (item.consecutive || 1) + ' 板', util.num(item.price, 2), util.pct(item.change_pct),
            util.money(item.amount), util.fixed(item.turnover_rate, 2) + '%',
            util.esc(item.first_limit_time || '--'),
            item.is_broken ? '--' : util.count(item.open_times),
            item.is_broken ? util.badge('炸板', 'down') : util.badge('封板', 'up'),
            util.esc(item.industry || '--')
          ];
        }, { initialSort: 2, initialAsc: false, onRowClick: openStock });
      }).catch(function (error) { fail(body, '涨停池不可用', error); });
    }

    function loadBreadth(body, sub) {
      return api.breadth().then(function (data) {
        if (sub) sub.textContent = '市场宽度 · 源 ' + (data.source || '--');
        body.innerHTML = '<div class="grid cols-2">' +
          '<div class="card"><div class="card-head"><h3>涨跌分布</h3></div>' +
          util.stackBar([
            { label: '上涨', value: data.up, color: 'var(--up)' },
            { label: '平盘', value: data.flat, color: 'var(--faint)' },
            { label: '下跌', value: data.down, color: 'var(--down)' }
          ]) + '<div style="margin-top:12px">' + util.kvList([
            ['上涨占比', util.fixed((data.up_ratio || 0) * 100, 2) + '%'],
            ['涨停 / 跌停', util.count(data.limit_up) + ' / ' + util.count(data.limit_down)],
            ['涨超 5% / 跌超 5%', util.count(data.up_over_5) + ' / ' + util.count(data.down_over_5)],
            ['两市成交额', util.money(data.total_amount)],
            ['合计家数', util.count(data.total)]
          ]) + '</div></div>' +
          '<div class="card"><div class="card-head"><h3>分布图</h3></div>' +
          '<div class="chart-box"><canvas id="breadthDonut"></canvas></div></div></div>';
        var canvas = util.$('#breadthDonut');
        if (canvas) {
          charts.donut(canvas, [
            { label: '上涨', value: data.up, color: charts.themeColors().up },
            { label: '平盘', value: data.flat, color: charts.themeColors().muted },
            { label: '下跌', value: data.down, color: charts.themeColors().down }
          ], { centerText: util.count(data.total), centerSub: '合计', legendSide: true, height: 240 });
        }
      }).catch(function (error) { fail(body, '市场宽度不可用', error); });
    }

    function loadIndices(body, sub) {
      return api.indices().then(function (data) {
        if (sub) sub.textContent = '指数速览 · 匹配 ' + data.matched + '/' + data.total;
        body.innerHTML = '<div class="card"><div class="card-head"><h3>主要指数</h3>' +
          '<span class="ch-sub">' + util.esc(data.as_of || '') + '</span></div>' +
          '<div class="grid cols-4">' + (data.items || []).map(function (item) {
            if (item.missing) return util.kpi(item.name, '<span class="faint">--</span>', '快照未返回');
            return util.kpi(item.name, '<span class="' + util.dirClass(item.change_pct) + '">' +
              util.num(item.price, 2) + '</span>', util.pct(item.change_pct) + ' · ' + util.money(item.amount));
          }).join('') + '</div>' +
          '<p class="small muted" style="margin-top:10px">指数行情来自全市场快照的代码匹配，' +
          '若某项显示 "--" 说明当前数据源未在快照中返回该指数；' +
          '可在「数据源」页面切换源后重试。</p></div>';
      }).catch(function (error) { fail(body, '指数不可用', error); });
    }

    function loadFlow(body, sub) {
      return api.sectorFlow(40).then(function (data) {
        if (sub) sub.textContent = '板块资金流 · 源 ' + (data.source || '--');
        var items = data.items || [];
        body.innerHTML = '<div class="card"><div class="card-head"><h3>板块主力资金净流入</h3>' +
          '<span class="ch-sub">正值为净流入</span></div>' +
          util.barList(items.map(function (item) {
            return {
              label: item.name, value: item.main_net_inflow,
              text: util.money(item.main_net_inflow) + ' ' + util.pct(item.change_pct),
              color: item.main_net_inflow >= 0 ? 'var(--up)' : 'var(--down)'
            };
          })) + '</div>';
      }).catch(function (error) { fail(body, '板块资金流不可用', error); });
    }

    function fail(body, title, error) {
      body.innerHTML = util.notice('bad', title,
        util.esc(error.message) +
        '<div class="small muted" style="margin-top:6px">可以尝试：到「数据源」页面查看健康度并手动切换源，' +
        '或点「手动刷新数据源」清空缓存重新拉取。</div>');
    }

    function openStock(item) {
      if (item && item.code) SS.app.navigate('#/stock?code=' + item.code);
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var tab = event.target.closest ? event.target.closest('[data-tab]') : null;
        if (tab) {
          state.tab = tab.dataset.tab;
          util.$$('[data-tab]').forEach(function (node) {
            node.classList.toggle('active', node.dataset.tab === state.tab);
          });
          load();
          return;
        }
        var rankBtn = event.target.closest ? event.target.closest('[data-rank]') : null;
        if (rankBtn) { state.rankKind = rankBtn.dataset.rank; load(); return; }

        // ---- 自选池操作 ----
        var unwatch = event.target.closest ? event.target.closest('[data-unwatch]') : null;
        if (unwatch) {
          var code = unwatch.dataset.unwatch;
          unwatch.disabled = true;
          api.removeWatch([code]).then(function () {
            util.toast('已从自选移除 ' + code, 'ok');
            return load();
          }).catch(function (error) {
            util.toast('移除失败: ' + error.message, 'error', 6000);
            unwatch.disabled = false;
          });
          return;
        }
        if (event.target.closest && event.target.closest('#watchExport')) {
          window.location.href = api.apiUrl('api/export/watchlist.csv');
          return;
        }
        if (event.target.closest && event.target.closest('#watchAddBatch')) {
          util.modal('批量添加自选',
            '<div class="field"><label>证券代码（逗号/空格/换行分隔）</label>' +
            '<textarea id="watchCodes" rows="4" placeholder="600519, 000001, 300750"></textarea></div>' +
            '<div class="btn-row"><button class="btn primary" id="watchCodesOk">添加</button></div>');
          var okBtn = util.$('#watchCodesOk');
          if (okBtn) okBtn.addEventListener('click', function () {
            var raw = (util.$('#watchCodes') || {}).value || '';
            var codes = raw.split(/[\s,，;；]+/).filter(Boolean);
            if (!codes.length) { util.toast('请先填写代码', 'error'); return; }
            api.addWatchBatch(codes).then(function (data) {
              util.closeModal();
              util.toast('已添加 ' + ((data && data.added) || codes.length) + ' 只到自选', 'ok');
              return load();
            }).catch(function (error) {
              util.toast('添加失败: ' + error.message, 'error', 6000);
            });
          });
          return;
        }
        if (event.target.closest && event.target.closest('#watchGoScreener')) {
          SS.app.navigate('#/screener');
          return;
        }
        var sectorBtn = event.target.closest ? event.target.closest('[data-sector]') : null;
        if (sectorBtn) { state.sectorKind = sectorBtn.dataset.sector; load(); return; }
        var reload = event.target.closest ? event.target.closest('#mktReload') : null;
        if (reload) {
          reload.disabled = true;
          util.toast('正在清空缓存并重新拉取快照，请稍候…', 'ok', 4000);
          api.refreshData('snapshot', true).then(function (data) {
            util.toast('已通过 ' + data.source + ' 刷新 ' + data.items + ' 条数据', 'ok');
            return SS.app.loadHealth();
          }).catch(function (error) {
            util.toast('刷新失败: ' + error.message, 'error', 6000);
          }).then(function () { reload.disabled = false; load(); });
        }
      });
      var auto = util.$('#mktAuto');
      ctx.setInterval(function () {
        if (auto && !auto.checked) return;
        load();
      }, 45000);
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(); });
    return load();
  }

  SS.views.market = { title: '行情中枢', render: render };
})(window);
