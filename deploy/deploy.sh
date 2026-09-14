#!/usr/bin/env bash
# =============================================================================
# StockSpace 一键部署脚本（Linux / 云服务器）
# =============================================================================
# 支持两种部署方式，脚本会自动探测并按需选择：
#
#   1) Docker 部署       —— 服务器已装 docker 且能拉镜像时优先使用
#   2) 直接部署(venv+systemd) —— 没有 Docker 时使用，只需 python3
#
# 用法：
#   sudo bash deploy/deploy.sh                 # 自动选择
#   sudo bash deploy/deploy.sh --mode docker   # 强制 Docker
#   sudo bash deploy/deploy.sh --mode direct   # 强制直接部署
#   sudo bash deploy/deploy.sh --port 9000     # 改端口
#   sudo bash deploy/deploy.sh --domain sa.example.com   # 顺便配 nginx
#   sudo bash deploy/deploy.sh --help
#
# 脚本的幂等性保证（重复执行是安全的）：
#   * venv 已存在则复用，不重建；
#   * .env 已存在则**只补缺失的键**，不覆盖已有值；
#   * systemd unit / nginx 配置已存在则跳过，除非显式 --force；
#   * 数据目录只创建不清理。
# =============================================================================
set -Eeuo pipefail

# ------------------------------------------------------------------ 输出样式
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

log()   { printf '%s[%s]%s %s\n' "$C_BLUE" "$(date +%H:%M:%S)" "$C_RESET" "$*"; }
ok()    { printf '%s[ OK ]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%s[WARN]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()   { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
title() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
die()   { err "$*"; exit 1; }

# ------------------------------------------------------------------ 参数
MODE="auto"
PORT=""
DOMAIN=""
RUN_USER=""
NO_SERVICE=0
FORCE=0
SKIP_BUILD=0

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)       MODE="${2:-auto}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --domain)     DOMAIN="${2:-}"; shift 2 ;;
    --user)       RUN_USER="${2:-}"; shift 2 ;;
    --no-service) NO_SERVICE=1; shift ;;
    --force)      FORCE=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -y|--yes)     shift ;;
    -h|--help)    usage ;;
    *)            die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

case "$MODE" in
  auto|docker|direct) ;;
  *) die "--mode 只能是 auto / docker / direct" ;;
esac

# ------------------------------------------------------------------ 目录推导
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$APP_DIR"

SERVICE_NAME="stock-space"
ENV_FILE="${APP_DIR}/.env"
ENV_EXAMPLE="${APP_DIR}/.env.example"

title "StockSpace 部署"
log "项目目录: ${APP_DIR}"
log "部署方式: ${MODE}"

# ------------------------------------------------------------------ 环境检查
title "1/8 环境检查"

if [ "$(uname -s)" != "Linux" ]; then
  die "本脚本用于 Linux；Windows 请使用 deploy/deploy.ps1"
fi

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    warn "当前不是 root，将尝试用 sudo 重新执行"
    exec sudo -E bash "${BASH_SOURCE[0]}" "$@"
  fi
  die "需要 root 权限（安装 systemd 服务或启动 Docker）"
fi

PKG_MGR=""
for candidate in apt-get dnf yum apk; do
  if command -v "$candidate" >/dev/null 2>&1; then PKG_MGR="$candidate"; break; fi
done
log "包管理器: ${PKG_MGR:-未识别}"

HAS_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  HAS_DOCKER=1
fi

if [ "$MODE" = "auto" ]; then
  if [ "$HAS_DOCKER" -eq 1 ]; then
    MODE="docker"
  else
    MODE="direct"
  fi
  log "自动选择部署方式: ${MODE}"
fi

if [ "$MODE" = "docker" ] && [ "$HAS_DOCKER" -eq 0 ]; then
  warn "指定了 Docker 部署，但 Docker 不可用（未安装或守护进程未运行）"
  if command -v docker >/dev/null 2>&1; then
    die "Docker 已安装但 docker info 失败，请先启动: systemctl start docker"
  fi
  die "请先安装 Docker，或改用 --mode direct"
fi

# ------------------------------------------------------------------ 端口
if [ -z "$PORT" ]; then
  PORT="$(grep -E '^HOST_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  [ -z "$PORT" ] && PORT="$(grep -E '^SS_SERVER__PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  [ -z "$PORT" ] && PORT=8770
fi
case "$PORT" in
  ''|*[!0-9]*) die "端口必须是数字: $PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围: $PORT"
log "对外端口: ${PORT}"

# ------------------------------------------------------------------ 生成 .env
title "2/8 生成配置文件"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "已从 .env.example 生成 .env"
  else
    : > "$ENV_FILE"
    ok "已创建空的 .env"
  fi
else
  log ".env 已存在，仅补充缺失的键（不覆盖已有值）"
fi

# 只补不覆盖
ensure_env() {
  key="$1"; value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    return 0
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  log "  补充 ${key}=${value}"
}

ensure_env "HOST_PORT" "$PORT"
ensure_env "SS_SERVER__PORT" "$PORT"
ensure_env "SS_SERVER__HOST" "0.0.0.0"
ensure_env "SS_DATA_SOURCES__MODE" "auto"
ensure_env "SS_QUOTAS__UNIVERSE_SIZE" "0"
ensure_env "SS_PUSH__ENABLED" "false"
ensure_env "TZ" "Asia/Shanghai"
chmod 600 "$ENV_FILE" 2>/dev/null || true
ok "配置就绪（.env 权限 600）"

# ------------------------------------------------------------------ 数据目录
title "3/8 准备数据目录"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs" "${APP_DIR}/config"
ok "data/ logs/ config/ 已就绪"

# =============================================================================
# 路线 A：Docker 部署
# =============================================================================
if [ "$MODE" = "docker" ]; then
  title "4/8 构建镜像"
  if [ "$SKIP_BUILD" -eq 1 ]; then
    log "--skip-build 已指定，跳过构建"
  else
    log "开始构建（首次约 2~6 分钟，取决于网络与镜像源）…"
    if ! docker compose build 2>&1 | tail -20; then
      warn "docker compose build 失败，尝试旧版 docker-compose…"
      docker-compose build 2>&1 | tail -20 || die "镜像构建失败"
    fi
    ok "镜像构建完成"
  fi

  title "5/8 启动容器"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  else
    COMPOSE="docker-compose"
  fi
  $COMPOSE up -d --remove-orphans 2>&1 | tail -20
  ok "容器已启动"

  title "6/8 等待健康检查"
  HEALTHY=0
  for i in $(seq 1 60); do
    if python3 - "$PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as r:
    data = json.loads(r.read().decode("utf-8", "replace"))
sys.exit(0 if data.get("status") == "ok" else 1)
PY
    then
      HEALTHY=1; break
    fi
    sleep 5
    if [ $((i % 6)) -eq 0 ]; then log "  等待中… (${i}/60)"; fi
  done

  if [ "$HEALTHY" -eq 1 ]; then
    ok "服务健康检查通过"
  else
    warn "健康检查未通过，最近日志："
    $COMPOSE logs --tail=40 2>&1 | sed 's/^/    /' || true
  fi

  title "7/8 防火墙"
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" >/dev/null 2>&1 && ok "ufw 已放行 ${PORT}/tcp"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    ok "firewalld 已放行 ${PORT}/tcp"
  else
    log "未检测到已启用的 ufw/firewalld，跳过端口放行"
  fi

  title "8/8 完成"
  IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$IP_ADDR" ] && IP_ADDR="<服务器IP>"
  cat <<EOF

${C_BOLD}部署完成（Docker 模式）${C_RESET}

  访问地址 : http://${IP_ADDR}:${PORT}/
  部署自检 : http://${IP_ADDR}:${PORT}/selfcheck
  接口文档 : http://${IP_ADDR}:${PORT}/docs
  健康检查 : http://${IP_ADDR}:${PORT}/api/health

  查看日志 : ${COMPOSE} logs -f --tail=200
  重启服务 : ${COMPOSE} restart
  停止服务 : ${COMPOSE} down
  升级部署 : git pull && ${COMPOSE} up -d --build

${C_YELLOW}重要提醒${C_RESET}
  1. 云控制台安全组也要放行 TCP ${PORT}，否则外网访问不了；
  2. 首次启动需要拉取全市场快照与日线缓存（约 1~5 分钟），
     期间页面可以打开，数据会逐步补齐；
  3. 企业微信推送请在页面「用户配置 → 消息推送」里填写 Webhook，
     填完点「发送测试推送」验证；
  4. 数据卷名为 ${CONTAINER_NAME:-stock-space}-data，容器重建不会丢数据。

EOF
  exit 0
fi

# =============================================================================
# 路线 B：直接部署（venv + systemd）
# =============================================================================
title "4/8 安装 Python 环境"

install_packages() {
  case "$PKG_MGR" in
    apt-get)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq python3 python3-venv python3-pip ca-certificates tzdata >/dev/null
      ;;
    dnf) dnf install -y -q python3 python3-pip ca-certificates tzdata >/dev/null ;;
    yum) yum install -y -q python3 python3-pip ca-certificates tzdata >/dev/null ;;
    apk) apk add --no-cache python3 py3-pip tzdata >/dev/null ;;
    *)   warn "无法识别包管理器，请自行确认 python3 / venv 已安装" ;;
  esac
}

if ! command -v python3 >/dev/null 2>&1; then
  log "未检测到 python3，尝试安装…"
  install_packages
fi
command -v python3 >/dev/null 2>&1 || die "python3 不可用，请手动安装"

PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
log "Python 版本: ${PY_VER}"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "需要 Python 3.10 及以上（当前 ${PY_VER}）"

if ! python3 -c 'import venv' >/dev/null 2>&1; then
  log "缺少 venv 模块，尝试安装…"
  install_packages
fi
python3 -c 'import venv' >/dev/null 2>&1 || die "python3-venv 不可用"

VENV_DIR="${APP_DIR}/.venv"
if [ -x "${VENV_DIR}/bin/python" ]; then
  log "复用已存在的虚拟环境"
else
  log "创建虚拟环境…"
  python3 -m venv "$VENV_DIR"
  ok "虚拟环境已创建"
fi

VENV_PY="${VENV_DIR}/bin/python"
log "升级 pip 并安装依赖…"
"$VENV_PY" -m pip install --quiet --upgrade pip setuptools wheel
if ! "$VENV_PY" -m pip install --quiet -r "${APP_DIR}/backend/requirements.txt"; then
  warn "默认源安装失败，尝试清华镜像…"
  "$VENV_PY" -m pip install --quiet \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --extra-index-url https://pypi.mirrors.ustc.edu.cn/simple \
    -r "${APP_DIR}/backend/requirements.txt" || die "依赖安装失败"
fi
ok "运行依赖已安装"

# 可选数据源：装不上不影响主流程
if [ "${INSTALL_OPTIONAL:-false}" = "true" ]; then
  log "安装可选数据源（AKShare / Ashare）…"
  "$VENV_PY" -m pip install --quiet -r "${APP_DIR}/backend/requirements-optional.txt" \
    && ok "可选数据源已安装" \
    || warn "可选数据源安装失败（不影响主功能，可稍后手动安装）"
else
  log "跳过可选数据源（如需启用请设置 INSTALL_OPTIONAL=true）"
fi

title "5/8 环境自检"
if ! "$VENV_PY" "${APP_DIR}/backend/run.py" --check; then
  warn "自检存在告警项，服务仍会继续部署；请按上面的提示处理"
fi

title "6/8 注册 systemd 服务"
if [ "$NO_SERVICE" -eq 1 ]; then
  log "--no-service 已指定，跳过 systemd 注册"
else
  if [ -z "$RUN_USER" ]; then
    # 优先用 sudo 调用者，否则建一个专用系统账号
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
      RUN_USER="$SUDO_USER"
    else
      if ! id -u stockspace >/dev/null 2>&1; then
        NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
        useradd --system --create-home --shell "$NOLOGIN" stockspace 2>/dev/null \
          || adduser -S -D -H -s "$NOLOGIN" stockspace 2>/dev/null || true
      fi
      RUN_USER="stockspace"
    fi
  fi
  log "运行账号: ${RUN_USER}"

  chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}/data" "${APP_DIR}/logs" 2>/dev/null || true

  UNIT_SRC="${APP_DIR}/deploy/stock-space.service"
  UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
  [ -f "$UNIT_SRC" ] || die "缺少 ${UNIT_SRC}"

  if [ -f "$UNIT_DST" ] && [ "$FORCE" -ne 1 ]; then
    log "systemd 服务已存在，跳过（如需覆盖请加 --force）"
  else
    sed -e "s|__APP_DIR__|${APP_DIR}|g" \
        -e "s|__RUN_USER__|${RUN_USER}|g" \
        "$UNIT_SRC" > "$UNIT_DST"
    ok "已写入 ${UNIT_DST}"
  fi

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${SERVICE_NAME}"
  log "服务已启动，等待就绪…"

  ACTIVE=0
  for i in $(seq 1 60); do
    if systemctl is-active --quiet "${SERVICE_NAME}"; then ACTIVE=1; fi
    if "$VENV_PY" - "$PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as r:
    data = json.loads(r.read().decode("utf-8", "replace"))
sys.exit(0 if data.get("status") == "ok" else 1)
PY
    then
      ok "服务健康检查通过"
      break
    fi
    sleep 5
    if [ $((i % 6)) -eq 0 ]; then log "  等待中… (${i}/60)"; fi
    if [ "$i" -eq 60 ]; then
      err "服务未在预期时间内就绪，最近日志："
      journalctl -u "${SERVICE_NAME}" -n 40 --no-pager 2>&1 | sed 's/^/    /' || true
      exit 1
    fi
  done

  if [ "$ACTIVE" -ne 1 ]; then
    err "systemd 单元未处于 active 状态"
    systemctl status "${SERVICE_NAME}" --no-pager -l 2>&1 | sed 's/^/    /' || true
    exit 1
  fi
fi

title "7/8 反向代理与防火墙"
if [ -n "$DOMAIN" ]; then
  NGINX_SRC="${APP_DIR}/deploy/nginx.conf"
  NGINX_DST="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
  if command -v nginx >/dev/null 2>&1; then
    if [ -f "$NGINX_DST" ] && [ "$FORCE" -ne 1 ]; then
      log "nginx 配置已存在，跳过（如需覆盖请加 --force）"
    else
      sed -e "s|server app:8770;|server 127.0.0.1:${PORT};|" \
          -e "s|server_name _;|server_name ${DOMAIN};|" \
          "$NGINX_SRC" > "$NGINX_DST"
      if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx 2>/dev/null || true
        ok "nginx 已配置并生效（${DOMAIN}）"
      else
        warn "nginx -t 校验失败，请检查 ${NGINX_DST}"
        nginx -t 2>&1 | sed 's/^/    /' || true
      fi
    fi
  else
    warn "未安装 nginx，跳过反向代理配置"
  fi
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 && ok "ufw 已放行 ${PORT}/tcp"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  ok "firewalld 已放行 ${PORT}/tcp"
else
  log "未检测到已启用的 ufw/firewalld，跳过端口放行"
fi

title "8/8 完成"
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP_ADDR" ] && IP_ADDR="<服务器IP>"
cat <<EOF

${C_BOLD}部署完成（直接部署模式）${C_RESET}

  访问地址 : http://${IP_ADDR}:${PORT}/
  部署自检 : http://${IP_ADDR}:${PORT}/selfcheck
  接口文档 : http://${IP_ADDR}:${PORT}/docs
  健康检查 : http://${IP_ADDR}:${PORT}/api/health

  查看状态 : systemctl status ${SERVICE_NAME} --no-pager
  实时日志 : journalctl -u ${SERVICE_NAME} -f --no-pager
  重启服务 : systemctl restart ${SERVICE_NAME}
  停止服务 : systemctl stop ${SERVICE_NAME}
  升级代码 : git pull && systemctl restart ${SERVICE_NAME}

${C_YELLOW}重要提醒${C_RESET}
  1. 云控制台安全组也要放行 TCP ${PORT}，否则外网访问不了；
  2. 首次启动需要拉取全市场快照与日线缓存（约 1~5 分钟），
     期间页面可以打开，数据会逐步补齐；
  3. 企业微信推送请在页面「用户配置 → 消息推送」里填写 Webhook，
     填完点「发送测试推送」验证；
  4. 备份只需要打包 data/ 目录（含数据库、日线缓存与运行期设置）。

EOF
