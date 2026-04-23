#!/bin/bash

if pkill -f "bot.py" 2>/dev/null; then
  echo "TG Audio Bot 已停止"
else
  echo "Bot 未在运行"
fi
