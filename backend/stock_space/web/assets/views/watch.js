/* ============================================================================
   views/watch.js —— 我的持仓（自选股池 + 模拟持仓）

   为什么单独成页：自选与模拟持仓原先只作为一张卡片塞在「用户配置」页最底部，
   而那一页是"改设置"的地方，不是"看盘"的地方。自选要盯行情、持仓要算浮盈，
   都属于日常看盘动作，因此独立成一页并放进侧边栏导航。

   自选池带实时行情（现价/涨跌幅/成交额/换手/量比/振幅/市值）；
   模拟持仓在成本与份额之上再取实时价，算出市值与浮动盈亏。

   依赖：api.watchlist / api.addWatchBatch / api.removeWatch /
        api.portfolio / api.closePosition / api.deletePosition / api.quotes
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace = global.StockSpace || {};
  var util = SS.util, api = SS.api;

  SS.views = SS.views || {};

  SS.views.watch = {
    title: '我的持仓',
    render: function (content, ctx) {
      var state = { tab: 'watch', portfolio: null, quotes: {} };

      var TABS = [
        { key: 'watch', label: '自选股池' },
        { key: 'portfolio', label: '模拟持仓' }
      ];

      function shell() {
        return '<div class="page-head"><div class="ph-left"><h1>我的持仓</h1>' +
          '<div class="ph-sub" id="watchSub">加载中…</div></div>' +
          '<div class="page-actions">' +
          '<button class="btn" id="watchReload">刷新行情</button>' +
          '</div></div>' +
          '<div id="watchKpi"></div>' +
          '<div class="tabs">' + TABS.map(function (t) {
            return '<button class="tab' + (t.key === state.tab ? ' active' : '') +
              '" data-wtab="' + t.key + '">' + util.esc(t.label) + '</button>';
          }).join('') + '</div>' +
          '<div id="watchBody"></div>';
      }

      // ------------------------------------------------------------ 数据合并
      /**
       * 带正负号与涨跌配色的金额。
       * ``util.money`` 只接受 (value, digits)，不能着色 —— 浮盈/浮亏必须区分颜色，
       * 所以这里包一层。
       */
      function moneySigned(value) {
        if (!util.isNum(value)) return '--';
        var n = util.toNum(value);
        var text = (n > 0 ? '+' : '') + util.money(n);
        return '<span class="' + util.dirClass(n) + '">' + text + '</span>';
      }

      /** 给一批标的补上实时行情：返回 {code: quote}，取不到的留空。 */
      function loadQuotes(codes) {
        if (!codes.length) return Promise.resolve({ items: [], source: '' });
        return api.quotes(codes).catch(function () { return { items: [], source: '' }; });
      }

      /**
       * 模拟持仓 + 实时行情 → 计算市值与浮动盈亏。
       * 取不到行情的持仓用成本价兜底（浮盈记 0），避免整行显示空白。
       */
      function enrichPortfolio(portfolio, quotes) {
        var byCode = {};
        (quotes.items || []).forEach(function (q) { byCode[q.code] = q; });
        var items = (portfolio.items || []).map(function (p) {
          var q = byCode[p.code] || {};
          var live = util.isNum(q.price) ? util.toNum(q.price) : null;
          var cost = util.toNum(p.price);
          var shares = util.toNum(p.shares);
          var price = p.status === 'closed'
            ? (util.isNum(p.close_price) ? util.toNum(p.close_price) : cost)
            : (live === null ? cost : live);
          var pnlPct = p.status === 'closed'
            ? (util.isNum(p.pnl_pct) ? util.toNum(p.pnl_pct) : 0)
            : (cost > 0 ? (price - cost) / cost * 100 : 0);
          return {
            id: p.id, code: p.code, name: q.name || p.name || '--',
            status: p.status, price: cost, shares: shares,
            live: live, current: price, hasLive: live !== null,
            market_value: price * shares, cost_value: cost * shares,
            pnl_pct: pnlPct, pnl_amount: (price - cost) * shares,
            reason: p.reason || '', strategy: p.strategy || '',
            opened_at_text: p.opened_at_text || '', closed_at_text: p.closed_at_text || ''
          };
        });
        return { items: items, source: (quotes && quotes.source) || '' };
      }

      // ------------------------------------------------------------ 渲染
      function paint() {
        var sub = util.$('#watchSub');
        var body = util.$('#watchBody');
        var kpi = util.$('#watchKpi');
        if (!body) return Promise.resolve();

        body.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载中…</p></div>';

        return Promise.all([api.watchlist(), api.portfolio()]).then(function (res) {
          var watch = (res[0] && res[0].items) || [];
          var portfolio = res[1] || {};
          state.portfolio = portfolio;

          var codes = {};
          watch.forEach(function (w) { codes[w.code] = 1; });
          (portfolio.items || []).forEach(function (p) {
            if (p.status === 'open') codes[p.code] = 1;
          });
          var all = Object.keys(codes);

          return loadQuotes(all).then(function (quotes) {
            var byCode = {};
            (quotes.items || []).forEach(function (q) { byCode[q.code] = q; });
            state.quotes = byCode;
            var enriched = enrichPortfolio(portfolio, quotes);

            paintKpi(kpi, watch, enriched, byCode, quotes);
            if (sub) {
              sub.textContent = '自选 ' + watch.length + ' 只 · 模拟持仓 持有 ' +
                (portfolio.open_count || 0) + ' / 已平 ' + (portfolio.closed_count || 0) +
                ' · 行情源 ' + (quotes.source || '--') +
                (quotes.as_of ? ' · ' + quotes.as_of : '');
            }
            if (state.tab === 'watch') paintWatch(body, watch, byCode, quotes);
            else paintPortfolio(body, enriched);
          });
        }).catch(function (error) {
          body.innerHTML = util.notice('bad', '加载失败', util.esc(error.message));
        });
      }

      function paintKpi(host, watch, portfolio, byCode, quotes) {
        if (!host) return;
        var up = watch.filter(function (w) {
          var q = byCode[w.code];
          return q && util.toNum(q.change_pct) > 0;
        }).length;
        var down = watch.filter(function (w) {
          var q = byCode[w.code];
          return q && util.toNum(q.change_pct) < 0;
        }).length;
        var open = portfolio.items.filter(function (i) { return i.status === 'open'; });
        var mv = open.reduce(function (s, i) { return s + i.market_value; }, 0);
        var cost = open.reduce(function (s, i) { return s + i.cost_value; }, 0);
        var pnl = mv - cost;
        var pnlPct = cost > 0 ? pnl / cost * 100 : 0;
        var noQuote = open.filter(function (i) { return !i.hasLive; }).length;

        host.innerHTML = '<div class="grid kpi">' +
          util.kpi('自选家数', util.count(watch.length), '涨 ' + up + ' / 跌 ' + down,
            up >= down ? 'up' : 'down') +
          util.kpi('持仓市值', util.money(mv), '成本 ' + util.money(cost)) +
          util.kpi('浮动盈亏', util.money(pnl), util.pct(pnlPct) + '（按现价）',
            pnl >= 0 ? 'up' : 'down') +
          util.kpi('持仓只数', util.count(open.length),
            noQuote ? noQuote + ' 只无行情(按成本计价)' : '全部有实时行情') +
          '</div>' +
          (noQuote ? util.notice('warn', '部分持仓缺少实时行情',
            noQuote + ' 只标的取不到现价，浮动盈亏按成本价计（显示为 0）。') : '');
      }

      function paintWatch(body, watch, byCode, quotes) {
        if (!watch.length) {
          body.innerHTML = '<div class="card"><div class="card-head"><h3>自选股池</h3></div>' +
            util.emptyState('自选池还是空的',
              '到「个股详情」页点「加入自选」，或点下面的按钮批量添加') +
            '<div class="btn-row" style="margin-top:10px">' +
            '<button class="btn sm primary" id="watchAddBatch">批量添加代码</button>' +
            '<button class="btn sm" id="watchGoScreener">去选股</button></div></div>';
          return;
        }
        var rows = watch.map(function (w) {
          var q = byCode[w.code] || {};
          return {
            code: w.code, name: q.name || w.name || '--', note: w.note || '',
            board: q.board, price: q.price, change_pct: q.change_pct, change: q.change,
            amount: q.amount, turnover_rate: q.turnover_rate, volume_ratio: q.volume_ratio,
            amplitude: q.amplitude, total_mv: q.total_mv, missing: !byCode[w.code]
          };
        });
        body.innerHTML = '<div class="card"><div class="card-head"><h3>自选股池</h3>' +
          '<div class="btn-row">' +
          '<button class="btn sm" id="watchAddBatch">批量添加</button>' +
          '<button class="btn sm" id="watchExport">导出 CSV</button>' +
          '</div></div>' +
          '<div class="ch-sub" style="margin-bottom:8px">点击行查看个股 · 「移除」仅从自选池删除</div>' +
          '<div id="watchTable"></div></div>';

        util.sortableTable(util.$('#watchTable'), [
          { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
          { label: '板块', key: 'board', cls: 'hide-sm' },
          { label: '现价', num: true, sort: function (i) { return i.price; } },
          { label: '涨跌幅', num: true, sort: function (i) { return i.change_pct; } },
          { label: '涨跌额', num: true, sort: function (i) { return i.change; } },
          { label: '成交额', num: true, sort: function (i) { return i.amount; } },
          //: 以下按窄屏收起（class 由 util.sortableTable 应用到 th/td）
          { label: '换手率', num: true, cls: 'hide-narrow', sort: function (i) { return i.turnover_rate; } },
          { label: '量比', num: true, cls: 'hide-narrow', sort: function (i) { return i.volume_ratio; } },
          { label: '振幅', num: true, cls: 'hide-narrow', sort: function (i) { return i.amplitude; } },
          { label: '总市值', num: true, cls: 'hide-narrow', sort: function (i) { return i.total_mv; } },
          { label: '备注', key: 'note', cls: 'hide-narrow' },
          { label: '', cls: 'hide-narrow' }
        ], rows, function (item) {
          var dash = '<span class="faint">--</span>';
          return [
            '<span class="mono">' + util.esc(item.code) + '</span>',
            util.esc(item.name),
            '<span class="faint small">' + util.esc(item.board || '') + '</span>',
            item.missing ? dash : util.num(item.price, 2),
            item.missing ? dash : util.pct(item.change_pct),
            item.missing ? dash : util.num(item.change, 2, { colored: true, sign: true }),
            item.missing ? dash : util.money(item.amount),
            item.missing ? dash : util.fixed(item.turnover_rate, 2) + '%',
            item.missing ? dash : util.fixed(item.volume_ratio, 2),
            item.missing ? dash : util.fixed(item.amplitude, 2) + '%',
            item.missing ? dash : util.money(item.total_mv),
            '<span class="small muted">' + util.esc(item.note) + '</span>',
            '<button class="btn sm" data-unwatch="' + util.esc(item.code) + '">移除</button>'
          ];
        }, { initialSort: 4, initialAsc: false, onRowClick: openStock });
      }

      function paintPortfolio(body, portfolio) {
        var open = portfolio.items.filter(function (i) { return i.status === 'open'; });
        var closed = portfolio.items.filter(function (i) { return i.status === 'closed'; });

        var html = '<div class="card"><div class="card-head"><h3>模拟持仓（' + open.length + '）</h3>' +
          '<span class="ch-sub">本地记录，不涉及任何真实交易；浮动盈亏按最新价计算</span></div>';
        if (!open.length) {
          html += util.emptyState('暂无持仓', '在「个股详情」页点「记入模拟持仓」');
        } else {
          html += '<div id="posTable"></div>';
        }
        html += '</div>';

        html += '<div class="card"><div class="card-head"><h3>已平仓（' + closed.length + '）</h3>' +
          '<span class="ch-sub">胜率 ' + util.fixed((portfolio.win_rate || 0) * 100, 1) +
          '% · 累计收益 ' + util.pct(portfolio.total_pnl_pct) +
          ' · 平均 ' + util.pct(portfolio.avg_pnl_pct) + '</span></div>';
        if (!closed.length) {
          html += util.emptyState('还没有平仓记录');
        } else {
          html += '<div id="closedTable"></div>';
        }
        html += '</div>';
        body.innerHTML = html;

        if (open.length) {
          util.sortableTable(util.$('#posTable'), [
            { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
            { label: '成本', num: true, sort: function (i) { return i.price; } },
            { label: '现价', num: true, sort: function (i) { return i.current; } },
            { label: '份额', num: true, sort: function (i) { return i.shares; } },
            { label: '成本额', num: true, sort: function (i) { return i.cost_value; } },
            { label: '市值', num: true, sort: function (i) { return i.market_value; } },
            { label: '浮动盈亏', num: true, sort: function (i) { return i.pnl_amount; } },
            { label: '收益率', num: true, sort: function (i) { return i.pnl_pct; } },
            { label: '开仓时间', key: 'opened_at_text' },
            { label: '理由' },
            { label: '' }
          ], open, function (item) {
            return [
              '<span class="mono">' + util.esc(item.code) + '</span>',
              util.esc(item.name),
              util.num(item.price, 2),
              item.hasLive ? util.num(item.current, 2)
                : util.num(item.current, 2) + ' <span class="faint small">成本价</span>',
              util.num(item.shares, 0),
              util.money(item.cost_value),
              util.money(item.market_value),
              moneySigned(item.pnl_amount),
              util.pct(item.pnl_pct),
              '<span class="small muted">' + util.esc(item.opened_at_text) + '</span>',
              '<span class="small muted">' + util.esc(item.reason || '--') + '</span>',
              '<button class="btn sm" data-close-pos="' + item.id + '">平仓</button>'
            ];
          }, { initialSort: 8, initialAsc: false });
        }

        if (closed.length) {
          util.sortableTable(util.$('#closedTable'), [
            { label: '代码', key: 'code' }, { label: '名称', key: 'name' },
            { label: '成本', num: true, sort: function (i) { return i.price; } },
            { label: '平仓价', num: true, sort: function (i) { return i.current; } },
            { label: '份额', num: true, sort: function (i) { return i.shares; } },
            { label: '盈亏额', num: true, sort: function (i) { return i.pnl_amount; } },
            { label: '收益率', num: true, sort: function (i) { return i.pnl_pct; } },
            { label: '平仓时间', key: 'closed_at_text' },
            { label: '' }
          ], closed, function (item) {
            return [
              '<span class="mono">' + util.esc(item.code) + '</span>',
              util.esc(item.name),
              util.num(item.price, 2),
              util.num(item.current, 2),
              util.num(item.shares, 0),
              moneySigned(item.pnl_amount),
              util.pct(item.pnl_pct),
              '<span class="small muted">' + util.esc(item.closed_at_text) + '</span>',
              '<button class="btn sm danger" data-del-pos="' + item.id + '">删除</button>'
            ];
          }, { initialSort: 6, initialAsc: false });
        }
      }

      // ------------------------------------------------------------ 交互
      function openStock(item) {
        if (item && item.code) SS.app.navigate('#/stock?code=' + item.code);
      }

      function batchDialog() {
        util.modal('批量添加自选',
          '<div class="field"><label>证券代码（逗号/空格/换行分隔）</label>' +
          '<textarea id="watchCodes" rows="4" placeholder="600519, 000001, 300750"></textarea></div>' +
          '<div class="btn-row"><button class="btn primary" id="watchCodesOk">添加</button></div>');
        var ok = util.$('#watchCodesOk');
        if (!ok) return;
        ok.addEventListener('click', function () {
          var raw = (util.$('#watchCodes') || {}).value || '';
          var codes = raw.split(/[\s,，;；]+/).filter(Boolean);
          if (!codes.length) { util.toast('请先填写代码', 'error'); return; }
          api.addWatchBatch(codes).then(function (data) {
            util.closeModal();
            util.toast('已添加 ' + ((data && data.added) || codes.length) + ' 只到自选', 'ok');
            return paint();
          }).catch(function (e) { util.toast('添加失败: ' + e.message, 'error', 6000); });
        });
      }

      function closeDialog(id) {
        util.modal('平仓', '<div class="field"><label>平仓价</label>' +
          '<input type="number" step="0.01" id="closePrice" placeholder="输入平仓价"></div>' +
          '<div class="field"><label>离场原因</label>' +
          '<input type="text" id="closeReason" placeholder="例如：破位止损"></div>',
          [
            util.el('button', {
              class: 'btn primary', text: '确认平仓', onclick: function () {
                var price = Number((util.$('#closePrice') || {}).value || 0);
                if (!(price > 0)) { util.toast('请填写有效的平仓价', 'error'); return; }
                api.closePosition(id, {
                  price: price,
                  reason: (util.$('#closeReason') || {}).value || ''
                }).then(function () {
                  util.closeModal();
                  util.toast('已平仓', 'ok');
                  return paint();
                }).catch(function (e) { util.toast('平仓失败: ' + e.message, 'error'); });
              }
            }),
            util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal })
          ]);
      }

      function bind() {
        content.addEventListener('click', function (event) {
          var closest = event.target.closest ? event.target.closest.bind(event.target) : null;
          if (!closest) return;

          var tab = closest('[data-wtab]');
          if (tab) {
            state.tab = tab.getAttribute('data-wtab');
            util.$$('[data-wtab]').forEach(function (n) {
              n.classList.toggle('active', n.getAttribute('data-wtab') === state.tab);
            });
            return paint();
          }

          var unwatch = closest('[data-unwatch]');
          if (unwatch) {
            var code = unwatch.getAttribute('data-unwatch');
            unwatch.disabled = true;
            api.removeWatch([code]).then(function () {
              util.toast('已从自选移除 ' + code, 'ok');
              return paint();
            }).catch(function (e) {
              util.toast('移除失败: ' + e.message, 'error');
              unwatch.disabled = false;
            });
            return;
          }

          var closePos = closest('[data-close-pos]');
          if (closePos) { closeDialog(Number(closePos.getAttribute('data-close-pos'))); return; }

          var delPos = closest('[data-del-pos]');
          if (delPos) {
            var pid = Number(delPos.getAttribute('data-del-pos'));
            util.confirmDialog('删除持仓记录', '确认删除这条模拟持仓记录？').then(function (yes) {
              if (!yes) return;
              api.deletePosition(pid).then(function () {
                util.toast('已删除', 'ok');
                return paint();
              }).catch(function (e) { util.toast(e.message, 'error'); });
            });
            return;
          }

          var id = event.target.id;
          if (id === 'watchReload') { paint(); return; }
          if (id === 'watchAddBatch') { batchDialog(); return; }
          if (id === 'watchExport') { window.location.href = api.apiUrl('api/export/watchlist.csv'); return; }
          if (id === 'watchGoScreener') { SS.app.navigate('#/screener'); return; }
        });
      }

      content.innerHTML = shell();
      bind();
      ctx.setRefresh(function () { return paint(); });
      return paint();
    }
  };
})(window);
