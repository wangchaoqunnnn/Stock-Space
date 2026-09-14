/* ============================================================================
   views/settings.js —— 用户配置页（需求 3）
   数据源与配额 / 定时与推送 / 资讯保留 / 企业微信 Webhook / 自选与模拟持仓 / 导入导出
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api;

  //: 配置项定义(分组 → 字段)，中文标签 + 说明 + 控件类型
  var SCHEMA = [
    {
      key: 'quotas', title: '数据与配额',
      desc: '控制扫描范围与内存占用。小内存机器建议把「覆盖标的数」设为有限值（如 1500）。',
      fields: [
        { path: 'quotas.universe_size', label: '覆盖标的数', type: 'number', unit: '只', hint: '0 = 全部 A 股（约 5400+）' },
        { path: 'quotas.scan_kline_budget', label: '单轮日线请求上限', type: 'number', unit: '次', hint: '保护上游、避免被封禁' },
        { path: 'quotas.kline_concurrency', label: '日线并发', type: 'number', unit: '', hint: '新浪反爬上限约 24' },
        { path: 'quotas.quote_concurrency', label: '行情并发', type: 'number', unit: '' },
        { path: 'quotas.memory_soft_limit_mb', label: '内存软上限', type: 'number', unit: 'MB', hint: '超过后压缩缓存到 50% 并 GC' },
        { path: 'quotas.memory_hard_limit_mb', label: '内存硬上限', type: 'number', unit: 'MB', hint: '超过后清空全部缓存' },
        { path: 'quotas.memory_interval_seconds', label: '内存采样间隔', type: 'number', unit: '秒' },
        { path: 'refresh.idle_ttl_seconds', label: '非交易时段缓存 TTL', type: 'number', unit: '秒' }
      ]
    },
    {
      key: 'scheduler', title: '定时任务',
      desc: '交易时段高频刷新，收盘后自动扫描与清理。修改后立即生效。',
      fields: [
        { path: 'scheduler.enabled', label: '启用调度器', type: 'boolean' },
        { path: 'scheduler.trading_interval_seconds', label: '交易时段间隔', type: 'number', unit: '秒' },
        { path: 'scheduler.idle_interval_seconds', label: '非交易时段间隔', type: 'number', unit: '秒' },
        { path: 'scheduler.daily_job_hour', label: '每日任务-小时', type: 'number', unit: '时', hint: '北京时间' },
        { path: 'scheduler.daily_job_minute', label: '每日任务-分钟', type: 'number', unit: '分' }
      ]
    },
    {
      key: 'push', title: '消息推送（企业微信）',
      desc: '推送内容为 markdown 消息，以 errcode=0 判定成功；失败会按指数退避重试并写入推送日志。',
      fields: [
        { path: 'push.enabled', label: '启用推送', type: 'boolean' },
        { path: 'push.wecom_webhook', label: '企业微信机器人 Webhook', type: 'secret',
          hint: '在企业微信群 → 添加群机器人 → 复制 Webhook 地址粘贴到此处' },
        { path: 'push.wecom_mentioned_mobile', label: '被 @ 的手机号', type: 'secret',
          hint: '可留空；填写后推送会 @ 该成员' },
        { path: 'push.schedules', label: '定时推送时刻', type: 'text', unit: 'HH:MM',
          hint: '逗号分隔，例如 09:00,11:35,15:05（北京时间）' },
        { path: 'push.min_signal_score', label: '信号推送最低评分', type: 'number', unit: '分' },
        { path: 'push.max_chars', label: '消息长度上限', type: 'number', unit: '字节', hint: '企业微信 markdown 上限约 4096 字节' },
        { path: 'push.retry', label: '失败重试次数', type: 'number', unit: '次' },
        { path: 'push.dedupe_window_seconds', label: '内容去重窗口', type: 'number', unit: '秒' },
        { path: 'push.events.strategy_signal', label: '策略信号提醒', type: 'boolean' },
        { path: 'push.events.source_down', label: '数据源异常提醒', type: 'boolean' },
        { path: 'push.events.memory_warning', label: '内存告警', type: 'boolean' }
      ]
    },
    {
      key: 'news', title: '资讯与保留期',
      fields: [
        { path: 'news.enabled', label: '启用资讯抓取', type: 'boolean' },
        { path: 'news.retention_days', label: '资讯保留天数', type: 'number', unit: '天', hint: '超期自动清理' },
        { path: 'news.poll_interval_seconds', label: '资讯轮询间隔', type: 'number', unit: '秒' }
      ]
    },
    {
      key: 'app', title: '应用',
      fields: [
        { path: 'app.public_base_url', label: '推送链接前缀', type: 'text',
          hint: '留空则按请求 Host 自动推导（推荐留空，避免写死域名）' },
        { path: 'server.cors_origins', label: '跨域白名单', type: 'list',
          hint: '留空 = 只允许同源；需要前后端分离时填写，如 http://localhost:5173' }
      ]
    }
  ];

  function getPath(obj, path) {
    var cursor = obj;
    var parts = String(path).split('.');
    for (var i = 0; i < parts.length; i++) {
      if (cursor === null || cursor === undefined) return undefined;
      cursor = cursor[parts[i]];
    }
    return cursor;
  }

  function render(content, ctx) {
    var state = { settings: null, section: 'quotas' };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>用户配置</h1>' +
        '<div class="ph-sub" id="setSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="setExport">导出配置</button>' +
        '<button class="btn" id="setImport">导入配置</button>' +
        '<button class="btn" id="setReset">恢复默认</button>' +
        '<button class="btn primary" id="setSave">保存全部</button>' +
        '</div></div>' +
        '<div id="setNotice"></div>' +
        '<div class="tabs" id="setTabs"></div>' +
        '<div id="setBody"></div>' +
        '<div class="card"><div class="card-head"><h3>数据源模式与登录凭据</h3>' +
        '<span class="ch-sub">需要登录的数据源请到「数据源」页面粘贴凭据</span></div>' +
        '<div id="setSources"></div></div>' +
        '<div class="card"><div class="card-head"><h3>自选与模拟持仓</h3>' +
        '<span class="ch-sub">本地保存，不涉及任何真实交易</span></div>' +
        '<div id="setUserData"></div></div>';
    }

    function load() {
      return Promise.all([api.settings(), api.watchlist(), api.portfolio()])
        .then(function (results) {
          state.settings = results[0];
          state.watchlist = results[1];
          state.portfolio = results[2];
          paint();
        })
        .catch(function (error) {
          util.$('#setBody').innerHTML = util.notice('bad', '设置不可用', util.esc(error.message));
        });
    }

    function paintTabs() {
      var box = util.$('#setTabs');
      if (!box) return;
      box.innerHTML = SCHEMA.map(function (group) {
        return '<button class="tab' + (group.key === state.section ? ' active' : '') +
          '" data-section="' + util.esc(group.key) + '">' + util.esc(group.title) + '</button>';
      }).join('');
    }

    function paint() {
      var settings = state.settings || {};
      var sub = util.$('#setSub');
      if (sub) {
        var runtime = settings.runtime || {};
        sub.textContent = '配置已保存到 ' + util.esc((settings.runtime_file || 'runtime_settings.json')) +
          ' · 推送' + (settings.push && settings.push.wecom_webhook_configured ? '已配置' : '未配置') +
          ' · 调度器' + ((runtime.scheduler || {}).running ? '运行中' : '未运行') +
          ' · ' + util.esc(runtime.now || '');
      }

      var notices = [];
      if (settings.push && settings.push.wecom_webhook_configured && !settings.push.enabled) {
        notices.push(util.notice('info', 'Webhook 已配置但推送开关未开启',
          '保存时请确认「启用推送」为开启状态，否则定时推送与告警不会发出。'));
      }
      if (!settings.push || !settings.push.wecom_webhook_configured) {
        notices.push(util.notice('warn', '尚未配置企业微信推送',
          '如需消息推送，请在「消息推送」分组填入群机器人 Webhook 地址，保存后点「发送测试推送」验证。'));
      }
      util.$('#setNotice').innerHTML = notices.join('');

      paintTabs();
      paintSection();
      paintSources();
      paintUserData();
    }

    function paintSection() {
      var settings = state.settings || {};
      var group = SCHEMA.filter(function (g) { return g.key === state.section; })[0];
      if (!group) return;
      var html = '<div class="card"><div class="card-head"><h2>' + util.esc(group.title) + '</h2></div>' +
        (group.desc ? '<p class="small muted">' + util.esc(group.desc) + '</p>' : '') +
        '<div class="form-grid">' + group.fields.map(function (field) {
          var value = getPath(settings, field.path);
          if (value === undefined && settings.defaults) {
            value = getPath(settings.defaults, field.path);
          }
          var id = 'f_' + field.path.replace(/\./g, '_');
          var control;
          if (field.type === 'boolean') {
            control = '<label class="switch"><input type="checkbox" id="' + id + '" data-path="' +
              util.esc(field.path) + '" data-type="boolean"' + (value ? ' checked' : '') + '> ' +
              util.esc(field.label) + '</label>';
            return '<div class="field">' + control +
              (field.hint ? '<span class="hint">' + util.esc(field.hint) + '</span>' : '') + '</div>';
          }
          if (field.type === 'list') {
            var listText = Array.isArray(value) ? value.join(', ') : (value || '');
            control = '<input type="text" id="' + id + '" data-path="' + util.esc(field.path) +
              '" data-type="list" value="' + util.esc(listText) + '" placeholder="逗号分隔">';
          } else if (field.type === 'secret') {
            control = '<input type="text" id="' + id + '" data-path="' + util.esc(field.path) +
              '" data-type="secret" value="' + util.esc(value || '') + '" placeholder="粘贴后保存（只回显掩码）" autocomplete="off">';
          } else if (field.type === 'number') {
            control = '<input type="number" step="any" id="' + id + '" data-path="' + util.esc(field.path) +
              '" data-type="number" value="' + util.esc(value === undefined || value === null ? '' : String(value)) + '">';
          } else {
            control = '<input type="text" id="' + id + '" data-path="' + util.esc(field.path) +
              '" data-type="text" value="' + util.esc(value === undefined || value === null ? '' : String(value)) + '">';
          }
          return '<div class="field"><label>' + util.esc(field.label) +
            (field.unit ? ' <span class="unit">' + util.esc(field.unit) + '</span>' : '') +
            '</label>' + control +
            (field.hint ? '<span class="hint">' + util.esc(field.hint) + '</span>' : '') + '</div>';
        }).join('') + '</div>';

      if (state.section === 'push') {
        html += '<div class="btn-row" style="margin-top:12px">' +
          '<button class="btn" id="pushTest">发送测试推送</button>' +
          '<button class="btn" id="pushMarket">立即推送市场情绪</button>' +
          '<button class="btn" id="pushLog">查看推送日志</button>' +
          '</div>';
      }
      html += '</div>';
      util.$('#setBody').innerHTML = html;
    }

    function collect() {
      var patch = {};
      util.$$('#setBody [data-path]').forEach(function (input) {
        var path = input.dataset.path;
        var type = input.dataset.type;
        var value;
        if (type === 'boolean') value = input.checked;
        else if (type === 'number') value = input.value === '' ? null : Number(input.value);
        else if (type === 'list') {
          value = input.value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
        } else value = input.value;
        if (type === 'secret' && /^\*+$/.test(String(value).replace(/[^*]/g, '')) && String(value).indexOf('*') >= 0) {
          return;   // 掩码值不覆盖真值
        }
        setPath(patch, path, value);
      });
      // 推送开关与 webhook 一起提交，避免"配了 webhook 但开关没开"
      if (state.section === 'push') {
        var enabled = util.$('#f_push_enabled');
        if (enabled) setPath(patch, 'push.enabled', enabled.checked);
      }
      return patch;
    }

    function setPath(target, path, value) {
      var parts = path.split('.');
      var cursor = target;
      for (var i = 0; i < parts.length - 1; i++) {
        if (typeof cursor[parts[i]] !== 'object' || cursor[parts[i]] === null) cursor[parts[i]] = {};
        cursor = cursor[parts[i]];
      }
      cursor[parts[parts.length - 1]] = value;
    }

    function save() {
      var patch = collect();
      if (!Object.keys(patch).length) { util.toast('没有需要保存的修改', 'error'); return; }
      api.saveSettings(patch).then(function (data) {
        var message = '已保存 ' + data.accepted.length + ' 项';
        if (data.rejected && data.rejected.length) message += '，忽略 ' + data.rejected.length + ' 项';
        if (data.restart_required && data.restart_required.length) {
          message += '；端口/鉴权等启动项需重启服务生效';
        }
        util.toast(message, 'ok', 5000);
        return load();
      }).catch(function (error) { util.toast('保存失败: ' + error.message, 'error', 6000); });
    }

    function paintSources() {
      var settings = state.settings || {};
      var ds = settings.data_sources || {};
      var mode = ds.mode || 'auto';
      util.$('#setSources').innerHTML = '<div class="kv-list">' +
        '<div class="kv"><span class="k">当前模式</span><span class="v">' +
        util.esc(mode === 'synthetic' ? '演示数据（合成）' : mode === 'real' ? '仅真实数据' : '自动') +
        '</span></div>' +
        '<div class="kv"><span class="k">已锁定的能力</span><span class="v">' +
        (Object.keys(ds.locked || {}).length
          ? util.esc(Object.keys(ds.locked).map(function (key) {
              return key + ' → ' + ds.locked[key];
            }).join('；'))
          : '无（全部自动择优）') + '</span></div>' +
        '<div class="kv"><span class="k">自定义上游地址</span><span class="v">' +
        (Object.keys(ds.custom_urls || {}).length
          ? util.esc(Object.keys(ds.custom_urls).join('、')) : '无（使用内置目录）') + '</span></div>' +
        '<div class="kv"><span class="k">已配置凭据的数据源</span><span class="v">' +
        ((ds.credential_providers || []).length
          ? util.esc(ds.credential_providers.join('、')) : '无') + '</span></div>' +
        '</div>' +
        '<div class="btn-row" style="margin-top:10px">' +
        '<a class="btn" href="#/datasources">前往「数据源」页面管理</a>' +
        '</div>';
    }

    function paintUserData() {
      var watchlist = (state.watchlist || {}).items || [];
      var portfolio = state.portfolio || {};
      var html = '<div class="grid cols-2">' +
        '<div><h4>自选（' + watchlist.length + '）</h4>' +
        (watchlist.length
          ? '<div class="table-wrap" style="max-height:260px"><table class="grid"><thead><tr>' +
            '<th>代码</th><th>名称</th><th>备注</th><th></th></tr></thead><tbody>' +
            watchlist.map(function (item) {
              return '<tr><td class="mono">' + util.esc(item.code) + '</td>' +
                '<td><a href="#/stock?code=' + util.esc(item.code) + '">' + util.esc(item.name || '--') +
                '</a></td><td class="small muted">' + util.esc(item.note || '') + '</td>' +
                '<td><button class="btn sm" data-unwatch="' + util.esc(item.code) + '">移除</button></td></tr>';
            }).join('') + '</tbody></table></div>'
          : util.emptyState('自选为空', '在个股详情页点「加入自选」')) +
        '<div class="btn-row" style="margin-top:8px">' +
        '<button class="btn sm" id="watchExport">导出自选 CSV</button>' +
        '<button class="btn sm" id="watchBatch">批量添加</button></div></div>' +
        '<div><h4>模拟持仓（持有 ' + (portfolio.open_count || 0) + ' / 已平 ' +
        (portfolio.closed_count || 0) + '）</h4>' +
        ((portfolio.items || []).length
          ? '<div class="table-wrap" style="max-height:260px"><table class="grid"><thead><tr>' +
            '<th>代码</th><th>买入价</th><th>状态</th><th class="n">收益</th><th></th></tr></thead><tbody>' +
            portfolio.items.map(function (item) {
              return '<tr><td class="mono">' + util.esc(item.code) + '</td>' +
                '<td class="n">' + util.num(item.price, 2) + '</td>' +
                '<td>' + (item.status === 'open' ? util.badge('持有', 'info') : util.badge('已平仓')) + '</td>' +
                '<td class="n">' + (item.pnl_pct === null || item.pnl_pct === undefined ? '--' : util.pct(item.pnl_pct)) + '</td>' +
                '<td>' + (item.status === 'open'
                  ? '<button class="btn sm" data-close-pos="' + item.id + '">平仓</button>'
                  : '<button class="btn sm danger" data-del-pos="' + item.id + '">删除</button>') +
                '</td></tr>';
            }).join('') + '</tbody></table></div>'
          : util.emptyState('暂无模拟持仓', '在个股详情页点「记入模拟持仓」')) +
        '</div></div>';
      util.$('#setUserData').innerHTML = html;
    }

    function pushTest() {
      var webhookInput = util.$('#f_push_wecom_webhook');
      var raw = webhookInput ? webhookInput.value.trim() : '';
      // 掩码值不传，直接用已保存的
      var webhook = (raw && raw.indexOf('*') < 0) ? raw : '';
      util.toast('正在发送测试推送…', 'ok', 2500);
      api.testPush(webhook).then(function (data) {
        util.toast('测试推送已发送（尝试 ' + (data.attempts || 1) + ' 次），请查看企业微信群', 'ok', 6000);
      }).catch(function (error) {
        util.toast('测试失败: ' + error.message, 'error', 8000);
      });
    }

    function pushMarket() {
      util.toast('正在推送市场情绪…', 'ok', 2500);
      api.pushMarket().then(function () {
        util.toast('已推送市场情绪摘要', 'ok');
      }).catch(function (error) { util.toast('推送失败: ' + error.message, 'error', 6000); });
    }

    function showPushLog() {
      api.pushLog(60).then(function (data) {
        var stats = data.stats || {};
        util.modal('推送日志',
          util.kvList([
            ['Webhook 配置', stats.configured ? '已配置' : '未配置'],
            ['推送开关', stats.enabled ? '开启' : '关闭'],
            ['累计推送', util.count(stats.total)],
            ['成功次数', util.count(stats.success)],
            ['最近一次', stats.last_at ? util.timeText(stats.last_at) : '--'],
            ['最近结果', stats.last_ok === null ? '--' : (stats.last_ok ? '成功' : '失败') + ' ' +
              util.esc(stats.last_error || '')]
          ]) + '<div class="table-wrap" style="max-height:46vh;margin-top:10px">' +
          '<table class="grid"><thead><tr><th>时间</th><th>类型</th><th>标题</th>' +
          '<th>结果</th><th>错误</th></tr></thead><tbody>' +
          ((data.items || []).length ? data.items.map(function (item) {
            return '<tr><td class="small">' + util.esc(item.time) + '</td>' +
              '<td class="small">' + util.esc(item.kind) + '</td>' +
              '<td>' + util.esc(item.title) + '</td>' +
              '<td>' + (item.ok ? util.badge('成功', 'ok') : util.badge('失败', 'bad')) + '</td>' +
              '<td class="small bad">' + util.esc(String(item.error || '').slice(0, 80)) + '</td></tr>';
          }).join('') : '<tr><td colspan="5" class="muted">暂无推送记录</td></tr>') +
          '</tbody></table></div>',
          [util.el('button', { class: 'btn primary', text: '关闭', onclick: util.closeModal })]);
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var node = event.target.closest ? event.target.closest('[data-section],[data-unwatch],[data-close-pos],[data-del-pos],button') : null;
        if (!node) return;
        var section = node.getAttribute && node.getAttribute('data-section');
        if (section) { state.section = section; paintTabs(); paintSection(); return; }
        var unwatch = node.getAttribute && node.getAttribute('data-unwatch');
        if (unwatch) {
          api.removeWatch([unwatch]).then(function () {
            util.toast('已移除自选', 'ok');
            return load();
          }).catch(function (e) { util.toast(e.message, 'error'); });
          return;
        }
        var closePos = node.getAttribute && node.getAttribute('data-close-pos');
        if (closePos) { closePositionDialog(Number(closePos)); return; }
        var delPos = node.getAttribute && node.getAttribute('data-del-pos');
        if (delPos) {
          util.confirmDialog('删除持仓记录', '确认删除这条模拟持仓记录？').then(function (yes) {
            if (!yes) return;
            api.deletePosition(Number(delPos)).then(function () {
              util.toast('已删除', 'ok');
              return load();
            }).catch(function (e) { util.toast(e.message, 'error'); });
          });
          return;
        }
        switch (node.id) {
          case 'setSave': save(); break;
          case 'setExport': window.location.href = api.apiUrl('api/settings/export'); break;
          case 'setImport': importDialog(); break;
          case 'setReset':
            util.confirmDialog('恢复默认设置', '将恢复配额、推送、调度、资讯等默认值（自选与持仓不受影响）。确认？')
              .then(function (yes) {
                if (!yes) return;
                api.resetSettings('').then(function () {
                  util.toast('已恢复默认设置', 'ok');
                  return load();
                }).catch(function (e) { util.toast(e.message, 'error'); });
              });
            break;
          case 'pushTest': pushTest(); break;
          case 'pushMarket': pushMarket(); break;
          case 'pushLog': showPushLog(); break;
          case 'watchExport': window.location.href = api.apiUrl('api/export/watchlist.csv'); break;
          case 'watchBatch': batchWatchDialog(); break;
        }
      });
    }

    function closePositionDialog(id) {
      var body = util.modal('平仓', '<div class="field"><label>平仓价</label>' +
        '<input type="number" step="0.01" id="closePrice" placeholder="输入平仓价">' +
        '</div><div class="field"><label>离场原因</label>' +
        '<input type="text" id="closeReason" placeholder="例如：破位止损"></div>',
        [
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '确认平仓',
            onclick: function () {
              var price = Number(util.$('#closePrice').value);
              if (!price) { util.toast('请输入有效价格', 'error'); return; }
              api.closePosition(id, { price: price, reason: util.$('#closeReason').value })
                .then(function (data) {
                  util.closeModal();
                  util.toast('已平仓，收益 ' + util.fixed(data.pnl_pct, 2) + '%', 'ok');
                  return load();
                }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          })
        ]);
      return body;
    }

    function batchWatchDialog() {
      util.modal('批量添加自选',
        '<div class="field"><label>股票代码（每行一个或用逗号分隔）</label>' +
        '<textarea id="batchCodes" placeholder="600519&#10;300750,688981"></textarea></div>',
        [
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '添加',
            onclick: function () {
              var codes = util.$('#batchCodes').value.split(/[\s,，]+/).filter(Boolean);
              if (!codes.length) { util.toast('请输入代码', 'error'); return; }
              api.addWatchBatch(codes).then(function (data) {
                util.closeModal();
                util.toast('新增 ' + data.added.length + ' 只' +
                  (data.failed.length ? '，失败 ' + data.failed.length + ' 只' : ''), 'ok');
                return load();
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          })
        ]);
    }

    function importDialog() {
      util.modal('导入配置',
        '<div class="field"><label>粘贴配置文件内容（JSON）</label>' +
        '<textarea id="importText" placeholder=\'{"push": {"enabled": true}}\'></textarea></div>' +
        '<p class="small muted">导入走与保存相同的白名单校验，越权字段会被忽略。</p>',
        [
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '导入',
            onclick: function () {
              var text = util.$('#importText').value.trim();
              if (!text) { util.toast('请粘贴内容', 'error'); return; }
              var payload;
              try { payload = JSON.parse(text); }
              catch (e) { util.toast('JSON 解析失败: ' + e.message, 'error'); return; }
              api.post('api/settings/import', payload).then(function (data) {
                util.closeModal();
                util.toast('已导入 ' + data.accepted.length + ' 项', 'ok');
                return load();
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          })
        ]);
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(); });
    return load();
  }

  SS.views.settings = { title: '用户配置', render: render };
})(window);
