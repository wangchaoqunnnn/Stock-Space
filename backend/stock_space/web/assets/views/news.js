/* ============================================================================
   views/news.js —— 资讯与公告
   快讯 / 公告 / 传闻 分频道浏览，重要消息高亮，关联个股一键跳转，可推送
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api;

  function render(content, ctx) {
    var state = { channel: '', limit: 80, items: [], filter: '' };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>资讯与公告</h1>' +
        '<div class="ph-sub" id="newsSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<input class="input" id="newsSearch" placeholder="标题关键词过滤" style="width:180px">' +
        '<button class="btn" id="newsExport">导出 CSV</button>' +
        '<button class="btn primary" id="newsReload">刷新</button>' +
        '</div></div>' +
        '<div class="tabs" id="newsTabs"></div>' +
        '<div id="newsBody"></div>';
    }

    function paintTabs() {
      var tabs = [
        { key: '', label: '全部' }, { key: 'flash', label: '快讯' },
        { key: 'announcement', label: '公告' }, { key: 'news', label: '资讯' },
        { key: 'rumor', label: '传闻（未经证实）' }
      ];
      var box = util.$('#newsTabs');
      if (!box) return;
      box.innerHTML = tabs.map(function (tab) {
        return '<button class="tab' + (tab.key === state.channel ? ' active' : '') +
          '" data-channel="' + util.esc(tab.key) + '">' + util.esc(tab.label) + '</button>';
      }).join('');
    }

    function load() {
      var body = util.$('#newsBody');
      body.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>抓取资讯…</p></div>';
      return api.news(state.limit, state.channel).then(function (data) {
        state.items = data.items || [];
        var sub = util.$('#newsSub');
        if (sub) {
          sub.textContent = state.items.length + ' 条 · ' + util.esc(data.as_of || '') +
            ((data.errors || []).length ? ' · 部分源失败: ' + data.errors.length + ' 个' : '');
        }
        paint(data);
      }).catch(function (error) {
        body.innerHTML = util.notice('bad', '资讯不可用', util.esc(error.message) +
          '<div class="small muted" style="margin-top:6px">可到「数据源」页面检查「财经快讯」「公司公告」两个能力是否可用。</div>');
      });
    }

    function paint(data) {
      var body = util.$('#newsBody');
      var items = state.items.filter(function (item) {
        if (!state.filter) return true;
        return String(item.title || '').toLowerCase().indexOf(state.filter.toLowerCase()) >= 0;
      });
      var html = '';
      if ((data.errors || []).length) {
        html += util.notice('warn', '部分资讯源不可用', data.errors.map(util.esc).join('<br>'));
      }
      if (data.hint) html += util.notice('info', '提示', util.esc(data.hint));
      if (!items.length) {
        body.innerHTML = html + util.emptyState('暂无资讯',
          '数据源不可用或尚未抓取；点「刷新」重试，或到「数据源」页面启用快讯/公告源。');
        return;
      }
      var important = items.filter(function (item) { return item.important; });
      if (important.length) {
        html += '<div class="card"><div class="card-head"><h3>重要消息</h3>' +
          '<span class="ch-sub">命中关键词（停复牌/立案/业绩预告/重组/政策等）</span></div>' +
          '<div class="table-wrap"><table class="grid"><tbody>' +
          important.slice(0, 12).map(function (item) {
            return '<tr><td style="width:70px">' + util.badge(SS.CHANNEL_LABELS[item.channel] || item.channel, 'warn') +
              '</td><td>' + titleHtml(item) + '</td>' +
              '<td class="small muted nowrap" style="width:130px">' + util.esc(item.source || '') + '</td></tr>';
          }).join('') + '</tbody></table></div></div>';
      }
      html += '<div class="card"><div class="card-head"><h3>资讯列表</h3>' +
        '<span class="ch-sub">共 ' + items.length + ' 条 · 点击关联代码查看个股</span></div>' +
        '<div class="table-wrap" style="max-height:70vh"><table class="grid"><thead><tr>' +
        '<th style="width:74px">频道</th><th style="min-width:280px">标题</th>' +
        '<th>关联个股</th><th style="width:110px">来源</th>' +
        '<th style="width:130px">发布时间</th></tr></thead><tbody>' +
        items.map(function (item) {
          return '<tr><td>' + util.badge(SS.CHANNEL_LABELS[item.channel] || item.channel,
              item.channel === 'rumor' ? 'warn' : (item.important ? 'up' : '')) + '</td>' +
            '<td>' + titleHtml(item) +
            (item.summary && item.summary !== item.title
              ? '<div class="small muted">' + util.esc(String(item.summary).slice(0, 160)) + '</div>' : '') +
            '</td>' +
            '<td>' + ((item.related_codes || []).length
              ? (item.related_codes || []).slice(0, 6).map(function (code) {
                  return '<a class="badge info" href="#/stock?code=' + util.esc(code) + '">' +
                    util.esc(code) + '</a>';
                }).join(' ')
              : '<span class="faint">--</span>') + '</td>' +
            '<td class="small muted">' + util.esc(item.source || '') + '</td>' +
            '<td class="small muted nowrap">' + util.esc(formatTime(item.published_at) || '--') + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table></div></div>';
      html += '<p class="small muted">资讯版权归原平台所有，本平台仅聚合标题与摘要并保留原文链接；' +
        '「传闻」频道内容未经证实，请自行判断。</p>';
      body.innerHTML = html;
    }

    function titleHtml(item) {
      var title = util.esc(item.title || '');
      if (item.url) {
        return '<a href="' + util.esc(item.url) + '" target="_blank" rel="noopener noreferrer">' +
          title + '</a>';
      }
      return title;
    }

    function formatTime(value) {
      var text = String(value || '');
      if (!text) return '';
      // 常见格式: 2026-09-14 09:30:00 / 20260914093000 / 时间戳
      var m = text.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
      if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
      if (/^\d{14}$/.test(text)) {
        return text.slice(0, 4) + '-' + text.slice(4, 6) + '-' + text.slice(6, 8) + ' ' +
          text.slice(8, 10) + ':' + text.slice(10, 12);
      }
      if (/^\d{10}$/.test(text)) return util.timeText(Number(text));
      return text.slice(0, 19);
    }

    function exportCsv() {
      if (!state.items.length) { util.toast('暂无可导出资讯', 'error'); return; }
      var rows = state.items.map(function (item) {
        return [formatTime(item.published_at), SS.CHANNEL_LABELS[item.channel] || item.channel,
          item.title, (item.related_codes || []).join(' '), item.source, item.url];
      });
      util.download('资讯_' + new Date().toISOString().slice(0, 10) + '.csv',
        util.toCsv(['时间', '频道', '标题', '关联个股', '来源', '链接'], rows));
      util.toast('已导出 CSV', 'ok');
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var tab = event.target.closest ? event.target.closest('[data-channel]') : null;
        if (tab) {
          state.channel = tab.dataset.channel;
          paintTabs();
          load();
          return;
        }
        var target = event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        if (target.id === 'newsReload') load();
        if (target.id === 'newsExport') exportCsv();
      });
      var search = util.$('#newsSearch');
      if (search) {
        search.addEventListener('input', util.debounce(function () {
          state.filter = search.value.trim();
          paint({ errors: [] });
        }, 250));
      }
    }

    content.innerHTML = shell();
    paintTabs();
    bind();
    ctx.setRefresh(function () { return load(); });
    ctx.setInterval(function () { load(); }, 120000);
    return load();
  }

  SS.views.news = { title: '资讯公告', render: render };
})(window);
