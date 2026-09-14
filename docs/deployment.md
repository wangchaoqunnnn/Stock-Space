# 部署与运维手册

本文档覆盖：云服务器部署（Docker 与直接部署两种方式）、配置、HTTPS、备份升级、
监控告警、故障排查。

> 首次部署请从「一、部署前准备」开始按顺序读；已有环境请直接跳到对应章节。

---

## 目录

- [一、部署前准备](#一部署前准备)
- [二、一键部署](#二一键部署)
- [三、Docker 部署详解](#三docker-部署详解)
- [四、直接部署详解（systemd）](#四直接部署详解systemd)
- [五、反向代理与 HTTPS](#五反向代理与-https)
- [六、子路径部署](#六子路径部署)
- [七、部署后验收](#七部署后验收)
- [八、配置](#八配置)
- [九、备份与升级](#九备份与升级)
- [十、监控与告警](#十监控与告警)
- [十一、故障排查速查](#十一故障排查速查)
- [十二、安全加固](#十二安全加固)

---

## 一、部署前准备

### 1.1 服务器规格

| 场景 | 建议规格 | 说明 |
|---|---|---|
| 体验 / 小范围 | 1 核 2 G | 可跑，但建议把 `universe_size` 设为 1500 左右 |
| **推荐** | **2 核 4 G** | 覆盖全部 A 股（约 5900 只）舒适运行 |
| 多策略频繁扫描 | 4 核 8 G | 并发扫描更快 |

磁盘：20 G 起（数据库与日线缓存会持续增长，全市场一年日线约 300~600 MB）。

### 1.2 系统要求

* Linux（Ubuntu 20.04+ / Debian 11+ / CentOS 7+ / AlmaLinux / Rocky 均可）
* 直接部署：Python 3.10+（推荐 3.12）
* Docker 部署：Docker 20.10+ 与 Docker Compose v2
* **时区建议设为 `Asia/Shanghai`**：

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl
```

> 平台内部行情时段判定使用固定 UTC+8，不依赖服务器时区；但日志时间戳会跟随系统时区，
> 设成北京时间排查更方便。

### 1.3 网络要求

服务器需要能**出网访问**行情上游。不同网络环境下的可达性差异很大，**部署后请务必先跑一次探测**：

```bash
cd /path/to/stock-space
python tools/probe_sources.py
```

输出会逐条列出每个上游端点是否可达、耗时、返回内容片段。若某些端点不可达，
可在「数据源」页面把它们替换为可达的镜像（或直接用页面的"手工输入地址"功能）。

### 1.4 云安全组

在云厂商控制台放行部署端口（默认 TCP 8770）。这是**最常被忘记的一步** ——
脚本只能放行服务器自身的 ufw/firewalld，管不了云控制台的安全组。

---

## 二、一键部署

### 2.1 Linux

```bash
# 获取代码
git clone <仓库地址> stock-space
cd stock-space

# 一键部署（脚本会自动选择 Docker 或直接部署）
sudo bash deploy/deploy.sh
```

脚本会依次完成 8 个阶段：

| 阶段 | 内容 |
|---|---|
| 1/8 环境检查 | 确认 Linux、root、识别包管理器、探测 Docker 可用性 |
| 2/8 生成配置 | 从 `.env.example` 生成 `.env`（已存在则只补缺失键），权限 600 |
| 3/8 准备目录 | 创建 `data/` `logs/` `config/` |
| 4/8 构建/安装 | Docker：构建镜像；直接部署：建 venv 并装依赖（失败自动切国内镜像） |
| 5/8 环境自检 | 跑 `run.py --check`，列出依赖、数据源注册情况、各能力可用源数量 |
| 6/8 注册服务 | 渲染 systemd unit（`__APP_DIR__`/`__RUN_USER__` 占位符），启动并等待健康检查 |
| 7/8 代理与防火墙 | 传了 `--domain` 则配 nginx；放行 ufw/firewalld 端口 |
| 8/8 完成 | 打印访问地址、日志命令、升级命令与注意事项 |

### 2.2 常用参数

```bash
sudo bash deploy/deploy.sh --mode docker            # 强制 Docker 部署
sudo bash deploy/deploy.sh --mode direct            # 强制直接部署
sudo bash deploy/deploy.sh --port 9000              # 改端口
sudo bash deploy/deploy.sh --domain sa.example.com  # 顺便配置 nginx 反向代理
sudo bash deploy/deploy.sh --user myuser            # 指定 systemd 运行账号
sudo bash deploy/deploy.sh --no-service             # 只装环境，不注册 systemd
sudo bash deploy/deploy.sh --force                  # 覆盖已存在的 systemd/nginx 配置
sudo bash deploy/deploy.sh --skip-build             # Docker 模式跳过镜像构建
sudo bash deploy/deploy.sh --help
```

### 2.3 Windows

```powershell
# Docker 模式（需要 Docker Desktop 已启动）
pwsh -File deploy\deploy.ps1 -Mode docker

# 直接部署 + 注册开机自启计划任务
pwsh -File deploy\deploy.ps1 -Mode direct -RegisterTask

# 指定端口
pwsh -File deploy\deploy.ps1 -Mode direct -Port 8770
```

Windows 直接部署会用后台进程运行服务，日志写到 `logs\console.log` 与 `logs\console.err.log`。

### 2.4 幂等性

脚本可以反复执行，不会破坏已有环境：

* 虚拟环境存在则复用，不重建；
* `.env` 只补充缺失的键，**不覆盖**已有值；
* systemd unit / nginx 配置已存在则跳过（除非 `--force`）；
* 数据目录只创建，从不清理。

因此**升级代码后直接再跑一次脚本即可**。

---

## 三、Docker 部署详解

### 3.1 手动部署

```bash
cp .env.example .env
vim .env                    # 按需修改 HOST_PORT / UNIVERSE_SIZE / 推送等
docker compose up -d --build
docker compose logs -f --tail=200
```

### 3.2 镜像设计要点

| 要点 | 原因 |
|---|---|
| **只有 Python 一个运行时** | 前端零构建（原生 HTML/CSS/JS），不需要 node 阶段 |
| 虚拟环境搬运式多阶段 | builder 里装依赖，runtime 只拷贝 `/opt/venv`，镜像更小 |
| 依赖层单独 COPY | 先 `COPY requirements.txt` 再装包，改业务代码不会让依赖层失效 |
| **刻意不执行 apt-get** | 云上 apt 往往是最慢的一步（实测可达 600 秒以上） |
| 健康检查只用标准库 | 基础镜像没有 curl/wget，为健康检查装它们不值得 |
| 非 root 运行 | 镜像内建 `appuser`（UID 10001） |
| 设置时区 | 容器默认 UTC，不设会让交易时段判定错位 |

### 3.3 数据持久化

```yaml
volumes:
  - stock-space-data:/app/data    # SQLite、日线缓存、运行期设置
  - stock-space-logs:/app/logs    # 日志
```

容器重建不会丢数据。查看卷位置：

```bash
docker volume inspect stock-space-data
```

### 3.4 健康检查

```bash
docker inspect --format '{{.State.Health.Status}}' stock-space
```

健康检查脚本 `backend/docker_healthcheck.py` 会做三层判断：
进程存活 → `/healthz` 可响应 → 业务健康（`degraded` 只打印提示，不影响存活判定）。

### 3.5 安装可选数据源

默认不装 AKShare / Ashare（会显著增加镜像体积与构建时间）。需要时：

```bash
INSTALL_OPTIONAL=true docker compose up -d --build
```

或者在 `.env` 里设置 `INSTALL_OPTIONAL=true` 后重新构建。

### 3.6 容器模式下的资源限制

```bash
# .env
MEMORY_SOFT_LIMIT_MB=800
MEMORY_HARD_LIMIT_MB=1400
UNIVERSE_SIZE=1500          # 小内存机器建议限制扫描范围
KLINE_CONCURRENCY=12
```

平台会**自动读取 cgroup 内存上限并收紧阈值**，避免被 OOM Killer 杀掉。

---

## 四、直接部署详解（systemd）

如果脚本自动选择了直接部署（或你指定 `--mode direct`），它会：

1. 安装 `python3` / `python3-venv` / `python3-pip` / `tzdata`（按发行版自动选包管理器）
2. 在项目目录建 `.venv` 并安装依赖
3. 渲染 `deploy/stock-space.service` 到 `/etc/systemd/system/stock-space.service`
4. `daemon-reload` + `enable` + `restart`
5. 轮询健康检查直到就绪（最多 5 分钟）

### 4.1 脚本手工部署（不想用脚本时）

```bash
cd /path/to/stock-space
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r backend/requirements.txt

cp .env.example .env

sudo tee /etc/systemd/system/stock-space.service > /dev/null <<'EOF'
[Unit]
Description=StockSpace 股票决策平台
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stockspace
Group=stockspace
WorkingDirectory=/path/to/stock-space
EnvironmentFile=-/path/to/stock-space/.env
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=/path/to/stock-space/.venv/bin/python /path/to/stock-space/backend/run.py
Restart=always
RestartSec=5
TimeoutStartSec=900
StandardOutput=journal
StandardError=journal
SyslogIdentifier=stock-space
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/path/to/stock-space/data /path/to/stock-space/logs
MemoryHigh=1200M
MemoryMax=1500M

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now stock-space
sudo systemctl status stock-space --no-pager
```

### 4.2 unit 里几个关键设置的由来

| 设置 | 为什么必须 |
|---|---|
| `TimeoutStartSec=900` | **首次启动要拉全市场快照与日线缓存（可能数分钟）**，默认 90 秒会被 systemd 判为失败并杀掉 |
| `Restart=always` + `RestartSec=5` | 崩溃自愈；上游故障导致的退出能自动拉起 |
| `ReadWritePaths=.../data .../logs` | `ProtectSystem=full` 会把文件系统挂成只读，必须显式放开数据与日志目录 |
| `MemoryHigh` / `MemoryMax` | 与后端自带的软/硬内存阈值配合：后端先自我压缩缓存，systemd 再兜底 |
| `MALLOC_ARENA_MAX=2` | 长时间运行的 Python 服务可以减少内存碎片（实测有效） |

---

## 五、反向代理与 HTTPS

### 5.1 Nginx 反向代理

模板在 `deploy/nginx.conf`，已包含：

* 安全响应头（`X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy`）
* gzip 压缩（含 `application/json`）
* **超时放宽到 300 秒** —— 扫描类接口可能运行数十秒，默认 60 秒会导致 504
* 静态资源长缓存（`/assets/` 7 天 immutable）
* 子路径部署示例

直接部署时，把模板里的 `server app:8770;` 改成 `server 127.0.0.1:8770;`：

```bash
sudo sed -e 's|server app:8770;|server 127.0.0.1:8770;|' \
         -e 's|server_name _;|server_name sa.example.com;|' \
         deploy/nginx.conf | sudo tee /etc/nginx/conf.d/stock-space.conf > /dev/null
sudo nginx -t && sudo systemctl reload nginx
```

### 5.2 HTTPS（Let's Encrypt）

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d sa.example.com
sudo systemctl status certbot.timer     # 自动续期
```

`deploy/nginx.conf` 底部有现成的 HTTPS server 段落，取消注释并改证书路径即可。

### 5.3 只跑在 127.0.0.1（推荐的安全做法）

如果前面有 nginx，可以让后端只监听本机：

```bash
# .env
SS_SERVER__HOST=127.0.0.1
```

这样即使云安全组误放行了 8770，外网也访问不到 —— 只能通过 nginx 进来。

---

## 六、子路径部署

平台**完全使用相对地址**（前端资源 `./assets/...`，接口 `api/...`），
因此部署到任意子路径都不需要改一行代码，只要反向代理把前缀正确转发。

Nginx 示例（部署在 `https://host/stock-space/`）：

```nginx
location = /stock-space { return 301 /stock-space/; }

location ^~ /stock-space/ {
    proxy_pass http://127.0.0.1:8770/;   # 末尾的 / 会把前缀去掉
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /stock-space/;
    proxy_set_header Connection "";
    proxy_read_timeout 300s;
}
```

Docker 部署时把 `127.0.0.1:8770` 换成 `app:8770`（Compose 服务名），
`deploy/nginx.conf` 里已经准备好了这段注释，取消注释即可。

**验证方法**：部署后打开 `https://host/stock-space/selfcheck`，若 10 组检查项都通过，
说明页面、静态资源与接口三者的前缀解析都正确。

---

## 七、部署后验收

### 7.1 必做：部署自检

打开 **`http://<你的地址>:端口/selfcheck`**，点「全部重跑」。

它会检查 10 组内容并给出结论：

| 检查组 | 检查内容 |
|---|---|
| 前端资源 | `util.js`/`api.js`/`app.css` 是否加载；页面与接口是否同源 |
| 后端与健康 | 版本、运行时长、内存、降级项、调度器、市场时段、数据源模式 |
| 就绪检查 | 数据源已注册 + 数据库可读写 |
| 数据库 | 文件大小与各表行数 |
| 内存监控 | 护栏是否运行、水位是否正常 |
| 数据源 | 源清单、能力覆盖、未安装的可选源、需登录未配置的源、限流冷却 |
| 实时数据 | 拉一次全市场快照，报告标的数与耗时 |
| 日线数据 | 随机取一只股票的日线，检查根数与来源 |
| 策略与扫描 | 策略目录 + 实跑一次扫描 |
| 消息推送 | Webhook 是否配置、推送开关、推送历史 |

### 7.2 手工验收清单

| # | 检查项 | 期望 |
|---|---|---|
| 1 | 打开首页 | 页面正常渲染，无空白、无控制台报错 |
| 2 | 仪表盘样本数 | ≈ 5900 只（或你配置的配额），四个板块均有标的 |
| 3 | **指数卡片** | 上证指数点位在 3000 量级，**不是个股价格** |
| 4 | 情绪温度计 | 情绪分 0~100，6 个分项都显示数值 |
| 5 | 涨停梯队 | 显示连板分布与龙头分层 |
| 6 | 个股详情 | `#/stock?code=600519` K 线图正常，含均线与成交量 |
| 7 | 策略选股 | 点「运行扫描」能返回结果与逐条依据 |
| 8 | 回测 | 点「开始回测」返回绩效指标与资金曲线 |
| 9 | 数据源页 | 能手动锁定某能力到指定源，刷新后依然生效 |
| 10 | 手工输地址 | 改某能力地址并保存 → 生效；清空 → 恢复内置 |
| 11 | 内存监控 | 显示 RSS、阈值、缓存明细与趋势图 |
| 12 | 用户配置 | 改配额保存 → 立即生效；越权字段被拒绝 |
| 13 | 企微推送 | 填 Webhook 点测试 → 群里收到消息 |
| 14 | 移动端 | 手机打开 → 抽屉导航 + 底部导航正常 |

---

## 八、配置

### 8.1 三层优先级

```
环境变量（SS_ 前缀，双下划线分隔）
    ↑ 覆盖
data/runtime_settings.json（页面「用户配置」保存，权限 600）
    ↑ 覆盖
config/app.toml 与 config/sources.toml
```

例：`SS_SERVER__PORT=9000` 等价于 `[server] port = 9000`。

### 8.2 性能相关配置建议

| 场景 | 建议配置 |
|---|---|
| 1 核 2 G | `UNIVERSE_SIZE=1200`、`KLINE_CONCURRENCY=8`、`MEMORY_SOFT_LIMIT_MB=500` |
| 2 核 4 G（推荐） | 全部默认（`UNIVERSE_SIZE=0` 全市场、并发 16、软上限 800） |
| 4 核 8 G | `KLINE_CONCURRENCY=24`（新浪反爬上限附近）、`SCAN_KLINE_BUDGET=3000` |
| 只做演示 | `SS_DATA_SOURCES__MODE=synthetic`（不访问外网） |

### 8.3 上游地址覆盖

三种方式任选：

```bash
# 方式 1：环境变量（适合容器编排）
SS_SOURCES__TENCENT__URLS__KLINE=https://my-mirror/appstock/app/newfqkline/get

# 方式 2：本机覆盖文件（不进版本库）
# config/sources.local.toml
[providers.tencent.urls]
kline = ["https://my-mirror/appstock/app/newfqkline/get"]
```

方式 3：网页「数据源 → 查看/编辑该能力地址」，可直接改并即时生效。

---

## 九、备份与升级

### 9.1 备份

需要备份的只有 `data/` 目录：

```bash
tar czf stock-space-backup-$(date +%F).tar.gz data/
```

它包含：SQLite 数据库、日线缓存、`runtime_settings.json`（含你配置的凭据与推送设置）。

也可以在「用户配置」页导出 JSON（默认不含敏感项，可选含明文）。

**建议加一条 crontab**：

```cron
0 3 * * * cd /path/to/stock-space && tar czf /var/backups/stock-space-$(date +\%F).tar.gz data/ && find /var/backups -name 'stock-space-*.tar.gz' -mtime +14 -delete
```

### 9.2 升级

```bash
cd /path/to/stock-space
git pull

# 直接部署
sudo bash deploy/deploy.sh

# Docker
docker compose up -d --build
```

数据库表结构使用 `CREATE TABLE IF NOT EXISTS`，新增字段是幂等的，升级不需要手工迁移。

升级后建议打开 `/selfcheck` 跑一次全检。

### 9.3 回滚

```bash
git log --oneline -5
git checkout <上一个版本>
sudo bash deploy/deploy.sh
```

数据目录不受代码回滚影响。

---

## 十、监控与告警

### 10.1 健康检查接口

| 接口 | 用途 |
|---|---|
| `GET /healthz` | 极简存活探针，返回 `{"status":"ok"}`，适合容器/负载均衡 |
| `GET /api/health` | 完整健康检查，返回 `degraded` 数组、内存、时段、能力覆盖数 |
| `GET /api/ready` | 就绪检查（源已注册 + 数据库可写） |
| `GET /api/system/memory/raw` | 极简内存视图，适合脚本解析 |

### 10.2 云监控配置建议

* **存活告警**：`/healthz` 连续 3 次失败 → 告警（进程可能死了）
* **降级告警**：`/api/health` 的 `degraded` 数组非空 → 告警（某个数据源挂了）
* **内存告警**：`/api/system/memory/raw` 的 `system_used_pct > 90` 或
  `soft_breaches > 0` 持续增长 → 告警

平台自身也支持把内存告警和数据源异常**推送到企业微信**（在「用户配置」里开启）。

### 10.3 日志

```bash
# 直接部署
journalctl -u stock-space -f --no-pager
journalctl -u stock-space -p err -n 100 --no-pager

# Docker
docker compose logs -f --tail=200

# 文件日志（两种方式都有，按天滚动，保留 14 天）
tail -f logs/stock_space.log
```

日志级别由 `SS_LOG__LEVEL` 控制（默认 `INFO`）。排查问题时可临时设为 `DEBUG`：

```bash
SS_LOG__LEVEL=DEBUG ./deploy/deploy.sh --mode direct
```

> 注意：`DEBUG` 会打印每个数据源的失败细节，日志量较大，排查完记得改回。

---

## 十一、故障排查速查

| 现象 | 排查步骤 |
|---|---|
| **服务起不来** | `journalctl -u stock-space -n 60 --no-pager` 看错误；常见原因是端口被占用（`ss -lntp \| grep 8770`）或 `data/` 无写权限（`chown -R stockspace:stockspace data logs`） |
| **服务起来了但外网打不开** | ① 本机 `curl http://127.0.0.1:8770/healthz` 是否正常；② 云控制台安全组是否放行；③ qf防火墙（`ufw status` / `firewall-cmd --list-ports`） |
| **页面能开但数据全空** | `python tools/probe_sources.py` 看上游可达性；检查服务器出网策略与 DNS |
| **提示"非 JSON"** | 反向代理没转发 `api/`；检查 nginx 的 location 与前缀 |
| **502 / 504** | 加大 `proxy_read_timeout`（模板已设 300s）；确认后端进程还活着 |
| **首次启动很久** | 正常，正在拉全市场日线缓存；看日志确认在进展中 |
| **扫描很慢** | 首次需要建立日线缓存；之后走磁盘缓存会快很多。也可把 `universe_size` 设小 |
| **扫描提示配额用尽** | 调大 `SCAN_KLINE_BUDGET`，或缩小 `UNIVERSE_SIZE` |
| **内存持续增长** | 看「内存监控」页的趋势与缓存明细；把 `universe_size` 设为有限值，或调大阈值 |
| **企业微信收不到** | 设置页点测试推送看错误码；确认「启用推送」已勾选；确认机器人还在群里 |
| **升级后页面还是旧的** | HTML 已设 `no-store`；强制刷新（Ctrl+F5）或清缓存 |
| **子路径部署 404** | 检查 `proxy_pass` 末尾的 `/`；用 `/selfcheck` 验证前缀解析 |
| **ExecStart 报 no such file** | systemd unit 里的 Python 路径不对；重新跑 `deploy.sh` 让它重新渲染 unit |

---

## 十二、安全加固

已经内置的措施：

* 容器内以非 root 用户（UID 10001）运行；
* systemd unit 启用 `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full` /
  `ProtectHome` / `ProtectKernelTunables` / `ProtectControlGroups` / `RestrictSUIDSGID`；
* 敏感项（Webhook、Cookie、管理口令）读取接口一律返回掩码，落盘文件权限 600；
* 设置接口按白名单校验，越权字段被拒绝；
* 静态资源由框架的 `StaticFiles` 限定目录，不做路径拼接；
* 模板自带安全响应头与 `no-store` 的 HTML 缓存策略。

建议额外做的：

| 措施 | 说明 |
|---|---|
| 后端只监听 127.0.0.1 | `SS_SERVER__HOST=127.0.0.1`，只让 nginx 进来 |
| 上 HTTPS | `certbot --nginx`，自动续期 |
| 关闭接口文档 | 生产环境设 `SS_SERVER__DOCS_ENABLED=false` |
| 限制来源 IP | nginx 里加 `allow` / `deny` |
| 定期备份 `data/` | 见第九章 |
| 不要把 `.env` 提交到 Git | `.gitignore` 已排除，注意别用 `-f` 强加 |

> **关于登录态**：平台默认免登录（单机自用模型）。若要限制写入操作，
> 可在「用户配置 → 应用」里开启 `auth.required` 并设置管理口令（需重启生效）。
