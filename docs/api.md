# 接口说明

* 交互式文档（可直接试）：`http://<地址>:端口/docs`
* 机器可读规范：`http://<地址>:端口/openapi.json`
* 运行时的接口清单：页面「系统状态」→ 接口清单

---

## 一、通用约定

### 1.1 统一响应信封

所有 `/api/*` 接口返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": { },
  "ts": 1789358675.71
}
```

| 字段 | 说明 |
|---|---|
| `code` | `0` = 成功；非 0 = 业务失败（此时 `message` 是可读原因） |
| `message` | 成功时为 `"ok"`，失败时为中文原因 |
| `data` | 业务数据；失败时可能为 `null` |
| `ts` | 服务器时间戳（秒） |

**为什么要信封**：前端需要区分"HTTP 200 但业务失败"与"网络层失败"，
前者应显示具体原因，后者应提示检查服务。有信封就只有一个判断点。

### 1.2 例外

| 路径 | 说明 |
|---|---|
| `/healthz` | 极简存活探针，**无信封**：`{"status":"ok","version":"1.0.0"}` |
| `/openapi.json` `/docs` `/redoc` | 标准 OpenAPI |
| `/assets/*` | 静态资源 |
| `/` `/selfcheck` `/favicon.ico` `/manifest.webmanifest` | 页面 |
| `/api/export/*.csv` | 直接返回 CSV 文件（带 UTF-8 BOM，Excel 可直接打开） |
| `/api/settings/export` `/api/settings/backup` | 直接返回 JSON 文件（带下载头） |

### 1.3 HTTP 状态码

| 状态码 | 含义 |
|---|---|
| 200 | 成功（业务失败也是 200，看 `code`） |
| 400 | 请求参数不合法（如未知的数据源名、非法端口） |
| 404 | 资源不存在（如未知策略 key） |
| 422 | 请求体/查询参数未通过 Pydantic 校验（如 `limit` 超范围） |
| 500 | 服务器内部错误（信封里带异常类型与消息） |
| 502 | 推送失败（上游企业微信返回非 0 错误码） |
| 503 | 数据能力不可用（如某能力全部数据源失败） |

### 1.4 关于"不返回假数据"

当某项能力的所有数据源都不可用时，接口返回 **503 + 明确原因**，而不是返回空数组
或编造的占位数据。原因里会带上每个源的失败详情。

唯一的例外是 `synthetic` 模式：此时返回的是内置合成行情，所有响应带 `source=synthetic`，
页面会显著标注「演示数据」。

### 1.5 路径前缀

所有接口路径都是**相对根路径**的（`/api/...`）。前端调用时使用相对地址（`api/...`），
因此应用可以部署在任意子路径 —— 只要反向代理把前缀正确转发。

---

## 二、系统与运维

### `GET /healthz`

极简存活探针，无信封。适合容器 HEALTHCHECK 与负载均衡。

```json
{"status": "ok", "version": "1.0.0"}
```

### `GET /api/health`

完整健康检查。

```json
{
  "status": "ok",
  "degraded": [],
  "version": "1.0.0",
  "uptime_seconds": 3821.4,
  "pid": 12345,
  "rss_mb": 312.55,
  "trade_date": "2026-09-14",
  "server_time": "2026-09-14 14:05:22",
  "session": { "phase": "trading", "label": "连续竞价(下午)", "is_trading": true,
               "should_poll": true, "interval_seconds": 30 },
  "source_mode": "auto",
  "scheduler_running": true,
  "capabilities_available": 17,
  "capabilities_total": 17
}
```

`degraded` 非空即为降级状态（例如某个数据能力无可用源、调度器未运行、内存接近上限）。
**云监控建议在 `degraded` 非空时告警。**

### `GET /api/ready`

就绪检查（K8s readinessProbe 用）：

```json
{"ready": true, "issues": []}
```

### `GET /api/version`

```json
{"version": "1.0.0", "title": "StockSpace 股票决策平台"}
```

### `GET /api/system/overview`

应用信息 + 内存 + 调度 + 数据库 + 数据源统计的总览（「系统状态」页用）。

### `GET /api/system/memory?points=120`

内存监控完整报告。

```json
{
  "process": { "pid": 1, "rss_mb": 312.55, "peak_rss_mb": 402.1,
               "rss_delta_mb": -3.2, "threads": 12 },
  "limits": { "soft_limit_mb": 800.0, "hard_limit_mb": 1400.0,
              "container_limit_mb": null,
              "soft_usage_pct": 39.07, "hard_usage_pct": 22.33 },
  "system": { "total_mb": 16219.4, "available_mb": 4726.8,
              "used_pct": 70.9, "source": "psutil" },
  "guard": { "running": true, "interval_seconds": 10.0,
             "soft_breaches": 0, "hard_breaches": 0, "gc_collections": 0,
             "last_action": "ttl_purge(12)", "last_action_ago_seconds": 4.1,
             "total_ttl_purged": 812, "total_evicted": 0 },
  "caches": {
    "total_entries": 4213,
    "items": [
      { "name": "kline_memory", "size": 1200, "max_entries": 1200,
        "ttl_seconds": 900.0, "hits": 8213, "misses": 421,
        "hit_rate": 0.9512, "expired_purged": 0, "lru_evicted": 3412,
        "usage_pct": 100.0 }
    ]
  },
  "trend": [ { "ts": 1789358600.1, "rss_mb": 312.5, "cache_entries": 4213 } ]
}
```

### `GET /api/system/memory/raw`

极简视图，适合监控脚本：

```json
{"rss_mb": 312.55, "system_used_pct": 70.9, "system_total_mb": 16219.4,
 "cache_entries": 4213, "soft_breaches": 0, "hard_breaches": 0}
```

### `POST /api/system/memory/flush`

立即释放全部缓存并 GC：

```json
{"freed_entries": 4213, "gc_collected": 31,
 "rss_before_mb": 312.55, "rss_after_mb": 298.12}
```

### `GET /api/system/scheduler`

调度器状态与各任务统计。

```json
{
  "running": true, "enabled": true, "uptime_seconds": 3821.4,
  "loop_count": 127, "last_tick_ago_seconds": 12.3,
  "session": { "label": "连续竞价(下午)" },
  "interval_seconds": 30,
  "jobs": [
    { "name": "refresh_snapshot", "runs": 127, "failures": 0,
      "last_run_ago_seconds": 12.3, "last_duration_ms": 7821.4,
      "last_error": "", "last_detail": "刷新 5913 只标的" }
  ],
  "daily_done": ["2026-09-14"],
  "push_schedules": "09:00,11:35,15:05",
  "pushed_today": ["2026-09-14 11:35"]
}
```

### `GET /api/system/jobs?limit=50`

任务执行日志（含耗时与错误）。

### `POST /api/system/jobs/{job}/run`

手动触发单个任务。可用 job 名：
`refresh_snapshot` / `signal_watch` / `scheduled_push` / `daily_tasks` / `housekeeping` / `memory_alert`

```json
{"job": "refresh_snapshot", "ok": true, "detail": "刷新 5913 只标的", "duration_ms": 7821.4}
```

### `GET /api/system/database`

```json
{
  "path_name": "stock_space.sqlite3",
  "size_bytes": 13381632,
  "rows": { "kline_daily": 83190, "scan_result": 250, "news_item": 412,
            "signal_log": 180, "push_log": 26, "job_log": 3821,
            "watchlist": 3, "portfolio": 5 }
}
```

### `POST /api/system/database/cleanup` / `POST /api/system/database/vacuum`

清理过期数据 / 整理数据库。

### `GET /api/system/integration`

11 个原始项目 → 本平台的落地对照表。

```json
{"items": [
  {"project": "92KeBi",
   "contribution": "情绪周期状态机 + 龙头分层 + 仓位建议 + 企微多 webhook 推送",
   "landed_in": ["engines/emotion.py", "services/push_service.py"]}
]}
```

---

## 三、行情

### `GET /api/market/clock`

市场时钟 —— 前端据此决定轮询节奏。

```json
{
  "phase": "trading", "label": "连续竞价(下午)",
  "is_trading": true, "is_trading_day": true,
  "should_poll": true, "interval_seconds": 30,
  "session_date": "2026-09-14", "next_open": null,
  "now_cn": "2026-09-14 14:05:22",
  "server_time": "2026-09-14 14:05:22", "timezone": "UTC+8",
  "refresh_hint_seconds": 30
}
```

`phase` 取值：`pre_open` / `call_auction` / `trading` / `lunch_break` / `closed` / `weekend` / `holiday`

### `GET /api/dashboard`

仪表盘聚合。任何单块失败都降级为 `null` 并记录在 `degraded` 里，**不整体报错**。

```json
{
  "clock": { "label": "连续竞价(下午)", "should_poll": true },
  "as_of": "2026-09-14 14:05:22",
  "trade_date": "2026-09-14",
  "degraded": [],
  "sample_limited": false,
  "universe_size": 5913,
  "breadth": { "up": 3576, "down": 1797, "flat": 540,
               "limit_up": 61, "limit_down": 17, "total_amount": 1113906272184.6,
               "up_ratio": 0.6048, "source": "eastmoney" },
  "index_quotes": [ { "code": "000001", "name": "上证指数", "price": 3894.28,
                      "change_pct": 0.16, "amount": 526345052522.5 } ],
  "leaders": {
    "gainers": [ { "code": "300903", "name": "科翔股份", "price": 30.2,
                   "change_pct": 20.0, "amount": 1.2e9, "industry": "元件" } ],
    "losers": [], "amount": [], "turnover": []
  },
  "emotion": {
    "emotion": { "score": 56.7, "level": "中性",
                 "parts": [ { "name": "涨停家数", "value": 61, "weight": 25.0,
                              "earned": 19.06, "threshold": "80 家封顶" } ],
                 "metrics": { "limit_up": 61, "broken_rate": 22.4,
                              "max_consecutive": 4 } },
    "cycle": { "phase": "ice", "label": "冰点", "evidence_score": -2,
               "confidence": 0.76, "advice": "空仓或极低仓位试错…",
               "evidence": [ { "name": "最高连板", "delta": -1, "value": 4,
                               "threshold": "≤ 3 板" } ] },
    "leaders": [ { "rank": 1, "code": "000993", "name": "闽东电力",
                   "tier": "龙头", "consecutive": 4, "amount": 8.1e8 } ],
    "sectors": [ { "sector": "电力", "limit_up": 3, "consecutive": 4 } ],
    "market": { "sample_size": 5913, "source": "eastmoney" }
  },
  "latest_scans": { "trend": { "trade_date": "2026-09-14", "items": [] } }
}
```

### `GET /api/market/indices`

主要指数行情。

> ⚠️ 指数与个股代码段会重叠（`000001` 既是上证指数也是平安银行），因此本接口
> **不使用股票快照匹配**，而是走独立接口 + 显式 secid 映射。`name` 一定是该指数的名称。

```json
{
  "items": [ { "name": "上证指数", "code": "000001", "market": "sh",
               "price": 3894.28, "change": 6.17, "change_pct": 0.16,
               "amount": 526345052522.5, "source": "eastmoney" } ],
  "matched": 8, "total": 8, "source": "eastmoney", "errors": [],
  "as_of": "2026-09-14 14:05:22"
}
```

### `GET /api/market/breadth`

```json
{"up": 3576, "down": 1797, "flat": 540, "limit_up": 61, "limit_down": 17,
 "broken_board": 0, "up_over_5": 812, "down_over_5": 143,
 "total_amount": 1113906272184.6, "source": "eastmoney",
 "total": 5913, "up_ratio": 0.6048}
```

### `GET /api/market/rank?kind=gainers&limit=50`

`kind` ∈ `gainers` / `losers` / `amount` / `turnover` / `speed` / `amplitude` / `volume_ratio`
；`limit` ∈ [1, 200]

> 东财 `clist` 单页硬上限 100 条，因此实际最多返回 100 条。

### `GET /api/market/sectors?kind=industry`

`kind` ∈ `industry` / `concept`

```json
{"kind": "industry", "items": [
  { "code": "BK1600", "name": "医疗研发外包", "change_pct": 5.45,
    "amount": 14960768386.0, "up_count": 29, "down_count": 0,
    "leader_name": "万邦医药", "leader_change_pct": 20.0,
    "main_net_inflow": 3844686592.0, "kind": "industry", "source": "eastmoney" }],
 "source": "eastmoney", "as_of": "..."}
```

### `GET /api/market/sector/{sector_code}?name=板块名`

板块详情 + 成分股。

```json
{"code": "BK1600", "name": "医疗研发外包", "member_count": 29,
 "up_count": 29, "up_ratio": 1.0, "avg_change_pct": 5.45,
 "amount": 14960768386.0, "members": [ { "code": "301520", "name": "万邦医药" } ]}
```

### `GET /api/market/sector-flow?limit=30`

板块主力资金净流入排名。

### `GET /api/market/limit-up`

涨停池与炸板池。

```json
{
  "date": "20260914", "limit_up_count": 48, "broken_count": 25,
  "broken_rate": 34.25, "max_consecutive": 4,
  "ladder": { "1": 39, "2": 5, "3": 3, "4": 1 },
  "limit_up": [ { "code": "000993", "name": "闽东电力", "price": 12.1,
                  "change_pct": 10.0, "amount": 8.1e8, "turnover_rate": 12.3,
                  "first_limit_time": "09:35:00", "last_limit_time": "10:12:00",
                  "open_times": 0, "consecutive": 4, "industry": "电力" } ],
  "broken": [], "source": "eastmoney"
}
```

> 非交易时段或涨停池接口不可用时返回 **503 + 原因**，而不是返回空结构。

### `GET /api/market/emotion`

情绪面板（与仪表盘里的 `emotion` 块结构一致）。

### `GET /api/market/emotion/history?days=30`

```json
{"dates": ["2026-09-14"], "latest": {"score": 56.7, "phase": "ice",
 "limit_up": 61, "max_consecutive": 4, "date": "2026-09-14"}}
```

### `GET /api/market/context?refresh=false`

当前市场上下文（策略扫描时注入的内容）。

```json
{
  "trade_date": "2026-09-14", "universe_size": 5913, "sample_limited": false,
  "source": "eastmoney", "benchmark_ret20": 2.31, "env_gate": "full",
  "sector_count": 129, "attention_count": 100,
  "top_sectors": [ { "name": "医疗研发外包", "avg_change_pct": 5.45,
                     "up_ratio": 1.0, "limit_up_ratio": 0.03, "count": 29.0 } ],
  "warnings": []
}
```

`env_gate` ∈ `full`（可正常开仓）/ `half`（减半仓）/ `off`（停开新仓，N 字战法用它做大盘闸门）

### `GET /api/quotes?codes=600519,000001`

```json
{"items": [ { "code": "600519", "name": "贵州茅台", "price": 1278.5,
              "change_pct": 0.26, "volume": 1051100, "amount": 1342592668.0,
              "turnover_rate": 0.58, "volume_ratio": 1.02,
              "limit_up": 1402.68, "limit_down": 1147.64,
              "industry": "酿酒行业", "source": "tencent" } ],
 "as_of": "...", "source": "tencent"}
```

> **单位口径**：`volume` 为**股**，`amount` 为**元**，`total_mv`/`float_mv` 为**元**。

### `GET /api/search?keyword=茅台&limit=20`

基于已缓存的全市场快照检索（不再额外请求上游）。

```json
{"items": [ { "code": "600519", "name": "贵州茅台", "price": 1278.5 } ],
 "keyword": "茅台", "total_scanned": 5913}
```

### `GET /api/stock/{code}`

个股详情：行情 + K 线 + 资金流（可选块失败时为 `null`，不影响整体）。

```json
{"code": "600519", "as_of": "...",
 "quote": { "code": "600519", "name": "贵州茅台", "price": 1278.5 },
 "kline": { "count": 250, "bars": [], "indicators": {} },
 "money_flow": { "main_net": -5.76e7, "main_net_pct": -0.57 }}
```

### `GET /api/stock/{code}/kline?days=250&period=day&indicators=true`

`period` ∈ `day` / `week` / `month`；`days` ∈ [30, 1200]

```json
{
  "code": "600519", "period": "day", "count": 250, "origin": "disk",
  "source": "tencent",
  "bars": [ { "date": "2026-09-14", "open": 1277.27, "high": 1285.53,
              "low": 1270.36, "close": 1278.5, "volume": 1051100,
              "amount": 0.0, "change_pct": 0.26, "turnover_rate": 0.0 } ],
  "indicators": {
    "ma5": [null, null, null, 1279.1], "ma10": [], "ma20": [], "ma60": [],
    "dif": [], "dea": [], "macd": [], "k": [], "d": [], "j": [],
    "boll_upper": [], "boll_mid": [], "boll_lower": [],
    "rsi14": [], "atr14": [], "vol_ma5": [], "vol_ma20": []
  }
}
```

`origin` ∈ `disk`（命中磁盘缓存）/ `network`（实时拉取）/ `aggregated`（由日线聚合）

> **周线与月线由日线本地聚合**（磁盘只存日线）。这样只维护一个数据口径，
> 不会出现"周线缓存与日线缓存不一致"导致的信号冲突。

### `GET /api/stock/{code}/minute`

```json
{"code": "600519", "name": "贵州茅台", "prev_close": 1275.16,
 "points": [ { "time": "09:30", "price": 1277.27, "avg": 1277.1, "volume": 1200 } ],
 "source": "eastmoney"}
```

### `GET /api/news?limit=60&channel=`

`channel` ∈ ``（全部）/ `news` / `flash` / `announcement` / `rumor`

```json
{"items": [ { "id": "sina-5092262", "title": "…", "summary": "…",
              "url": "…", "source": "sina", "channel": "flash",
              "published_at": "2026-09-14 11:20:00",
              "related_codes": ["600519"], "important": true,
              "sentiment": "neutral", "pushed": false } ],
 "count": 60, "errors": [], "as_of": "..."}
```

### `GET /api/review?kind=cn_close`

参数化复盘报告。`kind` ∈ `cn_close`（A 股收盘复盘）/ `us_open`（隔夜外围前瞻）

```json
{
  "kind": "cn_close", "title": "A股收盘复盘（2026-09-14）",
  "trade_date": "2026-09-14", "generated_at": "...",
  "rating": "偏冷", "stage": "反弹途中",
  "conclusion": "赚钱效应 偏冷，行情阶段 反弹途中。全市场上涨 3576 家…",
  "modules": [ { "key": "indices", "title": "大盘指数概览", "status": "ok",
                 "note": "", "data": { "indices": [] }, "bullets": [] } ],
  "degraded_blocks": [],
  "compliance": { "ok": true, "issues": [] },
  "risk_notice": "本文仅为行情复盘参考，不构成任何投资建议。股市有风险，投资需谨慎。"
}
```

`status` ∈ `ok` / `degraded` / `missing` —— **数据缺失是显式状态，不会静默留空**。

---

## 四、策略与回测

### `GET /api/strategies`

策略目录（含默认参数、逐条规则、回测口径）。

```json
{
  "items": [ {
    "key": "trend", "name": "趋势狙击", "category": "trend",
    "description": "自上而下找赛道 + 基本面筛选 + 技术面确认…",
    "source": "TrendSniper《趋势策略.md》",
    "regime": "适用于指数在 MA20 上方…",
    "min_bars": 130,
    "params": { "min_score": 60.0, "new_high_window": 60 },
    "param_defaults": { "min_score": 60.0 },
    "param_hints": { "min_score": {"label": "入选评分门槛", "unit": "分"} },
    "user_overrides": {},
    "rules": [ { "name": "均线多头排列", "passed": true, "threshold": "价 > MA20 > MA60 > MA120",
                 "weight": 0.2 } ],
    "backtest": { "stop_loss_pct": 8.0, "take_profit_pct": 0.0,
                  "max_hold_days": 30, "break_ma": 20 }
  } ],
  "order": ["trend", "quiet_rise", "limit_up_pullback", "n_pattern", "pattern"]
}
```

### `GET /api/strategies/{key}`

单个策略的完整文档。`pattern` 策略额外带 `patterns`（16 套形态清单）。

### `POST /api/strategies/{key}/params`

保存参数覆盖。**只接受该策略声明的键**，未知键会被拒绝并明确告知。

```json
// 请求
{"min_score": 70, "bogus_key": 1}

// 响应
{"strategy": "trend", "params": {"min_score": 70.0},
 "saved": ["min_score"], "rejected": ["bogus_key"]}
```

### `DELETE /api/strategies/{key}/params`

恢复默认参数。

### `POST /api/strategies/{key}/scan?refresh=false&limit=100&persist=true`

全市场扫描。请求体可选传参数覆盖。

```json
{
  "strategy": "quiet_rise", "trade_date": "2026-09-14",
  "total_evaluated": 5913, "passed_count": 164, "duration_ms": 41230.5,
  "fetcher": { "budget": 1200, "used": 1200, "disk_hits": 4713,
               "network_hits": 1200, "failures": 3 },
  "context": { "env_gate": "full", "benchmark_ret20": 2.31 },
  "warnings": ["本轮日线请求已达配额 1200…"],
  "items": [ {
    "strategy": "quiet_rise", "code": "600519", "name": "贵州茅台",
    "score": 82.86, "passed": true,
    "passed_count": 11, "total_count": 14, "pass_ratio": 0.7857,
    "reasons": [ { "name": "温和放量", "passed": true, "value": 1.42,
                   "threshold": "1.0 ~ 2.5", "weight": 0.2,
                   "detail": "5日均量/20日均量" } ],
    "metrics": { "trend_score": 75.0, "volume_score": 88.0,
                 "change_20d": 13.83, "atr_pct": 2.5 },
    "stop_loss": 60.02, "take_profit": 0.0,
    "entry_low": 56.2, "entry_high": 57.3,
    "tags": ["潜涨", "板块共振"], "note": ""
  } ]
}
```

> **首次扫描较慢**（需要拉取日线建立缓存），之后走磁盘缓存会快很多。
> `fetcher` 字段告诉你有多少走了磁盘、多少走了网络、配额用了多少。

### `POST /api/scan/all?refresh=false&per_strategy=20&persist=true`

一次跑完全部策略（共享同一份市场上下文与日线缓存）。

```json
{"results": { "trend": { "strategy": "trend", "total_evaluated": 5913 } },
 "order": ["trend", "quiet_rise", "limit_up_pullback", "n_pattern", "pattern"]}
```

> ⚠️ **上面两个同步扫描接口是阻塞的**，界面不要用它们。单个策略实测耗时 **158 秒**、
> 全策略约 **226 秒**；请求期间连接一直空转，反向代理 / 浏览器的空闲超时会把连接掐断，
> 前端只能显示"无法连接到后端服务"（用户实际遇到过的故障）。
> 界面走下面的异步任务接口。同步接口保留给脚本与 curl 使用。

### `POST /api/scan/jobs?strategy=all&refresh=false&limit=200&per_strategy=20&persist=true`

**创建异步扫描任务**，毫秒级返回（实测 0.2s），扫描在后台执行。

`strategy` 传策略 key 跑单个策略，传 `all` 逐个跑完全部策略。
`params`（JSON body）仅在单策略模式下生效，用于临时覆盖参数。

```json
{"code": 0, "message": "扫描任务已创建",
 "data": {"job": {"id": "7839670ab29a4496", "kind": "trend", "status": "queued",
                  "percent": 0.0, "total": 1, "done": 0, "current": "", "detail": "",
                  "elapsed_seconds": 0.0, "error": ""},
          "poll": "api/scan/jobs/7839670ab29a4496",
          "hint": "扫描在后台执行；请轮询 poll 字段获取进度与结果。"}}
```

### `GET /api/scan/jobs/{job_id}?result=true`

轮询任务进度与结果。`status` 取值：`queued` / `running` / `succeeded` /
`failed` / `cancelled`；进入 `succeeded`/`failed`/`cancelled` 后 `terminal` 为 `true`。

| 字段 | 说明 |
|---|---|
| `percent` | 0~100。单策略模式下按**已评估标的数**推进（如 `74.8`），`all` 模式按策略数折算 |
| `detail` | 细粒度说明，如 `trend: 已评估 4425/5913 只`；终态清空 |
| `current` | 当前正在跑的策略 key |
| `total` / `done` | `all` 模式下为策略总数 / 已完成数 |
| `elapsed_seconds` | 任务耗时 |
| `result` | 终态时的业务结果（与同步接口的 `data` 同构）；`result=false` 可只取进度 |

> 任务状态保存在进程内存，应用重启后历史任务丢失；但扫描结果在
> `persist=true` 时已落库，可用 `GET /api/strategies/{key}/last` 取回。

### `GET /api/scan/jobs?limit=10`

最近的任务列表（**不含 `result` 结果体**，避免列表接口体积失控）与正在运行的任务 id。

### `POST /api/scan/jobs/{job_id}/cancel`

请求取消任务；已进入终态则原样返回。

### `GET /api/strategies/{key}/last?date=`

最近一次落库的扫描结果。`date` 留空取最新。

### `POST /api/strategies/{key}/evaluate`

对单只股票跑某个策略 —— 个股详情页的"为什么入选/未入选"就用它。

```json
// 请求
{"code": "600519", "params": null}

// 响应（与 scan 的 items 结构一致，额外带 context）
{"strategy": "trend", "code": "600519", "score": 71.0, "passed": true,
 "reasons": [...], "metrics": {...}, "stop_loss": 1180.2,
 "context": {"env_gate": "full"}}
```

### `POST /api/backtest`

```json
// 请求
{
  "strategy": "trend",
  "params": { "min_score": 65 },
  "codes": ["600519", "300750"],   // 可选；留空则按成交额取前 limit 只
  "days": 250,                      // 回看交易日，120~800
  "limit": 120,                     // 标的数量上限，1~800
  "fill": "next_open",              // next_open（默认，无未来函数）| close
  "max_positions": 5,
  "position_pct": 0.2,
  "min_score": 65
}

// 响应
{
  "strategy": "trend", "start_date": "2025-09-15", "end_date": "2026-09-14",
  "universe_size": 118, "fill_mode": "next_open",
  "cost_model": { "commission_rate": 0.00023, "stamp_duty_rate": 0.0005,
                  "slippage_rate": 0.0015 },
  "metrics": {
    "trade_count": 305, "closed_count": 305, "win_count": 142, "loss_count": 163,
    "win_rate": 0.4656, "avg_win_pct": 9.12, "avg_loss_pct": -4.83,
    "payoff_ratio": 1.888, "profit_factor": 1.645,
    "expectancy_pct": 1.588, "total_return_pct": 42.13,
    "annualized_return_pct": 41.8, "max_drawdown_pct": 12.4,
    "calmar": 3.37, "sharpe": 1.84, "sortino": 2.61,
    "avg_hold_days": 8.4,
    "by_exit_reason": { "止损": {"count": 120, "win": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": -6.2, "pnl_sum": -744.0} }
  },
  "trades": [ { "code": "600519", "entry_date": "2026-01-05",
                "entry_price": 1500.2, "exit_date": "2026-01-20",
                "exit_price": 1620.5, "pnl_pct": 7.9, "pnl": 12030.0,
                "hold_days": 11, "exit_reason": "目标止盈",
                "max_gain_pct": 9.2, "max_loss_pct": -1.1, "score": 78.5 } ],
  "trade_count": 305, "equity_curve": [ { "date": "2025-09-15",
                                          "equity": 1000000.0, "positions": 0 } ],
  "warnings": ["样本仅 305 笔…"],
  "requested": 120, "available": 118, "failed": 2
}
```

**回测口径（必须了解）**：

| 项 | 说明 |
|---|---|
| 成交时点 | 默认 `next_open`（信号在 t 日收盘确认，t+1 日开盘成交）；`close` 为信号日收盘成交，**存在轻微前视偏差**，页面会提示 |
| T+1 | 买入当日不可卖出 |
| 成本 | 佣金 0.023%（双向）+ 印花税 0.05%（仅卖出）+ 滑点 0.15%（双向） |
| 离场优先级 | 止损 → 移动止盈 → 目标止盈 → 破线 → 时间止损 |
| 保守处理 | 同一根 K 线内止损与止盈同时触发时**一律按止损成交** |
| 一字板 | 开盘即涨停且最高=最低时无法买入 |
| 年化 | 按 244 个交易日 |

### `GET /api/signals?strategy=&limit=100`

信号流水（每次扫描后写入）。

---

## 五、数据源

### `GET /api/datasources`

数据源总览。

```json
{
  "mode": "auto",
  "categories": {
    "kline": {
      "locked": null, "last_used": "tencent",
      "configured_order": ["tencent", "sina", "ths", "eastmoney"],
      "effective_order": ["tencent", "sina", "ths", "eastmoney"],
      "has_fallback": true,
      "providers": [ { "alias": "tencent", "rank": 1, "active": true,
                       "label": "腾讯财经", "attempts": 421, "successes": 419,
                       "failures": 2, "success_rate": 0.9952,
                       "avg_latency_ms": 182.4, "health_score": 96.8,
                       "consecutive_failures": 0, "cooling": false,
                       "last_error": "" } ]
    }
  },
  "providers": [ { "name": "eastmoney", "label": "东方财富", "installed": true,
                   "usable": true, "capabilities": ["snapshot", "quote", ...],
                   "priority": 90, "requires_login": false, "logged_in": true,
                   "note": "...", "homepage": "...",
                   "metrics": { "attempts": 812, "success_rate": 0.99,
                                "health_score": 95.2 } } ],
  "recent_switches": [ { "time": "14:03:11", "category": "kline",
                         "from": "tencent", "to": "sina",
                         "reason": "腾讯全部K线入口失败: 上游返回 HTTP 501" } ],
  "blocked_hosts": { "vip.stock.finance.sina.com.cn": 574.9 },
  "cache": { "snapshot": { "hit_rate": 0.98 }, "kline": { "hit_rate": 0.95 } },
  "locks": {}, "disabled": [],
  "capability_labels": { "kline": { "label": "历史K线", "method": "fetch_kline" } }
}
```

### `GET /api/datasources/{capability}/candidates`

某个能力的候选源与锁定状态。

### `POST /api/datasources/{capability}/lock`

手动锁定某能力到指定源。`alias` 传空字符串表示恢复自动择优。

```json
// 请求
{"alias": "sina"}

// 响应
{"capability": "kline", "previous": null, "locked": "sina"}
```

**会持久化**，重启后依然生效。

### `POST /api/datasources/unlock-all`

全部恢复自动择优。

### `POST /api/datasources/{name}/toggle`

临时禁用/启用某个源（配置保留，不参与调度）。

```json
{"enabled": false}
→ {"name": "tencent", "enabled": false, "disabled": ["tencent"]}
```

### `POST /api/datasources/probe`

批量连通性探测（**会发起真实网络请求**，可能较慢）。

```json
// 请求
{"capability": "quote"}

// 响应
{"capability": "quote", "ok_count": 3, "total": 10,
 "items": [ { "name": "tencent", "label": "腾讯财经", "ok": true,
              "latency_ms": 182.4, "message": "连通正常" } ]}
```

### `POST /api/datasources/{name}/probe`

单源探测。

### `POST /api/datasources/refresh`

**手动刷新数据**：清空缓存 + 复位熔断器 + 重新拉取指定能力。

```json
// 请求
{"capability": "snapshot", "force": true}

// 响应
{"capability": "snapshot", "source": "eastmoney",
 "attempts": [{"source": "eastmoney", "ok": true, "latency_ms": 7812.3}],
 "cleared_cache_entries": 4213, "reset_breakers": 1, "items": 5913, "force": true}
```

### `POST /api/datasources/reset-breakers`

只复位熔断器与缓存，不拉数据。

### `GET /api/datasources/{name}/endpoints`

查看某源当前实际使用的上游地址（含用户覆盖）。

```json
{"name": "tencent", "label": "腾讯财经",
 "capabilities": ["quote", "kline", ...],
 "urls": { "kline": ["https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"] },
 "customized": false}
```

### `PUT /api/datasources/{name}/endpoints`

**手工覆盖上游地址**（每行一个，第一个失败自动试下一个）。传空数组恢复内置地址。

```json
// 请求
{"kline": ["https://my-mirror/appstock/app/newfqkline/get"]}
```

### `GET /api/datasources/{name}/credentials`

查看登录凭据（**只返回掩码**）。

```json
{"name": "xueqiu", "requires_login": true, "configured": true,
 "fields": {"cookie": "xq_a_tok********wxyz"},
 "editable": true,
 "hint": "请在浏览器登录目标站点后，从开发者工具 Network 面板复制完整 Cookie 串粘贴到此处…"}
```

### `POST /api/datasources/{name}/credentials`

保存登录凭据，**保存后自动用真实请求验证**。

```json
// 请求
{"cookie": "xq_a_token=abcdefg…"}

// 响应
{"name": "xueqiu", "saved_fields": ["cookie"],
 "verified": {"ok": true, "latency_ms": 320.1, "message": "Cookie 有效, 抓取成功"}}
```

### `DELETE /api/datasources/{name}/credentials`

清除凭据。

### `POST /api/datasources/mode`

切换数据源模式。

```json
// 请求
{"mode": "synthetic"}     // auto | real | synthetic

// 响应
{"mode": "synthetic", "synthetic_allowed": true,
 "note": "synthetic 仅用于离线演示, 展示的是合成数据, 不代表真实行情。"}
```

---

## 六、设置

### `GET /api/settings`

完整设置视图（敏感项已掩码）。

```json
{
  "server": { "host": "0.0.0.0", "port": 8770, "cors_origins": [] },
  "quotas": { "universe_size": 0, "scan_kline_budget": 1200,
              "memory_soft_limit_mb": 800, "memory_hard_limit_mb": 1400 },
  "push": { "enabled": false, "wecom_webhook": "",
            "wecom_webhook_configured": false,
            "schedules": "09:00,11:35,15:05", "min_signal_score": 75.0 },
  "news": { "retention_days": 15 },
  "scheduler": { "enabled": true, "trading_interval_seconds": 30 },
  "data_sources": { "mode": "auto", "locked": {}, "custom_urls": {},
                    "credentials": {}, "credential_providers": [] },
  "auth": { "required": false, "admin_key_configured": false },
  "editable_paths": ["quotas.universe_size", ...],
  "defaults": { },
  "runtime": { "session": {}, "now": "...",
               "restart_required_paths": ["server.port", "server.host", "auth.required"],
               "push_stats": { "total": 26, "success": 26 },
               "scheduler": { "running": true } }
}
```

### `PUT /api/settings`

保存设置。**按白名单写入**，越权字段被拒绝。

```json
// 请求
{"quotas": {"scan_kline_budget": 1500}, "evil": {"hack": 1}}

// 响应
{"accepted": ["quotas.scan_kline_budget"], "rejected": ["evil.hack"],
 "restart_required": []}
```

### `GET /api/settings/editable`

可写路径清单与"需重启才生效"的路径。

### `POST /api/settings/reset`

恢复默认。`section` 为空表示全部恢复。

```json
{"section": "news"}    // 或 ""
```

### `GET /api/settings/export?include_secrets=false`

导出设置（JSON 文件下载）。默认**不含**敏感项。

### `POST /api/settings/import`

导入设置（走同一套白名单校验）。

### `GET /api/settings/backup` / `POST /api/settings/backup/restore`

完整备份（含敏感项）/ 从备份恢复。

### `POST /api/settings/push/test`

发送测试推送到企业微信。支持"先测再存"：临时传入 `webhook` 只验证一次而不写配置。

```json
// 请求
{"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx", "title": ""}

// 响应
{"ok": true, "attempts": 1, "skipped": false, "error": "", "errcode": 0}
```

失败时返回 502，`message` 里带错误码与中文解释（如"Webhook 地址无效或机器人已被移除"）。

### `GET /api/settings/push/log?limit=50`

推送日志与统计。

```json
{"items": [ { "kind": "market_emotion", "title": "市场情绪与周期提醒",
              "ok": true, "error": "", "time": "2026-09-14 11:35:02" } ],
 "stats": { "configured": true, "enabled": true, "total": 26,
            "success": 26, "last_at": 1789358702.1, "last_ok": true,
            "last_error": "" }}
```

### `POST /api/settings/push/market`

立即推送一次市场情绪摘要（不等定时时刻）。

---

## 七、用户数据与导出

### `GET /api/watchlist` / `POST /api/watchlist` / `DELETE /api/watchlist`

```json
// POST 请求
{"code": "600519", "name": "贵州茅台", "note": "关注", "tags": ["趋势"]}

// DELETE 请求（JSON body）
{"codes": ["600519"]}
```

### `POST /api/watchlist/batch`

```json
{"codes": ["600519", "300750", "bad"]}
→ {"added": ["600519", "300750"], "failed": ["bad"]}
```

### `PUT /api/watchlist/{code}/note`

```json
{"note": "等待回踩 MA20"}
```

### `GET /api/portfolio?status=`

模拟持仓列表。`status` ∈ `` / `open` / `closed`

```json
{"items": [ { "id": 1, "code": "600519", "price": 1278.5, "shares": 100,
              "status": "open", "opened_at_text": "2026-09-14 10:20" } ],
 "open_count": 1, "closed_count": 5, "win_rate": 0.6,
 "avg_pnl_pct": 3.2, "total_pnl_pct": 16.0}
```

### `POST /api/portfolio/open`

```json
{"code": "600519", "name": "贵州茅台", "price": 1278.5, "shares": 100,
 "reason": "潜涨评分 82", "strategy": "quiet_rise"}
```

### `POST /api/portfolio/{id}/close`

```json
{"price": 1350.0, "reason": "到达目标位"}
→ {"id": 1, "close_price": 1350.0, "pnl_pct": 5.59}
```

### `DELETE /api/portfolio/{id}`

删除持仓记录。

### `GET /api/export/signals.csv` / `GET /api/export/watchlist.csv`

直接下载 CSV（带 UTF-8 BOM，Excel 可直接打开）。

### `POST /api/export/scan.csv`

把一个扫描结果导出为 CSV。

```json
{"items": [ { "code": "600519", "name": "贵州茅台", "score": 82.86,
              "passed": true, "passed_count": 11, "total_count": 14,
              "reasons": [ {"name": "温和放量", "passed": true} ] } ],
 "filename": "我的选股结果.csv"}
```

---

## 八、前端页面路由

| 路由 | 页面 |
|---|---|
| `#/dashboard` | 仪表盘 |
| `#/market` | 行情中枢 |
| `#/emotion` | 情绪周期 |
| `#/screener` | 策略选股（可用 `#/screener?strategy=trend` 直接定位） |
| `#/stock?code=600519` | 个股详情 |
| `#/backtest` | 回测分析 |
| `#/datasources` | 数据源 |
| `#/news` | 资讯公告 |
| `#/memory` | 内存监控 |
| `#/settings` | 用户配置 |
| `#/system` | 系统状态 |
| `/selfcheck` | 部署自检（独立页面） |

---

## 九、错误码速查

| 错误码 | 场景 | 处理建议 |
|---|---|---|
| `code=1, 400` | 参数不合法（未知数据源名、非法端口、未知分组） | 按 `message` 修正参数 |
| `code=1, 404` | 资源不存在（未知策略 key、持仓不存在） | 检查路径参数 |
| `code=1, 422` | Pydantic 校验失败（如 `limit` 超范围） | 检查查询参数范围 |
| `code=1, 500` | 服务器内部错误 | 看日志 `journalctl -u stock-space -n 60` |
| `code=1, 502` | 企业微信推送失败 | 按返回的 `errcode` 处理 |
| `code=503` | 数据能力全部源不可用 | 到「数据源」页看健康度、探测、切换源 |
| `code=503`（快照） | 全市场快照获取失败 | 检查服务器出网；跑 `tools/probe_sources.py` |

### 企业微信常见错误码

| errcode | 含义 |
|---|---|
| 0 | 成功 |
| 93000 | Webhook 地址无效或机器人已被移除 |
| 40001 | Webhook key 不正确 |
| 45009 | 接口调用超过限制（每个机器人每分钟 20 条） |
| 301002 | 无权限操作该机器人 |
