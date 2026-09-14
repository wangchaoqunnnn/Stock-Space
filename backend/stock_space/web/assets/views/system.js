/* ============================================================================
   views/system.js —— 系统状态
   应用信息 / 调度器与任务 / 数据库 / 数据源统计 / 原始项目落地对照 / 接口清单
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api, charts = SS.charts;

  function render(content, ctx) {
    var state = { overview: null };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>系统状态</h1>' +
        '<div class="ph-sub" id="sysSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="sysCleanup">清理过期数据</button>' +
        '<button class="btn" id="sysVacuum">整理数据库</button>' +
        '<button class="btn" id="sysJobs">任务日志</button>' +
        '<button class="btn primary" id="sysReload">刷新</button>' +
        '</div></div>' +
        '<div id="sysTop"></div>' +
        '<div class="card"><div class="card-head"><h3>定时任务</h3>' +
        '<span class="ch-sub">每个任务独立容错，单个失败不影响其它任务；可手动触发</span></div>' +
        '<div id="sysScheduler"></div></div>' +
        '<div class="grid cols-2">' +
        '<div class="card"><div class="card-head"><h3>数据库</h3></div><div id="sysDb"></div></div>' +
        '<div class="card"><div class="card-head"><h3>K 线缓存覆盖</h3></div><div id="sysKline"></div></div>' +
        '</div>' +
        '<div class="card"><div class="card-head"><h3>原始项目落地对照</h3>' +
        '<span class="ch-sub">11 个项目的能力如何映射到本平台（可追溯）</span></div>' +
        '<div id="sysIntegration"></div></div>' +
        '<div class="card"><div class="card-head"><h3>接口清单</h3>' +
        '<span class="ch-sub">共 <span id="sysApiCount">--</span> 个接口</span>' +
        '<a class="btn sm" href="docs" target="_blank" rel="noopener">打开 OpenAPI 文档</a></div>' +
        '<div id="sysApi"></div></div>' +
        '<div class="card"><div class="card-head"><h3>部署与运维速查</h3></div>' +
        '<div id="sysOps"></div></div>';
    }

    function load() {
      return Promise.all([
        api.systemOverview(),
        api.integration(),
        api.request('openapi.json', { raw: true }).catch(function () { return null; })
      ]).then(function (results) {
        state.overview = results[0];
        state.integration = results[1];
        state.openapi = results[2];
        paint();
      }).catch(function (error) {
        util.$('#sysTop').innerHTML = util.notice('bad', '系统信息不可用', util.esc(error.message));
      });
    }

    function paint() {
      var data = state.overview || {};
      var app = data.app || {};
      var memory = data.memory || {};
      var scheduler = data.scheduler || {};
      var db = data.database || {};
      var kline = data.kline || {};
      var sources = data.sources || {};

      var sub = util.$('#sysSub');
      if (sub) {
        sub.textContent = app.title + ' v' + app.version + ' · ' + util.esc(app.now || '') +
          ' · 交易日 ' + util.esc(app.trade_date || '') + ' · 时区 ' + util.esc(app.timezone || '');
      }

      var process = memory.process || {};
      var limits = memory.limits || {};
      util.$('#sysTop').innerHTML = '<div class="grid kpi">' +
        util.kpi('版本', util.esc(app.version || '--'), util.esc(app.source_mode || '') + ' 模式') +
        util.kpi('进程 RSS', util.fixed(process.rss_mb, 1) + ' MB',
          '峰值 ' + util.fixed(process.peak_rss_mb, 1) + ' MB（' +
          util.fixed(limits.soft_usage_pct, 0) + '% 软上限）') +
        util.kpi('调度器', scheduler.running ? '运行中' : '未运行',
          '循环 ' + util.count(scheduler.loop_count) + ' 次 · 间隔 ' +
          util.fixed(scheduler.interval_seconds, 0) + 's', scheduler.running ? 'up' : 'down') +
        util.kpi('已注册数据源', util.count(sources.providers),
          '锁定 ' + Object.keys(sources.locks || {}).length + ' · 禁用 ' +
          (sources.disabled || []).length) +
        util.kpi('数据库行数', util.count((db.rows || {}).kline_daily) + ' 根日线',
          '文件 ' + util.money(db.size_bytes || 0)) +
        util.kpi('K线覆盖', util.count(kline.coverage ? kline.coverage.codes : 0) + ' 只',
          '最新 ' + util.esc((kline.coverage || {}).last_date || '--')) +
        util.kpi('运行时长', util.secs(scheduler.uptime_seconds),
          util.esc((scheduler.session || {}).label || '')) +
        util.kpi('当前时段', util.esc((scheduler.session || {}).label || '--'),
          (scheduler.session || {}).should_poll ? '前端应轮询' : '无需轮询') +
        '</div>' +
        util.notice('info', '免责声明', util.esc(app.disclaimer || ''));

      var jobs = scheduler.jobs || [];
      util.$('#sysScheduler').innerHTML =
        '<div class="kv-list" style="margin-bottom:10px">' +
        '<div class="kv"><span class="k">下次推送时刻</span><span class="v">' +
        util.esc(scheduler.push_schedules || '未配置') + '</span></div>' +
        '<div class="kv"><span class="k">今日已推送时段</span><span class="v">' +
        util.esc((scheduler.pushed_today || []).join(', ') || '无') + '</span></div>' +
        '<div class="kv"><span class="k">今日已完成任务</span><span class="v">' +
        util.esc((scheduler.daily_done || []).join(', ') || '无') + '</span></div>' +
        '</div>' +
        '<div class="table-wrap"><table class="grid"><thead><tr>' +
        '<th>任务</th><th class="n">运行次数</th><th class="n">失败</th>' +
        '<th class="n">最近耗时</th><th>最近执行</th><th>最近结果</th><th></th>' +
        '</tr></thead><tbody>' +
        (jobs.length ? jobs.map(function (job) {
          return '<tr><td><strong>' + util.esc(job.name) + '</strong></td>' +
            '<td class="n">' + util.count(job.runs) + '</td>' +
            '<td class="n">' + (job.failures ? '<span class="bad">' + job.failures + '</span>' : '0') + '</td>' +
            '<td class="n">' + util.fixed(job.last_duration_ms, 0) + ' ms</td>' +
            '<td class="small muted">' + (job.last_run_ago_seconds === null ? '--'
              : util.ago(job.last_run_ago_seconds)) + '</td>' +
            '<td class="small ' + (job.last_error ? 'bad' : 'muted') + '">' +
            util.esc(job.last_error || job.last_detail || '--') + '</td>' +
            '<td><button class="btn sm" data-job="' + util.esc(job.name) + '">执行</button></td></tr>';
        }).join('') : '<tr><td colspan="7" class="muted">调度器尚未产生任何任务记录</td></tr>') +
        '</tbody></table></div>';

      var rows = db.rows || {};
      util.$('#sysDb').innerHTML = util.kvList([
        ['数据库文件', util.esc(db.path_name || '--')],
        ['文件大小', util.money(db.size_bytes || 0)],
        ['日线行数', util.count(rows.kline_daily)],
        ['K线抓取记录', util.count(rows.kline_fetch_log)],
        ['扫描结果', util.count(rows.scan_result)],
        ['信号流水', util.count(rows.signal_log)],
        ['资讯', util.count(rows.news_item)],
        ['推送日志', util.count(rows.push_log)],
        ['任务日志', util.count(rows.job_log)],
        ['自选', util.count(rows.watchlist)],
        ['模拟持仓', util.count(rows.portfolio)]
      ]);

      var coverage = kline.coverage || {};
      util.$('#sysKline').innerHTML = '<div class="grid kpi">' +
        util.kpi('覆盖标的', util.count(coverage.codes), '有日线缓存') +
        util.kpi('日线根数', util.count(coverage.bars)) +
        util.kpi('最早日期', util.esc(coverage.first_date || '--')) +
        util.kpi('最新日期', util.esc(coverage.last_date || '--')) +
        '</div>' +
        '<h4 style="margin-top:10px">内存缓存命中</h4>' +
        util.kvList([
          ['内存命中', util.count((kline.counters || {}).memory_hits)],
          ['磁盘命中', util.count((kline.counters || {}).disk_hits)],
          ['写入行数', util.count((kline.counters || {}).writes)]
        ]) +
        '<p class="small muted" style="margin-top:8px">读取顺序：内存缓存 → 磁盘日线库 → 上游数据源。' +
        '同一交易日内同一只股票只会向网络请求一次。</p>';

      var items = (state.integration || {}).items || [];
      util.$('#sysIntegration').innerHTML = items.length
        ? '<div class="table-wrap"><table class="grid"><thead><tr>' +
          '<th style="width:200px">原始项目</th><th>贡献的能力</th><th>落地位置</th></tr></thead><tbody>' +
          items.map(function (item) {
            return '<tr><td><strong>' + util.esc(item.project) + '</strong></td>' +
              '<td>' + util.esc(item.contribution) + '</td>' +
              '<td class="small mono">' + (item.landed_in || []).map(util.esc).join('<br>') + '</td></tr>';
          }).join('') + '</tbody></table></div>'
        : util.emptyState('无对照表数据');

      paintApi();

      util.$('#sysOps').innerHTML = '<div class="small">' +
        '<p><strong>查看日志：</strong>直接部署时执行 <code>journalctl -u stock-space -f --no-pager</code>；' +
        'Docker 部署时执行 <code>docker compose logs -f --tail=200</code>。' +
        '程序同时按天写入 <code>logs/stock_space.log</code>。</p>' +
        '<p><strong>健康检查：</strong><code>api/health</code> 返回 <code>degraded</code> 数组，' +
        '云监控可在该数组非空时告警；<code>healthz</code> 是极简存活探针。</p>' +
        '<p><strong>备份：</strong>备份 <code>data/</code> 目录即可（含 SQLite、日线缓存与运行期设置）；' +
        '「用户配置」页也支持导出 JSON。</p>' +
        '<p><strong>升级：</strong>拉取代码后重启服务；数据库表结构自动迁移，' +
        '新增字段通过 <code>CREATE TABLE IF NOT EXISTS</code> 保证幂等。</p>' +
        '<p><strong>内存告警：</strong>软/硬上限触发次数可在「内存监控」页查看；' +
        '持续触发请调大阈值或缩小扫描范围。</p>' +
        '</div>';
    }

    function paintApi() {
      var spec = state.openapi || {};
      var paths = Object.keys(spec.paths || {});
      var countBox = util.$('#sysApiCount');
      if (countBox) countBox.textContent = paths.length;
      var box = util.$('#sysApi');
      if (!box) return;
      if (!paths.length) {
        box.innerHTML = util.emptyState('接口清单不可用', '可能已关闭 OpenAPI 文档（server.docs_enabled=false）');
        return;
      }
      var groups = {};
      paths.forEach(function (path) {
        var parts = path.split('/');
        var group = parts.slice(0, 3).join('/');
        groups[group] = groups[group] || [];
        Object.keys(spec.paths[path]).forEach(function (method) {
          groups[group].push({
            method: method.toUpperCase(), path: path,
            summary: (spec.paths[path][method] || {}).summary || ''
          });
        });
      });
      box.innerHTML = Object.keys(groups).sort().map(function (group) {
        return '<h4 style="margin-top:12px">' + util.esc(group) + '</h4>' +
          '<div class="table-wrap"><table class="grid"><thead><tr><th style="width:80px">方法</th>' +
          '<th>路径</th><th>说明</th></tr></thead><tbody>' +
          groups[group].map(function (row) {
            return '<tr><td>' + util.badge(row.method, row.method === 'GET' ? 'info' : 'warn') +
              '</td><td class="mono small">' + util.esc(row.path) + '</td>' +
              '<td class="small muted">' + util.esc(row.summary) + '</td></tr>';
          }).join('') + '</tbody></table></div>';
      }).join('');
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var node = event.target.closest ? event.target.closest('[data-job],button') : null;
        if (!node) return;
        var job = node.getAttribute && node.getAttribute('data-job');
        if (job) {
          node.disabled = true;
          util.toast('正在执行任务 ' + job + ' …', 'ok', 3000);
          api.runJob(job).then(function (data) {
            util.toast('任务 ' + job + '：' + (data.ok ? '成功' : '失败') +
              ' — ' + util.esc(data.detail || data.error || ''), data.ok ? 'ok' : 'error', 6000);
            return load();
          }).catch(function (e) { util.toast(e.message, 'error'); })
            .then(function () { node.disabled = false; });
          return;
        }
        switch (node.id) {
          case 'sysReload': load(); break;
          case 'sysCleanup':
            api.dbCleanup().then(function (data) {
              util.toast('清理完成：' + Object.keys(data).map(function (key) {
                return key + ' ' + data[key];
              }).join('，'), 'ok', 5000);
              return load();
            }).catch(function (e) { util.toast(e.message, 'error'); });
            break;
          case 'sysVacuum':
            util.toast('正在整理数据库…', 'ok', 3000);
            api.dbVacuum().then(function () {
              util.toast('数据库整理完成', 'ok');
              return load();
            }).catch(function (e) { util.toast(e.message, 'error'); });
            break;
          case 'sysJobs': showJobs(); break;
        }
      });
    }

    function showJobs() {
      api.jobs(80).then(function (data) {
        util.modal('任务日志',
          '<div class="table-wrap" style="max-height:60vh"><table class="grid"><thead><tr>' +
          '<th>时间</th><th>任务</th><th>状态</th><th class="n">耗时</th><th>详情</th>' +
          '</tr></thead><tbody>' +
          ((data.items || []).length ? data.items.map(function (job) {
            return '<tr><td class="small">' + util.esc(job.time) + '</td>' +
              '<td>' + util.esc(job.job) + '</td>' +
              '<td>' + (job.status === 'ok' ? util.badge('成功', 'ok') : util.badge('失败', 'bad')) + '</td>' +
              '<td class="n">' + util.fixed(job.duration_ms, 0) + ' ms</td>' +
              '<td class="small ' + (job.status === 'ok' ? 'muted' : 'bad') + '">' +
              util.esc(String(job.detail || '').slice(0, 120)) + '</td></tr>';
          }).join('') : '<tr><td colspan="5" class="muted">暂无任务日志</td></tr>') +
          '</tbody></table></div>',
          [util.el('button', { class: 'btn primary', text: '关闭', onclick: util.closeModal })]);
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(); });
    ctx.setInterval(function () { load(); }, 60000);
    return load();
  }

  SS.views.system = { title: '系统状态', render: render };
})(window);
