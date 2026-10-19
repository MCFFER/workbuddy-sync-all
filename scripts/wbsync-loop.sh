#!/usr/bin/env bash
# wbsync-loop.sh — 独立后台循环（不依赖 WorkBuddy hook）
#
# 作用：每约 5 秒做两件事
#   1) 确保本机 Syncthing 在跑（幂等，端口在则跳过）
#   2) 补改名兜底：把 Syncthing 未完成的 .syncthing.<name>.tmp 改成正式文件
#      （某些 macOS 环境下 Syncthing 进程被单独限制 rename，导致同步卡住）
#
# 特点：纯本地文件操作，**不调用 AI、不消耗 token**；开销极低（每 5 秒一次轻量扫描）。
# 用法：
#   nohup ./wbsync-loop.sh >/dev/null 2>&1 &     # 启动
#   kill "$(cat ${TMPDIR:-/tmp}/wbsync-loop.pid)" # 停止
# 环境变量：WBSYNC_LOOP_INTERVAL 覆盖间隔秒数（默认 5）

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# wbsync.py 的定位：优先同目录（skill 内运行），否则回退到 skill 安装路径
# （安装到 ~/.workbuddy/scripts/ 时，wbsync.py 仍在 skill 目录下）
WBSYNC="$SCRIPT_DIR/wbsync.py"
if [ ! -f "$WBSYNC" ]; then
    WBSYNC="$HOME/.workbuddy/skills/workbuddy-sync-all/scripts/wbsync.py"
fi

if [ -x "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" ]; then
    PY="$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    PY="$(command -v python)"
fi

PIDFILE="${TMPDIR:-/tmp}/wbsync-loop.pid"
INTERVAL="${WBSYNC_LOOP_INTERVAL:-5}"
COMMS_FLAG="${TMPDIR:-/tmp}/wbsync-comms-unread.json"
A2A_FLAG="${TMPDIR:-/tmp}/wbsync-a2a-unread.json"

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

# 每约 6 轮（约 30 秒）做一次较重的事：Syncthing 存活检查 + 扫记事本/A2A 落盘未读提示
TICK=0
while true; do
    "$PY" "$WBSYNC" fixnames >/dev/null 2>&1
    if [ "$TICK" -le 0 ]; then
        "$PY" "$WBSYNC" start >/dev/null 2>&1
        "$PY" "$WBSYNC" --json comms check > "$COMMS_FLAG" 2>/dev/null
        "$PY" "$WBSYNC" --json a2a check > "$A2A_FLAG" 2>/dev/null
        TICK=6
    fi
    TICK=$((TICK-1))
    sleep "$INTERVAL"
done
