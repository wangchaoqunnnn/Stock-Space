# =============================================================================
# StockSpace 镜像（多阶段构建，单容器同源部署）
# =============================================================================
# 设计要点：
#   1. 前端**零构建**（原生 HTML/CSS/JS），因此不需要 node 阶段 ——
#      整个镜像只有 Python 一个运行时，构建更快、体积更小、攻击面更少。
#   2. 虚拟环境搬运式多阶段：builder 里装依赖，runtime 只拷贝 /opt/venv。
#   3. 依赖顺序优化：先 COPY requirements.txt 再装包，改代码不会让依赖层失效。
#   4. 刻意不执行 apt-get（云服务器上 apt 往往是最慢的一步）——
#      健康检查用镜像内标准库脚本，不依赖 curl/wget。
#   5. 非 root 运行 + 数据/日志挂载卷。
# =============================================================================

# ---------- 阶段 1：构建虚拟环境 ----------
FROM python:3.12-slim AS builder

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://pypi.mirrors.ustc.edu.cn/simple
ARG INSTALL_OPTIONAL=false

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 依赖层：只依赖 requirements.txt，改业务代码不会让它失效
COPY backend/requirements.txt backend/requirements-optional.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_OPTIONAL" = "true" ]; then \
        pip install --no-cache-dir -r requirements-optional.txt ; \
    fi

# ---------- 阶段 2：运行镜像 ----------
FROM python:3.12-slim AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH" \
    SS_HOME=/app \
    SS_DATA_DIR=/app/data \
    SS_LOG_DIR=/app/logs \
    SS_CONFIG_DIR=/app/config \
    SS_SERVER__HOST=0.0.0.0 \
    SS_SERVER__PORT=8770 \
    SS_SCHEDULER__ENABLED=true

# 时区数据：行情时段判定依赖北京时间，容器默认 UTC 会算错交易时段
RUN set -eux; \
    if [ -f /usr/share/zoneinfo/Asia/Shanghai ]; then \
        ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
        echo "Asia/Shanghai" > /etc/timezone; \
    fi

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 只拷贝运行需要的内容（前端在 backend/stock_space/web 内，随包一起进镜像）
COPY backend/ /app/backend/
COPY config/ /app/config/
COPY VERSION /app/VERSION

# 非 root 用户 + 可写目录
RUN set -eux; \
    groupadd --gid ${APP_GID} appuser 2>/dev/null || true; \
    useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --shell /usr/sbin/nologin appuser 2>/dev/null || true; \
    mkdir -p /app/data /app/logs; \
    chown -R ${APP_UID}:${APP_GID} /app

USER appuser

EXPOSE 8770

VOLUME ["/app/data", "/app/logs"]

# 健康检查用镜像内标准库脚本，不依赖 curl/wget
HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=4 \
    CMD ["python", "/app/backend/docker_healthcheck.py"]

CMD ["python", "/app/backend/run.py"]
