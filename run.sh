#!/usr/bin/env bash
# ChanPicker 一键启动脚本：安装依赖并启动后端 + 前端服务
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[1/3] 创建 Python 虚拟环境..."
  python3 -m venv .venv
fi

echo "[2/3] 安装依赖..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

echo "[3/3] 启动服务: http://127.0.0.1:8787"
exec .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787
