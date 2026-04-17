# ---- 构建阶段：只装依赖，保持运行镜像干净 ----
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# ---- 运行阶段 ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AI_SQL_LOG_FORMAT=json \
    AI_SQL_AUTH_MODE=api_key

# 非 root 运行
RUN useradd --create-home --uid 10001 ai_sql

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=ai_sql:ai_sql . /app

# 运行期产物目录（记忆库、审计库、Demo 数据）
RUN mkdir -p /app/runtime_data && chown -R ai_sql:ai_sql /app/runtime_data

USER ai_sql

EXPOSE 8000

# 存活探针交给编排系统调 /health，这里给一个基础的容器级健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"

# 单进程多线程：Pipeline 与索引在进程内共享，多 worker 会各建一份索引。
# 需要更高并发时优先加副本数，而不是加 worker。
CMD ["uvicorn", "ai_sql_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
