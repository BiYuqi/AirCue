#!/bin/bash

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 检查虚拟环境
if [ ! -d "venv" ]; then
  echo "未找到虚拟环境，正在创建..."
  python3 -m venv venv
  echo "安装依赖..."
  venv/bin/pip install -r requirements.txt
fi

# 检查 .env
if [ ! -f ".env" ]; then
  echo "错误：未找到 .env 文件，请先配置 TG_BOT_TOKEN"
  exit 1
fi

echo "启动 TG Audio Bot..."
venv/bin/python bot.py
