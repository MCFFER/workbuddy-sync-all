#!/usr/bin/env bash
# wbsync 写护栏 · 写后记账（PostToolUse hook）—— 对标借鉴 A「覆盖即告知」
#
# 作用：Write/Edit 受保护文件**之后**，把该文件 sha256(前16位) + 写者 sid 记入本机账本
#   `~/.workbuddy/.wbsync-file-ledger.json`（key=归一化路径 → {sha, sid, at, p}）。
#   供 PreToolUse（wbsync-write-guard.sh）写前比对：若「当前 sha ≠ 账本 sha」= 该文件自上次
#   记账后被改过 → 本次写入会【覆盖】那次改动 → 告警（不阻断）。
#
# 三条硬约束（同 PreToolUse 护栏）：
#   A. 必须完全静默（放行时零 stdout/stderr）；B. 永远 exit 0；C. 异常一律放行（fail-open）。
# 注册：wbsync wguard on --apply

set -u
INPUT=""
IFS= read -r -d '' INPUT || true
[ -z "$INPUT" ] && exit 0

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -z "$PY" ] && exit 0

"$PY" - "$INPUT" <<'PYEOF'
import hashlib, json, os, sys, time

try:
    d = json.loads(sys.argv[1] if len(sys.argv) > 1 else "{}")
except Exception:
    sys.exit(0)

tool = d.get("tool_name") or ""
if tool not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
    sys.exit(0)
ti = d.get("tool_input") or {}
fp = ti.get("file_path") or ""
if not fp or not os.path.isfile(fp):
    sys.exit(0)


def _norm(p):
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/").lower()


home = os.path.expanduser("~")
wb = os.environ.get("WORKBUDDY_HOME") or (home + "/.workbuddy")

# 只在受保护区记账（同步根 + skill）
SR = ""
cfg = wb + "/sync-assistant/config.json"
if os.path.exists(cfg):
    try:
        SR = _norm(json.loads(open(cfg, encoding="utf-8").read() or "{}").get("sync_root") or "")
    except Exception:
        SR = ""
if not SR:
    SR = _norm(home + "/WorkBuddySync")
n = _norm(fp)
skill = _norm(wb + "/skills/workbuddy-sync-all")
if not (n.startswith(SR + "/") or n.startswith(skill + "/")):
    sys.exit(0)

try:
    h = hashlib.sha256(open(fp, "rb").read()).hexdigest()[:16]
except Exception:
    sys.exit(0)

# 写者 sid 与 wbsync._session_id() 同源（env → 缓存 → hook 兜底）
sid = ""
for e in ("CLAUDE_SESSION_ID", "WB_SESSION_ID", "WORKBUDDY_SESSION_ID"):
    v = (os.environ.get(e) or "").strip()
    if v:
        sid = v
        break
if not sid:
    try:
        sid = open(wb + "/.wbsync-dialogue-id", encoding="utf-8").read().strip()
    except Exception:
        sid = ""
if not sid:
    sid = (d.get("session_id") or "").strip()

lp = wb + "/.wbsync-file-ledger.json"
try:
    led = json.loads(open(lp, encoding="utf-8").read() or "{}")
except Exception:
    led = {}
if not isinstance(led, dict):
    led = {}
led[n] = {"sha": h, "sid": sid, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "p": fp}
if len(led) > 500:                                   # 只留最近 500 条
    led = dict(sorted(led.items(), key=lambda kv: kv[1].get("at", ""))[-500:])
try:
    tmp = lp + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(led, ensure_ascii=False))
    os.replace(tmp, lp)
except Exception:
    pass
sys.exit(0)
PYEOF
exit 0
