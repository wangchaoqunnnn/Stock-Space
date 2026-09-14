/* ============================================================================
   views/datasources.js —— 数据源可观测与人工干预
   需求 8 的落地界面：多源列表 / 健康度 / 手动刷新 / 手动切换(锁定) /
   需要登录的源支持手工粘贴凭据 / 手工输入上游地址(不写死任何固定地址)
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace;
  var util = SS.util, api = SS.api;

  function render(content, ctx) {
    var state = { data: null, capability: 'snapshot' };

    function shell() {
      return '<div class="page-head"><div class="ph-left"><h1>数据源</h1>' +
        '<div class="ph-sub" id="dsSub">加载中…</div></div>' +
        '<div class="page-actions">' +
        '<button class="btn" id="dsProbeAll">全部探测</button>' +
        '<button class="btn" id="dsResetBreakers">复位熔断</button>' +
        '<button class="btn primary" id="dsRefresh">手动刷新数据</button>' +
        '</div></div>' +
        '<div id="dsMode"></div>' +
        '<div class="card"><div class="card-head"><h3>数据源列表</h3>' +
        '<span class="ch-sub">健康度 = 成功率与近期滑窗的加权；变慢或变差的源会自动沉到候选列表末尾</span>' +
        '</div><div id="dsProviders"></div></div>' +
        '<div class="card"><div class="card-head"><h3>能力路由与手动切换</h3>' +
        '<span class="ch-sub">锁定某个能力到指定源；留空表示自动择优</span></div>' +
        '<div id="dsCapabilities"></div></div>' +
        '<div class="card"><div class="card-head"><h3>切换历史</h3>' +
        '<span class="ch-sub">每次真实发生的"主源→备源"都会记录原因</span></div>' +
        '<div id="dsSwitches"></div></div>';
    }

    function load() {
      return api.datasources().then(function (data) {
        state.data = data;
        paint();
      }).catch(function (error) {
        util.$('#dsProviders').innerHTML = util.notice('bad', '数据源信息不可用', util.esc(error.message));
      });
    }

    function paint() {
      var data = state.data || {};
      var sub = util.$('#dsSub');
      if (sub) {
        var blocked = Object.keys(data.blocked_hosts || {});
        sub.textContent = '模式 ' + (data.mode || '--') +
          ' · 已注册源 ' + ((data.providers || []).length) +
          ' · 能力 ' + Object.keys(data.categories || {}).length +
          (blocked.length ? ' · 限流冷却中主机 ' + blocked.length + ' 个' : '');
      }

      // 模式切换
      var mode = data.mode || 'auto';
      var modeBox = util.$('#dsMode');
      if (modeBox) {
        modeBox.innerHTML = '<div class="card"><div class="card-head"><h3>数据源模式</h3></div>' +
          '<div class="btn-row">' +
          [['auto', '自动（推荐）', '按健康度依次尝试真实源；全部失败时明确报错'],
           ['real', '仅真实数据', '与自动相同，但绝不降级到合成数据'],
           ['synthetic', '演示数据', '只使用内置确定性合成行情，仅用于离线演示与测试']]
            .map(function (row) {
              return '<button class="btn' + (mode === row[0] ? ' primary' : '') +
                '" data-mode="' + row[0] + '" title="' + util.esc(row[2]) + '">' +
                util.esc(row[1]) + '</button>';
            }).join('') + '</div>' +
          (mode === 'synthetic'
            ? util.notice('warn', '当前为演示数据模式',
                '页面展示的是合成行情，<strong>不代表任何真实市场数据</strong>；' +
                '所有响应都会标注 source=synthetic。请勿用于任何实际决策。')
            : util.notice('info', '当前模式说明',
                mode === 'auto'
                  ? '按健康度依次尝试真实数据源，全部失败时返回明确的错误原因（不会用假数据顶替）。'
                  : '只使用真实数据源，任一能力全部失败即报错，便于及早暴露上游故障。')) +
          '</div>';
      }

      paintProviders(data.providers || []);
      paintCapabilities(data.categories || {}, data.capability_labels || {});
      paintSwitches(data.recent_switches || []);
    }

    function paintProviders(providers) {
      var box = util.$('#dsProviders');
      if (!box) return;
      if (!providers.length) { box.innerHTML = util.emptyState('尚无已注册数据源'); return; }
      box.innerHTML = '<div class="table-wrap"><table class="grid"><thead><tr>' +
        '<th>数据源</th><th>状态</th><th class="n">优先级</th><th class="n">尝试</th>' +
        '<th class="n">成功率</th><th class="n">平均延迟</th><th class="n">健康度</th>' +
        '<th>登录态</th><th>支持能力</th><th>最近错误</th><th></th>' +
        '</tr></thead><tbody>' + providers.map(function (p) {
          var metrics = p.metrics || {};
          var status = p.usable ? util.badge('可用', 'ok')
            : (p.installed === false ? util.badge('未安装', 'warn') : util.badge('不可用', 'bad'));
          if (p.disabled_manually) status = util.badge('已手动禁用', 'warn');
          return '<tr data-provider="' + util.esc(p.name) + '">' +
            '<td><strong>' + util.esc(p.label) + '</strong><div class="faint small">' +
            util.esc(p.name) + (p.homepage ? ' · <a href="' + util.esc(p.homepage) +
            '" target="_blank" rel="noopener noreferrer">官网</a>' : '') + '</div>' +
            (p.note ? '<div class="faint small">' + util.esc(p.note) + '</div>' : '') + '</td>' +
            '<td>' + status + '</td>' +
            '<td class="n">' + (p.priority || 0) + '</td>' +
            '<td class="n">' + util.count(metrics.attempts) + '</td>' +
            '<td class="n">' + util.fixed((metrics.success_rate || 0) * 100, 1) + '%</td>' +
            '<td class="n">' + util.fixed(metrics.avg_latency_ms, 0) + ' ms</td>' +
            '<td class="n">' + util.fixed(metrics.health_score, 1) + '</td>' +
            '<td>' + (p.requires_login
              ? (p.logged_in ? util.badge('已配置', 'ok') : util.badge('需配置', 'warn'))
              : '<span class="faint">不需要</span>') + '</td>' +
            '<td class="small muted">' + util.esc((p.capabilities || []).map(function (c) {
              return SS.CAP_LABELS[c] || c;
            }).join('、')) + '</td>' +
            '<td class="small bad">' + util.esc((metrics.last_error || '').slice(0, 70)) + '</td>' +
            '<td class="nowrap">' +
            '<button class="btn sm" data-probe="' + util.esc(p.name) + '">探测</button> ' +
            '<button class="btn sm" data-cred="' + util.esc(p.name) + '">凭据</button> ' +
            '<button class="btn sm" data-toggle="' + util.esc(p.name) + '" data-enabled="' +
            (p.disabled_manually ? '1' : '0') + '">' +
            (p.disabled_manually ? '启用' : '禁用') + '</button>' +
            '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }

    function paintCapabilities(categories, labels) {
      var box = util.$('#dsCapabilities');
      if (!box) return;
      var keys = Object.keys(categories);
      if (!keys.length) { box.innerHTML = util.emptyState('无能力信息'); return; }
      box.innerHTML = '<div class="tabs">' + keys.map(function (key) {
        var label = (labels[key] && labels[key].label) || SS.CAP_LABELS[key] || key;
        var info = categories[key];
        var cls = info.effective_order && info.effective_order.length ? '' : ' bad';
        return '<button class="tab' + (key === state.capability ? ' active' : '') +
          '" data-tab-cap="' + util.esc(key) + '">' + util.esc(label) +
          '<span class="' + cls + '">(' + ((info.effective_order || []).length) + ')</span></button>';
      }).join('') + '</div><div id="dsCapDetail"></div>';
      paintCapDetail(categories[state.capability], state.capability);
    }

    function paintCapDetail(info, capability) {
      var box = util.$('#dsCapDetail');
      if (!box) return;
      if (!info) { box.innerHTML = util.emptyState('该能力无信息'); return; }
      var providers = info.providers || [];
      var html = '<div class="btn-row" style="margin-bottom:8px">' +
        '<span class="small muted">配置顺序：' + util.esc((info.configured_order || []).join(' → ') || '未配置') + '</span>' +
        '<span class="small muted">生效顺序：' + util.esc((info.effective_order || []).join(' → ') || '无可用源') + '</span>' +
        (info.locked ? '<span class="badge warn">已锁定 ' + util.esc(info.locked) + '</span>' : '') +
        (info.has_fallback ? util.badge('有备用源', 'ok') : util.badge('无备用源', 'bad')) +
        '</div>';
      if (!providers.length) {
        html += util.notice('warn', '该能力当前没有可用数据源',
          '可能原因：相关库未安装（如 AKShare/Ashare）、需要登录凭据但尚未配置、' +
          '源被手动禁用，或全部处于熔断冷却中。');
      } else {
        html += '<div class="table-wrap"><table class="grid"><thead><tr>' +
          '<th class="col-no">#</th><th>数据源</th><th class="n">成功率</th>' +
          '<th class="n">平均延迟</th><th class="n">健康度</th><th>状态</th>' +
          '<th class="n">最近成功</th><th>最近错误</th><th></th></tr></thead><tbody>' +
          providers.map(function (p, index) {
            var stateText = p.cooling ? util.badge('熔断中 ' + util.fixed(p.cooling_remaining_seconds, 0) + 's', 'bad')
              : (p.active ? util.badge('当前生效', 'ok') : util.badge('备用'));
            return '<tr><td class="col-no">' + (index + 1) + '</td>' +
              '<td>' + util.esc(p.label || p.alias) + '<div class="faint small">' +
              util.esc(p.alias) + '</div></td>' +
              '<td class="n">' + util.fixed((p.success_rate || 0) * 100, 1) + '%</td>' +
              '<td class="n">' + util.fixed(p.avg_latency_ms, 0) + ' ms</td>' +
              '<td class="n">' + util.fixed(p.health_score, 1) + '</td>' +
              '<td>' + stateText + '</td>' +
              '<td class="n small muted">' + (p.last_ok_ago_seconds === null ? '--'
                : util.ago(p.last_ok_ago_seconds)) + '</td>' +
              '<td class="small bad">' + util.esc((p.last_error || '').slice(0, 60)) + '</td>' +
              '<td><button class="btn sm" data-lock="' + util.esc(p.alias) + '" data-cap="' +
              util.esc(capability) + '">锁定</button></td></tr>';
          }).join('') + '</tbody></table></div>';
      }
      html += '<div class="btn-row" style="margin-top:8px">' +
        '<button class="btn sm" data-cap="' + util.esc(capability) + '" data-unlock="1">恢复自动择优</button>' +
        '<button class="btn sm" data-endpoints="' + util.esc(capability) + '">查看/编辑该能力地址</button>' +
        '</div>';
      box.innerHTML = html;
    }

    function paintSwitches(switches) {
      var box = util.$('#dsSwitches');
      if (!box) return;
      if (!switches.length) { box.innerHTML = util.emptyState('暂无切换记录', '所有能力的首选源都工作正常'); return; }
      box.innerHTML = '<div class="table-wrap" style="max-height:280px"><table class="grid"><thead><tr>' +
        '<th>时间</th><th>能力</th><th>主源</th><th>切换至</th><th>原因</th></tr></thead><tbody>' +
        switches.map(function (s) {
          return '<tr><td class="small">' + util.esc(s.time) + '</td>' +
            '<td>' + util.esc(SS.CAP_LABELS[s.category] || s.category) + '</td>' +
            '<td>' + util.esc(s.from) + '</td><td>' + util.esc(s.to) + '</td>' +
            '<td class="small muted">' + util.esc(s.reason) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }

    /* ------------------------------------------------------- 交互动作 */
    function lock(capability, alias) {
      api.lockSource(capability, alias).then(function () {
        util.toast('已锁定 ' + (SS.CAP_LABELS[capability] || capability) + ' → ' + (alias || '自动'), 'ok');
        return load();
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function unlock(capability) {
      api.lockSource(capability, '').then(function () {
        util.toast('已恢复自动择优', 'ok');
        return load();
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function probeOne(name) {
      util.toast('正在探测 ' + name + ' …', 'ok', 2000);
      api.probeOne(name).then(function (data) {
        util.toast(data.label + '：' + (data.ok ? '连通正常' : '失败') + ' ' +
          (data.message || '') + '（' + util.fixed(data.latency_ms, 0) + ' ms）',
          data.ok ? 'ok' : 'error', 6000);
        return load();
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function probeAll() {
      var btn = util.$('#dsProbeAll');
      btn.disabled = true;
      btn.textContent = '探测中…';
      api.probeAll(state.capability).then(function (data) {
        var okCount = data.ok_count || 0;
        util.toast('探测完成：' + okCount + ' / ' + data.total + ' 个源连通正常', okCount ? 'ok' : 'error', 6000);
        var body = util.modal('连通性探测结果（' + (SS.CAP_LABELS[data.capability] || data.capability) + '）',
          '<div class="table-wrap"><table class="grid"><thead><tr><th>数据源</th><th>结果</th>' +
          '<th class="n">延迟</th><th>说明</th></tr></thead><tbody>' +
          (data.items || []).map(function (item) {
            return '<tr><td>' + util.esc(item.label || item.name) + '</td>' +
              '<td>' + (item.ok ? util.badge('正常', 'ok') : util.badge('失败', 'bad')) + '</td>' +
              '<td class="n">' + util.fixed(item.latency_ms, 0) + ' ms</td>' +
              '<td class="small muted">' + util.esc(item.message || '') + '</td></tr>';
          }).join('') + '</tbody></table></div>',
          [util.el('button', { class: 'btn primary', text: '关闭', onclick: util.closeModal })]);
      }).catch(function (error) {
        util.toast('探测失败: ' + error.message, 'error', 6000);
      }).then(function () {
        btn.disabled = false;
        btn.textContent = '全部探测';
      });
    }

    function switchMode(mode) {
      var confirmText = mode === 'synthetic'
        ? '切换到「演示数据」模式后，页面将展示合成行情，不代表真实市场。确认切换？'
        : '确认切换到「' + (mode === 'real' ? '仅真实数据' : '自动') + '」模式？';
      util.confirmDialog('切换数据源模式', confirmText).then(function (yes) {
        if (!yes) return;
        api.setSourceMode(mode).then(function () {
          util.toast('已切换数据源模式', 'ok');
          return SS.app.loadHealth();
        }).then(load).catch(function (error) { util.toast(error.message, 'error'); });
      });
    }

    function editCredentials(name) {
      Promise.all([api.credentials(name), api.datasources()]).then(function (results) {
        var info = results[0];
        var provider = (results[1].providers || []).filter(function (p) { return p.name === name; })[0] || {};
        var fields = Object.keys(info.fields || {});
        var known = fields.length ? fields : ['cookie'];
        var html = '<p class="small muted">' + util.esc(info.hint || '') + '</p>' +
          '<div class="form-grid">' + known.map(function (field) {
            return '<div class="field"><label>' + util.esc(field) +
              '</label><input type="password" data-cred-field="' + util.esc(field) +
              '" placeholder="' + util.esc(info.fields[field] || '粘贴完整内容') + '"></div>';
          }).join('') + '</div>' +
          '<div class="field" style="margin-top:8px"><label>新增/覆盖字段名' +
          ' <span class="unit">如 token、api_key</span></label>' +
          '<input type="text" id="credExtraName" placeholder="字段名"></div>' +
          '<div class="field"><label>字段值</label><input type="password" id="credExtraValue"></div>' +
          '<p class="small muted">凭据会保存到服务器本地 data/runtime_settings.json（权限 600），' +
          '读取接口只返回掩码，不会回显明文。保存后会自动做一次真实请求验证。</p>' +
          (provider.requires_login ? '' : '<p class="small warn">该数据源通常不需要登录凭据，' +
            '仅在你接入自建网关时才需要填写。</p>');

        var body = util.modal('登录凭据 · ' + name, html, [
          util.el('button', {
            class: 'btn danger', text: '清除凭据',
            onclick: function () {
              api.clearCredentials(name).then(function () {
                util.closeModal();
                util.toast('凭据已清除', 'ok');
                load();
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          }),
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn primary', text: '保存并验证',
            onclick: function () {
              var payload = {};
              util.$$('[data-cred-field]', body).forEach(function (input) {
                if (input.value.trim()) payload[input.dataset.credField] = input.value.trim();
              });
              var extraName = (util.$('#credExtraName').value || '').trim();
              var extraValue = (util.$('#credExtraValue').value || '').trim();
              if (extraName && extraValue) payload[extraName] = extraValue;
              if (!Object.keys(payload).length) { util.toast('请至少填写一个字段', 'error'); return; }
              api.saveCredentials(name, payload).then(function (data) {
                util.closeModal();
                var ok = data.verified && data.verified.ok;
                util.toast((ok ? '凭据有效：' : '已保存，但验证未通过：') +
                  util.esc((data.verified || {}).message || ''), ok ? 'ok' : 'error', 7000);
                load();
              }).catch(function (e) { util.toast('保存失败: ' + e.message, 'error', 6000); });
            }
          })
        ]);
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function editEndpoints(providerName, capability) {
      api.providerEndpoints(providerName).then(function (info) {
        var caps = capability ? [capability] : Object.keys(info.urls || {});
        var html = '<p class="small muted">' +
          '上游地址集中存放在 <code>config/sources.toml</code>，这里可以按能力整体替换（每行一个地址，' +
          '第一个失败会自动尝试下一个）。清空并保存可恢复内置地址。' +
          '平台代码中不写死任何域名，因此你可以把它指向自建网关或代理。</p>' +
          caps.map(function (cap) {
            var urls = (info.urls[cap] || []).join('\n');
            return '<div class="field" style="margin-bottom:10px"><label>' +
              util.esc(SS.CAP_LABELS[cap] || cap) + ' <span class="unit">' + util.esc(cap) +
              (info.customized ? '' : ' · 内置') + '</span></label>' +
              '<textarea data-ep-cap="' + util.esc(cap) + '" spellcheck="false">' +
              util.esc(urls) + '</textarea></div>';
          }).join('');
        var body = util.modal('上游地址 · ' + providerName, html, [
          util.el('button', { class: 'btn', text: '取消', onclick: util.closeModal }),
          util.el('button', {
            class: 'btn', text: '恢复内置地址',
            onclick: function () {
              var payload = {};
              caps.forEach(function (cap) { payload[cap] = []; });
              api.saveProviderEndpoints(providerName, payload).then(function () {
                util.closeModal();
                util.toast('已恢复内置地址', 'ok');
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          }),
          util.el('button', {
            class: 'btn primary', text: '保存地址',
            onclick: function () {
              var payload = {};
              util.$$('[data-ep-cap]', body).forEach(function (area) {
                payload[area.dataset.epCap] = area.value.split('\n')
                  .map(function (line) { return line.trim(); })
                  .filter(Boolean);
              });
              api.saveProviderEndpoints(providerName, payload).then(function (data) {
                util.closeModal();
                util.toast('已更新 ' + data.updated.length + ' 个能力的上游地址', 'ok');
                load();
              }).catch(function (e) { util.toast(e.message, 'error'); });
            }
          })
        ]);
      }).catch(function (error) { util.toast(error.message, 'error'); });
    }

    function manualRefresh() {
      var btn = util.$('#dsRefresh');
      btn.disabled = true;
      btn.textContent = '刷新中…';
      util.toast('正在清空缓存、复位熔断并重新拉取快照…', 'ok', 4000);
      api.refreshData('snapshot', true).then(function (data) {
        util.toast('已通过 ' + data.source + ' 刷新 ' + data.items + ' 条（缓存清理 ' +
          data.cleared_cache_entries + ' 条，复位熔断 ' + data.reset_breakers + ' 个）', 'ok', 6000);
        return SS.app.loadHealth();
      }).then(load).catch(function (error) {
        util.toast('刷新失败: ' + error.message, 'error', 7000);
      }).then(function () {
        btn.disabled = false;
        btn.textContent = '手动刷新数据';
      });
    }

    function bind() {
      content.addEventListener('click', function (event) {
        var node = event.target.closest ? event.target.closest(
          '[data-probe],[data-cred],[data-toggle],[data-lock],[data-unlock],[data-tab-cap],' +
          '[data-cap],[data-mode],[data-endpoints],#dsProbeAll,#dsResetBreakers,#dsRefresh'
        ) : null;
        if (!node) return;
        if (node.id === 'dsProbeAll') { probeAll(); return; }
        if (node.id === 'dsResetBreakers') {
          api.resetBreakers().then(function (data) {
            util.toast('已复位 ' + data.reset_breakers + ' 个熔断器，清理 ' +
              data.cleared_cache_entries + ' 条缓存', 'ok');
            load();
          }).catch(function (e) { util.toast(e.message, 'error'); });
          return;
        }
        if (node.id === 'dsRefresh') { manualRefresh(); return; }
        var probe = node.getAttribute('data-probe');
        if (probe) { probeOne(probe); return; }
        var cred = node.getAttribute('data-cred');
        if (cred) { editCredentials(cred); return; }
        var toggle = node.getAttribute('data-toggle');
        if (toggle) {
          var enabled = node.getAttribute('data-enabled') === '1';
          api.toggleSource(toggle, enabled).then(function () {
            util.toast(enabled ? '已启用 ' + toggle : '已禁用 ' + toggle, 'ok');
            load();
          }).catch(function (e) { util.toast(e.message, 'error'); });
          return;
        }
        var lockAlias = node.getAttribute('data-lock');
        if (lockAlias && node.getAttribute('data-cap')) {
          lock(node.getAttribute('data-cap'), lockAlias);
          return;
        }
        if (node.getAttribute('data-unlock')) {
          unlock(node.getAttribute('data-cap'));
          return;
        }
        var endpointsBtn = node.getAttribute('data-endpoints');
        if (endpointsBtn) {
          var info = (state.data.categories || {})[endpointsBtn] || {};
          var first = (info.effective_order || [])[0] || (info.configured_order || [])[0];
          if (!first) { util.toast('该能力尚未配置任何源的地址', 'error'); return; }
          editEndpoints(first, endpointsBtn);
          return;
        }
        var tabCap = node.getAttribute('data-tab-cap');
        if (tabCap) {
          state.capability = tabCap;
          paintCapabilities((state.data.categories || {}), (state.data.capability_labels || {}));
          return;
        }
        var mode = node.getAttribute('data-mode');
        if (mode) { switchMode(mode); return; }
      });
    }

    content.innerHTML = shell();
    bind();
    ctx.setRefresh(function () { return load(); });
    ctx.setInterval(function () { load(); }, 60000);
    return load();
  }

  SS.views.datasources = { title: '数据源', render: render };
})(window);
