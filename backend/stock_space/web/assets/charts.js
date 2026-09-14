/* ============================================================================
   charts.js —— 零依赖 Canvas 图表
   为什么不用图表库: 需求要求"不得引用静态地址"且可离线部署,
   任何 CDN 都不允许; 而把 ECharts 打进产物又会让体积膨胀数十倍。
   这里用 Canvas 手绘四类图, 总共不到 400 行, 且完全可控。

   已实现: 折线/面积图(资金曲线、情绪趋势)、K 线图(含成交量与均线)、
           环形分布图(涨跌家数)、柱状图(评分分布)。
   所有绘制都会做 devicePixelRatio 适配(Retina 不模糊)。
   ========================================================================== */
(function (global) {
  'use strict';

  var SS = global.StockSpace = global.StockSpace || {};

  var COLORS = {
    up: '#ff5d5d',
    down: '#36d399',
    accent: '#5b8cff',
    accent2: '#7c6cff',
    gold: '#f5c453',
    muted: '#8b97bd',
    grid: 'rgba(139, 151, 189, .18)',
    ma5: '#f5a623',
    ma10: '#5b8cff',
    ma20: '#9b59b6',
    ma60: '#36d399'
  };

  function themeColors() {
    var style = getComputedStyle(document.documentElement);
    function read(name, fallback) {
      var value = style.getPropertyValue(name);
      return value && value.trim() ? value.trim() : fallback;
    }
    return {
      up: read('--up', COLORS.up),
      down: read('--down', COLORS.down),
      accent: read('--accent', COLORS.accent),
      accent2: read('--accent-2', COLORS.accent2),
      gold: read('--gold', COLORS.gold),
      muted: read('--muted', COLORS.muted),
      text: read('--txt', '#e6ecff'),
      txt2: read('--txt-2', '#a9b6da'),
      grid: read('--line-soft', COLORS.grid),
      panel: read('--panel', '#141d3a')
    };
  }

  function prepare(canvas, height) {
    var dpr = global.devicePixelRatio || 1;
    var rect = canvas.parentElement ? canvas.parentElement.getBoundingClientRect() : { width: 800 };
    var width = Math.max(rect.width || 320, 280);
    var h = height || 260;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.height = h + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, h);
    return { ctx: ctx, width: width, height: h };
  }

  function empty(canvas, message, height) {
    var env = prepare(canvas, height || 200);
    env.ctx.fillStyle = themeColors().muted;
    env.ctx.font = '12.5px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
    env.ctx.textAlign = 'center';
    env.ctx.fillText(message || '暂无数据', env.width / 2, env.height / 2);
  }

  /** 计算"好看"的刻度间隔 */
  function niceStep(span, targetTicks) {
    if (!isFinite(span) || span <= 0) return 1;
    var rough = span / Math.max(1, targetTicks || 5);
    var mag = Math.pow(10, Math.floor(Math.log(rough) / Math.LN10));
    var norm = rough / mag;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  function formatTick(value, digits) {
    if (Math.abs(value) >= 1e8) return (value / 1e8).toFixed(1) + '亿';
    if (Math.abs(value) >= 1e4) return (value / 1e4).toFixed(1) + '万';
    return Number(value).toFixed(digits === undefined ? 2 : digits);
  }

  /* ------------------------------------------------------------ 折线/面积图 */
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {Object} opts { series:[{name,data:[{x,y}],color,fill,width,dash}],
   *                        height, yFormat, xFormat, baseline, yMin, yMax }
   */
  function line(canvas, opts) {
    opts = opts || {};
    var series = (opts.series || []).filter(function (s) { return s && s.data && s.data.length; });
    if (!series.length) { empty(canvas, opts.emptyText, opts.height); return; }
    var C = themeColors();
    var env = prepare(canvas, opts.height || 260);
    var ctx = env.ctx, W = env.width, H = env.height;
    var padL = 56, padR = 14, padT = 14, padB = 26;
    var plotW = Math.max(10, W - padL - padR);
    var plotH = Math.max(10, H - padT - padB);

    var allY = [], maxLen = 0;
    series.forEach(function (s) {
      maxLen = Math.max(maxLen, s.data.length);
      s.data.forEach(function (pt) { if (isFinite(pt.y)) allY.push(pt.y); });
    });
    if (!allY.length) { empty(canvas, opts.emptyText, opts.height); return; }
    var yMin = opts.yMin !== undefined ? opts.yMin : Math.min.apply(null, allY);
    var yMax = opts.yMax !== undefined ? opts.yMax : Math.max.apply(null, allY);
    if (opts.baseline !== undefined) { yMin = Math.min(yMin, opts.baseline); yMax = Math.max(yMax, opts.baseline); }
    if (yMax === yMin) { yMax = yMin + 1; yMin = yMin - 1; }
    var span = yMax - yMin;
    yMin -= span * 0.06; yMax += span * 0.06;

    function X(i) { return padL + (maxLen <= 1 ? plotW / 2 : plotW * i / (maxLen - 1)); }
    function Y(v) { return padT + plotH * (1 - (v - yMin) / (yMax - yMin)); }

    // 网格与 Y 轴
    var step = niceStep(yMax - yMin, 5);
    ctx.font = '11px ui-monospace, Consolas, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var v = Math.ceil(yMin / step) * step; v <= yMax; v += step) {
      var y = Y(v);
      if (y < padT - 1 || y > padT + plotH + 1) continue;
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y + 0.5);
      ctx.lineTo(padL + plotW, y + 0.5);
      ctx.stroke();
      ctx.fillStyle = C.muted;
      ctx.fillText(opts.yFormat ? opts.yFormat(v) : formatTick(v, 2), padL - 6, y);
    }

    // 基准线
    if (opts.baseline !== undefined) {
      var by = Y(opts.baseline);
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = 'rgba(245, 196, 83, .55)';
      ctx.beginPath();
      ctx.moveTo(padL, by);
      ctx.lineTo(padL + plotW, by);
      ctx.stroke();
      ctx.restore();
    }

    // X 轴标签(最多 6 个)
    var refData = series[0].data;
    var labelCount = Math.min(6, refData.length);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = C.muted;
    for (var i = 0; i < labelCount; i++) {
      var index = labelCount === 1 ? 0 : Math.round(i * (refData.length - 1) / (labelCount - 1));
      var point = refData[index];
      if (!point) continue;
      var label = opts.xFormat ? opts.xFormat(point.x, index) : String(point.x);
      ctx.fillText(label, X(index), padT + plotH + 7);
    }

    // 数据线
    series.forEach(function (s) {
      var color = s.color || C.accent;
      if (s.fill) {
        var grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, hexA(color, 0.32));
        grad.addColorStop(1, hexA(color, 0.02));
        ctx.beginPath();
        s.data.forEach(function (pt, i) {
          if (!isFinite(pt.y)) return;
          if (i === 0) ctx.moveTo(X(i), Y(pt.y)); else ctx.lineTo(X(i), Y(pt.y));
        });
        if (s.data.length) {
          var lastIndex = s.data.length - 1;
          ctx.lineTo(X(lastIndex), Y(Math.max(yMin, opts.baseline !== undefined ? opts.baseline : yMin)));
          ctx.lineTo(X(0), Y(Math.max(yMin, opts.baseline !== undefined ? opts.baseline : yMin)));
        }
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = s.width || 1.8;
      if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
      var started = false;
      s.data.forEach(function (pt, i) {
        if (!isFinite(pt.y)) { started = false; return; }
        if (!started) { ctx.moveTo(X(i), Y(pt.y)); started = true; }
        else ctx.lineTo(X(i), Y(pt.y));
      });
      ctx.stroke();
      ctx.setLineDash([]);
    });

    // 图例
    if (opts.legend !== false) {
      var lx = padL + 4, ly = padT + 4;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      series.forEach(function (s) {
        if (!s.name) return;
        ctx.fillStyle = s.color || C.accent;
        ctx.fillRect(lx, ly - 4, 9, 8);
        ctx.fillStyle = C.txt2;
        ctx.font = '11px -apple-system, "PingFang SC", sans-serif';
        ctx.fillText(s.name, lx + 13, ly);
        lx += 13 + ctx.measureText(s.name).width + 16;
        ctx.font = '11px ui-monospace, Consolas, monospace';
      });
    }
  }

  /* ------------------------------------------------------------ K 线图 */
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {Array} bars [{date, open, high, low, close, volume}]
   * @param {Object} opts {height, indicators:{ma5,ma20,ma60}, showVolume}
   */
  function kline(canvas, bars, opts) {
    opts = opts || {};
    bars = (bars || []).filter(function (b) { return b && isFinite(b.close); });
    if (!bars.length) { empty(canvas, '暂无K线数据', opts.height); return; }
    var C = themeColors();
    var env = prepare(canvas, opts.height || 320);
    var ctx = env.ctx, W = env.width, H = env.height;
    var padL = 52, padR = 12, padT = 12, padB = 20;
    var volH = opts.showVolume === false ? 0 : Math.round((H - padT - padB) * 0.2);
    var mainH = H - padT - padB - volH - (volH ? 8 : 0);
    var plotW = Math.max(10, W - padL - padR);

    var count = bars.length;
    var maxBars = Math.max(10, Math.floor(plotW / 6));
    var view = bars.slice(-maxBars);
    var offset = count - view.length;

    var highs = view.map(function (b) { return b.high; });
    var lows = view.map(function (b) { return b.low; });
    var yMax = Math.max.apply(null, highs);
    var yMin = Math.min.apply(null, lows);
    var span = yMax - yMin || 1;
    yMin -= span * 0.05; yMax += span * 0.05;

    function X(i) { return padL + (view.length <= 1 ? plotW / 2 : plotW * (i + 0.5) / view.length); }
    function Y(v) { return padT + mainH * (1 - (v - yMin) / (yMax - yMin)); }
    var barW = Math.max(1.6, Math.min(11, plotW / view.length * 0.68));

    // Y 轴
    ctx.font = '11px ui-monospace, Consolas, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    var step = niceStep(yMax - yMin, 4);
    for (var v = Math.ceil(yMin / step) * step; v <= yMax; v += step) {
      var y = Y(v);
      ctx.strokeStyle = C.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y + 0.5);
      ctx.lineTo(padL + plotW, y + 0.5);
      ctx.stroke();
      ctx.fillStyle = C.muted;
      ctx.fillText(v.toFixed(2), padL - 5, y);
    }

    // 均线
    var indicators = opts.indicators || {};
    var maConfig = [
      { key: 'ma5', data: indicators.ma5, color: COLORS.ma5 },
      { key: 'ma10', data: indicators.ma10, color: COLORS.ma10 },
      { key: 'ma20', data: indicators.ma20, color: COLORS.ma20 },
      { key: 'ma60', data: indicators.ma60, color: COLORS.ma60 }
    ];
    maConfig.forEach(function (cfg) {
      if (!cfg.data) return;
      var slice = cfg.data.slice(offset);
      if (!slice.length) return;
      ctx.beginPath();
      ctx.strokeStyle = cfg.color;
      ctx.lineWidth = 1.2;
      var started = false;
      slice.forEach(function (value, i) {
        if (value === null || value === undefined || !isFinite(value)) { started = false; return; }
        var px = X(i), py = Y(value);
        if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
      });
      ctx.stroke();
    });

    // K 线
    view.forEach(function (bar, i) {
      var rising = bar.close >= bar.open;
      var color = rising ? C.up : C.down;
      var x = X(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, Y(bar.high));
      ctx.lineTo(x, Y(bar.low));
      ctx.stroke();
      var yo = Y(bar.open), yc = Y(bar.close);
      var top = Math.min(yo, yc);
      var bodyH = Math.max(1, Math.abs(yc - yo));
      if (rising) {
        // A 股习惯: 阳线空心红边
        ctx.strokeRect(x - barW / 2, top, barW, bodyH);
      } else {
        ctx.fillRect(x - barW / 2, top, barW, bodyH);
      }
    });

    // 成交量
    if (volH) {
      var volTop = padT + mainH + 8;
      var maxVol = Math.max.apply(null, view.map(function (b) { return b.volume || 0; })) || 1;
      view.forEach(function (bar, i) {
        var h = (bar.volume || 0) / maxVol * (volH - 4);
        var rising = bar.close >= bar.open;
        ctx.fillStyle = hexA(rising ? C.up : C.down, 0.62);
        var x = X(i);
        ctx.fillRect(x - barW / 2, volTop + (volH - 4 - h), barW, Math.max(0.6, h));
      });
      ctx.fillStyle = C.muted;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.font = '10.5px -apple-system, "PingFang SC", sans-serif';
      ctx.fillText('成交量', padL + 2, volTop);
    }

    // X 轴日期
    ctx.fillStyle = C.muted;
    ctx.font = '10.5px ui-monospace, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var labelCount = Math.min(5, view.length);
    for (var li = 0; li < labelCount; li++) {
      var index = labelCount === 1 ? 0 : Math.round(li * (view.length - 1) / (labelCount - 1));
      var date = String(view[index].date || '');
      ctx.fillText(date.slice(5), X(index), padT + mainH + volH + 12);
    }
  }

  /* ------------------------------------------------------------ 环形图 */
  function donut(canvas, segments, opts) {
    opts = opts || {};
    segments = (segments || []).filter(function (s) { return s.value > 0; });
    var C = themeColors();
    var env = prepare(canvas, opts.height || 200);
    var ctx = env.ctx, W = env.width, H = env.height;
    if (!segments.length) { empty(canvas, '暂无数据', opts.height); return; }
    var total = segments.reduce(function (sum, s) { return sum + s.value; }, 0) || 1;
    var cx = W * (opts.legendSide ? 0.30 : 0.5);
    var cy = H / 2;
    var R = Math.min(W * (opts.legendSide ? 0.24 : 0.34), H * 0.40);
    var inner = R * 0.62;
    var start = -Math.PI / 2;
    segments.forEach(function (seg) {
      var angle = seg.value / total * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, start, start + angle);
      ctx.closePath();
      ctx.fillStyle = seg.color || C.accent;
      ctx.fill();
      start += angle;
    });
    ctx.beginPath();
    ctx.arc(cx, cy, inner, 0, Math.PI * 2);
    ctx.fillStyle = C.panel;
    ctx.fill();
    ctx.fillStyle = C.text;
    ctx.font = '600 18px ui-monospace, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(opts.centerText !== undefined ? String(opts.centerText) : String(total), cx, cy - 6);
    if (opts.centerSub) {
      ctx.font = '11px -apple-system, "PingFang SC", sans-serif';
      ctx.fillStyle = C.muted;
      ctx.fillText(opts.centerSub, cx, cy + 12);
    }
    if (opts.legendSide) {
      var lx = W * 0.58, ly = cy - (segments.length * 18) / 2 + 8;
      ctx.textAlign = 'left';
      segments.forEach(function (seg) {
        ctx.fillStyle = seg.color || C.accent;
        ctx.fillRect(lx, ly - 5, 9, 9);
        ctx.fillStyle = C.txt2;
        ctx.font = '11.5px -apple-system, "PingFang SC", sans-serif';
        var pctText = (seg.value / total * 100).toFixed(1) + '%';
        ctx.fillText(seg.label + '  ' + seg.value + '  ' + pctText, lx + 14, ly);
        ly += 18;
      });
    }
  }

  /* ------------------------------------------------------------ 柱状图 */
  function bars(canvas, items, opts) {
    opts = opts || {};
    items = (items || []).filter(function (it) { return it && isFinite(it.value); });
    if (!items.length) { empty(canvas, opts.emptyText, opts.height); return; }
    var C = themeColors();
    var env = prepare(canvas, opts.height || 200);
    var ctx = env.ctx, W = env.width, H = env.height;
    var padL = 44, padR = 12, padT = 14, padB = 26;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var values = items.map(function (it) { return it.value; });
    var vMax = Math.max.apply(null, values);
    var vMin = Math.min(0, Math.min.apply(null, values));
    if (vMax === vMin) vMax = vMin + 1;

    function Y(v) { return padT + plotH * (1 - (v - vMin) / (vMax - vMin)); }
    var step = niceStep(vMax - vMin, 4);
    ctx.font = '11px ui-monospace, Consolas, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var v = Math.ceil(vMin / step) * step; v <= vMax; v += step) {
      var y = Y(v);
      ctx.strokeStyle = C.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y + 0.5);
      ctx.lineTo(padL + plotW, y + 0.5);
      ctx.stroke();
      ctx.fillStyle = C.muted;
      ctx.fillText(formatTick(v, 0), padL - 5, y);
    }
    var slot = plotW / items.length;
    var barW = Math.max(3, Math.min(38, slot * 0.68));
    var zeroY = Y(Math.max(vMin, 0));
    items.forEach(function (item, i) {
      var x = padL + slot * (i + 0.5);
      var y = Y(item.value);
      ctx.fillStyle = item.color || (item.value >= 0 ? C.accent : C.down);
      var top = Math.min(y, zeroY);
      var h = Math.max(1, Math.abs(zeroY - y));
      ctx.fillRect(x - barW / 2, top, barW, h);
      if (opts.labels !== false && items.length <= 14) {
        ctx.fillStyle = C.muted;
        ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(String(item.label || '').slice(0, 8), x, padT + plotH + 6);
        ctx.font = '11px ui-monospace, Consolas, monospace';
      }
    });
  }

  /* ------------------------------------------------------------ 迷你走势 */
  /** 在表格里画迷你走势线(不用 Canvas, 用 SVG 更省资源) */
  function sparkline(values, opts) {
    opts = opts || {};
    var data = (values || []).filter(function (v) { return isFinite(v); });
    if (data.length < 2) return '<span class="faint">--</span>';
    var w = opts.width || 72, h = opts.height || 22;
    var min = Math.min.apply(null, data), max = Math.max.apply(null, data);
    var span = max - min || 1;
    var pts = data.map(function (v, i) {
      return (i / (data.length - 1) * w).toFixed(1) + ',' +
             (h - (v - min) / span * (h - 4) - 2).toFixed(1);
    }).join(' ');
    var rising = data[data.length - 1] >= data[0];
    var color = opts.color || (rising ? 'var(--up)' : 'var(--down)');
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
      '" preserveAspectRatio="none" aria-hidden="true">' +
      '<polyline points="' + pts + '" fill="none" stroke="' + color +
      '" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>';
  }

  function hexA(color, alpha) {
    if (!color) return 'rgba(91,140,255,' + alpha + ')';
    if (color.charAt(0) === '#') {
      var hex = color.slice(1);
      if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      var num = parseInt(hex, 16);
      return 'rgba(' + ((num >> 16) & 255) + ',' + ((num >> 8) & 255) + ',' + (num & 255) + ',' + alpha + ')';
    }
    if (color.indexOf('rgb(') === 0) return color.replace('rgb(', 'rgba(').replace(')', ',' + alpha + ')');
    return color;
  }

  SS.charts = {
    line: line, kline: kline, donut: donut, bars: bars,
    sparkline: sparkline, empty: empty, colors: COLORS, themeColors: themeColors
  };
})(window);
