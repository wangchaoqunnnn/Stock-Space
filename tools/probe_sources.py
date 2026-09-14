"""上游数据源可达性诊断(一次性工具, 用于核对端点与解析)。

直接对候选 URL 发真实请求并打印结果, 帮助判断:
  * 该机器/服务器能否访问某个上游;
  * 端点是否变更(404/501/456 等);
  * 解析结果是否符合预期。

用法::

    python tools/probe_sources.py              # 全部探测
    python tools/probe_sources.py kline tencent  # 只探测 Tencent 的 K 线
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: (分组, 名称, URL, 额外请求头)
PROBES: list[tuple[str, str, str, dict[str, str]]] = [
    # ---------------- 腾讯 ----------------
    ("quote", "腾讯批量行情", "https://qt.gtimg.cn/q=sh600519,sz000001",
     {"Referer": "https://gu.qq.com/"}),
    ("kline", "腾讯 web.ifzq fqkline", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,30,qfq",
     {"Referer": "https://gu.qq.com/"}),
    ("kline", "腾讯 proxy newfqkline", "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=sh600519,day,,,30,qfq",
     {"Referer": "https://gu.qq.com/"}),
    ("kline", "腾讯 ifzq.gtimg fqkline", "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,30,qfq",
     {"Referer": "https://gu.qq.com/"}),
    ("kline", "腾讯 web.ifzq newfqkline", "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param=sh600519,day,,,30,qfq",
     {"Referer": "https://gu.qq.com/"}),
    ("rank", "腾讯 getBoardRankList", "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=aStock&sort_type=turnover&direct=down&offset=0&count=10",
     {}),
    ("sector", "腾讯板块 getRank", "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy&sort_type=price&direct=down&offset=0&count=10",
     {}),
    # ---------------- 东方财富 ----------------
    ("snapshot", "东财 clist 全市场", "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f13,f14,f2,f3&ut=bd1d9ddb04089700cf9c27f6f7426281",
     {"Referer": "https://quote.eastmoney.com/"}),
    ("snapshot", "东财 clist 备用域名", "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f2,f3&ut=bd1d9ddb04089700cf9c27f6f7426281",
     {"Referer": "https://quote.eastmoney.com/"}),
    ("kline", "东财 push2his kline", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=10",
     {"Referer": "https://quote.eastmoney.com/"}),
    ("limit_up", "东财涨停池", "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=5&sort=fbt:asc&date=" + time.strftime("%Y%m%d"),
     {"Referer": "https://quote.eastmoney.com/"}),
    ("quote", "东财单只行情", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f57,f58,f169,f170&fltt=2&invt=2&ut=fa5fd1943c7b386f172d6893dbfba10b",
     {"Referer": "https://quote.eastmoney.com/"}),
    ("attention", "东财人气榜", "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
     {"Referer": "https://guba.eastmoney.com/"}),
    # ---------------- 新浪 ----------------
    ("quote", "新浪批量行情", "https://hq.sinajs.cn/list=sh600519,sz000001",
     {"Referer": "https://finance.sina.com.cn/"}),
    ("code_list", "新浪全市场列表(hs_a)", "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=5&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page",
     {"Referer": "https://finance.sina.com.cn/"}),
    ("kline", "新浪 money kline", "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=10",
     {"Referer": "https://finance.sina.com.cn/"}),
    ("kline", "新浪 quotes.sina.cn kline", "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=10",
     {"Referer": "https://finance.sina.com.cn/"}),
    ("news", "新浪 7x24 快讯", "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=5&zhibo_id=152&tag_id=0&dire=f&dpc=1",
     {"Referer": "https://finance.sina.com.cn/7x24/"}),
    # ---------------- 同花顺 ----------------
    ("kline", "同花顺日线 last.js", "https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js",
     {"Referer": "https://stockpage.10jqka.com.cn/"}),
    ("kline", "同花顺日线 today.js", "https://d.10jqka.com.cn/v6/line/hs_600519/01/today.js",
     {"Referer": "https://stockpage.10jqka.com.cn/"}),
    ("kline", "同花顺日线 all.js", "https://d.10jqka.com.cn/v6/line/hs_600519/01/all.js",
     {"Referer": "https://stockpage.10jqka.com.cn/"}),
    ("minute", "同花顺分时", "https://d.10jqka.com.cn/v6/time/hs_600519/last.js",
     {"Referer": "https://stockpage.10jqka.com.cn/"}),
    # ---------------- 巨潮 / 网易 ----------------
    ("announcement", "巨潮公告检索", "http://www.cninfo.com.cn/new/hisAnnouncement/query",
     {"Referer": "http://www.cninfo.com.cn/"}),
    ("quote", "网易财经行情", "http://api.money.126.net/data/feed/0600519,1000001?callback=_ntes_quote_callback",
     {}),
]


def probe(name: str, url: str, headers: dict[str, str], timeout: float = 12.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "ms": (time.perf_counter() - started) * 1000,
                "error": f"HTTP {exc.code} {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "ms": (time.perf_counter() - started) * 1000,
                "error": f"{type(exc).__name__}: {exc}"}
    elapsed = (time.perf_counter() - started) * 1000
    text = raw.decode("utf-8", errors="replace")
    if text.count("\ufffd") > 20:
        text = raw.decode("gb18030", errors="replace")
    return {"ok": True, "status": status, "ms": elapsed, "bytes": len(raw),
            "preview": text[:220].replace("\n", " ")}


def main() -> int:
    filters = [a.lower() for a in sys.argv[1:]]
    rows = []
    for group, name, url, headers in PROBES:
        if filters and not any(f in group.lower() or f in name.lower() for f in filters):
            continue
        result = probe(name, url, headers)
        rows.append((group, name, result))
        flag = "OK  " if result["ok"] else "FAIL"
        detail = result.get("preview", result.get("error", ""))
        print(f"{flag} [{group:12s}] {name:26s} {result['ms']:7.0f}ms  {detail[:150]}")
    ok = sum(1 for _, _, r in rows if r["ok"])
    print()
    print(f"合计 {len(rows)} 项, 可达 {ok} 项, 不可达 {len(rows) - ok} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
