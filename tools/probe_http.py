"""诊断: 对比 HTTP/1.1 与 HTTP/2 下各上游的响应, 并核对东财分页。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TARGETS = [
    ("腾讯 newfqkline",
     "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=sh600519,day,,,5,qfq",
     {"Referer": "https://gu.qq.com/"}),
    ("腾讯 批量行情", "https://qt.gtimg.cn/q=sh600519", {"Referer": "https://gu.qq.com/"}),
    ("东财 clist", "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=2&po=1&np=1"
                   "&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:1+t:2&fields=f12,f14,f2,f3"
                   "&ut=bd1d9ddb04089700cf9c27f6f7426281", {"Referer": "https://quote.eastmoney.com/"}),
    ("新浪行情", "https://hq.sinajs.cn/list=sh600519", {"Referer": "https://finance.sina.com.cn/"}),
    ("同花顺日线", "https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js",
     {"Referer": "https://stockpage.10jqka.com.cn/"}),
]


async def probe(http2: bool) -> None:
    print("=" * 78)
    print(f"HTTP/2 = {http2}")
    print("=" * 78)
    async with httpx.AsyncClient(
        http2=http2, timeout=15.0, follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "*/*",
                 "Accept-Language": "zh-CN,zh;q=0.9"},
    ) as client:
        for name, url, headers in TARGETS:
            try:
                response = await client.get(url, headers=headers)
                preview = response.content.decode("utf-8", "replace")[:110].replace("\n", " ")
                print(f"  {name:16s} HTTP {response.status_code}  {preview}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name:16s} FAIL {type(exc).__name__}: {str(exc)[:90]}")


async def check_pagination() -> None:
    print()
    print("=" * 78)
    print("东财 clist 分页核对")
    print("=" * 78)
    url = "https://push2delay.eastmoney.com/api/qt/clist/get"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}) as client:
        for page, size in ((1, 5), (1, 100), (1, 1000), (2, 1000)):
            response = await client.get(url, params={
                "pn": page, "pz": size, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12,f14,f2,f3", "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
            payload = response.json()
            data = payload.get("data") or {}
            diff = data.get("diff")
            if isinstance(diff, dict):
                kind = f"dict[{len(diff)}]"
            elif isinstance(diff, list):
                kind = f"list[{len(diff)}]"
            else:
                kind = type(diff).__name__
            print(f"  pn={page} pz={size:5d} -> HTTP {response.status_code} total={data.get('total')} diff={kind} "
                  f"bytes={len(response.content)}")

        # 全量分页耗时
        import time
        started = time.perf_counter()
        total_rows = 0
        total_reported = 0
        for page in range(1, 8):
            response = await client.get(url, params={
                "pn": page, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12,f14,f2,f3", "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
            data = response.json().get("data") or {}
            diff = data.get("diff")
            rows = list(diff.values()) if isinstance(diff, dict) else (diff or [])
            total_reported = data.get("total") or total_reported
            total_rows += len(rows)
            if len(rows) < 1000:
                break
        print(f"  全量分页: {total_rows} 行 / 上报 total={total_reported} / "
              f"{time.perf_counter() - started:.1f}s")

        # 抽查一行完整字段
        response = await client.get(url, params={
            "pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f6",
            "fs": "m:0+t:6,m:1+t:2",
            "fields": ("f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
                       "f20,f21,f22,f23,f24,f25,f26,f100,f115,f62,f184"),
            "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
        data = response.json().get("data") or {}
        diff = data.get("diff")
        row = (list(diff.values()) if isinstance(diff, dict) else diff)[0]
        print("  完整字段样例:", json.dumps(row, ensure_ascii=False))


async def compare_tencent_kline() -> None:
    """对比不同 header/参数组合下腾讯 K 线入口的响应。"""
    print()
    print("=" * 78)
    print("腾讯 K 线入口对比")
    print("=" * 78)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    variants = [
        ("最小 header", {"Referer": "https://gu.qq.com/"}),
        ("最小 header + param", {"Referer": "https://gu.qq.com/"}),
        ("无 Referer", {}),
        ("伪装浏览器全头", {
            "Referer": "https://gu.qq.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://gu.qq.com",
        }),
    ]
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for label, headers in variants:
            for params_desc, params in (
                ("无参数", None),
                ("param", {"param": "sh600519,day,,,5,qfq"}),
                ("param+_var", {"param": "sh600519,day,,,5,qfq", "_var": "kline_day"}),
            ):
                try:
                    response = await client.get(url, params=params,
                                                headers={"User-Agent": UA, **headers})
                    text = response.content.decode("utf-8", "replace")[:70].replace("\n", " ")
                    print(f"  {label:12s} {params_desc:12s} HTTP {response.status_code} {text}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label:12s} {params_desc:12s} FAIL {type(exc).__name__}: {str(exc)[:60]}")


async def main() -> int:
    await probe(False)
    await compare_tencent_kline()
    await check_pagination()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
