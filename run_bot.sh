#!/bin/bash
# 钉钉 MCP 机器人启动脚本（由 launchd 调用，对应 ~/Library/LaunchAgents/com.xmly.dingtalk-mcp-bot.plist）
# 作用：切到工程目录、防止重复实例（避免两个 Stream 连接冲突）、拉起 bot。
set -u

PROJ="/Users/xmly/dingtalk-mcp-bot"
cd "$PROJ" || exit 1
mkdir -p "$PROJ/logs"

# 已有 python bot.py 在跑就直接退出；launchd 的 KeepAlive 会在崩溃后自动重启。
if pgrep -f "[b]ot\.py" >/dev/null 2>&1; then
  echo "$(date) bot.py 已在运行，本次跳过" >&2
  exit 0
fi

exec "$PROJ/.venv/bin/python" bot.py
