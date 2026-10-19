#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 没有多余电脑时：用「本机第二个 Syncthing 实例」模拟一台新机器，演练接入流程。
#
# 原理：Syncthing 的设备 ID 由配置目录里的 cert.pem / key.pem 生成。给第二个
#   实例一个独立的 --home，它就拥有**全新设备 ID**，等价于一台新机器。
#   wbsync 侧再用独立的 WORKBUDDY_HOME 造出第二套「本机身份」（其 config.json
#   的 syncthing_home 指向该实例），与生产环境完全隔离、互不干扰。
#
# 能演练：编号分配（probe → D/E…）、建盘（workspace init）、配对与共享夹、
#   A2A / 记事本通道、权限与目录布局。
# 不能演练：跨网络的发现 / 中继 / AP 隔离（两实例同机走回环）；
#   WorkBuddy 客户端层（资料库登记、hook 交互、AskUserQuestion）。
#
# 用法：
#   bash scripts/simulate-new-machine.sh up                       # 起模拟机（幂等）
#   bash scripts/simulate-new-machine.sh status                   # 看模拟机状态
#   bash scripts/simulate-new-machine.sh run probe --json         # 在模拟机上跑命令
#   bash scripts/simulate-new-machine.sh down                     # 停实例 + 清理目录
#
# 可调：WBSYNC_SIM_DIR（默认 /tmp/wbsync-sim-new）、WBSYNC_SIM_GUI（默认 18394）
# ---------------------------------------------------------------------------
set -euo pipefail

SIM_DIR="${WBSYNC_SIM_DIR:-/tmp/wbsync-sim-new}"
SIM_GUI="${WBSYNC_SIM_GUI:-18394}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="$SKILL_DIR/scripts/wbsync.py"

find_syncthing() {
    for p in "$HOME/WorkBuddySync/bin/syncthing" "$HOME/.workbuddy/bin/syncthing"; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v syncthing 2>/dev/null && return 0
    return 1
}

wait_gui() {
    local i
    for i in $(seq 1 30); do
        curl -s -m 1 --noproxy '*' -o /dev/null "http://127.0.0.1:$SIM_GUI/" && return 0
        sleep 1
    done
    return 1
}

write_isolated_config() {
    mkdir -p "$SIM_DIR/wbhome/sync-assistant"
    cat > "$SIM_DIR/wbhome/sync-assistant/config.json" <<EOF
{
  "version": 1,
  "sync_root": "$SIM_DIR/sync",
  "peer_id": "",
  "peer_name": "",
  "syncthing_home": "$SIM_DIR/sthome",
  "syncthing_bin": "$(find_syncthing)",
  "dead_ids": [],
  "folder_ids": [],
  "disk_root": "$SIM_DIR/sync",
  "initialized": false,
  "workspace_root": "$SIM_DIR/ws"
}
EOF
}

cmd_up() {
    local st
    st="$(find_syncthing)" || { echo "找不到 syncthing 二进制（先跑 wbsync install）"; exit 1; }

    mkdir -p "$SIM_DIR/sthome" "$SIM_DIR/sync" "$SIM_DIR/ws"
    write_isolated_config
    # 让模拟机能「看到」已有的 A/B/C：把生产 coop 的**快照**拷进隔离 sync_root。
    # 用副本而非软链，避免任何写入回流到生产。
    if [ -d "$HOME/WorkBuddySync/coop" ] && [ ! -d "$SIM_DIR/sync/coop" ]; then
        cp -R "$HOME/WorkBuddySync/coop" "$SIM_DIR/sync/coop"
    fi

    if curl -s -m 1 --noproxy '*' -o /dev/null "http://127.0.0.1:$SIM_GUI/"; then
        echo "模拟机已在运行（管理端口 ${SIM_GUI}）"
    else
        nohup "$st" serve --no-browser -H "$SIM_DIR/sthome" \
              --gui-address="http://127.0.0.1:$SIM_GUI" \
              >> "$SIM_DIR/syncthing.log" 2>&1 &
        echo $! > "$SIM_DIR/syncthing.pid"
        wait_gui || { echo "模拟机启动超时，见 $SIM_DIR/syncthing.log"; exit 1; }
        echo "模拟机已启动（pid $(cat "$SIM_DIR/syncthing.pid")，管理端口 ${SIM_GUI}）"
    fi

    echo "--- 新机器身份 ---"
    SIM_DIR="$SIM_DIR" SIM_GUI="$SIM_GUI" bash "$0" run id 2>/dev/null | sed -n '4p'
    echo "--- 下一步 ---"
    echo "  bash $0 run probe --json         # 看它被判为第几台（预期 D）"
    echo "  bash $0 run workspace init --path $SIM_DIR/ws --apply"
    echo "  bash $0 down                     # 用完清理"
}

cmd_env() {
    echo "export WORKBUDDY_HOME=$SIM_DIR/wbhome"
    echo "export WBSYNC_GUI_ADDR=127.0.0.1:$SIM_GUI"
}

cmd_run() {
    WORKBUDDY_HOME="$SIM_DIR/wbhome" WBSYNC_GUI_ADDR="127.0.0.1:$SIM_GUI" \
        python3 "$W" "$@"
}

cmd_status() {
    if curl -s -m 1 --noproxy '*' -o /dev/null "http://127.0.0.1:$SIM_GUI/"; then
        echo "模拟机运行中：管理端口 ${SIM_GUI}；目录 $SIM_DIR"
    else
        echo "模拟机未运行（目录 ${SIM_DIR} 存在与否：$([ -d "$SIM_DIR" ] && echo 是 || echo 否)）"
    fi
}

cmd_down() {
    if [ -f "$SIM_DIR/syncthing.pid" ]; then
        kill "$(cat "$SIM_DIR/syncthing.pid")" 2>/dev/null || true
        rm -f "$SIM_DIR/syncthing.pid"
    fi
    pkill -f "$SIM_DIR/sthome" 2>/dev/null || true
    rm -rf "$SIM_DIR"
    echo "模拟机已停止，目录已清理：$SIM_DIR"
    echo "（生产实例与生产 config 全程未被触碰）"
}

case "${1:-up}" in
    up)     shift || true; cmd_up ;;
    env)    cmd_env ;;
    run)    shift; cmd_run "$@" ;;
    status) cmd_status ;;
    down)   cmd_down ;;
    *)      sed -n '2,30p' "$0" ;;
esac
