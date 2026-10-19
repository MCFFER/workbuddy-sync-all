#!/usr/bin/env bash
# wbsync 对话框生命周期 hook（SessionStart / UserPromptSubmit / SessionEnd）
#
# 作用：把「本对话框是开着还是已关闭」写成**显式状态**，而不是靠文件时间猜。
#   - SessionStart      → 记录标记为 open
#   - UserPromptSubmit  → 保活（刷新 last_seen，说明窗口还在用）
#   - SessionEnd        → 记录标记为 closed（带 close_reason）
#   - 崩溃兜底：仍为 open 但超时无活动 → 展示为「疑似已关闭（未收到关闭事件）」
#
# 两条硬约束：
#   A. 必须【完全静默】（不写 stdout/stderr）——SessionStart/UserPromptSubmit 的 stdout
#      会被当作额外上下文注入，静默才能保证零 token 成本。
#   B. 必须【永远 exit 0】——绝不能因本脚本影响会话开始 / 阻塞用户提问。
#
# 注册：wbsync dialogue-hook on --apply

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

WBSYNC=""
for cand in \
  "$HOME/.workbuddy/skills/workbuddy-sync-all/scripts/wbsync.py" \
  "$SELF_DIR/../skills/workbuddy-sync-all/scripts/wbsync.py"; do
  if [ -f "$cand" ]; then WBSYNC="$cand"; break; fi
done
[ -z "$WBSYNC" ] && exit 0

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -z "$PY" ] && exit 0

# stdin 是本条 hook 的 JSON；输出全部丢弃（约束 A）
exec "$PY" "$WBSYNC" dialogue hb >/dev/null 2>&1
