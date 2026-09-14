# 四个 Python 股票项目 · 上游数据源与网络层技术提取报告

> 只读审计（未修改/新建任何被审计项目的文件）。覆盖 `92KeBi`、`TrendSniper`、`NPatternStrategy`、`Limit-Up-Pullback-Buy-Setup`。
> 全部四个项目的网络出口均为**免费公开 HTTP 接口 + 标准库 `urllib`**（仅 Limit-Up 用 `httpx`）；**没有任何项目引入 AKShare / Ashare / tushare / baostock**（四个 `requirements.txt` 均已核对，Limit-Up 明确写「刻意不引入 akshare（体积大且接口易碎）」）。

## 0. 数据源分布总览

| 数据源 | 92KeBi | TrendSniper | NPatternStrategy | Limit-Up |
|---|---|---|---|---|
| 新浪 | ✅ 实时/全量列表/板块树/日K/5分钟K/资金流 | ✅ 全量快照/日K/指数K | ✅ 实时/列表/日K | ✅ 实时/列表/日K |
| 腾讯 | ✅ 实时/日K | ✅ 日K(主)/实时 | ✅ 实时/日K(主)/列表 | ✅ 实时/日K/列表/行业板块 |
| 东方财富 | ✅ 全量快照/行业成分/大宗交易 | ✅ 快照(主)/日K/财报 | ✅ 实时/列表/日K | ✅ 实时/列表/日K |
| 同花顺 | — | — | — | ✅ 日K/分时 |
| 网易 | ✅ 实时(第三源) | — | — | — |
| 雅虎 | — | — | — | ✅ 日K(境外兜底) |

---

## 1. 新浪财经

### 1.1 批量实时行情 `hq.sinajs.cn`
**URL**：`https://hq.sinajs.cn/list=sh600519,sz000001,bj920000`（代码带 sh/sz/bj 前缀，逗号连接）
**请求**：GET；**必须带 `Referer: https://finance.sina.com.cn`，否则 403**；返回 **GBK/GB18030** 文本，每行 `var hq_str_sh600519="贵州茅台,开,昨收,最新,高,低,...,日期,时间,...";`

字段下标（Limit-Up `sina.py:130-142` 实测确认，共 34 字段，`SINA_MIN_FIELDS=32`）：

| 下标 | 语义 | 下标 | 语义 |
|---|---|---|---|
| 0 | 名称 | 9 | 成交额(元) |
| 1 | 今开 | 30 | 日期 `YYYY-MM-DD` |
| 2 | 昨收 | 31 | 时间 `HH:MM:SS` |
| 3 | 最新价 | 8 | 成交量(**股**，需 ÷100 转手) |
| 4/5 | 最高/最低 | | |

**坑**：接口**不提供换手率**（Limit-Up 填 0「不伪造」）与涨跌幅（各项目自行算 `(最新-昨收)/昨收`）。分块大小各家不同：92KeBi `HQ_CHUNK=380`（注释「更大易触发 HTTP 431」）、NPatternStrategy 500、TrendSniper/Limit-Up 60。92KeBi 还会取腾讯的 `f[7]`/`f[8]`（外盘/内盘）算「主动净买」。

### 1.2 全市场 A 股列表 `Market_Center.getHQNodeData`
**URL**：`https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={页}&num=100&sort=symbol&asc=1&node={节点}&symbol=&_s_r_a=page`
参数：`node` 取值 `hs_a`(沪深京A)、`sh_a`(沪主板+科创)、`sz_a`(深主板+创业)、`cyb`、`kcb`；TrendSniper/NPatternStrategy 用 `_s_r_a=page|init`；Limit-Up 支持 `sort=amount&asc=0` 取成交额降序前 N。
返回 JSON 数组，字段：`code/symbol/name/trade/settlement/open/high/low/changepercent/pricechange/turnoverratio/volume/amount/mktcap(万元)/nmc(流通市值,万元)/per(PE)/pb`。

**降级**：TrendSniper/NPatternStrategy 用正则 `\[.*\]` 剥 JSONP 前缀；TrendSniper 10 页并发 `workers=8`；NPatternStrategy 并发仅 4（「降低被限流概率」）。**新浪不提供北交所列表（`node=bj_a` 返回空）**。

### 1.3 板块树 `Market_Center.getHQNodes`（仅 92KeBi）
`https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes`，递归遍历嵌套 list，叶子 `[name, child, id]`，`new_*`=行业(49个)、`gn_*`=概念。

### 1.4 日K / 分钟K（三种等价域名，注意差异）
- `https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=520`（92KeBi）
- `https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData?...`（TrendSniper）
- `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=250`（NPatternStrategy `sina_kline`、Limit-Up `KLINE_URL`）

参数：`scale=240` 即日线（**92KeBi 用 `scale=5` 取 5 分钟 K**，`datalen=800`）；`ma=no` 不返回均线；`datalen` 根数（Limit-Up 上限 1000，实现里夹到 450）。字段 `{day,open,high,low,close,volume}`，**`volume` 单位是股，需 ÷100 转手**。
**关键坑（Limit-Up docstring 明确记录）**：该接口是**不复权**数据，而腾讯是**前复权 qfq**，除权日附近有跳空；建议「同一次扫描只用同一个源」。新浪日线**无成交额/换手率**，框架按 `volume*100*close` 估算。

### 1.5 个股资金流（仅 92KeBi `real/moneyflow.py`）
`https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?daima=sh600519`（`daima`=带前缀代码，需 Referer），返回按交易日数组（最新在前），解析 `netamount`(净流入,元)、`r0_net`(超大单)、`ratioamount` 反推成交额 `amt=|net/ratio|`、`opendate`、`trade`。

---

## 2. 腾讯财经

### 2.1 批量实时行情 `qt.gtimg.cn`
**URL**：`https://qt.gtimg.cn/q=sh600519,sz000001`；备用域名 `https://web.sqt.gtimg.cn/q=`（NPatternStrategy 用于「域名级故障冗余」）。
**请求**：GET，无 Referer/Cookie；返回 **GBK/GB18030** 文本 `v_sh600519="1~贵州茅台~600519~1275.16~...";`，以 `~` 分隔（Limit-Up 实测 **88** 字段，`QT_MIN_FIELDS=49`；92KeBi 要求 `len(f)>=50`）。

字段索引（Limit-Up `tencent.py:136-156` 逐字）：

| 下标 | 语义 | 下标 | 语义 |
|---|---|---|---|
| 0 | 市场(1=沪,51=深) | 32 | 涨跌幅(%) |
| 1 / 2 | 名称 / 6位代码 | 33 / 34 | 最高 / 最低 |
| 3 / 4 / 5 | 最新 / 昨收 / 今开 | 37 | 成交额(**万元**,×10000→元) |
| 6 | 成交量(**手**) | 38 / 43 | 换手率(%) / 振幅(%) |
| 30 | 时间 `YYYYMMDDHHMMSS` | 44 / 45 | 流通市值 / 总市值(亿元) |
| 31 | 涨跌额 | 46 / 47 / 48 | 市净率 / 涨停价 / 跌停价 |

92KeBi 额外取 `f[7]`/`f[8]`=外盘/内盘、`f[39]`=PE。**分块 92KeBi `CHUNK=900`，其余项目 60**（NPatternStrategy/TrendSniper/Limit-Up）。腾讯是唯一**直接提供涨停价/跌停价**的源。

### 2.2 日K（多个等价入口，需入口级故障转移）
- `https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,520,qfq`（92KeBi）
- `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=...`（TrendSniper、Limit-Up 首选）
- `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get`（NPatternStrategy 首选，注释「无 WAF」）
- Limit-Up `KLINE_BASES` 四入口回退：`web.ifzq.gtimg.cn/appstock/app/fqkline/get` → `proxy.finance.qq.com/ifzqgtimg/...` → `ifzq.gtimg.cn/...` → `web.ifzq.gtimg.cn/appstock/app/newfqkline/get`

`param` 六段：`{symbol},day,{start},{end},{count},qfq`。解析 `data.<symbol>.qfqday`（前复权）或回退 `.day`（指数只有 `day`）。
**最容易写错的点（Limit-Up 用测试交叉验证）**：bar 数组顺序是 **`[日期, 开, 收, 高, 低, 成交量(手)]`，不是 OHLC**。

### 2.3 排行榜/股票列表 + 行业板块（Limit-Up、NPatternStrategy 未用板块）
- 排行榜：`https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=aStock&sort_type=turnover&direct=down&offset=0&count=100`（`sort_type=turnover&direct=down` 即成交额降序，单位万元，`offset` 分页，`count=200` 可用、**`count=500` 返回空**，无需 Referer）
- 行业板块清单：`https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy&sort_type=price&direct=down&offset=0&count=200`
- 板块成分：同排行榜接口 `?board_code={板块号}&sort_type=turnover&direct=down&offset=0&count=100`（Limit-Up `industry.py`，`MEMBERS_PER_BOARD=100`，`CACHE_TTL=86400`）

---

## 3. 东方财富（push2 / push2his / datacenter-web）

### 3.1 全市场列表与快照 `clist/get`
**URL**：`https://push2.eastmoney.com/api/qt/clist/get?pn={页}&pz={每页}&po=1&np=1&fltt=2&invt=2&fid=f12&fs={筛选串}&fields={字段}&ut={令牌}`
- `fs` 全市场串（各项目一致）：`m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048`（深主板/创业板/沪主板/科创板/北交所）
- 92KeBi 用 `pz=200`、`fid=f12`、`fields=f12,f13,f14,f2,f3,f15,f16,f17,f18,f5,f6,f8,f20,f21`
- TrendSniper 按交易所分 5 段抓取，`fields=f12,f14,f2,f3,f5,f6,f8,f9,f10,f15,f16,f18,f20,f21,f23,f62,f100,f184`，`ut=bd1d9ddb04089700cf9c27f6f7426281`，并用 `hosts=["https://push2delay.eastmoney.com", "https://push2.eastmoney.com"]` 做**主机回退**

字段语义：`f12`代码 `f13`市场号 `f14`名称 `f2`最新 `f3`涨跌% `f5`量 `f6`额 `f8`换手 `f9`PE `f10`量比 `f15/16`高/低 `f17`今开 `f18`昨收 `f20`总市值 `f21`流通市值 `f23`PB `f62`主力净流入 `f100`行业名 `f184`主力净流入占比。

> **Limit-Up 内部有两套并行的东财实现，地址相同但覆盖范围不同**：`providers/eastmoney.py`（旧 `BaseProvider`，`FS_ALL_A="m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"` **不含北交所**，且 `_board_of()` 只认 `("8","4")` 前缀 → `920xxx` 被误判为主板/SH，`fid="f12"`、`pz=200`、日K 带 `beg=0`）与 `providers/eastmoney_source.py`（新 `MarketSource`，`MARKET_FS` **含北交所**，`fid="f6"` 成交额降序、`pz=2000`、日K 无 `beg`）。`fid`/`po`/`np`/`fltt`/`invt` 的精确语义代码中**无注释**，仅确认为原样透传的固定参数。

### 3.2 批量行情 `ulist.np/get`、单只 `stock/get` 与 日K `stock/kline/get`
- 批量：`https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&secids=1.600519,0.000001&fields=f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124`（`secids`=`市场码.代码`，**沪=1、深/北=0**；`f124`=行情时间戳(秒)；`ut=fa5fd1943c7b386f172d6893dbfba10b`）
- 单只：`https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f170,f58,f169&fltt=2&invt=2`（Limit-Up 旧实现用，`f43`=最新价、`f170`=涨跌幅、`f58`=名称、`f169`=涨跌额）
- 日K：`https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg=20240101&end=20500101&lmt=250`
  `klt=101` 日线；`fqt` 0=不复权 / 1=前复权（NPatternStrategy 默认 0，复权时改 1）。`klines` 每行 `'日期,开,收,高,低,成交量(手),成交额(元),振幅,涨跌幅,涨跌额,换手率'`

### 3.3 数据中心 `datacenter-web.eastmoney.com/api/data/v1/get`
- **财报（TrendSniper）**：`reportName=RPT_LICO_FN_CPD&columns=ALL&pageNumber={页}&pageSize=500&sortColumns=SECURITY_CODE&sortTypes=1&filter=(REPORTDATE='2026-06-30')`；解析 `SECURITY_CODE / REPORTDATE / WEIGHTAVG_ROE(ROE) / SJLTZ(净利同比) / YSTZ(营收同比) / BASIC_EPS / BOARD_NAME(行业)`。取最新报告期用 `filter=(ISNEW="1")&sortColumns=REPORTDATE&sortTypes=-1&pageSize=1`
- **大宗交易（92KeBi）**：`reportName=RPT_BLOCKTRADE_STA&columns=ALL&pageSize=100&pageNumber={页}&source=WEB&client=WEB&sortColumns=TRADE_DATE&sortTypes=-1&filter={flt}`，`flt=(SECURITY_CODE="600519")(TRADE_DATE>='2025-09-11')`；解析 `VOLUME`(万股) `DEAL_AMT`(万元) `PREMIUM_RATIO`(折溢价,负=折价) `AVERAGE_PRICE` `CLOSE_PRICE` `D1/D5/D10_CLOSE_ADJCHRATE`。需 `Referer: https://data.eastmoney.com/`

---

## 4. 同花顺（仅 Limit-Up）

**URL**（`ths.py:41-44` 逐字）：
- 日K：`https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js`
- 分时：`https://d.10jqka.com.cn/v6/time/hs_{code}/last.js`
- **必须 `Referer: https://stockpage.10jqka.com.cn/`**；代码段统一 `hs_{code}`（不分交易所）

**返回**：JSONP `quotebridge_v6_line_hs_600519_01_last({...})`，用正则 `^\s*[A-Za-z_][\w.]*\s*\((.*)\)\s*;?\s*$` 剥壳，`data` 字段是**分号分隔**的字符串。
日线 11 字段实测：`日期,开,高,低,收,成交量(股,÷100→手),成交额,换手%,?,?,?`（`MIN_FIELDS=8`；**第 9/10/11 字段语义未确认，按「不猜测」原则忽略**）。
分时 `data="0930,1285.15,41150503,1285.150,32020;..."` 每条 `时间,价格,成交额,均价,成交量`，取最后一条价格为最新价；`node.name`=中文名、`node.pre`=昨收。

**定位与坑**：`supports_stock_list=False`、`supports_index=False`；日线**固定只返回最近约 140 根**（`days` 被忽略）；分时**不提供成交量/成交额/换手率**（统一填 0）。价值在于它是**唯一能在东财被阻断、新浪被限流时仍稳定提供科创板+北交所日线的免费源**。

## 5. 雅虎财经（仅 Limit-Up，境外兜底）

**URL**：`https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d`，其中 `{symbol}`=`600519.SS`（沪）/`300750.SZ`（深）。
**请求**：GET，无 Referer/Cookie/token，JSON。解析 `chart.result[0].timestamp` 与 `indicators.quote[0]` 的 `open/high/low/close/volume` **等长对齐**，任一为 null 的行跳过（停牌）；日期 `fromtimestamp(ts, tz=utc)`；**volume 是股，÷100 转手**。
**坑**：**不提供北交所**（`920000.BJ` 返回 404，代码直接抛错不发请求）；不提供成交额/换手率；深市指数覆盖差（`399006.SZ` 只有 1 根）。仅在 `DATA_SOURCE_ORDER` 末位（`eastmoney,tencent,ths,sina,yahoo`）作跨网络冗余。

## 6. 网易财经（仅 92KeBi，第三价格源）

**URL**：`http://api.money.126.net/data/feed/{codes}?callback=_ntes_quote_callback`，代码格式 `0+code`=沪、`1+code`=深（**北交所不支持，自动跳过**），单批 300。
返回 JSONP，用 `re.search(r"\((.*)\)\s*;?\s*$", raw, re.S)` 剥壳。字段：`name/price/percent/yestclose/open/high/low/volume/turnover`。

---

## 7. 可复用工具函数（HTTP 重试 / 编码 / 并发 / 缓存 / 多源容错）

### 7.1 HTTP 重试与编码
| 项目 | 函数签名（含路径） | 行为 |
|---|---|---|
| 92KeBi | `providers/sina.py::_http(url, timeout=12, tries=3)`、`providers/tencent.py::_http(url, timeout=10, tries=2)`、`providers/eastmoney.py::_get(params, timeout=12, tries=2)` | 每源各写一份（**无共享模块**）；退避 `sleep(0.3~0.4*(i+1))` |
| TrendSniper | `backend/dataapi.py::_http_text(url, timeout=10, retries=2, headers=None, hosts=None)`、`_http_json(...)` | **`hosts` 主机回退**（替换 `url[url.index("/api"):]` 前缀，用于东财 anti-bot 切镜像）；限流码 `456/403/501/429` 退避 `1.5*(i+1)`，其余 `0.35*(i+1)`；`ssl.CERT_NONE` |
| NPatternStrategy | `server/core/providers.py::http_get_text(url, headers=None, charset="utf-8", timeout=12.0, retries=3) -> str` | 合并默认 UA；`raw.decode(charset)` 失败回退 `utf-8, errors="replace"`；退避 `0.5*(i+1)`；`ssl.CERT_NONE` |
| Limit-Up | `providers/source_base.py::MarketSource.get_json(url, params=None, **kwargs)` / `get_text(url, params=None, encoding="utf-8", **kwargs)` | 基于惰性共享 `httpx.AsyncClient`（`trust_env=False` **忽略环境代理**、`follow_redirects=True`、`connect=min(6.0,timeout)`）；异常统一转 `MarketSourceError`；**本身不重试**，重试交给上层多源切换 |

核心片段（Limit-Up，`providers/source_base.py`）：
```python
async def get_text(self, url, params=None, encoding="utf-8", **kwargs) -> str:
    client = await self.client()
    try:
        resp = await client.get(url, params=params, **kwargs)
        resp.raise_for_status()
        return resp.content.decode(encoding, errors="ignore")
    except httpx.HTTPStatusError as exc:
        raise MarketSourceError(f"{self.name} HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise MarketSourceError(f"{self.name} 请求失败：{type(exc).__name__}: {exc}") from exc
```

### 7.2 并发抓取
`concurrent.futures.ThreadPoolExecutor`：TrendSniper `parallel(items, fn, workers=8)`（**返回与输入同序、异常项为 None**）；92KeBi `fetch_members_fast(threads=8/10)`、`fetch_industry_map(threads=4~8)`；NPatternStrategy 4~8。Limit-Up 用 `asyncio.Semaphore(concurrency)` + `gather`（`daily_kline_many`，单只失败不抛）。

### 7.3 多源容错（三套不同设计，值得复用）
- **92KeBi `providers/router.py`**：`_record(name, ms, ok)` 维护滚动延迟 `deque(maxlen=6)` 与连续失败计数（**失败 2 次标记不健康**）；`_pick(names)` 在健康源里选**平均延迟最小者**，全不健康则重置；`fetch_fast_quotes(symbols)` 返回 `(source, dict)`；`fetch_kline_any(code, n, symbol)` 新浪主/腾讯备并**剔除腾讯当日盘中未收盘K**；`health_status()` 暴露 ok/fails/avg_ms。
- **NPatternStrategy `core/providers.py`**：`chain_order(chain)` 实现**粘滞切换**——最近成功的源优先，`_STICKY_TTL=600` 秒后自动回探主源；`fetch_spot_chain` 对**非首选源先探测 1 只**再批量（「避免失效源拖慢整轮抓取」）。
- **Limit-Up `providers/resilient.py`**：`SourceHealth` 指数退避（连续失败 n 次冷却 `min(120, 3*2^(n-1))` 秒）；`NEVER_SUCCEEDED_THRESHOLD=1`（**从未成功过的源失败 1 次即长期跳过**，避免每次白等连接超时）；`_try_sources(operation, capability, call)` 按能力筛选源，「返回 None 或 len==0 也算失败」；`MIN_BARS=20`（腾讯对北交所只返回 1 根，低于阈值判失败）；`_fetch_klines` 事后归因，避免次新股把全部源判死。

### 7.4 缓存与代码规范化
- `providers/cache.py::FileCache(cache_dir, ttl_seconds=300)`：`get/set/age/invalidate/clear/stats`，key 用 `sha1[:20].json`，写入 `tempfile.mkstemp + os.replace` **原子替换**。
- `providers/klines_cache.py::KlineCache(path, max_days=400)`：SQLite 单表 `bars` 主键 `(code,date)`，WAL + `synchronous=NORMAL`，`upsert_many` 整批一事务。**性能坑（作者实测）**：逐只 SELECT ≈17s，`ROW_NUMBER` 倒排 ≈3.5s，最终改为 `ORDER BY code,date` 走索引；`pd.to_datetime` 必须显式 `format="%Y-%m-%d"`。
- 代码规范化：`normalize_code(raw)`（支持 `600519`/`sh600519`/`600519.SH`）、`market_of_code`、`board_of_code`、`prefixed(code, style)`、`eastmoney_secid(code)`（沪=1.x 深/北=0.x）、`chunked(items, size)`、`limit_pct(board, is_st)`（主板10%/创业科创20%/北交所30%/ST 5%）。
- 其他：`92KeBi/providers/sina.py::limit_rate(code,name)`、`is_limit_up/is_limit_down/is_new_listing`（涨跌停全由**本地规则判定**，四项目均**不调用上游涨停列表接口**）；`TrendSniper/config.py::tx_symbol/em_secid/classify_board`。

---

## 8. 已知的坑与降级策略（汇总）

**编码**：新浪行情/腾讯行情 = GBK 或 GB18030（Limit-Up 明确用 `encoding="gb18030"`）；解码策略各家为 `errors="ignore"`（92KeBi/NPatternStrategy/Limit-Up）或 `errors="replace"`（TrendSniper）。新浪列表与日K为 UTF-8。

**限流与反爬**：
- 新浪 `Market_Center.getHQNodeData` 高频翻页（约 4~5 次/秒、数十页）会返回 **HTTP 456 并被封约 10 分钟**；Limit-Up 对策：`LIST_NODE_DELAY=0.15`、`LIST_PAGE_DELAY=0.45`、`BLOCKED_RETRY_DELAY=3.0`（3s/6s/9s 线性递增，`MAX_BLOCKED_RETRY=3`），仍失败只放弃当前 node；92KeBi 另设 `_IND_BACKOFF=120s` 退避「避免新浪 456 限流风暴」。
- 腾讯日K主入口高频访问返回 **HTTP 501 + JS 校验页**（此时实时接口仍正常）→ 四入口故障转移并记住成功入口；`board_code=ksh/cyb`（科创/创业板块）**首请求后对本 IP 持续返回空数组**（会话级限流）。
- 92KeBi `sina.py` docstring 记录：**「东财 push2 系列接口在本环境被拒连」**，故其主源为新浪、东财仅作行业映射回退。
- Limit-Up `eastmoney_source.py` 记录：东财域名在部分网络环境**在 TLS 层被直接阻断**（`Server disconnected without sending a response`），故 connect 超时压到 4 秒「快速失败」。

**单位与口径**（最易出错）：
- 成交量：新浪 hq = **股**（÷100→手）、新浪日K = **股**、同花顺 = **股**、雅虎 = **股**；腾讯 = **手**、东财 = **手**。
- 成交额：腾讯 `f37` = **万元**（×10000→元）；东财 = 元；新浪 = 元。
- 市值：新浪 `mktcap/nmc` = **万元**（TrendSniper ×1e4）；东财 `f20/f21` = 元；腾讯 `f44/f45` = 亿元。
- 复权：新浪日K = **不复权**；腾讯 `qfq` = 前复权；东财 `fqt=1` = 前复权。混源会有除权日跳空。

**盘中未收盘K**：新浪日K**不返回**当日未收盘K；腾讯**会返回**，故 92KeBi `router.fetch_kline_any` 与 `real/market.get_index_kline` 都显式剔除 `rows[-1]["day"] == today`。

**超时/代理/安全**：
- 超时：8~15s（Limit-Up `http_timeout=10.0`、腾讯日K 10s、新浪 hq 10s、东财 12~15s）；TrendSniper `fetch_all_quotes` 用 `deadline_s=45` **整体时间预算**「东财不可达时不拖慢启动」。
- 代理：92KeBi / TrendSniper / NPatternStrategy **无任何代理配置**；Limit-Up 显式 `trust_env=False` 忽略容器环境代理。
- **TLS 校验被全局关闭**：TrendSniper `dataapi.py` 与 NPatternStrategy `core/providers.py` 均设 `check_hostname=False` + `verify_mode=CERT_NONE`。**无任何 Cookie / token / 签名；仅伪装浏览器 UA + 新浪两个接口的 Referer。**

**降级链**：
- 92KeBi 全市场快照：新浪 → 东财 `fetch_all_quotes` → 内置全市场名单 + 腾讯/新浪 hq → 本地样本池（`_load_members() or _load_universe()`）；行业映射：新浪 → 东财 → 旧缓存 → 仓库内置映射 → 空骨架。
- NPatternStrategy：行情 新浪→腾讯→腾讯备域→东财；日K 腾讯proxy→腾讯web→东财→新浪money；列表 新浪→东财；切换后 10 分钟粘滞。
- Limit-Up：`DATA_SOURCE_ORDER="eastmoney,tencent,ths,sina,yahoo"`，`auto` 模式全失败才降级合成数据，`real` 模式**绝不降级**（接口报 503）。

---

## 9. 无法确定 / 代码中未体现

- **AKShare / Ashare / tushare / baostock**：四个项目**全部未引入**（grep 无命中 + 四个 requirements.txt 双重否证）。
- **资金流**：仅 92KeBi 有（新浪 `MoneyFlow.ssl_qsfx_zjlrqs`）；其余三个项目无。**财报**：仅 TrendSniper 有（东财 `RPT_LICO_FN_CPD`）。**涨停列表**：四个项目**均无上游接口**，全部由本地「行情 + 涨跌幅限制规则」判定。
- **板块（行业/概念）**：92KeBi（新浪 `getHQNodes` + 东财 `m:90+t:2`/`b:BKxxxx`）、TrendSniper（用东财 `f100` 字段）、Limit-Up（腾讯 `board_type=hy`）有；NPatternStrategy **无上游板块接口**，`market.classify()` 仅按代码段本地归类。
- **分钟K**：仅 92KeBi 有（新浪 `scale=5`）。TrendSniper/NPatternStrategy/Limit-Up 的 `scale=240`、`klt=101` 均为日线常量硬编码，**无分钟级接口**。
- `NPatternStrategy` 的 `providers.json.index_codes` 定义了却**无任何代码读取**（死配置，`market.INDEX_SYMBOLS` 硬编码）；该文件 provider 条目**没有 `enabled` / `priority` 键**，顺序完全由 `chains` 数组位置决定，且 `_prov()` 每次调用重新读盘（改 JSON 立即生效）。
- 东财 `fields2` 中 `f58/f59/f60` 语义、腾讯 49~87 号字段语义、同花顺日线第 9/10/11 字段：**代码中未体现/注释明确标注「语义未确认」**。
- 两个可疑点（子代理静态推导，未运行验证）：① `Limit-Up/providers/eastmoney_source.py::index_snapshot` 用 `eastmoney_secid("000001")` 构造指数 secid，因 `market_of_code("000001")→SZ` 得到 `0.000001`（平安银行）而非上证指数 `1.000001`；② `Limit-Up/app/utils.py::market_of_code` 不识别 `920` 新段（920xxx→SZ），与 `source_base.market_of_code`（920→BJ）行为不一致。
