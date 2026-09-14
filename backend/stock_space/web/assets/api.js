/* ============================================================================
   api.js —— 后端接口封装
   所有请求一律使用**相对路径**(去掉前导斜杠), 因此部署在任意子路径
   (如 https://host/stock-space/) 时都能自动跟随页面前缀, 不依赖任何固定地址。

   统一处理:
     * 信封解包 {code, message, data}
     * 非 JSON 响应的可诊断报错(常见于反向代理未转发 /api)
     * 超时与网络错误的中文提示
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace = global.StockSpace || {};
  var util = SS.util;

  var DEFAULT_TIMEOUT = 60000;

  function apiUrl(path) {
    var rel = String(path || '');
    if (rel.charAt(0) === '/') rel = rel.slice(1);
    return rel;   // 相对当前页面目录解析
  }

  function buildQuery(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') return;
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function describeError(response, text) {
    var ct = (response.headers.get('content-type') || '').toLowerCase();
    if (ct.indexOf('json') < 0) {
      return '服务响应异常(非 JSON, 疑似返回了 HTML)。请检查: ① 后端服务是否已启动; ' +
             '② 反向代理是否把 api/ 路径转发到后端; ③ 部署子路径前缀是否配置正确。' +
             (response.status ? ' (HTTP ' + response.status + ')' : '');
    }
    if (text) {
      try {
        var payload = JSON.parse(text);
        if (payload && payload.message) return payload.message;
      } catch (e) { /* 忽略 */ }
    }
    return '请求失败 (HTTP ' + response.status + ' ' + (response.statusText || '') + ')';
  }

  /**
   * 发起请求。
   * @param {string} path 形如 'api/health' 的相对路径
   * @param {Object} opts {method, params, body, timeout, raw, signal}
   * @returns {Promise<any>} 默认返回解包后的 data
   */
  function request(path, opts) {
    opts = opts || {};
    var url = apiUrl(path) + buildQuery(opts.params);
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, opts.timeout || DEFAULT_TIMEOUT);

    var init = {
      method: opts.method || 'GET',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    };
    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
    if (controller) init.signal = controller.signal;
    if (opts.headers) Object.keys(opts.headers).forEach(function (k) { init.headers[k] = opts.headers[k]; });

    return fetch(url, init).then(function (response) {
      return response.text().then(function (text) {
        var payload = null;
        try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
        if (!response.ok) {
          var message = describeError(response, text);
          var error = new Error(message);
          error.status = response.status;
          error.payload = payload;
          throw error;
        }
        if (payload && typeof payload === 'object' && 'code' in payload) {
          if (payload.code !== 0) {
            var err = new Error(payload.message || '业务处理失败');
            err.code = payload.code;
            err.payload = payload;
            throw err;
          }
          return opts.raw ? payload : payload.data;
        }
        return payload;
      });
    }).catch(function (error) {
      if (error && error.name === 'AbortError') {
        throw new Error('请求超时(>' + Math.round((opts.timeout || DEFAULT_TIMEOUT) / 1000) + '秒)，' +
          '首次拉取全市场数据可能较慢，请稍后重试。');
      }
      if (error && error.message === 'Failed to fetch') {
        throw new Error('无法连接到后端服务。请确认服务已启动，且页面与接口同源同前缀。');
      }
      throw error;
    }).then(function (result) {
      clearTimeout(timer);
      return result;
    }, function (error) {
      clearTimeout(timer);
      throw error;
    });
  }

  function get(path, params, opts) {
    return request(path, Object.assign({ method: 'GET', params: params }, opts || {}));
  }
  function post(path, body, opts) {
    return request(path, Object.assign({ method: 'POST', body: body === undefined ? {} : body }, opts || {}));
  }
  function put(path, body, opts) {
    return request(path, Object.assign({ method: 'PUT', body: body === undefined ? {} : body }, opts || {}));
  }
  function del(path, body, opts) {
    return request(path, Object.assign({ method: 'DELETE', body: body }, opts || {}));
  }

  /* ------------------------------------------------------------ 业务接口 */
  var API = {
    request: request, get: get, post: post, put: put, del: del, apiUrl: apiUrl,

    health: function () { return get('api/health'); },
    systemOverview: function () { return get('api/system/overview'); },
    memory: function (points) { return get('api/system/memory', { points: points || 120 }); },
    memoryFlush: function () { return post('api/system/memory/flush'); },
    scheduler: function () { return get('api/system/scheduler'); },
    jobs: function (limit) { return get('api/system/jobs', { limit: limit || 50 }); },
    runJob: function (job) { return post('api/system/jobs/' + encodeURIComponent(job) + '/run'); },
    database: function () { return get('api/system/database'); },
    dbCleanup: function () { return post('api/system/database/cleanup'); },
    dbVacuum: function () { return post('api/system/database/vacuum'); },
    integration: function () { return get('api/system/integration'); },

    clock: function () { return get('api/market/clock'); },
    dashboard: function () { return get('api/dashboard'); },
    indices: function () { return get('api/market/indices'); },
    breadth: function () { return get('api/market/breadth'); },
    rank: function (kind, limit) { return get('api/market/rank', { kind: kind, limit: limit || 50 }); },
    sectors: function (kind) { return get('api/market/sectors', { kind: kind || 'industry' }); },
    sectorDetail: function (code, name) { return get('api/market/sector/' + encodeURIComponent(code), { name: name }); },
    sectorFlow: function (limit) { return get('api/market/sector-flow', { limit: limit || 30 }); },
    limitUp: function () { return get('api/market/limit-up'); },
    emotion: function () { return get('api/market/emotion'); },
    emotionHistory: function (days) { return get('api/market/emotion/history', { days: days || 30 }); },
    context: function (refresh) { return get('api/market/context', { refresh: refresh ? 'true' : '' }); },
    quotes: function (codes) { return get('api/quotes', { codes: codes }); },
    search: function (keyword, limit) { return get('api/search', { keyword: keyword, limit: limit || 20 }); },
    stock: function (code) { return get('api/stock/' + encodeURIComponent(code)); },
    kline: function (code, days, period) {
      return get('api/stock/' + encodeURIComponent(code) + '/kline',
        { days: days || 250, period: period || 'day', indicators: 'true' }, { timeout: 90000 });
    },
    minute: function (code) { return get('api/stock/' + encodeURIComponent(code) + '/minute'); },
    news: function (limit, channel) { return get('api/news', { limit: limit || 60, channel: channel || '' }); },
    review: function (kind) { return get('api/review', { kind: kind || 'cn_close' }, { timeout: 120000 }); },

    strategies: function () { return get('api/strategies'); },
    strategy: function (key) { return get('api/strategies/' + encodeURIComponent(key)); },
    saveStrategyParams: function (key, params) {
      return post('api/strategies/' + encodeURIComponent(key) + '/params', params);
    },
    resetStrategyParams: function (key) {
      return del('api/strategies/' + encodeURIComponent(key) + '/params');
    },
    scan: function (key, opts) {
      opts = opts || {};
      return post('api/strategies/' + encodeURIComponent(key) + '/scan',
        opts.params || null,
        { params: { refresh: opts.refresh ? 'true' : '', limit: opts.limit || 100, persist: opts.persist === false ? 'false' : 'true' },
          timeout: opts.timeout || 300000 });
    },
    scanAll: function (opts) {
      opts = opts || {};
      return post('api/scan/all', null,
        { params: { refresh: opts.refresh ? 'true' : '', per_strategy: opts.perStrategy || 20,
                    persist: opts.persist === false ? 'false' : 'true' }, timeout: 600000 });
    },

    /* ---- 异步扫描任务（界面走这条）------------------------------------------
       为什么不直接用上面的同步接口：单个策略实测要 158 秒，同步长请求会被
       反向代理/浏览器的空闲超时掐断，前端只能显示"无法连接到后端服务"。
       异步接口毫秒级返回 job_id，再用 pollScanJob 轮询。 */
    scanJob: function (jobId, opts) {
      opts = opts || {};
      return get('api/scan/jobs/' + encodeURIComponent(jobId),
        { result: opts.result === false ? 'false' : 'true' });
    },
    scanJobs: function (limit) { return get('api/scan/jobs', { limit: limit || 10 }); },
    cancelScanJob: function (jobId) {
      return post('api/scan/jobs/' + encodeURIComponent(jobId) + '/cancel');
    },
    /** 创建扫描任务。strategy 传 'all' 跑全部策略，否则传策略 key。 */
    createScanJob: function (strategy, opts) {
      opts = opts || {};
      return post('api/scan/jobs', null, {
        params: {
          strategy: strategy || 'all',
          refresh: opts.refresh ? 'true' : '',
          limit: opts.limit || 200,
          per_strategy: opts.perStrategy || 20,
          persist: opts.persist === false ? 'false' : 'true'
        },
        timeout: 30000
      });
    },
    lastScan: function (key, date) { return get('api/strategies/' + encodeURIComponent(key) + '/last', { date: date || '' }); },
    evaluate: function (key, code, params) {
      return post('api/strategies/' + encodeURIComponent(key) + '/evaluate',
        { code: code, params: params || null }, { timeout: 120000 });
    },
    backtest: function (payload) { return post('api/backtest', payload, { timeout: 900000 }); },
    signals: function (strategy, limit) { return get('api/signals', { strategy: strategy || '', limit: limit || 100 }); },

    datasources: function () { return get('api/datasources'); },
    candidates: function (capability) { return get('api/datasources/' + encodeURIComponent(capability) + '/candidates'); },
    lockSource: function (capability, alias) {
      return post('api/datasources/' + encodeURIComponent(capability) + '/lock', { alias: alias || '' });
    },
    unlockAll: function () { return post('api/datasources/unlock-all'); },
    toggleSource: function (name, enabled) {
      return post('api/datasources/' + encodeURIComponent(name) + '/toggle', { enabled: !!enabled });
    },
    probeAll: function (capability) { return post('api/datasources/probe', { capability: capability || 'quote' }, { timeout: 180000 }); },
    probeOne: function (name) { return post('api/datasources/' + encodeURIComponent(name) + '/probe', {}, { timeout: 60000 }); },
    refreshData: function (capability, force) {
      return post('api/datasources/refresh', { capability: capability || 'snapshot', force: force !== false }, { timeout: 300000 });
    },
    resetBreakers: function () { return post('api/datasources/reset-breakers'); },
    providerEndpoints: function (name) { return get('api/datasources/' + encodeURIComponent(name) + '/endpoints'); },
    saveProviderEndpoints: function (name, payload) {
      return put('api/datasources/' + encodeURIComponent(name) + '/endpoints', payload);
    },
    credentials: function (name) { return get('api/datasources/' + encodeURIComponent(name) + '/credentials'); },
    saveCredentials: function (name, payload) {
      return post('api/datasources/' + encodeURIComponent(name) + '/credentials', payload, { timeout: 60000 });
    },
    clearCredentials: function (name) { return del('api/datasources/' + encodeURIComponent(name) + '/credentials'); },
    setSourceMode: function (mode) { return post('api/datasources/mode', { mode: mode }); },

    settings: function () { return get('api/settings'); },
    saveSettings: function (payload) { return put('api/settings', payload); },
    editablePaths: function () { return get('api/settings/editable'); },
    resetSettings: function (section) { return post('api/settings/reset', { section: section || '' }); },
    testPush: function (webhook, title) {
      return post('api/settings/push/test', { webhook: webhook || '', title: title || '' }, { timeout: 60000 });
    },
    pushLog: function (limit) { return get('api/settings/push/log', { limit: limit || 50 }); },
    pushMarket: function () { return post('api/settings/push/market', {}, { timeout: 120000 }); },

    watchlist: function () { return get('api/watchlist'); },
    addWatch: function (payload) { return post('api/watchlist', payload); },
    addWatchBatch: function (codes) { return post('api/watchlist/batch', { codes: codes }); },
    removeWatch: function (codes) { return del('api/watchlist', { codes: codes }); },
    /** 历史自选股池：已移出的标的 + 放入/放出事件流水 */
    watchHistory: function (limit) { return get('api/watchlist/history', { limit: limit || 500 }); },

    /* ---- 历史绩效（第6/7/8条）---- */
    strategyPerformance: function (key, opts) {
      opts = opts || {};
      return get('api/strategies/' + encodeURIComponent(key) + '/performance', {
        start: opts.start || '', end: opts.end || '', limit_days: opts.limitDays || 60
      }, { timeout: 600000 });
    },
    portfolioPerformance: function (opts) {
      opts = opts || {};
      return get('api/portfolio/performance', { strategy: opts.strategy || '', limit: opts.limit || 500 });
    },
    positionReview: function (id) { return get('api/portfolio/' + id + '/review'); },

    /* ---- 日终快照 / 日历 ----
       快照只在收盘后写一次（用户明确要求，不做盘中每分钟落库），
       因此"日历能选的日期"必须由后端给出，否则用户会点到没有数据的空日期。 */
    snapshotDates: function (limit) { return get('api/snapshots/dates', { limit: limit || 90 }); },
    snapshotDay: function (date) { return get('api/snapshots/day', { date: date || '' }); },
    captureSnapshot: function (date) {
      return post('api/snapshots/capture', {},
        { params: { date: date || '' }, timeout: 180000 });
    },

    portfolio: function (status) { return get('api/portfolio', { status: status || '' }); },
    openPosition: function (payload) { return post('api/portfolio/open', payload); },
    closePosition: function (id, payload) { return post('api/portfolio/' + id + '/close', payload); },
    deletePosition: function (id) { return del('api/portfolio/' + id); }
  };

  SS.api = API;
})(window);
