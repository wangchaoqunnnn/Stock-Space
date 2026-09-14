"""核对可用端点的返回结构与字段含义(一次性诊断工具)。"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url: str, headers: dict[str, str] | None = None, encoding: str = "utf-8") -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            status = response.status
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
    return status, raw.decode(encoding, errors="replace")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    # ---- 1) 腾讯 fqkline 的 bar 结构 ----
    section("1) 腾讯 proxy newfqkline —— bar 字段顺序")
    _, text = fetch(
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        "?param=sh600519,day,,,5,qfq",
        {"Referer": "https://gu.qq.com/"},
    )
    try:
        data = json.loads(text)
        node = data["data"]["sh600519"]
        print("node keys:", list(node.keys()))
        for key in ("qfqday", "day"):
            if key in node:
                print(f"  {key}:")
                for bar in node[key][:3]:
                    print("   ", bar)
    except Exception as exc:  # noqa: BLE001
        print("解析失败:", exc, text[:200])

    # ---- 2) 东财 push2delay 快照字段 ----
    section("2) 东财 push2delay clist —— 字段映射核对")
    fields = ("f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
              "f20,f21,f22,f23,f24,f25,f26,f100,f115,f62,f184")
    _, text = fetch(
        "https://push2delay.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=2&po=1&np=1&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        f"&fields={fields}&ut=bd1d9ddb04089700cf9c27f6f7426281",
        {"Referer": "https://quote.eastmoney.com/"},
    )
    try:
        data = json.loads(text)
        print("total:", data["data"]["total"])
        for row in data["data"]["diff"][:2]:
            print(json.dumps(row, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print("解析失败:", exc, text[:200])

    # ---- 3) 东财 push2delay 是否支持带 secids 的 ulist / 单只 ----
    section("3) 东财 push2delay 单只行情 / 指数")
    for label, url in [
        ("stock/get 600519",
         "https://push2delay.eastmoney.com/api/qt/stock/get?secid=1.600519"
         "&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170&fltt=2&invt=2"
         "&ut=fa5fd1943c7b386f172d6893dbfba10b"),
        ("stock/get 上证指数",
         "https://push2delay.eastmoney.com/api/qt/stock/get?secid=1.000001"
         "&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170&fltt=2&invt=2"
         "&ut=fa5fd1943c7b386f172d6893dbfba10b"),
        ("ulist.np 批量",
         "https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
         "&secids=1.600519,0.000001,1.000001&fields=f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18"
         "&ut=fa5fd1943c7b386f172d6893dbfba10b"),
        ("指数 clist (m:1+s:2)",
         "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2"
         "&fid=f3&fs=m:1+s:2&fields=f12,f13,f14,f2,f3,f4&ut=bd1d9ddb04089700cf9c27f6f7426281"),
    ]:
        _, text = fetch(url, {"Referer": "https://quote.eastmoney.com/"})
        print(f"  {label}: {text[:260]}")

    # ---- 4) 东财 push2his 的可用镜像 ----
    section("4) K 线镜像探测")
    kline_q = ("api/qt/stock/kline/get?secid=1.600519&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=3")
    for host in ("push2his.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com",
                 "push2his.eastmoney.com".replace("push2his", "pushhis")):
        _, text = fetch(f"https://{host}/{kline_q}", {"Referer": "https://quote.eastmoney.com/"})
        print(f"  {host:32s} -> {text[:150]}")

    # ---- 5) 东财板块 / 资金流（delay 域名）----
    section("5) 板块 / 资金流（delay 域名）")
    for label, url in [
        ("行业板块 m:90+t:2",
         "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2"
         "&fid=f3&fs=m:90+t:2+f:!50&fields=f1,f2,f3,f4,f6,f8,f12,f13,f14,f104,f105,f128,f136,f140,f207,f208"
         "&ut=bd1d9ddb04089700cf9c27f6f7426281"),
        ("板块资金流",
         "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2"
         "&fid=f62&fs=m:90+t:2+f:!50&fields=f12,f13,f14,f3,f62,f184"
         "&ut=b2884a393a59ad64002292a3e90d46a5"),
    ]:
        _, text = fetch(url, {"Referer": "https://data.eastmoney.com/"})
        print(f"  {label}: {text[:260]}")

    # ---- 6) 同花顺 last.js 的 year/data 结构 ----
    section("6) 同花顺 last.js —— year 结构")
    _, text = fetch("https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js",
                    {"Referer": "https://stockpage.10jqka.com.cn/"})
    start = text.find("(")
    end = text.rfind(")")
    payload = json.loads(text[start + 1:end]) if 0 <= start < end else {}
    node = payload.get("hs_600519") or next((v for v in payload.values() if isinstance(v, dict)), {})
    print("top keys:", list(payload.keys())[:6])
    print("node keys:", list(node.keys())[:12])
    print("name:", node.get("name"), "start:", node.get("start"), "total:", node.get("total"))
    for key in ("data", "sortYear", "year"):
        value = node.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            print(f"  {key}: str, 前80字符 = {value[:80]}")
        elif isinstance(value, list):
            print(f"  {key}: list[{len(value)}], 前2项 = {value[:2]}")
        elif isinstance(value, dict):
            items = list(value.items())[:2]
            print(f"  {key}: dict[{len(value)}], 前2项 = {items}")

    # ---- 7) 同花顺 all.js 的完整历史 ----
    section("7) 同花顺 all.js —— 历史结构")
    _, text = fetch("https://d.10jqka.com.cn/v6/line/hs_600519/01/all.js",
                    {"Referer": "https://stockpage.10jqka.com.cn/"})
    start = text.find("(")
    end = text.rfind(")")
    payload = json.loads(text[start + 1:end]) if 0 <= start < end else {}
    node = next((v for v in payload.values() if isinstance(v, dict)), payload)
    print("node keys:", list(node.keys())[:12])
    sort_year = node.get("sortYear")
    print("sortYear:", str(sort_year)[:200])
    data = str(node.get("data") or "")
    print("data 前 220 字符:", data[:220])

    # ---- 8) 新浪列表限流是否恢复 ----
    section("8) 新浪列表节点可达性")
    for node_name in ("hs_a", "sh_a", "sz_a"):
        status, text = fetch(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeData?page=1&num=2&sort=symbol&asc=1&node={node_name}&symbol=&_s_r_a=page",
            {"Referer": "https://finance.sina.com.cn/"})
        print(f"  {node_name}: status={status} {text[:160]}")

    # ---- 9) 新浪 7x24 是否真的返回数据 ----
    section("9) 新浪 7x24 快讯内容")
    _, text = fetch(
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=3&zhibo_id=152&tag_id=0&dire=f&dpc=1",
        {"Referer": "https://finance.sina.com.cn/7x24/"})
    print(text[:400])

    # ---- 10) 巨潮公告（POST, 先取 Cookie）----
    section("10) 巨潮公告 POST")
    import urllib.parse

    body = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": 3, "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": "", "searchkey": "", "secid": "", "category": "",
        "trade": "", "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
    }).encode()
    request = urllib.request.Request(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                 "X-Requested-With": "XMLHttpRequest"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print("status", response.status, response.read().decode("utf-8", "replace")[:300])
    except Exception as exc:  # noqa: BLE001
        print("失败:", type(exc).__name__, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
