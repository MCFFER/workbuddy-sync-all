#!/usr/bin/env bash
# WorkBuddy 多机同步：随 WB 会话启动 Syncthing，WB 退出后自动停止。
# 由 WorkBuddy 的 SessionStart hook 调用。跨平台（macOS / Linux / Windows 需 bash）。
#
# 设计目标：
#   - 打开对话 -> 拉起 Syncthing（幂等，已在跑则跳过）
#   - 关闭 WB 客户端 -> 看门狗检测到主进程消失 -> 自动 wbsync stop
#   - 不依赖开机自启，也不依赖用户手动跑命令

set -u

# ---- 路径自动探测（不硬编码用户名/绝对路径） ----

# 本脚本所在目录 = skill 的 scripts/ 目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
# wbsync.py 的定位：优先同目录（skill 内运行），否则回退到 skill 安装路径
# （安装到 ~/.workbuddy/scripts/ 时，wbsync.py 仍在 skill 目录下）
WBSYNC="$SCRIPT_DIR/wbsync.py"
if [ ! -f "$WBSYNC" ]; then
    WBSYNC="$HOME/.workbuddy/skills/workbuddy-sync-all/scripts/wbsync.py"
fi

# Python：优先用 WorkBuddy 托管的 python3，否则退回系统 python3
if [ -x "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" ]; then
    PY="$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
else
    echo "[wbsync-hook] 未找到 python，跳过" >&2
    exit 1
fi

WATCHDOG_PIDFILE="${TMPDIR:-/tmp}/wbsync-watchdog.pid"

# 机器间留言的「未读提示」落点：每次会话启动时跑一次 comms check，
# 把结果写到这里，供本机 Agent 在会话开始时读取、主动提醒用户。
COMMS_FLAG="${TMPDIR:-/tmp}/wbsync-comms-unread.json"

# A2A 结构化任务的「未读提示」落点（与 comms 同理）。
A2A_FLAG="${TMPDIR:-/tmp}/wbsync-a2a-unread.json"

# 看门狗自己的日志：只记关键决策（启动 / 判定 WB 消失 / 停同步）。
# 加它的理由：2026-10-14 排查看门狗反复消失时全靠猜，因为没有留痕。
WLOG="${TMPDIR:-/tmp}/wbsync-watchdog.log"
wlog() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$WLOG" 2>/dev/null || true; }

# ---- 1. 拉起 Syncthing（幂等） ----
"$PY" "$WBSYNC" start >/dev/null 2>&1

# ---- 1.5 扫一眼机器间沟通记事本，落盘未读提示 ----
# 只在记事本存在时检查；Syncthing 刚拉起、文件可能还没同步到位，静默容错。
if [ -x "$PY" ]; then
    "$PY" "$WBSYNC" --json comms check > "$COMMS_FLAG" 2>/dev/null || \
        echo '{"messages":[]}' > "$COMMS_FLAG" 2>/dev/null || true
fi

# ---- 2. 幂等保护：看门狗已在跑就直接退出 ----
# pidfile 里存的是「看门狗子壳自己的 PID」（由下方 $! 写入）。
# 若文件在、但进程已不在（陈旧 pidfile），顺手清掉再往下走。
if [ -f "$WATCHDOG_PIDFILE" ]; then
    OLD_PID="$(cat "$WATCHDOG_PIDFILE" 2>/dev/null)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0
    fi
    rm -f "$WATCHDOG_PIDFILE" 2>/dev/null
fi

# ---- 3. 判断 WB 主进程是否存活（按平台） ----
wb_alive() {
    case "$(uname -s)" in
        Darwin)
            # 匹配任意 WorkBuddy 进程（主进程 / Helper(GPU) / Helper(Renderer) 都含此路径片段）。
            # 2026-10-14 修正：原先只匹配 ".../Contents/MacOS/Electron"，实测该主进程在
            # 部分执行环境里对 pgrep **不可见**（助手进程可见、主进程不可见），于是
            # wb_alive 永远返回假 → 看门狗 60 秒后必然判「WB 已退出」→ wbsync stop 并自杀，
            # 表现为「看门狗总是莫名其妙不见了」。放宽到 Contents 路径片段即可稳定命中。
            pgrep -f "WorkBuddy\.app/Contents" >/dev/null 2>&1
            ;;
        Linux)
            pgrep -f "workbuddy" >/dev/null 2>&1
            ;;
        *)
            # Windows（Git Bash / MSYS）：查 WorkBuddy.exe
            pgrep -f "WorkBuddy.exe" >/dev/null 2>&1 || pgrep -f "WorkBuddy" >/dev/null 2>&1
            ;;
    esac
}

# ---- 4. 启动看门狗（含频繁扫描记事本） ----
#
# PID 记录方式（2026-10-14 修复）：
#   必须由「父壳」用 $! 取后台子壳的真实 PID 写进 pidfile，不能在子壳里用 $$。
#   原因：macOS 自带 /bin/bash 为 3.2（无 $BASHPID），而 POSIX 语义下子壳里的 $$
#         仍等于**父壳** PID，父壳紧接着就 exit 了。原先 `echo $$ > pidfile` 记的是
#         「已退出的父壳」PID，后果两条：
#           ① 幂等保护（上面的 kill -0）永远失效 → 每次开会话多起一个看门狗；
#           ② 任何状态检查都会把**存活的**看门狗误报成「已死」。
#   另外把 stdin 重定向到 /dev/null，避免子壳持有调用方的终端/管道。
(
    trap 'rm -f "$WATCHDOG_PIDFILE"' EXIT INT TERM

    # 给 WB 一个启动窗口，60 秒后再开始判定
    sleep 60
    wlog "看门狗就绪，开始监测 WB 存活（每 10 秒一次）"

    # 上次扫描到的留言「指纹」（用于检测「是否有新留言」）
    LAST_SIG=""
    LAST_A2A_SIG=""

    MISS=0
    while true; do
        if wb_alive; then
            MISS=0

            # —— 频繁扫描机器间沟通记事本 ——
            # 每次循环（约 10 秒）跑一次 comms check，检测有没有新留言；
            # 有变化就更新未读提示文件，供本机 Agent 读取、主动提醒/处理。
            if [ -x "$PY" ]; then
                NEW_OUT="$("$PY" "$WBSYNC" --json comms check 2>/dev/null)"
                if [ -n "$NEW_OUT" ]; then
                    NEW_SIG="$(printf '%s' "$NEW_OUT" | shasum 2>/dev/null | awk '{print $1}')"
                    if [ -z "$NEW_SIG" ]; then
                        NEW_SIG="$(printf '%s' "$NEW_OUT" | md5sum 2>/dev/null | awk '{print $1}')"
                    fi
                    # 首次或内容变化时，落盘提示文件
                    if [ "$NEW_SIG" != "$LAST_SIG" ]; then
                        printf '%s' "$NEW_OUT" > "$COMMS_FLAG" 2>/dev/null
                        LAST_SIG="$NEW_SIG"
                    fi
                fi

                # —— 补改名兜底：若本机 Syncthing 进程被限制 rename，
                #    它下载完的 .syncthing.<name>.tmp 无法改名成正式文件（会卡住同步）。
                #    这里每轮代它完成改名。纯本地文件操作，无 AI、无 token。
                "$PY" "$WBSYNC" fixnames >/dev/null 2>&1

                # —— 顺带扫 A2A 结构化任务，落盘未读提示 ——
                A2A_OUT="$("$PY" "$WBSYNC" --json a2a check 2>/dev/null)"
                if [ -n "$A2A_OUT" ]; then
                    A2A_SIG="$(printf '%s' "$A2A_OUT" | shasum 2>/dev/null | awk '{print $1}')"
                    if [ "$A2A_SIG" != "$LAST_A2A_SIG" ]; then
                        printf '%s' "$A2A_OUT" > "$A2A_FLAG" 2>/dev/null
                        LAST_A2A_SIG="$A2A_SIG"
                    fi
                fi
            fi
        else
            MISS=$((MISS+1))
            # 连续 3 次（约 30 秒）检测不到才判定退出，防偶发抖动
            if [ "$MISS" -ge 3 ]; then
                wlog "连续 3 次判定 WB 已退出 → 执行 wbsync stop --apply 并退出看门狗"
                "$PY" "$WBSYNC" stop --apply >/dev/null 2>&1
                rm -f "$WATCHDOG_PIDFILE"
                exit 0
            fi
        fi
        sleep 10
    done
) </dev/null >/dev/null 2>&1 &

# 由父壳记录后台子壳的真实 PID（详见上方注释）
echo $! > "$WATCHDOG_PIDFILE"
wlog "hook: 已拉起看门狗 pid=$!"

exit 0
