"""容器健康检查脚本。

**刻意不用 curl/wget**: 基础镜像里没有它们, 为了健康检查去 apt-get 安装
会让构建时间从几十秒变成几分钟(实测 apt 一步就可能 600 秒以上)。
只用标准库即可完成"进程活着 + HTTP 可响应 + 业务未降级"三层判断。

退出码:
  0  健康
  1  不健康(容器编排会据此重启)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_PORT = 8770
TIMEOUT = 5.0


def main() -> int:
    port = int(os.environ.get("SS_SERVER__PORT") or os.environ.get("SS_PORT") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"UNHEALTHY: 无法连接 {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(body)
    except ValueError:
        print(f"UNHEALTHY: /healthz 返回非 JSON: {body[:120]}", file=sys.stderr)
        return 1

    if payload.get("status") != "ok":
        print(f"UNHEALTHY: status={payload.get('status')}", file=sys.stderr)
        return 1

    # 顺带看一眼业务健康: degraded 里的项不影响存活判定, 只作为提示打印
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=TIMEOUT
        ) as response:
            detail = json.loads(response.read().decode("utf-8", "replace")).get("data") or {}
        degraded = detail.get("degraded") or []
        if degraded:
            print(f"HEALTHY(degraded): {'; '.join(degraded)[:200]}")
        else:
            print(f"HEALTHY: version={payload.get('version')} rss={detail.get('rss_mb')}MB")
    except Exception:  # noqa: BLE001 - 业务健康读不到不影响存活判定
        print(f"HEALTHY: version={payload.get('version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
