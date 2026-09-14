/* ============================================================================
   views/emotion.js —— 情绪周期
   加权情绪分拆解 / 周期阶段与证据 / 龙头分层 / 板块涨停聚集 / 复盘报告
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = { reviewKind: 'cn_close', review: null };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>情绪周期</h1>' +
        '<div class="ph-sub" id="emoSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="emoPush">推送情绪到企业微信</button>' +
        '<button class="btn primary" id="emoReload">刷新</button>' +
        '</div></div>' +
        '<div id="emoTop"></div>' +
        '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>情绪分拆解</h3>' +
        '<span class="ch-sub">加权情绪分 = 各分项贡献之和</span></div><div id="emoParts"></div></div>' +
        '<div class="card"><div class="card-head"><h3>周期阶段判定</h3>' +
        '<span class="ch-sub">整数证据打分(约 -12 ~ +11)</span></div><div id="emoCycle"></div></div>' +
        '</div>' +
        '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>涨停板块聚集</h3></div><div id="emoSectors"></div></div>' +
        '<div class="card"><div class="card-head"><h3>龙头/高标分层</h3>' +
        '<span class="ch-sub">按连板高度归层</span></div><div id="emoLeaders"></div></div>' +
        '</div>' +
        //: 涨停池 20 行按默认行高需 740px。原先挤在这张卡的滚动区里(max-height:340px)，
        //: 实测藏了 400px 还得内部滚动。整表移到下方通栏后不再需要限制高度。
        '<div class="card"><div class="card-head"><h3>涨停梯队</h3>' +
        '<span class="ch-sub">涨停池全量明细 · 按连板高度排序 · 点击行查看个股</span></div>' +
        '<div id="emoLadderTable"></div></div>' +
        '<div class="card"><div class="card-head"><h3>行情复盘报告</h3>' +
        '<div class="btn-row">' +
        '<button class="btn sm" data-review="cn_close">A股收盘复盘</button>' +
        '<button class="btn sm" data-review="us_open">隔夜外围前瞻</button>' +
        '<button class="btn sm" id="reviewExport">导出 HTML</button>' +
        '</div></div><div id="emoReview"></div></div>';
    }

    function load() {
      var sub = util.$('#emoSub');
      return api.emotion().then(function (data) {
        var emotion = data.emotion || {};
        var cycle = data.cycle || {};
        var metrics = emotion.metrics || {};
        if (sub) {
          sub.textContent = '样本 ' + util.count(util.pick(data, 'market.sample_size')) + ' 只 · ' +
            util.esc(util.pick(data, 'market.updated_at', '')) +
            ' · 源 ' + util.esc(util.pick(data, 'market.source', '--')) +
            (data.sample_limited ? ' · 样本受限' : '');
        }

        util.$('#emoTop').innerHTML = '<div class="grid kpi">' +
          util.kpi('情绪分', util.fixed(emotion.score, 1), util.esc(emotion.level || '--'),
            emotion.score >= 60 ? 'up' : (emotion.score < 40 ? 'down' : '')) +
          util.kpi('周期阶段', util.esc(cycle.label || '--'),
            '置信度 ' + util.fixed((cycle.confidence || 0) * 100, 0) + '%') +
          util.kpi('涨停 / 跌停', util.count(metrics.limit_up) + ' / ' + util.count(metrics.limit_down),
            '炸板 ' + util.count(metrics.broken) + ' 家') +
          util.kpi('炸板率', util.fixed(metrics.broken_rate, 1) + '%',
            metrics.broken_rate >= 30 ? '偏高' : '健康', metrics.broken_rate >= 30 ? 'down' : 'up') +
          util.kpi('最高连板', util.count(metrics.max_consecutive) + ' 板',
            '竞价涨停 ' + util.count(metrics.auction_limit_up) + ' 家') +
          util.kpi('上涨占比', util.fixed((metrics.up_ratio || 0) * 100, 1) + '%',
            '涨 ' + util.count(metrics.up) + ' / 跌 ' + util.count(metrics.down)) +
          '</div>' +
          util.notice('info', '当前阶段操作建议', util.esc(cycle.advice || '--'));

        util.$('#emoParts').innerHTML = '<div class="bar-list">' + (emotion.parts || []).map(function (part) {
          var ratio = part.weight ? part.earned / part.weight : 0;
          var cls = ratio >= 0.7 ? 'up' : (ratio >= 0.4 ? '' : 'down');
          return '<div class="bar-row"><span class="bar-label" title="' + util.esc(part.threshold) + '">' +
            util.esc(part.name) + '</span>' +
            '<span class="bar-track"><i class="bar-fill" style="width:' + (ratio * 100).toFixed(1) +
            '%;background:var(--' + (cls || 'accent') + ')"></i></span>' +
            '<span class="bar-value">' + util.fixed(part.earned, 1) + ' / ' + util.fixed(part.weight, 0) +
            '</span></div>';
        }).join('') + '</div>' + '<p class="small muted" style="margin-top:8px">指标值：' +
          (emotion.parts || []).map(function (p) {
            return util.esc(p.name) + '=' + p.value;
          }).join(' · ') + '</p>';

        var evidence = cycle.evidence || [];
        var score = cycle.evidence_score || 0;
        util.$('#emoCycle').innerHTML =
          '<div class="gauge-wrap"><div class="gauge-text">' +
          '<div class="g-value ' + util.dirClass(score) + '">' + (score > 0 ? '+' : '') + score + '</div>' +
          '<div class="g-label">证据分 · 阶段 ' + util.esc(cycle.label || '--') + '</div></div>' +
          (cycle.changed ? '<span class="badge warn">阶段切换：' + util.esc(cycle.prev_phase || '--') +
            ' → ' + util.esc(cycle.phase) + '</span>' : '<span class="badge">阶段未切换</span>') +
          '</div>' +
          '<div class="table-wrap" style="margin-top:10px"><table class="grid"><thead><tr>' +
          '<th>证据</th><th class="n">实测值</th><th>判定阈值</th><th class="n">加减分</th></tr></thead><tbody>' +
          (evidence.length ? evidence.map(function (item) {
            var cls = item.delta > 0 ? 'up' : (item.delta < 0 ? 'down' : '');
            return '<tr><td>' + util.esc(item.name) + '</td><td class="n">' + util.esc(String(item.value)) +
              '</td><td class="small muted">' + util.esc(item.threshold) + '</td>' +
              '<td class="n ' + cls + '">' + (item.delta > 0 ? '+' : '') + item.delta + '</td></tr>';
          }).join('') : '<tr><td colspan="4" class="muted">暂无证据(数据不足)</td></tr>') +
          '</tbody></table></div>';

        util.$('#emoSectors').innerHTML = (data.sectors || []).length
          ? util.barList(data.sectors.map(function (item) {
              return {
                label: item.sector, value: item.limit_up,
                text: item.limit_up + ' 家 · 最高 ' + item.consecutive + ' 板',
                color: 'var(--up)'
              };
            }))
          : util.emptyState('暂无板块涨停聚集', '涨停池数据不可用时该面板为空');

        //: 窄栏只放"分层概览"（各层家数 + 最高连板），完整明细在下方通栏表。
        var tiers = {};
        (data.leaders || []).forEach(function (item) { tiers[item.tier] = (tiers[item.tier] || 0) + 1; });
        var tierMax = {};
        (data.leaders || []).forEach(function (item) {
          var c = item.consecutive || 1;
          if (!tierMax[item.tier] || c > tierMax[item.tier]) tierMax[item.tier] = c;
        });
        var tierRows = Object.keys(tiers).map(function (tier) {
          return { label: tier, value: tiers[tier],
                   text: tiers[tier] + ' 只 · 最高 ' + tierMax[tier] + ' 板',
                   color: tier === '龙头' ? 'var(--up)' : 'var(--accent)' };
        });
        util.$('#emoLeaders').innerHTML = (data.leaders || []).length
          ? util.barList(tierRows) +
            '<p class="small muted" style="margin-top:8px">共 ' + data.leaders.length +
            ' 只涨停样本；完整明细见下方「涨停梯队」。</p>'
          : util.emptyState('暂无龙头样本');

        util.$('#emoLadderTable').innerHTML = (data.leaders || []).length
          ? '<div class="table-wrap"><table class="grid compact"><thead><tr>' +
            '<th class="col-no">#</th><th>分层</th><th>名称</th><th class="n">连板</th>' +
            '<th class="n">涨跌幅</th><th class="n">成交额</th>' +
            '<th class="n hide-narrow">换手</th>' +
            '<th class="n hide-narrow">首封</th>' +
            '<th class="n hide-narrow">开板</th>' +
            '<th class="hide-narrow">行业</th></tr></thead><tbody>' +
            data.leaders.map(function (item, index) {
              return '<tr class="clickable" data-code="' + util.esc(item.code) + '">' +
                '<td class="col-no">' + (index + 1) + '</td>' +
                '<td><span class="badge' + (item.tier === '龙头' ? ' up' : '') + '">' + util.esc(item.tier) + '</span></td>' +
                '<td>' + util.esc(item.name) + ' <span class="faint small">' + util.esc(item.code) + '</span></td>' +
                '<td class="n">' + (item.consecutive || 1) + '</td>' +
                '<td class="n">' + util.pct(item.change_pct) + '</td>' +
                '<td class="n">' + util.money(item.amount) + '</td>' +
                '<td class="n hide-narrow">' + util.fixed(item.turnover_rate, 2) + '%</td>' +
                '<td class="n small muted hide-narrow">' + util.esc(item.first_limit_time || '--') + '</td>' +
                '<td class="n hide-narrow">' + util.count(item.open_times) + '</td>' +
                '<td class="small muted hide-narrow">' + util.esc(item.industry || '--') + '</td></tr>';
            }).join('') + '</tbody></table></div>'
          : util.emptyState('暂无涨停样本', '非交易日或涨停池数据源不可用');

        state.emotion = data;
        if (!state.review) return loadReview(state.reviewKind);
        return null;
      }).catch(function (error) {
        util.$('#emoTop').innerHTML = util.notice('bad', '情绪数据不可用',
          util.esc(error.message) + '<div class="small muted" style="margin-top:6px">' +
          '情绪计算依赖全市场快照与涨停池；若数据源暂时不可用，可到「数据源」页面切换源。</div>');
        return null;
      });
    }

    function loadReview(kind) {
      var box = util.$('#emoReview');
      if (!box) return Promise.resolve();
      box.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>正在生成复盘报告…</p></div>';
      return api.review(kind).then(function (data) {
        state.review = data;
        var html = '<div class="grid kpi">' +
          util.kpi('报告类型', util.esc(data.title || '--')) +
          util.kpi('赚钱效应', util.esc(data.rating || '--'), '由涨停/炸板/涨跌比综合') +
          util.kpi('行情阶段', util.esc(data.stage || '--')) +
          util.kpi('生成时间', util.esc(data.generated_at || '--')) +
          '</div>';
        html += util.notice(data.compliance && data.compliance.ok ? 'ok' : 'warn',
          '合规校验', data.compliance && data.compliance.ok
            ? '未出现荐股式表述，且包含风险提示'
            : (data.compliance && data.compliance.issues || []).join('；'));
        html += util.notice('info', '结论', util.esc(data.conclusion || '--'));
        if ((data.degraded_blocks || []).length) {
          html += util.notice('warn', '部分数据块降级',
            data.degraded_blocks.map(function (b) {
              return util.esc(b.block) + '：' + util.esc(b.reason);
            }).join('<br>'));
        }
        html += (data.modules || []).map(function (module) {
          var kindCls = module.status === 'ok' ? 'ok' : (module.status === 'degraded' ? 'warn' : 'bad');
          var kindText = module.status === 'ok' ? '数据完整'
            : (module.status === 'degraded' ? '数据不完整' : '数据源不可用');
          var body = '';
          if (module.bullets && module.bullets.length) {
            body += '<ul class="small" style="margin:6px 0 8px;padding-left:18px">' +
              module.bullets.map(function (line) { return '<li>' + util.esc(line) + '</li>'; }).join('') +
              '</ul>';
          }
          if (module.key === 'indices' && (module.data || {}).indices) {
            body += '<div class="table-wrap"><table class="grid"><thead><tr><th>指数</th>' +
              '<th class="n">点位</th><th class="n">涨跌幅</th><th class="n">成交额</th></tr></thead><tbody>' +
              module.data.indices.map(function (item) {
                return '<tr><td>' + util.esc(item.name) + '</td><td class="n">' + util.num(item.price, 2) +
                  '</td><td class="n">' + util.pct(item.change_pct) + '</td>' +
                  '<td class="n">' + util.money(item.amount) + '</td></tr>';
              }).join('') + '</tbody></table></div>';
          }
          if (module.key === 'sectors' && module.data) {
            var top = module.data.top || [];
            var bottom = module.data.bottom || [];
            body += '<div class="grid cols-2"><div><p class="small muted">涨幅前列</p>' +
              util.barList(top.map(function (row) {
                return { label: row.name, value: row.change_pct, text: util.pct(row.change_pct) };
              })) + '</div><div><p class="small muted">跌幅前列</p>' +
              util.barList(bottom.map(function (row) {
                return { label: row.name, value: row.change_pct, text: util.pct(row.change_pct) };
              })) + '</div></div>';
          }
          if (module.key === 'news' && module.data && module.data.items) {
            body += '<div class="table-wrap" style="max-height:280px"><table class="grid"><thead><tr>' +
              '<th>频道</th><th>标题</th><th>来源</th></tr></thead><tbody>' +
              module.data.items.map(function (item) {
                return '<tr><td>' + util.esc(SS.CHANNEL_LABELS[item.channel] || item.channel || '') +
                  '</td><td>' + (item.url ? '<a href="' + util.esc(item.url) +
                  '" target="_blank" rel="noopener noreferrer">' + util.esc(item.title) + '</a>'
                  : util.esc(item.title)) + '</td><td class="small muted">' +
                  util.esc(item.source || '') + '</td></tr>';
              }).join('') + '</tbody></table></div>';
          }
          return '<div class="card" style="background:var(--panel-2)"><div class="card-head">' +
            '<h3>' + util.esc(module.title) + '</h3>' +
            '<span class="badge ' + kindCls + '">' + kindText + '</span></div>' +
            (module.note ? '<p class="small muted">' + util.esc(module.note) + '</p>' : '') +
            body + '</div>';
        }).join('');
        html += '<p class="small muted">' + util.esc(data.risk_notice || '') + '</p>';
        box.innerHTML = html;
      }).catch(function (error) {
        box.innerHTML = util.notice('bad', '复盘报告生成失败', util.esc(error.message));
      });
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var row = event.target.closest ? event.target.closest('tr[data-code]') : null;
        if (row) { SS.app.navigate('#/stock?code=' + row.dataset.code); return; }
        var reviewBtn = event.target.closest ? event.target.closest('[data-review]') : null;
        if (reviewBtn) {
          state.reviewKind = reviewBtn.dataset.review;
          loadReview(state.reviewKind);
          return;
        }
        var exportBtn = event.target.closest ? event.target.closest('#reviewExport') : null;
        if (exportBtn) {
          exportReview();
          return;
        }
        var pushBtn = event.target.closest ? event.target.closest('#emoPush') : null;
        if (pushBtn) {
          pushBtn.disabled = true;
          api.pushMarket().then(function () {
            util.toast('已推送情绪摘要到企业微信', 'ok');
          }).catch(function (error) {
            util.toast('推送失败: ' + error.message, 'error', 6000);
          }).then(function () { pushBtn.disabled = false; });
          return;
        }
        var reload = event.target.closest ? event.target.closest('#emoReload') : null;
        if (reload) load();
      });
    }

    function exportReview() {
      if (!state.review) { util.toast('请先生成报告', 'error'); return; }
      var data = state.review;
      var html = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width, initial-scale=1">' +
        '<title>' + util.esc(data.title) + '</title><style>' +
        'body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;' +
        'max-width:900px;margin:0 auto;padding:20px;line-height:1.7;color:#222;background:#f5f7fa}' +
        'h1{font-size:20px}h2{font-size:16px;margin-top:20px;border-left:4px solid #2f6bd8;padding-left:8px}' +
        '.card{background:#fff;border-radius:10px;padding:14px 16px;margin:12px 0;box-shadow:0 2px 8px rgba(0,0,0,.05)}' +
        'table{width:100%;border-collapse:collapse;font-size:13px}' +
        'th,td{padding:6px 8px;border-bottom:1px solid #eee;text-align:left}' +
        '.muted{color:#888;font-size:12px}.up{color:#cf1322}.down{color:#389e0d}' +
        '</style></head><body>';
      html += '<h1>' + util.esc(data.title) + '</h1>';
      html += '<p class="muted">生成时间 ' + util.esc(data.generated_at) +
        '｜数据源 ' + util.esc(data.source || '--') + '</p>';
      html += '<div class="card"><strong>结论：</strong>' + util.esc(data.conclusion) + '</div>';
      (data.modules || []).forEach(function (module) {
        html += '<h2>' + util.esc(module.title) + '（' +
          (module.status === 'ok' ? '数据完整' : module.status === 'degraded' ? '不完整' : '不可用') + '）</h2>';
        if (module.note) html += '<p class="muted">' + util.esc(module.note) + '</p>';
        html += '<div class="card"><ul>';
        (module.bullets || []).forEach(function (line) { html += '<li>' + util.esc(line) + '</li>'; });
        html += '</ul></div>';
      });
      html += '<p class="muted">' + util.esc(data.risk_notice) + '</p></body></html>';
      util.download('复盘报告_' + (data.trade_date || '') + '.html', html, 'text/html;charset=utf-8');
      util.toast('报告已导出（可直接双击打开，无需联网）', 'ok');
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(); });
    ctx.setInterval(function () { load(); }, 90000);
    return load();
  }

  SS.views.emotion = { title: '情绪周期', render: render };
})(window);
