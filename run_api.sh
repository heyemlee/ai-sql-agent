#!/usr/bin/env bash
# 启动 AI SQL Agent FastAPI 服务
cd "$(dirname "$0")"
exec uvicorn ai_sql_api.app:app --host 0.0.0.0 --port 8000 "$@"
