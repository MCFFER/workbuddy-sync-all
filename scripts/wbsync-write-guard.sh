#!/usr/bin/env bash
# wbsync 写护栏（PreToolUse hook）
#
# 解决什么问题：同一台机器（同世代）上多个并行对话改同一份协作共享文件时，
#   Syncthing 不会产生 *.sync-conflict-* 副本，而是【后写覆盖先写】——无声、最难发现。
#   本护栏把这种「静默覆盖」变成「显式拦截」。
#
# 判定逻辑（任一不满足即放行）：
#   1. 工具必须是 Write / Edit / MultiEdit / NotebookEdit
#   2. file_path 必须落在协作共享区（见下方「协作共享区」；同步根取本机 config.json，
#      支持非默认同步根）
#   3. 命中下列任一即阻止：
#        a. 存在【他人】持有的、未过期的**同机编辑锁**（wbsync lock）
#        b. 该文件被【他机 / 他会话】**跨机预约**（wbsync reserve，写进 coop/shared）
#   → 才 exit 2 阻止，并把原因写 stderr 回传给 Agent
#
# 三条硬约束：
#   A. 放行路径必须【零输出】（exit 0，无 stdout/stderr）——否则每次写盘都会往上下文注入内容，白费 token
#   B. 任何异常一律放行（fail-open）——绝不因本脚本故障瘫痪所有文件写入
#   C. 只拦 agent 工具写入；用户在编辑器里手工改、或 Bash 内 sed -i，不在覆盖范围（已知限制）
#
# 注册：wbsync wguard on --apply   （写入 ~/.workbuddy/settings.json 的 hooks.PreToolUse）
# 卸载：wbsync wguard off --apply

set -u

INPUT=""
IFS= read -r -d '' INPUT || true
[ -z "$INPUT" ] && exit 0

# ---- 同步根：与 wbsync.py 的 cfg_sync_root() **同源** ----
#   wbsync 把真实同步根记在本机配置 `~/.workbuddy/sync-assistant/config.json` 的 `sync_root`
#   （可被用户改成非默认位置）。历史版本这里硬编码 "$HOME/WorkBuddySync"：一旦用户改了
#   sync_root，护栏读预约表的位置就与 `wbsync reserve` 落盘的位置不一致
#   → 跨机预约拦截**静默失效**（fail-open 不报错）。故改为运行时从同一份配置取值。
#   纯 bash 内建读取（无子进程），取不到才退回默认根。
_WB="${WORKBUDDY_HOME:-${HOME:-}/.workbuddy}"
_WB="${_WB%/}"
_SR=""
if [ -f "$_WB/sync-assistant/config.json" ]; then
  _CC="$(<"$_WB/sync-assistant/config.json")"
  if [[ "$_CC" =~ \"sync_root\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
    _SR="${BASH_REMATCH[1]}"
    _SR="${_SR//\\\\//}"       # JSON 转义的双反斜杠 → 正斜杠
    _SR="${_SR//\\//}"         # Windows 路径里的单反斜杠 → 正斜杠
  fi
fi
[ -z "$_SR" ] && _SR="${HOME:-}/WorkBuddySync"
_SR="${_SR%/}"

# ---- 快速负判定（逐层筛掉，只把少数可疑调用交给 python）----
# 完整判定要起 python（实测 70~300ms），而绝大多数工具调用与「写共享文件」无关。
# 以下每一层都只用 bash 内建（正则 + 参数展开），
# 安全方向：每一层都只可能「放行」，不可能「误拦」；任一层判断不了就落到最后的 python 精判。

# 第 1 层：非文件写入类工具（Bash / Read / Grep / Glob …）直接放行
if [[ "$INPUT" =~ \"tool_name\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  shopt -s nocasematch
  case "${BASH_REMATCH[1]}" in
    write|edit|multiedit|notebookedit) _ISWRITE=1 ;;
    *) _ISWRITE=0 ;;
  esac
  shopt -u nocasematch
  [ "$_ISWRITE" -eq 0 ] && exit 0
fi

if [ -z "${WBSYNC_SHARED_ROOTS:-}" ]; then
  # 第 2 层：写入目标不在协作共享区 → 放行
  if [[ "$INPUT" =~ \"file_path\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
    # JSON 里反斜杠是双写转义（\\），逐个替换成正斜杠；重复斜杠不影响子串判定
    NFP="${BASH_REMATCH[1]//\\\\//}"
    NFP="${NFP//\\//}"
    shopt -s nocasematch
    case "$NFP" in
      *"/.workbuddy/skills/workbuddy-sync-all/"*|*"${_WB}/skills/workbuddy-sync-all/"*) MATCH=1; _ONSKILL=1 ;;  # skill 代码 → 继续（且须进 python 做自动认领）
      *"${_SR}/"*) MATCH=1 ;;                                                                                    # 其它共享区 → 继续
      *) MATCH=0 ;;                                                                                              # 明确不共享 → 放行
    esac
    shopt -u nocasematch
    [ "$MATCH" -eq 0 ] && exit 0
  fi
fi

# 第 3 层：本机锁文件与跨机预约文件里都没有任何记录 → 放行（无锁时必然不拦，最常见路径）
# 用 $(<file) 与 bash 模式匹配读取，避免再起 grep 子进程（本机每起一个子进程约 13ms）
_LK="${_WB}/.wbsync-edit-locks.json"
_RS="${_SR}/coop/shared/.file-reservations.json"
_HASANY=0
if [ -f "$_LK" ]; then
  _LC="$(<"$_LK")"
  [[ "$_LC" == *'"sid"'* ]] && _HASANY=1
fi
if [ "$_HASANY" -eq 0 ] && [ -f "$_RS" ]; then
  _RC="$(<"$_RS")"
  [[ "$_RC" == *'"holder_session"'* ]] && _HASANY=1
fi
# skill 目标即使当前无锁也要进 python（#6 自动认领）；其它目标无锁则放行（最常见路径，零成本）
[ "$_HASANY" -eq 0 ] && [ "${_ONSKILL:-0}" -eq 0 ] && exit 0

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -z "$PY" ] && exit 0

# 全部判定放在一次 python 调用里完成；退出码约定：0=放行（静默）、2=阻止（stderr 有原因）
"$PY" - "$INPUT" <<'PYEOF'
import json
import os
import sys
import time


def _norm(p):
    """归一化路径：反斜杠→正斜杠、去重复斜杠、小写（Windows/macOS 大小写不敏感）。"""
    if not p:
        return ""
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/").lower()


def _starts_with(path, root):
    if not root:
        return False
    return path == root or path.startswith(root + "/")


try:
    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    data = json.loads(raw)
except Exception:
    sys.exit(0)          # 解析失败 → 放行（约束 B）

tool = data.get("tool_name") or ""
if tool not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
    sys.exit(0)

ti = data.get("tool_input") or {}
fp = _norm(ti.get("file_path") or "")
if not fp:
    sys.exit(0)

home = _norm(os.path.expanduser("~") or os.environ.get("HOME") or "")
wb = _norm(os.environ.get("WORKBUDDY_HOME") or "") or (home + "/.workbuddy" if home else "")


def _cfg_sync_root():
    """与 wbsync.py 的 cfg_sync_root() **同源**：读本机 config.json 的 sync_root。

    护栏若与 reserve 用不同的根，跨机预约拦截会静默失效（fail-open 掩盖），故必须同源。
    读不到配置 / 字段为空时退回默认根 `<主目录>/WorkBuddySync`。
    """
    cfg = wb + "/sync-assistant/config.json" if wb else ""
    r = ""
    if cfg and os.path.exists(cfg):
        try:
            r = json.loads(open(cfg, encoding="utf-8").read() or "{}").get("sync_root") or ""
        except Exception:
            r = ""
    r = _norm(r)
    return r or ((home + "/workbuddysync") if home else "")


sync_root = _cfg_sync_root()

# ---- 豁免：本机制自身产物（不豁免会导致 wake 自动登记心跳被自己拦住）----
EXEMPT_SUBSTR = (
    "/.dialogue-registry", "/.stversions", "/.stfolder",
    "/__pycache__", ".sync-conflict-", "/.wbsync-edit-locks.json",
    "/.wbsync-dialogue-id", "/wbsync-write-guard.sh",
    "/.file-reservations.json",   # 预约台账自身：由 wbsync 维护，勿拦（否则 claim 写入被自己挡）
)
low = fp
for s in EXEMPT_SUBSTR:
    if s.lower() in low:
        sys.exit(0)

# ---- 协作共享区 ----
# 根 = 本机真实同步根（读 config.json，见 _cfg_sync_root；非默认同步根同样覆盖）
#    + skill 代码目录（一动影响三机）；可用环境变量 WBSYNC_SHARED_ROOTS（冒号分隔）追加。
roots = []
if sync_root:
    roots.append(sync_root)
    roots.append(sync_root + "/workspaces")
    roots.append(sync_root + "/coop/shared")
if wb:
    roots.append(wb + "/skills/workbuddy-sync-all")
extra = os.environ.get("WBSYNC_SHARED_ROOTS") or ""
for r in extra.split(os.pathsep):
    if r.strip():
        roots.append(_norm(os.path.expanduser(r.strip())))

if not any(_starts_with(fp, r) for r in roots):
    sys.exit(0)          # 不在共享区 → 放行（绝大多数写入走这条，零成本）

# ---- 对标借鉴 A · 覆盖告知：写前比对「上次记账后文件是否被改过」→ 会覆盖则告警（不阻断）----
#   账本由 PostToolUse（wbsync-write-guard-post.sh）在每次写入后记 {sha, sid}。
#   当前 sha ≠ 账本 sha ⇒ 自上次写入以来文件已被改动 ⇒ 本次写将覆盖那次改动 → 提醒先看最新内容。
try:
    import hashlib as _hl
    _lp = home + "/.workbuddy/.wbsync-file-ledger.json"
    _led = json.loads(open(_lp, encoding="utf-8").read() or "{}") if os.path.exists(_lp) else {}
    _rec = _led.get(fp) if isinstance(_led, dict) else None
    if _rec and _rec.get("sha") and os.path.isfile(ti.get("file_path")):
        _cur = _hl.sha256(open(ti.get("file_path"), "rb").read()).hexdigest()[:16]
        if _cur != _rec["sha"]:
            sys.stderr.write(
                "[wbsync 写护栏] ⚠️ 覆盖提醒：%s\n"
                "  该文件自上次写入（%s，写者 %s）后已被修改 —— 你继续写会【覆盖】那次改动。\n"
                "  建议：先读最新内容再改（别把别人的改动冲掉）。本条不阻断写入。\n"
                % (ti.get("file_path"), _rec.get("at") or "-", (_rec.get("sid") or "")[:8]))
except Exception:
    pass

# ---- 查锁（#6：scope 感知 + skill 自动认领）----
#   锁记录可带 scope（路径前缀）：空=全局（覆盖一切）；非空=仅覆盖该前缀内的文件。
if not home:
    sys.exit(0)
lock_path = home + "/.workbuddy/.wbsync-edit-locks.json"
try:
    locks = ((json.loads(open(lock_path, encoding="utf-8").read() or "{}")).get("locks") or []
             if os.path.exists(lock_path) else [])
except Exception:
    locks = []           # 锁文件坏了 → 视为无锁，继续走预约判定（约束 B：绝不误拦）

# 本对话 sid 取数与 wbsync._session_id() **同源**（env → 本机缓存 → 兜底 hook 的 session_id）：
#   ① me 用全集 → 任何来源匹配都算「本对话」，消除自拦（子 agent/新进程 pid 会变，但不影响）；
#   ② 写锁用 sid_me（按 wbsync 优先级）→ 该锁能被 `wbsync lock release` 正常释放。
_sid_env = ""
for _e in ("CLAUDE_SESSION_ID", "WB_SESSION_ID", "WORKBUDDY_SESSION_ID"):
    _v = (os.environ.get(_e) or "").strip()
    if _v:
        _sid_env = _v
        break
_sid_cache = ""
try:
    _sid_cache = open(home + "/.workbuddy/.wbsync-dialogue-id", encoding="utf-8").read().strip()
except Exception:
    _sid_cache = ""
_sid_hook = (data.get("session_id") or "").strip()
me = {x for x in (_sid_env, _sid_cache, _sid_hook) if x}
sid_me = _sid_env or _sid_cache or _sid_hook

now = time.time()


def _covers(lock, path):
    """该锁是否覆盖 path：scope 空 = 全局；否则 path 须落在 scope 内。"""
    sc = _norm(lock.get("scope") or "")
    return (not sc) or _starts_with(path, sc)


# 1) 【他人】持「覆盖本文件」的锁 → 拦（同机静默覆盖）
foreign = [l for l in locks
           if (l.get("expires") or 0) > now
           and (l.get("sid") or "") not in me
           and _covers(l, fp)]
for l in foreign:
    mins = max(0, int(((l.get("expires") or 0) - now) // 60))
    sys.stderr.write(
        "[wbsync 写护栏] 已阻止写入共享文件：%s\n"
        "  原因：另一对话正持锁改共享文件 —— %s（用途：%s，剩余约 %d 分钟）\n"
        "  说明：同机多对话改同一文件不会产生冲突副本，而是后写静默覆盖先写。\n"
        "  处理：等对方 `wbsync lock release` 或锁自动过期；"
        "若确认无冲突，先 `wbsync lock claim \"<用途>\" --force` 再写。\n"
        % (ti.get("file_path"), l.get("owner") or "?", l.get("purpose") or "-", mins))
if foreign:
    sys.exit(2)

# 2) skill 代码目录：**自动认领**（#6 零摩擦强制 · 用户强条「同一时刻全集群只允许一个机器的一个对话改 skill」）
#    本对话无覆盖 skill 的锁、且无他人锁 → 自动占一条 scope=skill 的锁（TTL 默认 20min）。
#    零输出放行（约束 A：不污染上下文）；写盘失败 fail-open 放行（约束 B）。
# 高风险文件（2026-10-17 用户定：折中「只对高风险强制」）——
#   skill 代码 / 规范 / 纪律 / README / 登记表：一律**自动认领锁**。
#   语义 = 强制互斥 + 零摩擦：无锁时自动占一条；已有他人锁则被上面的 1) 拦掉。
#   这样「两个对话都忘了 claim 就同时写」这个缺口被堵上，而不用每次人工 claim。
HIGH_RISK = (
    "/skills/workbuddy-sync-all/",
    "/coop/shared/规范-", "/coop/shared/纪律-", "/coop/shared/README.md",
    "/coop/shared/工作空间登记.json", "/coop/shared/.file-reservations.json",
)
skill_root = _norm(wb + "/skills/workbuddy-sync-all") if wb else ""
_risk_scope = ""
if skill_root and _starts_with(fp, skill_root):
    _risk_scope = skill_root
else:
    for _p in HIGH_RISK:
        if _p in fp:
            _risk_scope = _norm(sync_root + _p) if _p.startswith("/coop/") else fp
            break
# 以文件级 scope 更精确（避免整目录互斥过宽）
if _risk_scope and _risk_scope != skill_root and os.path.isfile(ti.get("file_path") or ""):
    _risk_scope = fp

if _risk_scope:
    mine_cov = [l for l in locks if (l.get("sid") or "") in me
                and (l.get("expires") or 0) > now and _covers(l, fp)]
    if not mine_cov and sid_me:
        ttl = int(os.environ.get("WBSYNC_AUTOLOCK_TTL_MIN") or 10)
        rec = {"sid": sid_me, "owner": "auto", "scope": _risk_scope,
               "purpose": ("自动锁·改 skill" if _risk_scope == skill_root
                           else "自动锁·高风险共享文件"),
               "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "expires": now + ttl * 60}
        keep = [l for l in locks if (l.get("expires") or 0) > now]
        _payload = json.dumps({"locks": keep + [rec],
                               "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False)
        try:
            _tmp = lock_path + ".tmp.%d" % os.getpid()      # ② 原子写，防与 wbsync 并发读-改-写互相覆盖
            with open(_tmp, "w", encoding="utf-8") as fh:
                fh.write(_payload)
            os.replace(_tmp, lock_path)
        except Exception:
            try:
                os.unlink(_tmp)
            except Exception:
                pass
            try:                                            # 回退：直接写（非原子，但保证认领成功）
                with open(lock_path, "w", encoding="utf-8") as fh:
                    fh.write(_payload)
            except Exception:
                pass         # 都失败 → 放行（fail-open，约束 B）

# ---- 跨机预约（写进 coop/shared，随 Syncthing 同步）：他机 / 他会话持有效预约 → 同样阻止 ----
# 主键必须与 wbsync.py 的 _reservation_key() **严格一致**（两边算出不同键 → 拦截静默失效）：
#   ① 同步根内 → 相对同步根的路径；② 否则主目录内 → "~/…"（如 skill 代码目录）；③ 都不在 → 不比对。
resv_path = (sync_root + "/coop/shared/.file-reservations.json") if sync_root else ""
if sync_root and _starts_with(fp, sync_root):
    key = fp[len(sync_root) + 1:]
elif home and _starts_with(fp, home):
    key = "~/" + fp[len(home) + 1:]
else:
    key = ""
if key and resv_path and os.path.exists(resv_path):
    try:
        resv = json.loads(open(resv_path, encoding="utf-8").read() or "{}")
    except Exception:
        resv = {}
    for k, v in (resv or {}).items():
        if (_norm(k) == key and (v.get("expires") or 0) > now
                and (v.get("holder_session") or "") not in me):
            mins = max(0, int(((v.get("expires") or 0) - now) // 60))
            sys.stderr.write(
                "[wbsync 写护栏] 已阻止写入共享文件：%s\n"
                "  原因：另一机器/对话已【预约】该文件（跨机互斥）—— %s 机（会话 %s，剩余约 %d 分钟，用途：%s）\n"
                "  说明：跨机同时改同一份文件会互相覆盖；预约是为了让 A/B/C 错峰。\n"
                "  处理：等对方 `wbsync reserve release` / 会话关闭 / TTL 到期；"
                "确认无冲突先 `wbsync reserve claim \"<用途>\" --force` 再写。\n"
                % (ti.get("file_path"), v.get("holder_machine") or "?",
                   (v.get("holder_session") or "")[:8], mins, v.get("reason") or "-"))
            sys.exit(2)
sys.exit(0)
PYEOF
rc=$?
exit $rc
