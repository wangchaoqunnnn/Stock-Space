/* ============================================================================
   app.js —— 应用外壳: 路由、导航、主题、会话时钟、全局搜索、刷新调度
   零构建: 所有视图脚本以 <script> 顺序加载并注册到 SS.views。
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace = global.StockSpace || {};
  var util = SS.util;
  var api = SS.api;

  SS.views = SS.views || {};

  var state = {
    route: '',
    params: {},
    session: null,
    clockTimer: null,
    pageTimer: null,
    refreshHandler: null,
    booted: false,
    health: null
  };

  /* ---------------------------------------------------------------- 主题 */
  function applyTheme(theme) {
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
      try { localStorage.setItem('ss_theme', theme); } catch (e) { /* 隐私模式 */ }
    }
    SS.charts && SS.charts.themeColors();
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('ss_theme'); } catch (e) { /* 忽略 */ }
    if (!saved) {
      saved = global.matchMedia && global.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light' : 'dark';
    }
    applyTheme(saved);
  }

  /* ---------------------------------------------------------------- 路由 */
  function parseHash() {
    var raw = String(global.location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('?');
    var path = parts[0] || 'dashboard';
    if (path === '' || path === '/') path = 'dashboard';
    var query = {};
    if (parts[1]) {
      parts[1].split('&').forEach(function (pair) {
        if (!pair) return;
        var kv = pair.split('=');
        query[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
      });
    }
    return { name: path.replace(/\/$/, ''), query: query };
  }

  function navigate(hash) {
    if (global.location.hash === hash) render();
    else global.location.hash = hash;
  }

  function setActiveNav(name) {
    util.$$('[data-nav]').forEach(function (node) {
      var target = node.getAttribute('data-nav');
      node.classList.toggle('active', target === name);
    });
  }

  function closeNav() {
    var nav = util.$('#sidenav');
    var backdrop = util.$('#navBackdrop');
    if (nav) nav.classList.remove('open');
    if (backdrop) backdrop.hidden = true;
    var toggle = util.$('#navToggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  }

  /* ---------------------------------------------------------------- 渲染 */
  function setLoading(on) {
    var bar = util.$('#loadBar');
    if (bar) bar.hidden = !on;
    var btn = util.$('#refreshBtn');
    if (btn) btn.disabled = !!on;
  }

  function render() {
    var parsed = parseHash();
    state.route = parsed.name;
    state.params = parsed.query;
    setActiveNav(parsed.name);
    closeNav();
    global.scrollTo({ top: 0, behavior: 'auto' });

    // 清理上一页的定时器
    if (state.pageTimer) { clearInterval(state.pageTimer); state.pageTimer = null; }
    state.refreshHandler = null;

    var view = SS.views[parsed.name] || SS.views.dashboard;
    if (!view) {
      util.$('#content').innerHTML = util.notice('bad', '页面不存在',
        '未找到路由 <code>#/' + util.esc(parsed.name) + '</code>，已跳转到仪表盘。');
      setTimeout(function () { navigate('#/dashboard'); }, 900);
      return;
    }
    if (parsed.name === 'stock' && !parsed.query.code) {
      parsed.query.code = '';
    }

    var content = util.$('#content');
    content.innerHTML = '<div class="boot-placeholder"><div class="spinner"></div><p>加载中…</p></div>';
    setLoading(true);

    var ctx = {
      params: parsed.query,
      navigate: navigate,
      setRefresh: function (fn) { state.refreshHandler = fn; },
      setInterval: function (fn, ms) {
        if (state.pageTimer) clearInterval(state.pageTimer);
        // ⚠️ 必须显式调用 window.setInterval：本函数的形参名与全局函数同名，
        // 直接写 setInterval(...) 会解析成本函数自身，造成无限递归。
        state.pageTimer = global.setInterval(function () {
          if (document.hidden) return;   // 后台标签不打扰
          if (state.session && state.session.should_poll === false && ms < 60000) return;
          try { fn(); } catch (e) { /* 单次失败不影响后续 */ }
        }, ms);
      },
      session: function () { return state.session; }
    };

    Promise.resolve()
      .then(function () { return view.render(content, ctx); })
      .catch(function (error) {
        content.innerHTML = util.notice('bad', '页面加载失败', util.esc(error.message || String(error))) +
          '<div class="card"><p class="muted small">排查建议：</p><ul class="small muted">' +
          '<li>确认后端服务已启动：访问 <code>api/health</code></li>' +
          '<li>首次启动会拉取全市场数据，可稍后点右上角刷新</li>' +
          '<li>到「数据源」页面查看各源健康度，必要时手动切换</li>' +
          '</ul></div>';
      })
      .then(function () { setLoading(false); });
  }

  /* ------------------------------------------------------- 会话时钟与状态 */
  function updateClock() {
    var session = state.session;
    var clockPill = util.$('#clockPill');
    var sessionPill = util.$('#sessionPill');
    if (clockPill) {
      var now = new Date();
      // 服务器时间为北京时间(UTC+8)时, 直接用本地显示会受浏览器时区影响,
      // 因此这里以服务器下发的 now_cn 为基准做本地推进。
      var base = state.serverTimeMs || now.getTime();
      var drift = Date.now() - (state.syncedAt || Date.now());
      var cn = new Date(base + drift);
      clockPill.textContent = util.pad(cn.getHours()) + ':' + util.pad(cn.getMinutes()) + ':' +
        util.pad(cn.getSeconds());
    }
    if (sessionPill && session) {
      sessionPill.textContent = session.label || '--';
      sessionPill.classList.toggle('trading', !!session.is_trading);
      sessionPill.classList.toggle('closed', !session.should_poll);
    }
  }

  function syncClock() {
    return api.clock().then(function (data) {
      state.session = data;
      var nowCn = String(data.now_cn || '');
      var match = nowCn.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
      if (match) {
        state.serverTimeMs = new Date(
          Number(match[1]), Number(match[2]) - 1, Number(match[3]),
          Number(match[4]), Number(match[5]), Number(match[6])
        ).getTime();
        state.syncedAt = Date.now();
      }
      updateClock();
      return data;
    }).catch(function () {
      if (util.$('#sessionPill')) util.$('#sessionPill').textContent = '离线';
      return null;
    });
  }

  function loadBranding() {
    return api.settings().then(function (data) {
      var app = data.app || {};
      var sub = util.$('#brandSub');
      if (sub && app.title) sub.textContent = app.title;
      var disc = util.$('#navDisclaimer');
      if (disc && app.disclaimer) disc.textContent = app.disclaimer;
      if (app.title && document.title.indexOf('StockSpace') === 0) {
        document.title = app.title;
      }
      return data;
    }).catch(function () { return null; });
  }

  function loadHealth() {
    return api.health().then(function (data) {
      state.health = data;
      var pill = util.$('#sourcePill');
      if (pill) {
        var mode = data.source_mode === 'synthetic' ? '演示数据' :
          (data.source_mode === 'real' ? '实盘模式' : '自动模式');
        pill.textContent = mode + ' · ' + (data.status === 'ok' ? '正常' : '降级');
        pill.classList.toggle('bad', data.status !== 'ok');
        pill.title = (data.degraded && data.degraded.length)
          ? '降级项: ' + data.degraded.join('；')
          : '全部数据能力可用';
      }
      return data;
    }).catch(function () {
      var pill = util.$('#sourcePill');
      if (pill) { pill.textContent = '服务离线'; pill.classList.add('bad'); }
      return null;
    });
  }

  /* ---------------------------------------------------------------- 搜索 */
  function initSearch() {
    var input = util.$('#globalSearch');
    var panel = util.$('#searchPanel');
    if (!input || !panel) return;
    var activeIndex = -1;
    var results = [];

    function hide() { panel.hidden = true; activeIndex = -1; results = []; }

    function show(items, keyword) {
      results = items;
      if (!items.length) {
        panel.innerHTML = '<div class="sp-empty">未找到匹配 <strong>' + util.esc(keyword) +
          '</strong> 的股票。可直接在「个股详情」页输入 6 位代码。</div>';
        panel.hidden = false;
        return;
      }
      panel.innerHTML = items.map(function (item, index) {
        return '<div class="sp-item' + (index === activeIndex ? ' active' : '') + '" data-code="' +
          util.esc(item.code) + '" data-index="' + index + '">' +
          '<span><strong>' + util.esc(item.code) + '</strong> ' + util.esc(item.name) +
          ' <span class="faint small">' + util.esc(item.board || '') + '</span></span>' +
          '<span class="' + util.dirClass(item.change_pct) + '">' + util.num(item.price, 2) +
          ' ' + util.pct(item.change_pct) + '</span></div>';
      }).join('');
      panel.hidden = false;
    }

    var doSearch = util.debounce(function () {
      var keyword = input.value.trim();
      if (keyword.length < 1) { hide(); return; }
      api.search(keyword, 12).then(function (data) {
        show((data && data.items) || [], keyword);
      }).catch(function (error) {
        panel.innerHTML = '<div class="sp-empty">' + util.esc(error.message) + '</div>';
        panel.hidden = false;
      });
    }, 280);

    input.addEventListener('input', doSearch);
    input.addEventListener('focus', function () { if (input.value.trim()) doSearch(); });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { hide(); input.blur(); return; }
      if (!results.length) {
        if (event.key === 'Enter' && /^\d{6}$/.test(input.value.trim())) {
          navigate('#/stock?code=' + input.value.trim());
          hide();
        }
        return;
      }
      if (event.key === 'ArrowDown') { activeIndex = Math.min(results.length - 1, activeIndex + 1); show(results, input.value); event.preventDefault(); }
      else if (event.key === 'ArrowUp') { activeIndex = Math.max(0, activeIndex - 1); show(results, input.value); event.preventDefault(); }
      else if (event.key === 'Enter') {
        var target = results[activeIndex >= 0 ? activeIndex : 0];
        if (target) { navigate('#/stock?code=' + target.code); hide(); input.blur(); }
      }
    });
    panel.addEventListener('click', function (event) {
      var item = event.target.closest ? event.target.closest('.sp-item') : null;
      if (!item) return;
      navigate('#/stock?code=' + item.dataset.code);
      hide();
      input.blur();
    });
    document.addEventListener('click', function (event) {
      if (!panel.hidden && !panel.contains(event.target) && event.target !== input) hide();
    });
  }

  /* ---------------------------------------------------------------- 启动 */
  /**
   * 从后端拉取策略清单并刷新前端缓存（顺序 + 标签）。
   *
   * 为什么必须动态化：策略清单原先在 util.js 里**写死**，
   * 于是后端新增策略时前端页签永远不出现 —— 实测加了
   * "缩量回调后温和放量"之后，策略选股页仍只显示 5 个页签，
   * 而 /api/strategies 已经返回 6 个。后端加策略不必改前端。
   */
  function loadCatalog() {
    return SS.api.strategies().then(function (data) {
      var items = (data && data.items) || [];
      if (!items.length) return;
      SS.STRATEGY_ORDER = items.map(function (item) { return item.key; });
      items.forEach(function (item) {
        if (item.key && item.name) SS.STRATEGY_LABELS[item.key] = item.name;
      });
      state.catalog = items;
    }).catch(function () { /* 拉取失败时保留兜底清单，不影响页面可用 */ });
  }

  function boot() {
    if (state.booted) return;
    state.booted = true;

    initTheme();
    initSearch();

    util.$('#navToggle').addEventListener('click', function () {
      var nav = util.$('#sidenav');
      var backdrop = util.$('#navBackdrop');
      var open = !nav.classList.contains('open');
      nav.classList.toggle('open', open);
      backdrop.hidden = !open;
      this.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    util.$('#navBackdrop').addEventListener('click', closeNav);

    util.$('#themeBtn').addEventListener('click', function () {
      var current = document.documentElement.getAttribute('data-theme') || 'dark';
      applyTheme(current === 'dark' ? 'light' : 'dark');
      var handler = state.refreshHandler;
      if (handler) { try { handler(true); } catch (e) { /* 忽略 */ } }
    });

    util.$('#refreshBtn').addEventListener('click', function () {
      var handler = state.refreshHandler;
      setLoading(true);
      Promise.resolve()
        .then(function () { return syncClock(); })
        .then(function () { return loadHealth(); })
        .then(function () { return handler ? handler(true) : render(); })
        .then(function () { util.toast('已刷新', 'ok', 1600); })
        .catch(function (error) { util.toast('刷新失败: ' + error.message, 'error'); })
        .then(function () { setLoading(false); });
    });

    global.addEventListener('hashchange', render);
    if (!global.location.hash) global.location.hash = '#/dashboard';

    syncClock().then(function () {
      setInterval(function () { syncClock(); }, 60000);
      setInterval(updateClock, 1000);
      updateClock();
    });
    loadBranding();
    loadHealth();
    setInterval(loadHealth, 120000);

    //: 策略清单要在首次 render 之前拿到，否则策略选股页首次进入时
    //: 只有兜底列表（会漏掉后端新增的策略）。
    loadCatalog().then(function () { render(); });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) {
        syncClock();
        var handler = state.refreshHandler;
        if (handler) { try { handler(); } catch (e) { /* 忽略 */ } }
      }
    });
    //: 首次渲染统一由上面的 loadCatalog().then(render) 触发 ——
    //: 这里不再重复调用 render()，避免闪烁两次。
  }

  // 同时暴露成 SS.boot / SS.app.boot 两种写法。
  // （踩过的坑：曾经只挂 SS.app.boot，而 index.html 调用的是 SS.boot，
  //   于是启动函数从未执行，页面永久停在"正在加载平台"。）
  SS.app = {
    boot: boot,
    navigate: navigate,
    state: state,
    render: render,
    syncClock: syncClock,
    loadHealth: loadHealth,
    loadCatalog: loadCatalog,
    applyTheme: applyTheme,
    setLoading: setLoading
  };
  SS.boot = boot;
  SS.navigate = navigate;
  SS.render = render;
  SS.loadHealth = loadHealth;
})(window);
