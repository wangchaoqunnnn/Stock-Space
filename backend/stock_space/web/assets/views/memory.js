/* ============================================================================
   views/memory.js —— 内存监控（需求 2）
   进程 RSS 趋势 / 系统内存 / 缓存明细 / 内存护栏动作 / 一键释放
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = { report: null, timer: 8000 };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>内存监控</h1>' +
        '<div class="ph-sub" id="memSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<label class="switch">采样间隔 ' +
        '<select id="memInterval" class="input" style="width:92px">' +
        '<option value="3000">3 秒</option><option value="8000" selected>8 秒</option>' +
        '<option value="20000">20 秒</option><option value="0">手动</option></select></label>' +
        '<button class="btn" id="memFlush">立即释放缓存</button>' +
        '<button class="btn primary" id="memReload">刷新</button>' +
        '</div></div>' +
        '<div id="memTop"></div>' +
        '<div class="card"><div class="card-head"><h3>进程内存趋势</h3>' +
        '<span class="ch-sub">进程 RSS（MB）· 由内存护栏定期采样</span></div>' +
        '<div class="chart-box"><canvas id="memChart"></canvas></div>' +
        '<div class="chart-legend">' +
        '<span><i style="background:' + charts.colors.accent + '"></i>进程 RSS</span>' +
        '<span class="faint">虚线为软上限与硬上限</span></div></div>' +
        '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>缓存明细</h3>' +
        '<span class="ch-sub">所有缓存都受容量与 TTL 双重约束</span></div>' +
        '<div id="memCaches"></div></div>' +
        '<div class="card"><div class="card-head"><h3>护栏状态</h3></div>' +
        '<div id="memGuard"></div></div>' +
        '</div>' +
        '<div class="card"><div class="card-head"><h3>说明</h3></div>' +
        '<div class="small muted">' +
        '<p><strong>为什么需要内存护栏：</strong>本平台要缓存全市场快照与数千只股票的日线，' +
        '如果是"只增不减"的缓存，长跑几天就会吃满内存被系统杀掉。</p>' +
        '<p><strong>三层约束：</strong>① 每个缓存都有明确的条目上限，超出按最久未使用淘汰；' +
        '② 条目带 TTL，读取与巡检时清理过期项；③ 进程 RSS 超过软上限时把所有缓存压缩到 50% 并触发 GC，' +
        '超过硬上限则清空全部缓存。</p>' +
        '<p><strong>阈值可调：</strong>在「用户配置」页修改 <code>memory_soft_limit_mb</code> 与 ' +
        '<code>memory_hard_limit_mb</code>；容器部署时会自动读取 cgroup 上限并收紧阈值，避免被 OOM Killer 杀掉。</p>' +
        '</div></div>';
    }

    function load() {
      return api.memory(240).then(function (data) {
        state.report = data;
        paint();
      }).catch(function (error) {
        util.$('#memTop').innerHTML = util.notice('bad', '内存数据不可用', util.esc(error.message));
      });
    }

    function paint() {
      var data = state.report || {};
      var process = data.process || {};
      var limits = data.limits || {};
      var guard = data.guard || {};
      var system = data.system || {};
      var caches = data.caches || {};

      var sub = util.$('#memSub');
      if (sub) {
        sub.textContent = '采样间隔 ' + util.fixed(guard.interval_seconds, 0) + ' 秒 · ' +
          (guard.running ? '护栏运行中' : '护栏未运行') +
          ' · 最近动作 ' + util.esc(guard.last_action || 'none');
      }

      var tone = limits.soft_usage_pct >= 100 ? 'down' : (limits.soft_usage_pct >= 70 ? 'warn' : 'up');
      util.$('#memTop').innerHTML = '<div class="grid kpi">' +
        util.kpi('当前 RSS', util.fixed(process.rss_mb, 1) + ' MB',
          'PID ' + process.pid + ' · 线程 ' + process.threads) +
        util.kpi('峰值 RSS', util.fixed(process.peak_rss_mb, 1) + ' MB',
          '趋势变化 ' + util.fixed(process.rss_delta_mb, 2) + ' MB') +
        util.kpi('软上限占用', util.fixed(limits.soft_usage_pct, 1) + '%',
          '阈值 ' + util.fixed(limits.soft_limit_mb, 0) + ' MB',
          limits.soft_usage_pct >= 100 ? 'down' : (limits.soft_usage_pct >= 70 ? 'warn' : '')) +
        util.kpi('硬上限占用', util.fixed(limits.hard_usage_pct, 1) + '%',
          '阈值 ' + util.fixed(limits.hard_limit_mb, 0) + ' MB') +
        util.kpi('缓存条目', util.count(caches.total_entries),
          (caches.items || []).length + ' 个缓存实例') +
        util.kpi('软/硬触发次数', util.count(guard.soft_breaches) + ' / ' + util.count(guard.hard_breaches),
          'GC 次数 ' + util.count(guard.gc_collections)) +
        util.kpi('整机内存占用', util.fixed(system.used_pct, 1) + '%',
          '可用 ' + util.fixed(system.available_mb, 0) + ' / ' + util.fixed(system.total_mb, 0) + ' MB') +
        util.kpi('容器内存上限', limits.container_limit_mb
            ? util.fixed(limits.container_limit_mb, 0) + ' MB' : '未检测到',
          limits.container_limit_mb ? '已自动收紧阈值' : '裸机或未限制') +
        '</div>' +
        (limits.soft_usage_pct >= 100
          ? util.notice('warn', '已触发内存护栏',
              '缓存已被自动压缩；如果持续触发说明阈值或扫描范围不合理，' +
              '建议到「用户配置」页调大内存阈值，或把 <code>quotas.universe_size</code> 设为有限值（如 1500）以缩小扫描范围。')
          : '');

      var canvas = util.$('#memChart');
      var trend = data.trend || [];
      if (canvas && trend.length) {
        var series = [{
          name: '进程 RSS', color: charts.themeColors().accent, fill: true,
          data: trend.map(function (point) { return { x: point.ts, y: point.rss_mb }; })
        }];
        if (limits.soft_limit_mb) {
          series.push({
            name: '软上限', color: charts.themeColors().gold, dash: [4, 4], width: 1,
            data: trend.map(function (point) { return { x: point.ts, y: limits.soft_limit_mb }; })
          });
        }
        if (limits.hard_limit_mb) {
          series.push({
            name: '硬上限', color: charts.themeColors().down, dash: [4, 4], width: 1,
            data: trend.map(function (point) { return { x: point.ts, y: limits.hard_limit_mb }; })
          });
        }
        charts.line(canvas, {
          series: series, height: 280, yMin: 0,
          xFormat: function (v) { return util.timeText(v).slice(6, 11); },
          yFormat: function (v) { return v.toFixed(0); }
        });
      } else if (canvas) {
        charts.empty(canvas, '暂无采样点，等待护栏采集');
      }

      var items = caches.items || [];
      util.$('#memCaches').innerHTML = items.length
        ? '<div class="table-wrap"><table class="grid"><thead><tr>' +
          '<th>缓存</th><th class="n">条目</th><th class="n">上限</th><th class="n">占用</th>' +
          '<th class="n">命中率</th><th class="n">TTL</th><th class="n">过期清理</th>' +
          '<th class="n">LRU 淘汰</th></tr></thead><tbody>' +
          items.map(function (item) {
            var cls = item.usage_pct >= 95 ? 'bad' : (item.usage_pct >= 75 ? 'warn' : '');
            return '<tr><td>' + util.esc(item.name) + '</td>' +
              '<td class="n">' + util.count(item.size) + '</td>' +
              '<td class="n">' + util.count(item.max_entries) + '</td>' +
              '<td class="n">' + util.progress(item.size, item.max_entries, cls) +
              ' <span class="small">' + util.fixed(item.usage_pct, 0) + '%</span></td>' +
              '<td class="n">' + util.fixed((item.hit_rate || 0) * 100, 1) + '%</td>' +
              '<td class="n">' + util.fixed(item.ttl_seconds, 0) + 's</td>' +
              '<td class="n">' + util.count(item.expired_purged) + '</td>' +
              '<td class="n">' + util.count(item.lru_evicted) + '</td></tr>';
          }).join('') + '</tbody></table></div>'
        : util.emptyState('暂无缓存实例');

      util.$('#memGuard').innerHTML = util.kvList([
        ['护栏运行', guard.running ? '<span class="ok">是</span>' : '<span class="bad">否</span>'],
        ['采样间隔', util.fixed(guard.interval_seconds, 0) + ' 秒'],
        ['最近动作', util.esc(guard.last_action || 'none')],
        ['最近动作时间', guard.last_action_ago_seconds === null ? '--' : util.ago(guard.last_action_ago_seconds)],
        ['软上限触发', util.count(guard.soft_breaches) + ' 次'],
        ['硬上限触发', util.count(guard.hard_breaches) + ' 次'],
        ['GC 次数', util.count(guard.gc_collections)],
        ['TTL 累计清理', util.count(guard.total_ttl_purged) + ' 条'],
        ['累计淘汰', util.count(guard.total_evicted) + ' 条'],
        ['整机内存数据来源', util.esc(system.source || '--')]
      ]);
    }

    function flush() {
      var btn = util.$('#memFlush');
      btn.disabled = true;
      api.memoryFlush().then(function (data) {
        util.toast('已释放 ' + data.freed_entries + ' 条缓存，GC 回收 ' + data.gc_collected +
          ' 个对象；RSS ' + data.rss_before_mb + ' → ' + data.rss_after_mb + ' MB', 'ok', 6000);
        return load();
      }).catch(function (error) {
        util.toast('释放失败: ' + error.message, 'error');
      }).then(function () { btn.disabled = false; });
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var target = event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        if (target.id === 'memFlush') flush();
        if (target.id === 'memReload') load();
      });
      var select = util.$('#memInterval');
      if (select) {
        select.addEventListener('change', function () {
          state.timer = Number(select.value) || 0;
          schedule();
        });
      }
    }

    function schedule() {
      ctx.setInterval(function () {
        if (state.timer > 0) load();
      }, state.timer || 8000);
    }

    content.innerHTML = shell();
    bind();
    schedule();
    ctx.setRefresh(function () { return load(); });
    return load();
  }

  SS.views.memory = { title: '内存监控', render: render };
})(window);
