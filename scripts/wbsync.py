#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbsync —— WorkBuddy 多电脑同步助手
macOS / Windows / Linux 三平台原生，单文件，仅用 Python 标准库。

它解决的核心问题
----------------
WorkBuddy 的配置只存在本机。这个工具把它们同步到你名下的每一台电脑，
同步范围覆盖四类：技能、专家、连接器、工作区。并保证「同步过去的东西
真的能用」—— 后者才是难点。

同步分两层，缺一层就出问题：

  文件层   技能 / 专家 / 连接器 / 协同目录的文件，交给 Syncthing 双向同步。全自动。
  登记层   让专家与连接器在界面里真正生效的登记项（installed_plugins.json 等）。
           这些文件里写的是**含用户名的绝对路径**，直接跨机复制必然失效 ——
           而且界面不会报错，只会静默地跑旧版本。必须在本机重写一次。

用法
----
  wbsync plan                 看本机现状（只读，不改任何东西）
  wbsync install              下载并安装 Syncthing 到本机
  wbsync setup                建立同步夹并注册（默认演练，加 --apply 才落盘）
  wbsync id                   显示本机设备 ID，拿它对端配对
  wbsync pair <对端设备ID>     添加对端并共享同步夹
  wbsync mailbox put|get      通过 wb-coop 信箱跨网交换设备 ID（免手抄）
  wbsync newdisk [--name N] [--location 目录]   新建一个文件同步盘（统一 A/B/C/D 后缀，位置首次询问后记住）
  wbsync listdisks            列出登记表里已有的同步盘
  wbsync expert               专家/连接器登记层自愈（默认演练，加 --apply 才落盘）
  wbsync autostart status|on|off   跨平台开机自启
  wbsync doctor               全面体检，给出该做什么

约定：所有会写盘的命令默认只演练（dry-run），确认无误后再加 --apply。
      写之前一律自动备份。加 --json 可获得机器可读输出。
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, date, timezone
from pathlib import Path

APP = "wbsync"
VERSION = "4.42"

import socket as _socket

HOSTNAME = _socket.gethostname()

# --------------------------------------------------------------------------
# 平台与路径
# --------------------------------------------------------------------------

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PLATFORM_LABEL = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}.get(
    sys.platform, sys.platform
)

HOME = Path(os.path.expanduser("~"))
WB_ROOT = Path(os.environ.get("WORKBUDDY_HOME") or (HOME / ".workbuddy"))

DEFAULT_SYNC_ROOT = HOME / ("WorkBuddySync" if not IS_WIN else "WorkBuddySync")
DEFAULT_WORKBUDDY_ROOT = HOME / "WorkBuddy"

def _wb_install_roots():
    """WorkBuddy 安装根的候选列表（供 `space_api.py` 与 `database/` 共用）。

    为什么要有这个（R-20261017-B-board-push-win，B 实测）：原来两处各自硬编码 mac 路径，
    Windows 只兜 `LOCALAPPDATA` 的两个标准位置。B 把 WorkBuddy 装在**非系统盘**
    （`E:/workbuddy`）→ 两个候选都不存在 → `board push` 报
    「can't open file 'C:\\Applications\\WorkBuddy.app\\...'」（mac 路径被当相对路径拼到 C: 盘）。

    统一规则：**env → 各平台默认 → 各盘符根扫描**，找不到再退回首选（让报错路径可读）。
    """
    roots = []
    env = (os.environ.get("WBSYNC_WORKBUDDY_HOME") or "").strip()
    if env:
        roots.append(Path(env).expanduser())
    if IS_WIN:
        la = (os.environ.get("LOCALAPPDATA") or "").strip()
        pf = (os.environ.get("PROGRAMFILES") or "").strip()
        if la:
            roots.append(Path(la) / "WorkBuddy" / "app-1.0.0" / "resources" / "app.asar.unpacked"
                         / "resources" / "plugins" / "workbuddy-builtin")
            roots.append(Path(la) / "Programs" / "WorkBuddy" / "resources" / "app.asar.unpacked"
                         / "resources" / "plugins" / "workbuddy-builtin")
        if pf:
            roots.append(Path(pf) / "WorkBuddy" / "resources" / "app.asar.unpacked"
                         / "resources" / "plugins" / "workbuddy-builtin")
        # 非系统盘安装（B 是 E:\workbuddy）：逐盘符扫，路径存在与否由调用方 exists() 判
        for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            for name in ("workbuddy", "WorkBuddy"):
                roots.append(Path("%s:/" % drive) / name / "resources" / "app.asar.unpacked"
                             / "resources" / "plugins" / "workbuddy-builtin")
    else:
        _mac = "Contents/Resources/app.asar.unpacked/resources/plugins/workbuddy-builtin"
        roots.append(Path("/Applications/WorkBuddy.app") / _mac)
        roots.append(HOME / "Applications" / "WorkBuddy.app" / _mac)
    roots.append(WB_ROOT / "plugins" / "builtin")
    return roots


def _library_script(name, env_hint=""):
    """在候选安装根里找 `skills/library/<name>`；找不到返回首选路径（报错时可读）。"""
    hint = (os.environ.get(env_hint) or "").strip() if env_hint else ""
    cands = []
    if hint:
        cands.append(Path(hint).expanduser())
    for r in _wb_install_roots():
        cands.append(Path(r) / "skills" / "library" / name)
    for c in cands:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return cands[0] if cands else Path(name)


# 资料库 API 脚本路径（WorkBuddy 内置），用于读取跨机器登记表
LIBRARY_SPACE_API = _library_script("space_api.py", "WBSYNC_LIBRARY_SPACE")

# 四个同步夹：id 必须两端逐字一致，path 是本机真实路径
# 说明：技能（wb-skills）、专家源包（wb-experts）、专家运行副本（wb-experts-cache）
#       都由 Syncthing 双向同步；连接器（connector）作为插件也落在这三夹里。
#       wb-coop 是「机器间协同目录」，兼作设备 ID 信箱与同步盘登记表的落点。
FOLDER_SPECS = [
    ("wb-skills", "工作区技能", lambda: WB_ROOT / "skills"),
    ("wb-experts", "专家源包", lambda: WB_ROOT / "plugins" / "marketplaces" / "my-experts"),
    ("wb-experts-cache", "专家运行副本", lambda: WB_ROOT / "plugins" / "cache" / "my-experts"),
    ("wb-coop", "机器间协同目录", lambda: cfg_sync_root() / "coop"),
]

EXPERT_MARKETPLACE = "my-experts"
SYNCTHING_PORT = 8384
API_BASE = "http://127.0.0.1:%d" % SYNCTHING_PORT


def _gui_address_raw(config_xml=None):
    """从 Syncthing 的 config.xml 里取 <gui><address>（形如 127.0.0.1:8384）。

    只认 <gui> 段内的 address —— config.xml 里另有若干 <address>dynamic</address>
    属于**监听**地址，不能拿来当管理端口。config_xml 传空则返回 None（不猜）。
    """
    if not config_xml:
        return None
    try:
        txt = Path(config_xml).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"<gui[^>]*>(.*?)</gui>", txt, re.S)
    if not m:
        return None
    a = re.search(r"<address>\s*([^<\s]+)\s*</address>", m.group(1))
    return a.group(1) if a else None


def gui_port(config_xml=None):
    """本机 Syncthing 的管理端口。

    取值优先级：环境变量 WBSYNC_GUI_ADDR（host:port 或完整 URL）→
    config.xml 的 <gui><address> → 默认 8384。

    存在的意义：在同一台机器上另起一个 Syncthing 实例演练「新机器接入」时，
    管理端口必须跟着那个实例走，否则所有读写都会打到生产实例的 8384 上。
    """
    env = (os.environ.get("WBSYNC_GUI_ADDR") or "").strip()
    for src in (env, _gui_address_raw(config_xml or find_config_xml()) or ""):
        m = re.search(r":(\d+)\s*/?$", src)
        if m:
            return int(m.group(1))
    return SYNCTHING_PORT


def api_base(config_xml=None):
    """管理 API 的基址。

    解析得到明确端口时用它；解析不到（含测试注入的裸 Syncthing 对象）则回落到
    模块级 API_BASE，保持既有行为与注入能力不变。
    """
    env = (os.environ.get("WBSYNC_GUI_ADDR") or "").strip()
    for src in (env, _gui_address_raw(config_xml) or ""):
        m = re.search(r":(\d+)\s*/?$", src)
        if m:
            return "http://127.0.0.1:%d" % int(m.group(1))
    return API_BASE

# 协同目录（wb-coop）下的 shared 子目录：设备 ID 信箱与同步盘登记表都放这里。
def coop_shared():
    return cfg_sync_root() / "coop" / "shared"

def _mailbox_dir():
    return os.environ.get("WBSYNC_MAILBOX_DIR") or str(coop_shared() / "设备ID")


# 机器间沟通：每机专线文件机制（根治多机并发冲突）。
# 关键教训（B 机实战指出）：多机共写同一个文件，两端同时编辑必然触发 Syncthing 冲突，
# 一方的版本被降级成 *.sync-conflict-* 副本、主文件名被另一方占用，导致「写了对方说没收到」。
# 根治方案：每台机器只写自己的文件（机器间沟通-<后缀>.md），读对方时只读对方文件，互不覆盖。
COMMS_NOTEBOOK = "机器间沟通.md"           # 历史文件（已冻结，仅作兼容读取）
COMMS_PER_MACHINE = "机器间沟通-{suffix}.md"  # 每机专线：A 写 机器间沟通-A.md，B 写 -B.md

# 标准状态词（只认这些；标题里若出现「请遵守/准实时」等非标准词会被跳过，不当状态）
_STANDARD_STATUS = {
    "待回复", "已回复", "已解决", "已关闭", "已完成", "已确认",
    "待确认", "待处理", "已处理",
    "closed", "resolved", "done", "completed", "ack",
}

# 视为「已闭环」的状态（对方已给回执）
_CLOSED_STATUS = {
    "已回复", "已解决", "已关闭", "已完成", "已确认", "已处理",
    "closed", "resolved", "done", "completed", "ack",
}

# --------------------------------------------------------------------------
# 文件通道版 A2A（Agent↔Agent）
# 用共享目录承载 A2A 语义：每机发布一份能力卡（Agent Card）+ 一个任务事件日志。
# 不开放网络端口，传输仍走 Syncthing 文件同步；每机只写自己的文件（避免冲突）。
# --------------------------------------------------------------------------
A2A_DIR = "a2a"
A2A_PROTOCOL_VERSION = "file-a2a/0.1"
# #13：两类「结束」要分清 ——
#   A2A_TERMINAL_STATES = 不再计入「待办 / 超时」的集合（completed 也算完成）
#   A2A_FINAL_STATES    = **不可再变**的真终态（只有 canceled / failed）
#   completed 仍算「完成」，但**可被打回**（completed → rejected），故不是真终态。
A2A_TERMINAL_STATES = {"completed", "failed", "canceled"}
A2A_FINAL_STATES = {"canceled", "failed"}
A2A_STATES = ("submitted", "working", "completed", "rejected", "rework", "failed", "canceled")
# #13 状态机：当前状态 → 允许迁往的状态（角色另校验）
# 注：`submitted → completed` 是**兼容边**——既有流程普遍不显式经过 working，
#     语义等价「直接做完」，不视为非法（否则会打断所有历史用法）。
A2A_TRANSITIONS = {
    "submitted": {"working", "completed", "canceled"},
    "working": {"completed", "rejected", "canceled"},
    "completed": {"rejected"},
    "rejected": {"rework", "canceled"},
    "rework": {"completed", "rejected", "canceled"},
    "canceled": set(),
    "failed": set(),
}
# 需要校验「谁来做」的边（未列出的边不限制角色）
A2A_TRANSITION_ROLE = {
    ("submitted", "working"): "receiver",
    ("working", "completed"): "receiver",
    ("submitted", "completed"): "receiver",
    ("working", "rejected"): "assigner",
    ("completed", "rejected"): "assigner",
    ("rejected", "rework"): "receiver",
    ("rework", "completed"): "receiver",
    ("rework", "rejected"): "assigner",
}
A2A_REWORK_MAX = 3      # 防无限返工 ping-pong（attempt ≤ 3；到顶只能 canceled 重发）
A2A_REASON_MAX = 100    # 打回理由上限（超截断）

# --------------------------------------------------------------------------
# P1 · 高危动作授权闸门 + STOP 刹车（借鉴 ACP-lite / Ferryman）
# --------------------------------------------------------------------------
# 为什么需要：`--apply` 只防「手滑」，不防「agent 自信地判定某机是残留」。
# 真事故：B 机误删 A 机目录 → 经 Syncthing 传播成三机同删（靠回收站才救回）。
# 做法：高危动作要在**人的终端**签一张一次性放行票（TTL 300s），agent 不该代签；
#       另有一个 STOP 文件，存在即全组急停（经 Syncthing 秒级传播）。
AUTHZ_DIRNAME = ".a2a-auth"      # coop/shared/.a2a-auth/{<指纹>.json, STOP}
AUTHZ_TTL_DEFAULT = 300          # 票有效期（秒）：短窗口降低误用
STOP_FILENAME = "STOP"
A2A_PRIORITIES = ("P0", "P1", "P2", "P3")
A2A_REQUIRES_VALUES = ("none", "write", "delete")
A2A_GATED_REQUIRES = "delete"    # 只有 delete 走闸门；write 由人在对话里确认即可
A2A_PRIORITY_DEFAULT = "P3"

# 本机对外声明的能力（skill），对应可被对端委派的确定性操作
A2A_SKILLS = [
    {"id": "notify", "name": "通知 / 协调", "description": "向对方传达一条说明或请求（自由文本，承载在任务消息里）"},
    {"id": "verify", "name": "多机自检", "description": "连接 / 工作空间 / 同步 三重自检"},
    {"id": "fixnames", "name": "补改名兜底", "description": "把 Syncthing 未完成的 .tmp 改成正式文件"},
    {"id": "report", "name": "状态上报", "description": "回报本机在线状态与本地时间"},
    {"id": "workspace.init", "name": "初始化工作空间", "description": "建立 sync-all-<后缀> 工作空间目录"},
]


_COMMS_TEMPLATE = """# 机器间沟通 · {suffix} 机专线（{suffix} 只写，其他机只读）

> 【每机专线机制 · 根治并发冲突】
> 多机共写同一文件会触发 Syncthing 冲突（一方的版本被降级成 *.sync-conflict-* 副本）。
> 故从即日起，每台机器**只写自己的文件**：
> - A 机只写 `机器间沟通-A.md`（B/C/D 只读）
> - B 机只写 `机器间沟通-B.md`（A/C/D 只读）
> - 本文件是 {suffix} 机的专线，只有 {suffix} 机写，其他机器只读。
> - 读对方留言时，去读对方的专线文件（如 A 读 `机器间沟通-B.md`）。

---

## 沟通约定

1. **每机一个文件**：本机只写本文件，读对方时读对方的专线文件，互不覆盖、不再冲突。
2. **消息格式**：
   ```
   ---
   ## [{suffix}→X] 简短标题（状态）（时间：YYYY-MM-DD HH:MM）
   正文：说清楚要对方做什么、何时需要回复。
   ```
   - 方向标记：谁发 → 发给谁（`[全员]` 表示发给所有机器）。
   - 状态标记只允许标准词：`待回复` / `已回复` / `已解决` / `已完成` / `已确认`。
     其他说明文字（如「请遵守」「准实时」）放到正文，不要放标题括号里。
3. **回执闭环**：回执时带 `--reply-to <原消息标题>`，自动把原消息状态改为「已回复」。
4. **读位**：本机读到各机专线的位置记在本文件下方的「读位」区（`wbsync comms cursor` 查看、
   `wbsync comms read --peer X` 只读未读并自动推进）。**读位只写本机文件，不写对方文件。**
5. **别滥用**：只放「需要跨机协作/确认」的事。

---

## 读位（本机读到各机专线的位置；只由本机写）

<!-- wbsync-cursor 本机读位（此块由 wbsync 维护；字段：peer=X read=N at=时间 title=已读末条标题） -->

## 消息历史

"""


def _my_comms_file():
    """本机专线文件路径：机器间沟通-<本机后缀>.md。取不到后缀则回退历史文件。"""
    suffix = _my_suffix_short()
    if suffix:
        return coop_shared() / COMMS_PER_MACHINE.format(suffix=suffix)
    return coop_shared() / COMMS_NOTEBOOK


def comms_notebook_path():
    """本机「写」用的专线文件。读对方留言时应遍历所有 comms 文件。"""
    return _my_comms_file()


def _all_comms_files():
    """所有机器间沟通文件（本机写 + 各对端专线 + 历史文件），供 check 扫描。"""
    d = coop_shared()
    files = []
    if not d.exists():
        return files
    # 各机专线 + 历史文件，按文件名排序保证稳定
    for p in sorted(d.glob("机器间沟通*.md")):
        # 排除归档和冲突副本
        if "归档" in p.name or "sync-conflict" in p.name:
            continue
        files.append(p)
    return files


def ensure_comms_notebook(apply_flag=True):
    """确保本机专线文件存在；不存在则创建（含每机专线模板）。返回 (path, created)。"""
    p = _my_comms_file()
    if p.exists():
        return p, False
    if not apply_flag:
        return p, False
    suffix = _my_suffix_short() or "?"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_COMMS_TEMPLATE.format(suffix=suffix), encoding="utf-8")
        return p, True
    except Exception:
        return p, False


def append_comms_message(direction, title, body, status="待回复", reply_to=None):
    """往本机专线文件末尾追加一条消息。direction 如 'A→B'、'全员'，或只给目标 'B'。

    消息头格式：[A→B] 标题（状态）（时间：YYYY-MM-DD HH:MM）
    若 direction 不含发送方（如只写了 'B' 或 '全员'），自动补上本机后缀，形成 'A→B' / 'A→全员'，
    保证每条消息都能识别「谁发的」，从而支持 awaiting_ack（待对方确认）的问答闭环。
    reply_to 用于「回执」：传原消息标题，会在正文末尾标注「↳ 回复：<原消息标题>」。
    返回补全后的最终 direction 字符串；失败返回 None。
    """
    p = _my_comms_file()
    if not p.exists():
        p, _ = ensure_comms_notebook(True)
        if not p.exists():
            return None
    direction = (direction or "").strip()
    # 若无发送方，补上本机后缀
    if "→" not in direction and "->" not in direction:
        my = _my_suffix_short()
        if my:
            if direction in ("全员", "所有机器", "all"):
                direction = my + "→全员"
            elif direction:
                direction = my + "→" + direction
            # direction 为空则保持原样（罕见）
    stamp = time.strftime("%Y-%m-%d %H:%M")
    head = "[%s] %s（%s）（时间：%s）" % (direction, title, status, stamp)
    if reply_to:
        body = (body or "").rstrip() + "\n↳ 回复：%s" % reply_to
    block = "\n---\n## %s\n%s\n" % (head, body)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(block)
        return direction
    except Exception:
        return None


def _first_status_paren(rest):
    """返回消息头里第一个「内容属于标准状态词」的括号的 match；没有则返回 None。

    不能盲取第一个括号——标题本身可能带非状态括号（如「（结构化任务委派）」）。
    """
    for mm in re.finditer(r"（([^（）]*?)）", rest):
        if mm.group(1).strip() in _STANDARD_STATUS:
            return mm
    return None


def _set_status_in_head(rest, new_status):
    """把消息头的「状态括号」改成 new_status。

    优先替换已存在的标准状态括号；若没有，则插到「（时间：…）」之前；再没有就追加到末尾。
    """
    m = _first_status_paren(rest)
    if m:
        return rest[:m.start()] + "（%s）" % new_status + rest[m.end():]
    t = re.search(r"（时间[^（）]*?）", rest)
    if t:
        return rest[:t.start()] + "（%s）" % new_status + rest[t.start():]
    return rest + "（%s）" % new_status


def _self_close_local(acked_titles):
    """本机自查闭环：本机专线文件里若有「标题已收到对方闭环回执」的待回复消息，把它标为「已回复」。

    每机专线原则：**绝不写别人的文件，只改本机自己的文件**。所以闭环由文件的主人自己做——
    本机发出、对方已回的消息，由本机在自己的文件里标记闭环（B 机的消息则由 B 机自己标）。
    只改状态词，不动正文。返回被更新的消息条数。
    """
    if not acked_titles:
        return 0
    p = _my_comms_file()
    if not p.exists():
        return 0
    my = _my_suffix_short()
    text = p.read_text(encoding="utf-8", errors="replace")
    out_lines = []
    updated = 0
    head_re = re.compile(r"^(##\s*\[([^\]]+)\]\s*)(.*)$")
    for line in text.split("\n"):
        m = head_re.match(line)
        if m:
            prefix = m.group(1)
            direction = m.group(2).strip()
            rest = m.group(3)
            title = re.sub(r"（[^（）]*?）", "", rest).strip()
            # 只处理「本机发出」的消息（方向 [A→…]），且标题已有对方闭环回执
            frm = direction.split("→")[0].strip() if "→" in direction else None
            sp = _first_status_paren(rest)
            cur_status = sp.group(1).strip() if sp else ""
            if frm == my and title in acked_titles and cur_status not in _CLOSED_STATUS:
                line = prefix + _set_status_in_head(rest, "已回复")
                updated += 1
        out_lines.append(line)
    if updated:
        p.write_text("\n".join(out_lines), encoding="utf-8")
    return updated


# --------------------------------------------------------------------------
# 专线读位（read cursor）：每台机器记录「本机读到各机专线的第几条」，
# 免得每次会话都把对方整份专线重读一遍（专线已长到约两万字节）。
#
# 三条设计约束（都来自 2026-10-14 的实测教训）：
#   1) 只写本机自己的文件 —— 读位写在本机专线文件（机器间沟通-<后缀>.md）里，
#      绝不写对方的文件（写对方文件会触发 Syncthing 冲突，也违反「每机专线」原则）。
#   2) 位置用「第几条」而不是时间戳 —— 专线只追加，序号稳定；时间戳只到分钟，
#      同一分钟里两条消息必然漏读或重读。
#   3) 记下已读末条的标题做校验 —— 对方文件若被归档/重排，序号语义会漂移；
#      标题对不上就报「读位漂移」，宁可多读，不可漏读。
# --------------------------------------------------------------------------
_CURSOR_BEGIN = "<!-- wbsync-cursor"
_CURSOR_END = "-->"
_CURSOR_HEADING = "## 读位（本机读到各机专线的位置；只由本机写）"
_CURSOR_LINE_RE = re.compile(
    r"^peer=(\S+)\s+read=(\d+)\s+at=(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*(?:title=(.*))?$")
_COMMS_PEER_FILE_RE = re.compile(r"^机器间沟通-([A-Za-z0-9]+)\.(?:md|MD)$")


def _comms_file_peer(name):
    """从专线文件名抽机器后缀；历史文件「机器间沟通.md」返回 None。"""
    m = _COMMS_PEER_FILE_RE.match(name or "")
    return m.group(1).upper() if m else None


def _comms_parse_cursor(text):
    """解析专线正文里的读位注释块 → {peer: {"read": int, "at": str, "title": str}}。"""
    cur = {}
    if not text or _CURSOR_BEGIN not in text:
        return cur
    seg = text.split(_CURSOR_BEGIN, 1)[1]
    if _CURSOR_END in seg:
        seg = seg.split(_CURSOR_END, 1)[0]
    for line in seg.splitlines():
        m = _CURSOR_LINE_RE.match(line.strip())
        if m:
            cur[m.group(1).upper()] = {
                "read": int(m.group(2)),
                "at": m.group(3),
                "title": (m.group(4) or "").strip(),
            }
    return cur


def _comms_render_cursor(cursor):
    """读位字典 → 可嵌进专线文件的注释块（人可读、机可解析）。"""
    lines = [_CURSOR_BEGIN + " 本机读位（只由本机写；字段 peer=X read=N at=时间 title=已读末条标题）"]
    for peer in sorted(cursor or {}):
        e = cursor[peer] or {}
        line = "peer=%s read=%d at=%s" % (peer, int(e.get("read", 0) or 0), e.get("at") or "")
        if e.get("title"):
            line += " title=" + str(e["title"])
        lines.append(line)
    lines.append(_CURSOR_END)
    return "\n".join(lines)


def _comms_unread_of(msgs, entry):
    """纯函数：对端消息列表 + 读位条目 → (已读条数, 未读消息列表, 是否漂移)。

    entry 为空 = 未初始化：已读记 0，全部视为未读（宁可多读，不可漏读）。
    漂移 = 记录序号越界，或已读末条标题与记录的 title 对不上（文件被归档/重排）。
    """
    total = len(msgs)
    ent = entry or {}
    try:
        read = int(ent.get("read", 0) or 0)
    except (TypeError, ValueError):
        read, drift = 0, True
        return read, list(msgs), drift
    drift = False
    if read < 0:
        read, drift = 0, True
    if read > total:
        read, drift = total, True
    elif read > 0 and ent.get("title"):
        if (msgs[read - 1].get("title") or "").strip() != str(ent["title"]).strip():
            drift = True
    return read, list(msgs[read:]), drift


def read_comms_cursor():
    """读本机专线文件里的读位；文件不存在或读失败返回 {}。"""
    p = _my_comms_file()
    try:
        if not p.exists():
            return {}
        return _comms_parse_cursor(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def write_comms_cursor(cursor):
    """把读位写回**本机**专线文件：只替换读位注释块，不动任何消息。返回 True/False。"""
    p = _my_comms_file()
    if not p.exists():
        p, _ = ensure_comms_notebook(True)
        if not p.exists():
            return False
    block = _CURSOR_HEADING + "\n\n" + _comms_render_cursor(cursor) + "\n"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        if _CURSOR_BEGIN in text:
            i = text.index(_CURSOR_BEGIN)
            j = text.find(_CURSOR_END, i)
            before = text[:i]
            # 连标题行一起替换（避免留下孤立的「## 读位」标题）
            if _CURSOR_HEADING in before:
                i = before.rindex(_CURSOR_HEADING)
            new = text[:i] + block + (text[j + len(_CURSOR_END):] if j != -1 else "\n")
        else:
            anchor = -1
            for pat in ("\n## 消息历史", "\n## ["):
                anchor = text.find(pat)
                if anchor != -1:
                    break
            if anchor == -1:
                new = text.rstrip("\n") + "\n\n" + block
            else:
                new = text[:anchor] + "\n\n" + block + "\n" + text[anchor:]
        p.write_text(new, encoding="utf-8")
        return True
    except Exception:
        return False


def _comms_peer_status():
    """各对端专线的读位状态（只读，不写盘）。返回 (rows, cursor)。"""
    my = _my_suffix_short()
    cursor = read_comms_cursor()
    rows = []
    for f in _all_comms_files():
        peer = _comms_file_peer(f.name)
        if not peer or peer == my:
            continue
        try:
            msgs = _parse_comms_messages(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        read, unread, drift = _comms_unread_of(msgs, cursor.get(peer))
        rows.append({
            "peer": peer,
            "path": str(f),
            "total": len(msgs),
            "read": read,
            "unread": len(unread),
            "msgs": msgs,
            "unread_msgs": unread,
            "unread_first": (unread[0].get("title") or "") if unread else "",
            "last_title": (msgs[-1].get("title") or "") if msgs else "",
            "drift": drift,
            "initialized": peer in cursor,
        })
    return rows, cursor


def _comms_push_cursor(cursor, rows, peers, now=None):
    """把选定对端的读位推进到最新（纯数据操作，返回被推进的 (peer, read) 列表）。"""
    now = now or time.strftime("%Y-%m-%d %H:%M")
    touched = []
    for r in rows:
        if peers and r["peer"] not in peers:
            continue
        cursor[r["peer"]] = {"read": r["total"], "at": now, "title": r["last_title"]}
        touched.append((r["peer"], r["total"]))
    return touched


def _comms_print_unread_summary():
    """comms check 里附一行未读概览。"""
    rows, _ = _comms_peer_status()
    if not rows:
        return
    parts = []
    for r in rows:
        tip = "%d 条" % r["unread"]
        if not r["initialized"]:
            tip += "（未初始化）"
        if r["drift"]:
            tip += "（读位漂移）"
        parts.append("%s %s" % (r["peer"], tip))
    total_unread = sum(r["unread"] for r in rows)
    if total_unread:
        info("未读：%s  → 读未读：wbsync comms read --peer <机>" % " / ".join(parts))
    else:
        info("未读：无（%s）；读位详情 wbsync comms cursor" % " / ".join(parts))
    # 记忆读位概览（降 token：一眼看到对方 memory 日志有无新增段）
    _mem_print_unread_summary()


# --------------------------------------------------------------------------
# 机器间 memory 日志：增量读（降 token，避免全量 Read 对方大日志文件）
# 机制与 comms 读位同源：游标记在「本机专线文件」的 <!-- wbsync-mem-cursor --> 块，
# 位置用「读到第几个 ## 段」而非时间戳；对端未初始化时全部视为未读（宁可多读不可漏读）。
# --------------------------------------------------------------------------
_MEM_CURSOR_BEGIN = "<!-- wbsync-mem-cursor"
_MEM_CURSOR_HEADING = "## 记忆读位（本机读到各机 memory 当日日志的位置；只由本机写）"
_MEM_CURSOR_LINE_RE = re.compile(
    r"^peer=(\S+)\s+read=(\d+)\s+at=(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*(?:title=(.*))?$")


def _mem_parse_cursor(text):
    """解析本机专线里的 memory 读位块 → {peer: {read, at, title}}。"""
    cur = {}
    if not text or _MEM_CURSOR_BEGIN not in text:
        return cur
    seg = text.split(_MEM_CURSOR_BEGIN, 1)[1]
    if _CURSOR_END in seg:
        seg = seg.split(_CURSOR_END, 1)[0]
    for line in seg.splitlines():
        m = _MEM_CURSOR_LINE_RE.match(line.strip())
        if m:
            cur[m.group(1).upper()] = {
                "read": int(m.group(2)),
                "at": m.group(3),
                "title": (m.group(4) or "").strip(),
            }
    return cur


def _mem_render_cursor(cursor):
    lines = [_MEM_CURSOR_BEGIN + " 本机记忆读位（只由本机写；字段 peer=X read=N at=时间 title=已读末段标题）"]
    for peer in sorted(cursor or {}):
        e = cursor[peer] or {}
        line = "peer=%s read=%d at=%s" % (peer, int(e.get("read", 0) or 0), e.get("at") or "")
        if e.get("title"):
            line += " title=" + str(e["title"])
        lines.append(line)
    lines.append(_CURSOR_END)
    return "\n".join(lines)


def read_mem_cursor():
    p = _my_comms_file()
    try:
        if not p.exists():
            return {}
        return _mem_parse_cursor(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _mem_write_cursor(cursor):
    p = _my_comms_file()
    if not p.exists():
        p, _ = ensure_comms_notebook(True)
        if not p.exists():
            return False
    block = _MEM_CURSOR_HEADING + "\n\n" + _mem_render_cursor(cursor) + "\n"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        if _MEM_CURSOR_BEGIN in text:
            i = text.index(_MEM_CURSOR_BEGIN)
            j = text.find(_CURSOR_END, i)
            before = text[:i]
            if _MEM_CURSOR_HEADING in before:
                i = before.rindex(_MEM_CURSOR_HEADING)
            new = text[:i] + block + (text[j + len(_CURSOR_END):] if j != -1 else "\n")
        else:
            new = text.rstrip("\n") + "\n\n" + block
        p.write_text(new, encoding="utf-8")
        return True
    except Exception:
        return False


def _parse_memory_segments(text):
    """解析 memory 当日日志：按 '## ' 段落切成段。返回 [{title, body}]。"""
    segs = []
    cur = None
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            if cur:
                segs.append(cur)
            cur = {"title": line[3:].strip(), "body": []}
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        segs.append(cur)
    for s in segs:
        s["body"] = "\n".join(s["body"]).strip()
    return segs


def _mem_peer_log_dir(peer):
    my = _my_suffix_short()
    if not my or peer == my:
        return None
    try:
        reg = _read_ws_registry()
        ws = _ws_local_dir(reg, my)
    except Exception:
        return None
    return ws / peer / "memory"


def _mem_peer_log_file(peer):
    d = _mem_peer_log_dir(peer)
    if not d or not d.exists():
        return None
    today = time.strftime("%Y-%m-%d")
    cand = d / ("%s-%s.md" % (today, peer))
    if cand.exists():
        return cand
    # 只认日期命名（YYYY-MM-DD-<peer>.md），取最大日期；否则返回 None 让调用方走
    # 「无当日日志」分支。绝不能用 *-<peer>.md 宽 glob（会把 MEMORY-*/STATE-* 当日志）。
    date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})-%s\.md$" % re.escape(peer))
    cands = [f for f in d.glob("*.md") if f.is_file() and date_re.match(f.name)]
    return max(cands, key=lambda f: f.name) if cands else None


def _mem_index_classify(name, peer):
    """把 memory 目录下的文件名分类。返回 (kind, date_str_or_None)。
    kind ∈ {log, longterm, state, legacy, None(忽略)}。严格正则，不做宽 glob。"""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}-%s\.md" % re.escape(peer), name):
        return "log", name[:10]
    if name == "MEMORY-%s.md" % peer:
        return "longterm", None
    if name == "STATE-%s.md" % peer:
        return "state", None
    return None, None


def _mem_index_bucket(days_ago):
    if days_ago is None:
        return None
    if days_ago <= 0:  # 0=今天；负数为未来日期（时区/命名异常），归入今天而非 d3
        return "today"
    if days_ago <= 3:
        return "d3"
    if days_ago <= 7:
        return "d7"
    if days_ago <= 30:
        return "d30"
    return "older"


def _mem_index_cmd(args):
    """只读：把（本机 + 各对端）memory/ 全貌渲染成「文件×日期×距今×档位×段标题」索引。"""
    my = _my_suffix_short()
    try:
        reg = _read_ws_registry()
        ws = _ws_local_dir(reg, my)
    except Exception:
        ws = None
    if not ws or not ws.exists():
        info("无 memory 目录。")
        return 0

    want = (getattr(args, "peer", None) or "").strip().upper()
    titles_n = int(getattr(args, "titles", 8) or 8)
    days = getattr(args, "days", None)
    logs_only = bool(getattr(args, "logs_only", False))

    # 候选机：ws 下单字母目录 + 存在 memory 子目录（含本机）
    peers = []
    for sub in sorted(ws.iterdir()):
        if not sub.is_dir():
            continue
        if not re.fullmatch(r"[A-Z]", sub.name):
            continue
        if (sub / "memory").exists():
            peers.append(sub.name.upper())
    if want:
        if want not in peers:
            die("找不到该机 memory：%s（现有：%s）" % (want, ", ".join(peers) or "无"))
        peers = [want]

    today = date.today()
    rows = []
    for peer in peers:
        d = ws / peer / "memory"
        # 当日日志 + 长期 + 状态（*.md，严格分类）
        for f in sorted(d.glob("*.md")):
            if not f.is_file():
                continue
            kind, ds = _mem_index_classify(f.name, peer)
            if kind is None:
                continue
            days_ago = None
            if ds:
                days_ago = (today - date(*map(int, ds.split("-")))).days
            try:
                segs = _parse_memory_segments(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                segs = []
            rows.append({
                "peer": peer,
                "kind": kind,
                "path": str(f.relative_to(ws)) if str(f).startswith(str(ws)) else str(f),
                "date": ds,
                "days_ago": days_ago,
                "bucket": (kind if kind in ("longterm", "state")
                           else _mem_index_bucket(days_ago) if days_ago is not None else kind),
                "bytes": f.stat().st_size,
                "segments": len(segs),
                "last_title": (segs[-1].get("title") or "") if segs else "",
                "titles": [s.get("title") or "" for s in segs][:titles_n],
                "more_titles": len(segs) > titles_n,
            })
        # legacy/*.md（文件名前 10 字符匹配日期则取日期）
        leg = d / "legacy"
        if leg.is_dir():
            for f in sorted(leg.glob("*.md")):
                if not f.is_file():
                    continue
                ds = f.name[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.name[:10]) else None
                days_ago = None
                if ds:
                    days_ago = (today - date(*map(int, ds.split("-")))).days
                rows.append({
                    "peer": peer,
                    "kind": "legacy",
                    "path": str(f.relative_to(ws)) if str(f).startswith(str(ws)) else str(f),
                    "date": ds,
                    "days_ago": days_ago,
                    "bucket": _mem_index_bucket(days_ago) if days_ago is not None else "legacy",
                    "bytes": f.stat().st_size,
                    "segments": 0,
                    "last_title": "",
                    "titles": [],
                    "more_titles": False,
                })

    # 过滤：--days（只滤带日期条目；无日期的 longterm/state 保留）；--logs-only
    if days is not None:
        rows = [r for r in rows if r["date"] is None or r["days_ago"] <= days]
    if logs_only:
        rows = [r for r in rows if r["kind"] in ("log", "legacy")]

    if getattr(args, "json", False):
        files = [{k: v for k, v in r.items() if k != "more_titles"} for r in rows]
        out(json.dumps({
            "my_suffix": my,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total_files": len(files),
            "files": files,
        }, ensure_ascii=False, indent=2))
        return 0

    head("memory 索引（本机 %s · 截至 %s）" % (my or "?", today.isoformat()))
    if not rows:
        info("无 memory 条目%s。" % ("（匹配当前过滤）" if (days is not None or logs_only) else ""))
        return 0

    bucket_label = {"today": "今天", "d3": "3天内", "d7": "7天内", "d30": "30天内",
                    "older": "更久", "longterm": "长期", "state": "状态", "legacy": "归档"}
    cur_peer = None
    for r in rows:
        if r["peer"] != cur_peer:
            cur_peer = r["peer"]
            out()
        bl = bucket_label.get(r["bucket"], r["bucket"] or "—")
        date_s = r["date"] or ""
        seg = ("%d段" % r["segments"]) if r["kind"] in ("log",) else ("%d节" % r["segments"] if r["kind"] == "longterm" else "")
        title_tail = ""
        if r["titles"]:
            t = " / ".join(r["titles"])
            if r["more_titles"]:
                t += " / …"
            title_tail = "   " + t
        info("  %s 机  [%s]  %-28s %6dB  %-6s%s" % (
            r["peer"], bl, (date_s + "-" + r["peer"] + ".md") if r["kind"] == "log"
            else r["path"].rsplit("/", 1)[-1],
            r["bytes"], seg, title_tail))
    out()
    info("读某段 → wbsync mem read --peer <机>（增量）或直接 Read 该文件；")
    info("列全量标题 → --titles 99；只要日志 → --logs-only；JSON → --json")
    return 0


def _mem_peer_status():
    """各对端 memory 当日日志的读位状态（只读，不写盘）。返回 (rows, cursor)。"""
    my = _my_suffix_short()
    cursor = read_mem_cursor()
    rows = []
    try:
        reg = _read_ws_registry()
        ws = _ws_local_dir(reg, my)
    except Exception:
        return rows, cursor
    if not ws.exists():
        return rows, cursor
    for sub in sorted(ws.iterdir()):
        if not sub.is_dir():
            continue
        peer = sub.name.upper()
        if peer == my:
            continue
        if not (sub / "memory").exists():
            continue
        f = _mem_peer_log_file(peer)
        if not f:
            continue
        try:
            segs = _parse_memory_segments(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        read, unread, drift = _comms_unread_of(segs, cursor.get(peer))
        rows.append({
            "peer": peer,
            "path": str(f),
            "total": len(segs),
            "read": read,
            "unread": len(unread),
            "segs": segs,
            "unread_segs": unread,
            "unread_first": (unread[0].get("title") or "") if unread else "",
            "last_title": (segs[-1].get("title") or "") if segs else "",
            "drift": drift,
            "initialized": peer in cursor,
        })
    return rows, cursor


def _mem_print_unread_summary():
    rows, _ = _mem_peer_status()
    if not rows:
        return
    parts = []
    for r in rows:
        tip = "%d 段" % r["unread"]
        if not r["initialized"]:
            tip += "（未初始化）"
        if r["drift"]:
            tip += "（漂移）"
        parts.append("%s %s" % (r["peer"], tip))
    info("记忆未读：%s  → 读未读：wbsync mem read --peer <机>" % " / ".join(parts))


def _mem_cursor_cmd(args):
    rows, cursor = _mem_peer_status()
    my = _my_suffix_short() or "?"
    want = (getattr(args, "peer", None) or "").strip().upper()
    if getattr(args, "mark", False):
        peers = {want} if want else set()
        if want and want not in {r["peer"] for r in rows}:
            die("找不到对端 memory：%s（现有：%s）" % (want, ", ".join(r["peer"] for r in rows) or "无"))
        touched = _comms_push_cursor(cursor, rows, peers)
        if not touched:
            bad("没有可推进的对端（无对端 memory）。")
            return 1
        if not _mem_write_cursor(cursor):
            bad("记忆读位写入失败（本机专线文件不可写？）")
            return 1
        head("记忆读位已推进")
        info("本机 = %s 机；下表为推进后的位置：" % my)
        for peer, n in touched:
            out("  ─ %s 机：已读 %d 段" % (peer, n))
        return 0
    if getattr(args, "json", False):
        out(json.dumps({"my_suffix": my, "rows": [{k: v for k, v in r.items()
                  if k not in ("segs", "unread_segs")} for r in rows]},
                 ensure_ascii=False, indent=2))
        return 0
    head("memory 读位")
    if not rows:
        info("还没有对端 memory 日志（无未读）。")
        return 0
    info("本机 = %s 机；读位只记在本机专线文件里，不写对方文件。" % my)
    for r in rows:
        line = "  ─ %s 机：共 %d 段，已读 %d，未读 %d" % (r["peer"], r["total"], r["read"], r["unread"])
        out(line)
        if r["unread"]:
            out("    未读起：%s" % r["unread_first"])
            out("    读未读：wbsync mem read --peer %s" % r["peer"])
        else:
            out("    已读完")
        if not r["initialized"]:
            warn("%s 机的记忆读位未初始化 → 全部视为未读；确认已读过可用 "
                 "wbsync mem cursor --mark --peer %s 置零" % (r["peer"], r["peer"]))
        if r["drift"]:
            warn("%s 机记忆读位漂移，建议重读一遍后用 --mark 重置" % r["peer"])
    return 0


def _mem_read_cmd(args):
    rows, cursor = _mem_peer_status()
    want = (getattr(args, "peer", None) or "").strip().upper()
    picked = [r for r in rows if (not want or r["peer"] == want)]
    if not picked:
        die("找不到对端 memory 日志：%s（现有：%s）" % (want or "无", ", ".join(r["peer"] for r in rows) or "无"))
    show_all = bool(getattr(args, "all", False))
    do_mark = not bool(getattr(args, "no_mark", False))
    head("读 memory（%s）" % ("全部" if show_all else "仅未读"))
    printed = 0
    to_push = []
    for r in picked:
        if r["drift"]:
            warn("%s 机记忆读位漂移，按保守位置输出" % r["peer"])
        if not r["initialized"]:
            warn("%s 机记忆读位未初始化 → 视为全部未读" % r["peer"])
        sel = r["segs"] if show_all else r["unread_segs"]
        info("%s 机：%d 段%s" % (r["peer"], len(sel), "（全部）" if show_all else "（未读）"))
        if not sel:
            out("  （没有新增）")
        for s in sel:
            printed += 1
            out("  ─ %s" % (s.get("title") or "(无标题)"))
            for bl in (s.get("body") or "").splitlines():
                if bl.strip():
                    out("    %s" % bl)
        if do_mark:
            to_push.append(r)
    if do_mark and to_push and printed:
        touched = _comms_push_cursor(cursor, picked, {r["peer"] for r in to_push})
        if _mem_write_cursor(cursor):
            ok("记忆读位已推进：%s" % ", ".join("%s→%d/%d" % (p, n, next(
                x["total"] for x in picked if x["peer"] == p)) for p, n in touched))
        else:
            bad("记忆读位写入失败")
            return 1
    elif do_mark and not printed:
        info("没有新增，读位未变。")
    return 0


def cmd_mem(args):
    action = getattr(args, "action", "cursor")
    if action == "read":
        return _mem_read_cmd(args)
    if action == "index":
        return _mem_index_cmd(args)
    return _mem_cursor_cmd(args)


def vendor_dir():
    """skill 自带的离线安装包目录（随 skill 一起分发，跨机免下载）。"""
    here = Path(__file__).resolve().parent  # scripts/
    return here.parent / "vendor"


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

class C:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    R = "\033[0m" if on else ""
    B = "\033[1m" if on else ""
    DIM = "\033[2m" if on else ""
    RED = "\033[31m" if on else ""
    GRN = "\033[32m" if on else ""
    YEL = "\033[33m" if on else ""
    BLU = "\033[34m" if on else ""
    CYN = "\033[36m" if on else ""


def out(msg=""):
    print(msg)


def ok(msg):
    out("  %s✓%s %s" % (C.GRN, C.R, msg))


def bad(msg):
    out("  %s✗%s %s" % (C.RED, C.R, msg))


def warn(msg):
    out("  %s!%s %s" % (C.YEL, C.R, msg))


def info(msg):
    out("  %s·%s %s" % (C.BLU, C.R, msg))


def section(title):
    out()
    out("%s%s%s" % (C.B, title, C.R))
    out("%s%s%s" % (C.DIM, "─" * max(12, min(70, len(title) * 2 + 8)), C.R))


def head(title):
    out()
    out("%s=== %s ===%s" % (C.B, title, C.R))


def die(msg, code=2):
    out()
    bad(msg)
    sys.exit(code)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0


def dispwidth(s):
    """终端显示宽度：中日韩全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def wpad(s, width):
    """按显示宽度左对齐补齐，避免中文标签把后面的列挤歪。"""
    return s + " " * max(0, width - dispwidth(s))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

CONFIG_PATH = WB_ROOT / "sync-assistant" / "config.json"

DEFAULT_CONFIG = {
    "version": 1,
    "sync_root": str(DEFAULT_SYNC_ROOT),
    "peer_id": "",
    "peer_name": "",
    "syncthing_home": "",
    "syncthing_bin": "",
    "dead_ids": [],
    "folder_ids": [s[0] for s in FOLDER_SPECS],
    # 同步盘根目录：首次建盘时由对话引导询问一次，写进这里记住，之后复用不再问
    "disk_root": "",
    # 是否已在本账号上完成过本 skill 的初始化（引导问过位置、装过同步引擎等）。
    # 首次运行时为 false；引导完成后由 AI 调 `wbsync mark-initialized --apply` 置为 true。
    "initialized": False,
    "initialized_at": "",
}


def cfg_sync_root():
    c = load_config(quiet=True)
    p = c.get("sync_root") or str(DEFAULT_SYNC_ROOT)
    return Path(p)


def wb_identity_key():
    """读取当前 WorkBuddy 账号的 identityKey（账号 UUID，跨机器一致）。

    落点在 plugin-account-state/<uuid>.json 的 identityKey 字段，
    或直接取 plugin-account-state 目录下的文件名（即账号 UUID）。
    读不到时返回空字符串，绝不抛异常——这是客户端私有状态，属「尽力而为」。
    """
    try:
        state_dir = WB_ROOT / "plugin-account-state"
        if state_dir.is_dir():
            # 文件名本身就是账号 UUID（如 ca248369-aa5f-4db7-ac40-a2fd041e0649.json）
            for p in state_dir.glob("*.json"):
                name = p.stem
                if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", name):
                    # 优先取文件内 identityKey 字段（更权威），去掉末尾可能的分隔符
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        ik = (d or {}).get("identityKey", "")
                        if ik:
                            return ik.rstrip("|")
                    except Exception:
                        pass
                    return name
    except Exception:
        pass
    return ""


def load_config(quiet=False):
    if not CONFIG_PATH.exists():
        if not quiet:
            info("配置文件尚未创建：%s" % CONFIG_PATH)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data or {})
        return merged
    except Exception as e:
        if not quiet:
            warn("配置文件解析失败，按默认值处理：%s" % e)
        return dict(DEFAULT_CONFIG)


def save_config(cfg, apply_flag):
    if not apply_flag:
        info("演练模式：配置未写入 %s" % CONFIG_PATH)
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(CONFIG_PATH, cfg)
    return True


def atomic_write_json(path, obj):
    """写 JSON 文件（直接写入，不使用临时文件 + rename）。

    为什么不「临时文件 + os.replace」：本项目所在环境（macOS + WorkBuddy）里，
    **删除文件会触发 WorkBuddy「文件安全守护」的授权弹窗**，而 rename 受限时临时文件
    又无法改名为正式文件、也无法删除 → 既残留垃圾又反复弹窗。
    故改为直接写目标文件（对小 JSON 足够；牺牲极端情况下的原子性）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    with open(str(path), "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def atomic_write_text(path, text):
    """写文本文件（同 atomic_write_json 的理由：不用临时文件 + rename）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def backup_file(path):
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name("%s.wbsync-bak-%s" % (path.name, stamp))
    shutil.copy2(str(path), str(bak))
    return bak


# --------------------------------------------------------------------------
# 平台适配层
# --------------------------------------------------------------------------

def pid_alive(pid):
    """跨平台判断进程是否存活。这是原 Windows 版最严重的一处静默错误来源。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def list_processes():
    """返回 [(pid, name, cmdline)]。

    **枚举失败时返回 None，而不是空列表** —— 这点很重要。
    若失败与「确实没有进程」都表现为 []，上层就会把「我没查到」误报成
    「它没在跑」，这正是静默假阴性那类故障。调用方必须区分两种情况。
    """
    res = []
    try:
        if IS_WIN:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0:
                return None
            for line in (r.stdout or "").splitlines():
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    name = parts[0].lstrip('"')
                    pidtxt = parts[1].strip('"')
                    if pidtxt.isdigit():
                        res.append((int(pidtxt), name, ""))
        else:
            r = subprocess.run(
                ["ps", "-axo", "pid=,comm=,args="],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                return None
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                bits = line.split(None, 2)
                if len(bits) >= 2 and bits[0].isdigit():
                    res.append((int(bits[0]), os.path.basename(bits[1]),
                                bits[2] if len(bits) > 2 else ""))
    except Exception:
        return None
    return res


def find_processes(name_substr):
    """找名字或命令行里含 name_substr 的进程。

    返回 None 表示无法枚举（不是「没有」）。若 ps 被限制，回退到 pgrep ——
    pgrep 的语义天然就是「找匹配某个模式的进程」，正好对得上。
    """
    n = name_substr.lower()
    procs = list_processes()

    if procs is None:
        try:
            r = subprocess.run(["pgrep", "-fl", name_substr],
                               capture_output=True, text=True, timeout=20)
        except Exception:
            return None
        if r.returncode not in (0, 1):
            return None
        procs = []
        for line in (r.stdout or "").splitlines():
            bits = line.split(None, 1)
            if bits and bits[0].isdigit():
                txt = bits[1] if len(bits) > 1 else ""
                procs.append((int(bits[0]), txt.split()[0] if txt else "", txt))

    found = []
    for pid, name, cmd in procs:
        joined = ("%s %s" % (name, cmd)).lower()
        if n in joined:
            if "wbsync" in joined:
                continue
            found.append((pid, name, cmd))
    return found


def port_open(host="127.0.0.1", port=None, timeout=1.5):
    if port is None:
        port = gui_port()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def which(name):
    return shutil.which(name)


def kill_pid(pid, force=False):
    """终止进程，跨平台。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if IS_WIN:
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except Exception:
        return False


def machine_arch():
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return "amd64"


def default_syncthing_home():
    """优先沿用系统默认位置，避免和已有安装打架。"""
    if IS_MAC:
        return HOME / "Library" / "Application Support" / "Syncthing"
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(HOME / "AppData" / "Local")
        return Path(base) / "Syncthing"
    base = os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config")
    return Path(base) / "syncthing"


# --------------------------------------------------------------------------
# 自启登记（平台差异全部收敛在这里）
# --------------------------------------------------------------------------

LABEL = "com.workbuddy.wbsync.syncthing"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_RUN_NAME = "WorkBuddySync-Syncthing"


def mac_plist_path():
    return HOME / "Library" / "LaunchAgents" / (LABEL + ".plist")


def linux_unit_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config")
    return Path(base) / "systemd" / "user" / "wbsync-syncthing.service"


def _serve_argv(binpath, shome):
    # Syncthing v2.x 用 kong 风格 flag：目录用 --home=PATH（等号连接），
    # 且 serve 是显式子命令。旧版 `--home <path>`（空格分隔）会报 unknown flag。
    return [str(binpath), "serve", "--no-browser", "--home=%s" % str(shome)]


def autostart_status(binpath, shome):
    """返回 (是否已登记, 说明)。"""
    if IS_MAC:
        p = mac_plist_path()
        if not p.exists():
            return False, "未登记（%s 不存在）" % p
        txt = p.read_text(encoding="utf-8", errors="replace")
        if str(binpath) not in txt:
            return False, "已登记但指向旧路径，需重建"
        loaded = _launchd_loaded(LABEL)
        if loaded is True:
            return True, "已登记且已装载（launchd）"
        if loaded is False:
            return False, "plist 已写入但未装载 —— 在终端执行 launchctl bootstrap 即可"
        return True, "已登记（launchd，装载状态无法确认）"
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, WIN_RUN_NAME)
            if str(binpath) in (val or ""):
                return True, "已登记（注册表 Run）"
            return False, "已登记但指向旧路径，需重建"
        except FileNotFoundError:
            return False, "未登记（注册表 Run 项不存在）"
        except Exception as e:
            return False, "无法读取注册表：%s" % e
    if IS_LINUX:
        p = linux_unit_path()
        if not p.exists():
            return False, "未登记（%s 不存在）" % p
        return True, "已登记（systemd --user）"
    return False, "该平台不支持自动登记"


def _launchd_loaded(label):
    """查证任务是否真的装载进了 launchd。

    必须查证的原因：`launchctl load -w`（旧命令）可能返回 0 却并未真正装载，
    若只按返回码判断就会报出「已装载」的假成功 —— 用户以为自启生效了，其实没有。
    """
    try:
        r = subprocess.run(
            ["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    if r.returncode == 0:
        return True
    # 明确报「找不到服务」才是真的没装；其他错误说明查不了，返回 None 不妄断
    if "Could not find service" in (r.stdout + r.stderr):
        return False
    return None


def autostart_enable(binpath, shome, apply_flag):
    argv = _serve_argv(binpath, shome)
    if IS_MAC:
        plist = mac_plist_path()
        args_xml = "\n".join("        <string>%s</string>" % a for a in argv)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '    <key>Label</key>\n    <string>%s</string>\n'
            '    <key>ProgramArguments</key>\n    <array>\n%s\n    </array>\n'
            '    <key>RunAtLoad</key>\n    <true/>\n'
            '    <key>KeepAlive</key>\n    <true/>\n'
            '    <key>StandardOutPath</key>\n    <string>%s</string>\n'
            '    <key>StandardErrorPath</key>\n    <string>%s</string>\n'
            '</dict>\n</plist>\n'
        ) % (
            LABEL, args_xml,
            str(cfg_sync_root() / "logs" / "syncthing.out.log"),
            str(cfg_sync_root() / "logs" / "syncthing.err.log"),
        )
        if not apply_flag:
            info("演练模式：将写入 %s" % plist)
            info("  内容为 launchd plist，RunAtLoad + KeepAlive，登录即拉起")
            return True
        plist.parent.mkdir(parents=True, exist_ok=True)
        (cfg_sync_root() / "logs").mkdir(parents=True, exist_ok=True)
        if plist.exists():
            backup_file(plist)
        plist.write_text(body, encoding="utf-8")
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d" % uid, str(plist)],
                       capture_output=True, text=True)
        err = ""
        r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, str(plist)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            r2 = subprocess.run(["launchctl", "load", "-w", str(plist)],
                                capture_output=True, text=True)
            if r2.returncode != 0 and not err:
                err = (r2.stderr or r2.stdout or "").strip()

        # 关键：不按返回码下结论，去查证任务是否真的装进去了
        loaded = _launchd_loaded(LABEL)
        if loaded is True:
            ok("已登记并装载 launchd 任务")
            return True

        warn("plist 已写入，但 launchctl 没有把它装载生效")
        if err:
            out("      launchctl 报错：%s" % err.splitlines()[0][:120])
        if loaded is None:
            out("      （也无法确认当前装载状态）")
        info("plist 位置：%s" % plist)
        info("可在你自己的终端里手动装载：")
        out("      launchctl bootstrap gui/%d \"%s\"" % (uid, plist))
        return False

    if IS_WIN:
        cmdline = subprocess.list2cmdline(argv)
        if not apply_flag:
            info("演练模式：将写入注册表 %s\\%s" % (WIN_RUN_KEY, WIN_RUN_NAME))
            info("  值：%s" % cmdline)
            return True
        try:
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, WIN_RUN_NAME, 0, winreg.REG_SZ, cmdline)
            ok("已写入注册表自启项")
            return True
        except Exception as e:
            warn("写注册表失败：%s" % e)
            return False

    if IS_LINUX:
        unit = linux_unit_path()
        body = (
            "[Unit]\nDescription=WorkBuddy Sync (Syncthing)\n\n"
            "[Service]\nType=simple\nExecStart=%s\nRestart=always\n\n"
            "[Install]\nWantedBy=default.target\n"
        ) % " ".join('"%s"' % a if " " in a else a for a in argv)
        if not apply_flag:
            info("演练模式：将写入 %s" % unit)
            return True
        unit.parent.mkdir(parents=True, exist_ok=True)
        if unit.exists():
            backup_file(unit)
        unit.write_text(body, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", unit.name],
                           capture_output=True, text=True)
        if r.returncode != 0:
            warn("systemctl 启用失败：%s" % (r.stderr or "").strip())
            return False
        ok("已登记 systemd --user 服务")
        return True

    warn("该平台不支持自动登记自启")
    return False


def autostart_disable(apply_flag):
    if IS_MAC:
        plist = mac_plist_path()
        if not plist.exists():
            info("本就没有登记，无需处理")
            return True
        if not apply_flag:
            info("演练模式：将移除 %s 并 bootout" % plist)
            return True
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d" % uid, str(plist)],
                       capture_output=True, text=True)
        backup_file(plist)
        plist.unlink()
        ok("已移除 launchd 自启")
        return True
    if IS_WIN:
        if not apply_flag:
            info("演练模式：将删除注册表项 %s\\%s" % (WIN_RUN_KEY, WIN_RUN_NAME))
            return True
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, WIN_RUN_NAME)
            ok("已删除注册表自启项")
        except FileNotFoundError:
            info("本就没有登记")
        except Exception as e:
            warn("删除失败：%s" % e)
            return False
        return True
    if IS_LINUX:
        unit = linux_unit_path()
        if not unit.exists():
            info("本就没有登记")
            return True
        if not apply_flag:
            info("演练模式：将停用并删除 %s" % unit)
            return True
        subprocess.run(["systemctl", "--user", "disable", "--now", unit.name],
                       capture_output=True, text=True)
        unit.unlink()
        ok("已移除 systemd --user 服务")
        return True
    warn("该平台不支持")
    return False


# --------------------------------------------------------------------------
# Syncthing：定位 / 安装 / 启动 / REST API
# --------------------------------------------------------------------------

def candidate_bins():
    root = cfg_sync_root()
    cands = []
    if IS_WIN:
        cands += [
            root / "bin" / "syncthing.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Syncthing" / "syncthing.exe",
            Path(r"C:\Program Files\Syncthing\syncthing.exe"),
            Path(r"C:\Program Files (x86)\Syncthing\syncthing.exe"),
        ]
    else:
        cands += [
            root / "bin" / "syncthing",
            Path("/opt/homebrew/bin/syncthing"),
            Path("/usr/local/bin/syncthing"),
            Path("/usr/bin/syncthing"),
            Path("/Applications/Syncthing.app/Contents/Resources/syncthing-macos-universal/syncthing"),
        ]
    c = load_config(quiet=True).get("syncthing_bin")
    if c:
        cands.insert(0, Path(c))
    return cands


def find_syncthing_bin():
    for p in candidate_bins():
        try:
            if p and p.exists() and os.access(str(p), os.X_OK):
                return p
        except OSError:
            continue
    w = which("syncthing")
    if w:
        return Path(w)
    return None


def candidate_homes():
    c = load_config(quiet=True).get("syncthing_home")
    out_ = []
    if c:
        out_.append(Path(c))
    out_ += [
        cfg_sync_root() / "syncthing-config",
        default_syncthing_home(),
        HOME / ".config" / "syncthing",
    ]
    return out_


def find_config_xml():
    for h in candidate_homes():
        x = h / "config.xml"
        try:
            if x.exists():
                return x
        except OSError:
            continue
    return None


def read_api_key(config_xml):
    try:
        txt = Path(config_xml).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"<apikey>\s*([^<\s]+)\s*</apikey>", txt)
    return m.group(1) if m else None


def _device_id_from_cert(config_xml):
    """从 cert.pem 证书计算本机设备 ID（Syncthing 算法，best-effort 兜底）。

    config.xml 里的 <device> 列表是「对端设备」，取第一个会拿到对端 ID，
    而不是本机 ID，所以不能用它。本机 ID 由本机证书（cert.pem 与
    config.xml 同目录）计算：deviceID = base32(sha256(证书 DER)) 前 52 字符
    + luhn32 校验字符。
    """
    cfg = Path(config_xml)
    cert = cfg.parent / "cert.pem"
    try:
        pem = cert.read_bytes()
    except Exception:
        return None
    try:
        b64 = b"".join(
            line for line in pem.split(b"\n")
            if line and not line.startswith(b"-----")
        )
        der = base64.b64decode(b64)
    except Exception:
        return None
    digest = hashlib.sha256(der).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    if len(b32) < 52:
        return None
    head = b32[:52]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    # luhn mod 32（Syncthing 算法）：正向遍历，factor 从 1 开始，逐字符切换 1→2→1→2…
    total = 0
    factor = 1
    for ch in head:
        addend = alphabet.index(ch) * factor
        factor = 2 if factor == 1 else 1
        addend = (addend // 32) + (addend % 32)
        total += addend
    check = (32 - (total % 32)) % 32
    check_ch = alphabet[check]
    device_id = head + check_ch
    return "-".join(device_id[i:i + 7] for i in range(0, len(device_id), 7))


def read_my_id_from_config(config_xml):
    """取本机设备 ID：优先用官方 `syncthing device-id`，失败再算证书。

    注：Syncthing 对证书 DER 的序列化与磁盘 cert.pem 可能不完全一致，
    自算哈希会有细微偏差，因此以官方命令为准、自算仅作离线兜底。
    """
    if not config_xml:
        return None
    binpath = find_syncthing_bin()
    if binpath:
        try:
            shome = Path(config_xml).parent
            r = subprocess.run(
                [str(binpath), "device-id", "--home=%s" % shome],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                cand = normalize_device_id(r.stdout.strip())
                if cand:
                    return cand
        except Exception:
            pass
    return _device_id_from_cert(config_xml)


class Syncthing:
    def __init__(self, config_xml=None):
        self.config_xml = config_xml or find_config_xml()
        self.key = read_api_key(self.config_xml) if self.config_xml else None
        # 管理端口跟随**本对象自己的** config.xml：同一台机器上跑第二个
        # Syncthing 实例（演练新机器接入）时，读写才会落到那个实例上。
        self._gui_base = api_base(self.config_xml) if self.config_xml else None

    @property
    def available(self):
        return bool(self.key) and port_open(
            port=gui_port(self.config_xml) if self.config_xml else None)

    def api(self, method, path, body=None, timeout=25):
        if not self.key:
            raise RuntimeError("没有 API key（Syncthing 未初始化）")
        url = (getattr(self, "_gui_base", None) or API_BASE) + path
        data = None
        headers = {"X-API-Key": self.key}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8", "replace")

    def status(self):
        return self.api("GET", "/rest/system/status")

    def config(self):
        return self.api("GET", "/rest/config")

    def my_id(self):
        try:
            return self.status().get("myID")
        except Exception:
            return None

    def folders(self):
        try:
            return {f["id"]: f for f in (self.config().get("folders") or [])}
        except Exception:
            return {}

    def devices(self):
        try:
            return {d["deviceID"]: d for d in (self.config().get("devices") or [])}
        except Exception:
            return {}

    def add_device(self, device_id, name):
        if device_id in self.devices():
            return False
        self.api("POST", "/rest/config/devices",
                 {"deviceID": device_id, "name": name})
        return True

    def ensure_folder(self, folder_id, label, path):
        f = self.folders().get(folder_id)
        payload = {
            "id": folder_id,
            "label": label,
            "path": str(path),
            "type": "sendreceive",
            "fsWatcherEnabled": True,
            "rescanIntervalS": 3600,
        }
        if f is None:
            payload["devices"] = [{"deviceID": self.my_id()}]
            self.api("PUT", "/rest/config/folders/%s" % folder_id, payload)
            return "created"
        changed = {}
        for k in ("label", "path", "type"):
            if f.get(k) != payload[k]:
                changed[k] = payload[k]
        if changed:
            self.api("PATCH", "/rest/config/folders/%s" % folder_id, changed)
            return "updated"
        return "unchanged"

    def share_folder(self, folder_id, device_id):
        f = self.folders().get(folder_id)
        if not f:
            return False, "同步夹未注册"
        ids = [d.get("deviceID") for d in (f.get("devices") or [])]
        if device_id in ids:
            return False, "已共享"
        ids.append(device_id)
        body = dict(f)
        body["devices"] = [{"deviceID": i} for i in ids]
        self.api("PUT", "/rest/config/folders/%s" % folder_id, body)
        return True, "已共享"

    def remove_device(self, device_id):
        self.api("DELETE", "/rest/config/devices/%s" % device_id)
        return True

    def db_status(self, folder_id):
        try:
            return self.api("GET", "/rest/db/status?folder=%s" % folder_id)
        except Exception:
            return None

    def restart(self):
        try:
            self.api("POST", "/rest/system/restart")
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------
# 安装 Syncthing
# --------------------------------------------------------------------------

RELEASE_API = "https://api.github.com/repos/syncthing/syncthing/releases/latest"
RELEASES_LATEST = "https://github.com/syncthing/syncthing/releases/latest"


def _official_asset_name(plat=None, arch=None, tag=""):
    """按官方命名规则拼出当前平台的产物名（**无需 GitHub API**）。
    命名形如 syncthing-<os>-<arch>-<tag>.zip / .tar.gz。"""
    plat = plat or sys.platform
    arch = arch or machine_arch()
    if plat == "darwin":
        os_name, ext = "macos", ".zip"
    elif plat.startswith("win"):
        os_name, ext = "windows", ".zip"
    else:
        os_name, ext = "linux", ".tar.gz"
    return "syncthing-%s-%s-%s%s" % (os_name, arch, tag, ext)


def fetch_latest_release():
    """取官方最新发布信息。

    优先 api.github.com（含每个产物的精确 size 与下载地址）；**失败时退回「网页重定向」**：
    访问 github.com/.../releases/latest → 跟随 302 拿到 tag（如 v2.1.6）→ 按官方命名规则
    直接拼出产物 URL。**api.github.com 在国内常被墙/超慢，而 github.com 往往可达**——这条
    退路让 `install` 在「API 被墙」时仍能下载（Windows 家庭网络尤其常见）。"""
    req = urllib.request.Request(RELEASE_API, headers={"User-Agent": APP})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        warn("GitHub API 不可用（%s），退回重定向解析最新版本…" % e)
        req2 = urllib.request.Request(RELEASES_LATEST, headers={"User-Agent": APP})
        with urllib.request.urlopen(req2, timeout=30) as r2:
            final = r2.geturl() or ""
        tag = final.rstrip("/").rsplit("/", 1)[-1]
        if not re.match(r"^v?\d", tag):
            raise RuntimeError("无法从重定向解析版本号：%s" % final)
        name = _official_asset_name(tag=tag)
        dl = "https://github.com/syncthing/syncthing/releases/download/%s/%s" % (tag, name)
        return {"tag_name": tag,
                "assets": [{"name": name, "size": 0, "browser_download_url": dl}]}


def _platform_keyword(plat=None, arch=None):
    """返回当前平台对应的官方产物名关键字（用于在 vendor 里匹配离线包）。"""
    plat = plat or sys.platform
    arch = arch or machine_arch()
    if plat == "darwin":
        return ("macos-universal", "macos-%s" % arch)
    if plat.startswith("win"):
        return ("windows-%s" % arch,)
    return ("linux-%s" % arch,)


def find_vendor_archive(plat=None, arch=None):
    """在 skill 的 vendor/ 目录里找当前平台的离线安装包。

    返回 (archive_path, asset_name) 或 (None, None)。
    """
    vd = vendor_dir()
    if not vd.is_dir():
        return None, None
    for kw in _platform_keyword(plat, arch):
        for f in sorted(vd.iterdir()):
            if not f.is_file():
                continue
            name = f.name.lower()
            if kw in name and (name.endswith(".zip") or name.endswith(".tar.gz")):
                return f, f.name
    return None, None


def pick_asset(release, plat=None, arch=None):
    """按平台挑官方产物。跨平台的关键就在这里。"""
    plat = plat or sys.platform
    arch = arch or machine_arch()
    if plat == "darwin":
        # 优先「架构专用」包（体积约为 universal 的一半，下载更快）；匹配不到再退回 universal
        for want in ("macos-%s" % arch, "macos-universal"):
            for a in release.get("assets", []):
                if want in a["name"] and a["name"].endswith(".zip"):
                    return a
    elif plat.startswith("win"):
        for a in release.get("assets", []):
            if "windows-%s" % arch in a["name"] and a["name"].endswith(".zip"):
                return a
    else:
        for a in release.get("assets", []):
            if "linux-%s" % arch in a["name"] and a["name"].endswith(".tar.gz"):
                return a
    return None


def fetch_checksums(release):
    """官方只提供 GPG 签名的 sha256sum.txt.asc，这里剥掉签名外壳取哈希表。"""
    url = None
    for a in release.get("assets", []):
        if a["name"] == "sha256sum.txt.asc":
            url = a["browser_download_url"]
            break
    if not url:
        return {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": APP})
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception:
        return {}
    table = {}
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or ":" in line.split(" ")[0]:
            continue
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if m:
            table[m.group(2).strip()] = m.group(1).lower()
    return table


def download(url, dest, label, retries=3):
    """带自动重试的下载（网络抖动/连接重置时重来，最多 retries 次）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            info("下载 %s%s" % (label, ("（第 %d/%d 次）" % (attempt, retries)) if attempt > 1 else ""))
            req = urllib.request.Request(url, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=300) as r, open(str(dest), "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                last = 0.0
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if total and now - last > 0.5:
                        last = now
                        pct = got * 100.0 / total
                        sys.stdout.write("\r    %5.1f%%  %s / %s   " % (pct, human(got), human(total)))
                        sys.stdout.flush()
            if total:
                sys.stdout.write("\r" + " " * 48 + "\r")
            # 完整性：若服务端给了 Content-Length，校验字节数，避免半截包当成功
            if total and dest.stat().st_size != total:
                raise RuntimeError("下载不完整：%d/%d 字节" % (dest.stat().st_size, total))
            ok("已下载 %s" % human(dest.stat().st_size))
            return dest
        except Exception as e:
            last_err = e
            warn("下载失败：%s" % e)
            if attempt < retries:
                time.sleep(min(2 * attempt, 6))
    raise RuntimeError("下载重试 %d 次仍失败：%s" % (retries, last_err))


def sha256_of(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_real_binary_member(name, size, target_name):
    """判定包内成员是不是真正的可执行文件。

    坑：官方 macOS 包里有两个叫 syncthing 的东西 ——
      etc/freebsd-rc/syncthing   ~1.7KB 的 shell 包装脚本（在包内排前面）
      <顶层>/syncthing           ~57MB 的真二进制
    按名字取第一个会拿到脚本，启动时报 /etc/rc.subr 不存在。
    所以排除 etc/ 与 .app/，并在多个候选中取最大者。
    """
    if os.path.basename(name) != target_name:
        return False
    norm = name.replace("\\", "/")
    if "/etc/" in norm or norm.startswith("etc/") or ".app/" in norm:
        return False
    if size < 1 << 20:  # 真二进制至少 1MB
        return False
    return True


def extract_binary(archive, dest_bin):
    dest_bin = Path(dest_bin)
    dest_bin.parent.mkdir(parents=True, exist_ok=True)
    target_name = "syncthing.exe" if IS_WIN else "syncthing"

    if str(archive).endswith(".zip"):
        with zipfile.ZipFile(str(archive)) as z:
            cands = [i for i in z.infolist()
                     if not i.is_dir()
                     and _is_real_binary_member(i.filename, i.file_size, target_name)]
            if not cands:
                raise RuntimeError("压缩包内找不到真正的 %s" % target_name)
            pick = max(cands, key=lambda i: i.file_size)
            with z.open(pick.filename) as src, open(str(dest_bin), "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(str(archive), "r:gz") as t:
            cands = [m for m in t.getmembers()
                     if m.isfile()
                     and _is_real_binary_member(m.name, m.size, target_name)]
            if not cands:
                raise RuntimeError("压缩包内找不到真正的 %s" % target_name)
            pick = max(cands, key=lambda m: m.size)
            src = t.extractfile(pick)
            with open(str(dest_bin), "wb") as dst:
                shutil.copyfileobj(src, dst)

    if not IS_WIN:
        dest_bin.chmod(dest_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if IS_MAC:
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(dest_bin)],
                           capture_output=True)
    return dest_bin


def cmd_install(args):
    head("安装 Syncthing")
    info("平台：%s / %s" % (PLATFORM_LABEL, machine_arch()))

    existing = find_syncthing_bin()
    if existing and not args.force:
        ok("已经装了：%s" % existing)
        info("要强制重装请加 --force")
        return 0

    dest_dir = cfg_sync_root() / "bin"
    dest_bin = dest_dir / ("syncthing.exe" if IS_WIN else "syncthing")
    staging = cfg_sync_root() / "downloads"
    staging.mkdir(parents=True, exist_ok=True)

    from_file = (getattr(args, "from_file", "") or "").strip()
    mirror = (getattr(args, "mirror", "") or os.environ.get("WBSYNC_DL_MIRROR") or "").strip()

    # —— 离线优先 ①：显式 --from-file 本地包（零下载，最快；可从 U 盘/资料库/另一台机拿）——
    # —— 离线优先 ②：skill 自带 vendor/ 里有匹配当前平台的包 ——
    vendor_archive, vendor_name = find_vendor_archive()
    local_archive = None
    if from_file:
        fa = Path(os.path.expanduser(from_file))
        if not fa.exists():
            die("--from-file 指定的文件不存在：%s" % fa)
        archive, asset_name, local_archive = fa, fa.name, fa
        if not args.apply:
            info("演练模式：将用本地包 %s 解出 %s（无需联网）" % (asset_name, dest_bin))
            info("确认后加 --apply 真正安装")
            return 0
        ok("使用本地安装包：%s（跳过下载）" % asset_name)
    elif vendor_archive:
        archive = vendor_archive
        asset_name = vendor_name
        local_archive = vendor_archive
        if not args.apply:
            info("演练模式：将用离线包 %s 解出 %s（无需联网）" % (asset_name, dest_bin))
            info("确认后加 --apply 真正安装")
            return 0
        ok("离线包命中：%s（跳过下载）" % asset_name)
    else:
        try:
            rel = fetch_latest_release()
        except Exception as e:
            die("取不到官方发布信息，且无本地/内置离线包：%s\n"
                "  提示：① 用本地包离线装：`wbsync install --from-file <syncthing-*.zip> --apply`；"
                "② 网络慢/被墙时加 `--mirror <镜像前缀>`（或设 WBSYNC_DL_MIRROR）。" % e)
        info("官方最新版：%s" % rel.get("tag_name"))

        asset = pick_asset(rel)
        if not asset:
            die("找不到适配 %s/%s 的官方产物" % (PLATFORM_LABEL, machine_arch()))
        info("选中产物：%s%s" % (asset["name"],
                                ("（%s）" % human(asset["size"])) if asset.get("size") else ""))
        asset_name = asset["name"]
        archive = staging / asset_name

        url = asset.get("browser_download_url") or ""
        if mirror:
            url = (mirror.rstrip("/") + "/" + url) if ("://" in mirror and url) \
                else (mirror.rstrip("/") + "/" + asset["name"])
            info("经镜像下载：%s" % mirror)
        if not url:
            die("官方产物缺少下载地址（browser_download_url）：%s" % asset.get("name"))

        if not args.apply:
            info("演练模式：将下载到 %s 并解出 %s" % (archive, dest_bin))
            info("确认后加 --apply 真正安装")
            return 0

        try:
            download(url, archive, asset_name)
        except Exception as e:
            die("下载失败：%s\n  提示：网络差/被墙可加 `--mirror <镜像前缀>` 换下载源，"
                "或用 `--from-file <syncthing-*.zip>` 本地包离线装。" % e)

    # —— 校验：在线下载走官方 checksums；本地/内置包走同名 .sha256 清单（若有） ——
    if local_archive:
        sidecar = Path(str(archive) + ".sha256")
        if sidecar.exists():
            want = sidecar.read_text(encoding="utf-8").strip().split()[0].lower()
            got = sha256_of(archive)
            if got != want:
                die("离线包校验失败！期望 %s，实际 %s" % (want[:16], got[:16]))
            ok("离线包 SHA256 校验通过")
        else:
            warn("离线包未附带 .sha256 清单，跳过校验（随 skill 分发，默认可信）")
    else:
        checks = fetch_checksums(rel)
        want = checks.get(asset_name)
        if want:
            got = sha256_of(archive)
            if got != want:
                die("校验失败！期望 %s，实际 %s" % (want[:16], got[:16]))
            ok("SHA256 校验通过")
        else:
            warn("官方未提供可解析的校验值，跳过校验（源码已 HTTPS 传输）")

    try:
        extract_binary(archive, dest_bin)
    except Exception as e:
        die("解压失败：%s" % e)
    ok("已安装：%s" % dest_bin)

    if local_archive is None:
        try:
            archive.unlink()   # 仅在线下载的临时包才删；本地/内置包属于用户/分发资产，保留
        except OSError:
            pass

    cfg = load_config(quiet=True)
    cfg["syncthing_bin"] = str(dest_bin)
    save_config(cfg, True)
    ok("已记录安装位置到配置")

    r = subprocess.run([str(dest_bin), "--version"], capture_output=True, text=True)
    v = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    if v:
        info(v[0])
    return 0


# --------------------------------------------------------------------------
# 启动 / 停止
# --------------------------------------------------------------------------

def syncthing_home_effective():
    cfg = load_config(quiet=True)
    if cfg.get("syncthing_home"):
        return Path(cfg["syncthing_home"])
    x = find_config_xml()
    if x:
        return x.parent
    return cfg_sync_root() / "syncthing-config"


def start_syncthing(binpath, shome, apply_flag=True):
    shome = Path(shome)
    if port_open():
        ok("Syncthing 已在运行（端口 %d 有响应）" % gui_port())
        return True
    if not apply_flag:
        info("演练模式：将执行 %s" % " ".join(_serve_argv(binpath, shome)))
        return True
    shome.mkdir(parents=True, exist_ok=True)
    logs = cfg_sync_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logf = open(str(logs / "syncthing.out.log"), "ab")
    kwargs = dict(stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(_serve_argv(binpath, shome), **kwargs)
    except Exception as e:
        bad("启动失败：%s" % e)
        return False
    for _ in range(60):
        if port_open():
            ok("Syncthing 已启动")
            return True
        time.sleep(0.5)
    warn("等了一分钟端口仍未就绪，看日志：%s" % (logs / "syncthing.out.log"))
    return False


def cmd_start(args):
    head("启动 Syncthing")
    b = find_syncthing_bin()
    if not b:
        die("没找到 Syncthing，先跑 wbsync install")
    info("可执行文件：%s" % b)
    shome = syncthing_home_effective()
    info("配置目录：%s" % shome)
    start_syncthing(b, shome, apply_flag=True)
    return 0


def cmd_stop(args):
    head("停止 Syncthing")
    procs = find_processes("syncthing")
    if procs is None:
        warn("无法枚举进程（权限受限），请到活动监视器/任务管理器里确认")
        return 1
    if not procs:
        info("没有在跑的 Syncthing 进程")
        return 0
    for pid, name, _ in procs:
        if not args.apply:
            info("演练模式：将终止 PID %d (%s)" % (pid, name))
            continue
        kill_pid(pid, force=True)
        ok("已终止 PID %d" % pid)
    if not args.apply:
        info("确认后加 --apply")
    return 0


# --------------------------------------------------------------------------
# 登记层自愈（本工具的核心价值）
# --------------------------------------------------------------------------

def find_marketplace_plugin_json(pkg_dir):
    for rel in ("plugin.json", ".codebuddy-plugin/plugin.json",
                ".workbuddy-plugin/plugin.json", "manifest.json",
                ".claude-plugin/plugin.json"):
        p = Path(pkg_dir) / rel
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")), p
            except Exception:
                continue
    return None, None


def norm_slashes(s):
    return str(s).replace("\\", "/")


def _resolve_version_dir(plugin_dir, ver):
    """在 plugin_dir 下找出与 ver 对应的版本目录。

    坑（实测踩到）：登记文件里的 version 与磁盘上的目录名可能只差一个分隔符。
    例：wind-finance 的 version 是 `1.2.2+wb.78fa61451b4bf44c`，
        而目录名是 `1.2.2-wb.78fa61451b4bf44c` —— `+` 变成了 `-`。
    所以精确匹配失败时，把 + 归一成 - 再找一次；只有唯一命中才采用，
    避免在有多个相似目录时猜错。
    """
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return None
    exact = plugin_dir / str(ver)
    if exact.is_dir():
        return exact

    def norm(s):
        return str(s).replace("+", "-").replace("_", "-")

    target = norm(ver)
    hits = [c for c in plugin_dir.iterdir() if c.is_dir() and norm(c.name) == target]
    return hits[0] if len(hits) == 1 else None


# 明确属于"应用自带、与本机用户无关"的路径 —— 一律不碰。
# 注意判据的方向：**只排除明确是应用内部的**，而不是"只处理含 /Users/ 的"。
# 后者在非标准安装位置（如 D:\Apps\.workbuddy）下会把该修的条目误判成内置而跳过。
APP_INTERNAL = re.compile(r"\.app/|/Applications/|program files", re.I)


def heal_installed_plugins(apply_flag):
    """重建 installed_plugins.json 里写死的绝对路径。

    关键洞察：installPath 的**尾部结构**是机器无关的 ——
        <根>/plugins/cache/<市场名>/<专家名>/<版本>
    所以不必解析旧路径，直接按本机根目录重建即可，天然跨平台（顺手把反斜杠修正）。
    """
    path = WB_ROOT / "plugins" / "installed_plugins.json"
    result = {"file": str(path), "exists": path.exists(),
              "fixed": [], "missing": [], "total": 0,
              "skipped_builtin": 0, "already_ok": 0}

    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        result["error"] = str(e)
        return result

    plugins = data.get("plugins") or {}
    changed = False

    for key, installs in list(plugins.items()):
        if "@" not in key:
            continue
        name, market = key.rsplit("@", 1)
        if not isinstance(installs, list):
            continue
        for entry in installs:
            if not isinstance(entry, dict):
                continue
            ver = entry.get("version") or ""
            # 明确是应用自带的（.app / Program Files）→ 跳过，不动
            if APP_INTERNAL.search(norm_slashes(entry.get("installPath", ""))):
                result["skipped_builtin"] += 1
                continue
            result["total"] += 1

            cur = Path(entry.get("installPath") or "")

            # 规则 A：现有路径在本机真实存在 —— 已经是对的。
            # 不做任何改动，也不报成问题。这条能挡住"版本号与目录名写法不一致"
            # 造成的误报（见 _resolve_version_dir 的说明）。
            if cur.is_dir():
                result["already_ok"] += 1
                continue

            if not ver:
                result["missing"].append((key, "缺少 version，无法重建"))
                continue

            want = _resolve_version_dir(WB_ROOT / "plugins" / "cache" / market / name, ver)
            if want is not None:
                result["fixed"].append((key, str(cur), str(want)))
                entry["installPath"] = str(want)
                changed = True
            else:
                result["missing"].append(
                    (key, "本机不存在：%s" % (WB_ROOT / "plugins" / "cache" / market / name / ver)))

    if changed and apply_flag:
        bak = backup_file(path)
        if bak:
            result["backup"] = str(bak)
        atomic_write_json(path, data)
    result["changed"] = changed
    return result


def heal_known_marketplaces(apply_flag):
    path = WB_ROOT / "plugins" / "known_marketplaces.json"
    result = {"file": str(path), "exists": path.exists(), "fixed": [], "total": 0}
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        result["error"] = str(e)
        return result

    changed = False
    for mname, m in data.items():
        if not isinstance(m, dict):
            continue
        if m.get("isBuiltIn") or m.get("type") != "directory":
            continue
        cur = m.get("installLocation") or ""
        if APP_INTERNAL.search(norm_slashes(cur)):
            continue
        result["total"] += 1
        want = WB_ROOT / "plugins" / "marketplaces" / mname
        if norm_slashes(cur) != norm_slashes(want) and want.exists():
            result["fixed"].append((mname, str(cur), str(want)))
            m["installLocation"] = str(want)
            if isinstance(m.get("source"), dict) and m["source"].get("path"):
                m["source"]["path"] = str(want)
            changed = True

    # 确保 my-experts 市场已登记
    if EXPERT_MARKETPLACE not in data:
        d = WB_ROOT / "plugins" / "marketplaces" / EXPERT_MARKETPLACE
        if d.exists():
            data[EXPERT_MARKETPLACE] = {
                "manifestName": EXPERT_MARKETPLACE,
                "type": "directory",
                "source": {"source": "directory", "path": str(d)},
                "installLocation": str(d),
                "description": "个人专家市场（由 wbsync 同步）",
                "lastUpdated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "autoUpdate": False,
            }
            result["fixed"].append((EXPERT_MARKETPLACE, "(缺失)", str(d)))
            changed = True

    if changed and apply_flag:
        bak = backup_file(path)
        if bak:
            result["backup"] = str(bak)
        atomic_write_json(path, data)
    result["changed"] = changed
    return result


def version_drift_report():
    """比对源包版本 vs 已登记运行版本 —— 检出「装了但跑旧版」这一静默故障。"""
    src_root = WB_ROOT / "plugins" / "marketplaces" / EXPERT_MARKETPLACE
    if not src_root.exists():
        return []
    reg_path = WB_ROOT / "plugins" / "installed_plugins.json"
    installed = {}
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        for key, installs in (data.get("plugins") or {}).items():
            if key.endswith("@" + EXPERT_MARKETPLACE):
                versions = [e.get("version") for e in installs if isinstance(e, dict)]
                installed[key.split("@")[0]] = versions
    except Exception:
        pass

    drift = []
    for pkg in sorted(src_root.iterdir()):
        if not pkg.is_dir() or pkg.name.startswith("."):
            continue
        meta, mpath = find_marketplace_plugin_json(pkg)
        src_ver = (meta or {}).get("version")
        reg_vers = installed.get(pkg.name)
        if reg_vers is None:
            drift.append((pkg.name, src_ver, None, "未登记（仅同步了源包）"))
        elif src_ver and src_ver not in reg_vers:
            drift.append((pkg.name, src_ver, reg_vers, "运行版本 ≠ 源包版本（静默跑旧版）"))
    return drift


def cmd_expert(args):
    head("专家 / 连接器 登记层自愈")
    info("登记目录：%s" % WB_ROOT)
    out()
    info("原理：登记文件里写的是含用户名的绝对路径，跨机必须重建。")
    info("      尾部结构 <根>/plugins/cache/<市场>/<专家或连接器>/<版本> 是机器无关的，据此重建。")
    info("      覆盖专家与连接器两类插件：它们登记在同一个 installed_plugins.json。")

    if not args.apply:
        info("当前为演练模式，不会写盘")

    section("1. installed_plugins.json")
    r1 = heal_installed_plugins(args.apply)
    if not r1["exists"]:
        warn("文件不存在：%s" % r1["file"])
    elif r1.get("error"):
        bad("解析失败：%s" % r1["error"])
    else:
        info("检查 %d 条本机路径条目（另有 %d 条内置插件，按设计不动）"
             % (r1["total"], r1.get("skipped_builtin", 0)))
        if r1["fixed"]:
            ok("需要重建 %d 条" % len(r1["fixed"]))
            for key, old, new in r1["fixed"][:10]:
                out("      %s" % key)
                out("        旧 %s" % old)
                out("        新 %s" % new)
            if len(r1["fixed"]) > 10:
                out("      …… 还有 %d 条" % (len(r1["fixed"]) - 10))
        else:
            ok("无需重建")
        if r1.get("already_ok"):
            info("%d 条路径已是本机的且真实存在" % r1["already_ok"])
        if r1["missing"]:
            warn("%d 条无法重建" % len(r1["missing"]))
            for key, why in r1["missing"][:5]:
                out("      %s —— %s" % (key, why))
        if r1.get("backup"):
            info("已备份：%s" % r1["backup"])

    section("2. known_marketplaces.json")
    r2 = heal_known_marketplaces(args.apply)
    if not r2["exists"]:
        warn("文件不存在")
    elif r2.get("error"):
        bad("解析失败：%s" % r2["error"])
    else:
        if r2["fixed"]:
            ok("需要重建 %d 处" % len(r2["fixed"]))
            for name, old, new in r2["fixed"][:8]:
                out("      %s → %s" % (name, new))
        else:
            ok("无需重建")
        if r2.get("backup"):
            info("已备份：%s" % r2["backup"])

    section("3. 版本漂移检查（装了但跑旧版）")
    drift = version_drift_report()
    if not drift:
        ok("没有发现漂移")
    else:
        for name, sv, rv, why in drift:
            warn("%s：源包 %s / 运行 %s —— %s" % (name, sv, rv, why))

    if not args.apply:
        out()
        info("演练结束，未改动任何文件。加 %s--apply%s 真正执行。" % (C.B, C.R))
    else:
        out()
        ok("登记层自愈完成。建议重启 WorkBuddy 让登记生效。")
    return 0


# --------------------------------------------------------------------------
# 四个同步夹
# --------------------------------------------------------------------------

def folder_table():
    rows = []
    for fid, label, getter in FOLDER_SPECS:
        try:
            p = Path(getter())
        except Exception:
            p = Path("?")
        rows.append((fid, label, p, p.exists()))
    return rows


SECURE_MARK = "wbsync-secure/1"
SECURE_RULES = """// wbsync-secure/1 · 同步夹禁放应用层密钥（`wbsync secure on --apply` 维护）
// 命中即不上传；Syncthing .stignore 语法。下方可追加你自己的规则
*.pem
*.key
*.p12
*.pfx
*.keystore
*.jks
.env
.env.*
id_rsa*
id_ed25519*
id_ecdsa*
credentials.json
secrets.json
.secrets*
*.token
"""
_SECRET_NAME_PAT = re.compile(
    r"([^/]*\.(pem|key|p12|pfx|keystore|jks)$|(^|/)\.env(\..*)?$|(^|/)id_(rsa|ed25519|ecdsa).*$"
    r"|(^|/)credentials\.json$|(^|/)secrets\.json$)", re.I)
_SECRET_BODY_PAT = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})")


def _secure_check_state(folder_path):
    """返回 (有 .stignore?, 含 wbsync 规则?, 行数)。"""
    p = Path(folder_path) / ".stignore"
    if not p.exists():
        return False, False, 0
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True, False, 0
    return True, SECURE_MARK in txt, len(txt.splitlines())


def cmd_secure(args):
    """安全加固（问题队列 #9 ①②③④；**不含全盘加密——归 OS**）。

    - status（默认）：各同步夹是否有 .stignore + 是否含密钥忽略规则
    - on --apply：写入/追加 .stignore（保留已有规则）；.stignore 本机生效、不入同步 → **每机各跑一次**
    - scan：扫 4 个同步夹的疑似密钥（文件名 + 内容指纹）
    """
    action = (getattr(args, "action", None) or "status").lower()
    rows = folder_table()

    if action == "status":
        head("同步夹安全加固状态（.stignore 密钥忽略）")
        # 变量名必须避开模块级函数 bad()——否则整个 cmd_secure 里 `bad` 被判为局部变量，
        # on 分支的 except 调 bad(...) 会在「局部未赋值」上抛 UnboundLocalError
        # （B 机 2026-10-17 首次跑 secure on --apply 即崩，见 R-20261017-B-代码v4 §1）。
        nbad = 0
        for fid, label, p, exists in rows:
            if not exists:
                info("  ✗ %s（%s）目录不存在，跳过" % (label, fid))
                continue
            has, has_rule, n = _secure_check_state(p)
            if not has:
                warn("  ✗ %s —— 无 .stignore（密钥可能被同步）" % label)
                nbad += 1
            elif not has_rule:
                warn("  ⚠️ %s —— 有 .stignore 但缺 wbsync-secure 规则" % label)
                nbad += 1
            else:
                ok("  ✓ %s —— 就绪（%d 行，含密钥规则）" % (label, n))
        out()
        if nbad:
            info("修复：wbsync secure on --apply（保留已有规则，追加密钥忽略）")
        else:
            ok("同步夹均已加固。")
        return 0

    if action == "on":
        head("加固同步夹：写入 .stignore 密钥忽略规则")
        applied = 0
        for fid, label, p, exists in rows:
            if not exists:
                continue
            fp = Path(p) / ".stignore"
            old = ""
            if fp.exists():
                try:
                    old = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    old = ""
            if SECURE_MARK in old:
                info("  · %s —— 已有规则，跳过" % label)
                continue
            if not getattr(args, "apply", False):
                info("  · 演练：将向 %s 追加 %d 行规则" % (fp, SECURE_RULES.count("\n")))
                applied += 1
                continue
            new = (old.rstrip() + "\n\n" if old.strip() else "") + SECURE_RULES
            try:
                fp.write_text(new, encoding="utf-8")
                ok("  ✓ %s —— 已写入 %s" % (label, fp))
                applied += 1
            except Exception as e:
                bad("  ✗ %s 写入失败：%s" % (label, e))
        if not getattr(args, "apply", False):
            info("以上为演练。加 --apply 真正写入（.stignore 本机生效、不入同步 → **每台机各跑一次**）")
        elif applied:
            info("已完成 %d 个夹。提示：其余机器需各自跑一次同样命令。" % applied)
        return 0

    if action == "scan":
        head("密钥扫描（文件名 + 内容指纹）")
        hits = []
        skip = {".git", "node_modules", ".stversions", "__pycache__", "downloads", "logs"}
        for fid, label, p, exists in rows:
            if not exists:
                continue
            root = Path(p)
            cnt = 0
            try:
                for f in root.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        rel = f.relative_to(root)
                    except Exception:
                        continue
                    if set(rel.parts) & skip:
                        continue
                    rs = str(rel).replace("\\", "/")
                    if _SECRET_NAME_PAT.search(rs):
                        hits.append((label, rs, "文件名"))
                        cnt += 1
                        continue
                    try:
                        if f.stat().st_size > 512 * 1024:
                            continue
                        txt = f.open("rb").read(4096).decode("utf-8", "ignore")
                    except Exception:
                        continue
                    m = _SECRET_BODY_PAT.search(txt)
                    if m:
                        hits.append((label, rs, "内容:" + m.group(1)[:24]))
                        cnt += 1
                    if cnt > 20:
                        break
            except Exception:
                pass
        if not hits:
            ok("未发现疑似密钥（文件名 / 内容指纹均无命中）。")
            info("覆盖 4 个同步夹；跳过 .git/.stversions/node_modules/downloads/logs。")
        else:
            warn("发现 %d 处疑似密钥（人工确认后移出同步夹）：" % len(hits))
            for label, rel, why in hits[:20]:
                out("  · [%s] %s   ← %s" % (label, rel, why))
            if len(hits) > 20:
                info("（还有 %d 处未显示）" % (len(hits) - 20))
            info("修法：移到同步夹外（如 ~/secrets/），或确认后加入本机 .stignore。")
        return 0

    die("未知动作：%s（应为 status / on / scan）" % action, 1)


def cmd_folders(args):
    head("同步夹")
    st = Syncthing()
    reg = st.folders() if st.available else {}
    for fid, label, p, exists in folder_table():
        mark = "✓" if exists else "✗"
        status = "未注册"
        if fid in reg:
            status = "已注册"
            n = len(reg[fid].get("devices") or [])
            status += "，已共享给 %d 台设备" % max(0, n - 1)
        out("  %s %s" % (mark, label))
        out("      id   %s" % fid)
        out("      路径 %s" % p)
        out("      注册 %s" % status)
    return 0


def ensure_folders(apply_flag, st=None):
    st = st or Syncthing()
    created_needed = []
    for fid, label, p, exists in folder_table():
        if not exists:
            created_needed.append((fid, label, p))
    return created_needed


# --------------------------------------------------------------------------
# 现状体检 / 安装配置 / 配对 / 体检
# --------------------------------------------------------------------------

def gather_state():
    s = {}
    s["platform"] = "%s / %s" % (PLATFORM_LABEL, platform.machine())
    s["python"] = sys.version.split()[0]
    s["wb_root"] = str(WB_ROOT)
    s["wb_root_exists"] = WB_ROOT.exists()
    s["sync_root"] = str(cfg_sync_root())
    b = find_syncthing_bin()
    s["syncthing_bin"] = str(b) if b else None
    s["config_xml"] = str(find_config_xml()) if find_config_xml() else None
    s["port_open"] = port_open()
    s["procs"] = find_processes("syncthing")
    st = Syncthing()
    s["api_ok"] = st.available
    s["my_id"] = st.my_id() if st.available else read_my_id_from_config(find_config_xml())
    s["folders"] = folder_table()
    s["devices"] = st.devices() if st.available else {}
    cfg = load_config(quiet=True)
    s["peer_id"] = cfg.get("peer_id") or ""
    s["initialized"] = bool(cfg.get("initialized"))
    s["initialized_at"] = cfg.get("initialized_at") or ""
    s["account_user"] = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    s["home"] = str(HOME)
    s["autostart"] = autostart_status(b, syncthing_home_effective()) if b else (False, "Syncthing 未安装")
    s["plugin_registry"] = str(WB_ROOT / "plugins" / "installed_plugins.json")
    s["plugin_registry_exists"] = (WB_ROOT / "plugins" / "installed_plugins.json").exists()
    return s


def cmd_plan(args):
    head("WorkBuddy 多机同步 · 现状")
    s = gather_state()
    if args.json:
        out(json.dumps(s, ensure_ascii=False, indent=2, default=str))
        return 0

    section("本机")
    info("平台      %s" % s["platform"])
    info("Python    %s" % s["python"])
    info("配置根    %s %s" % (s["wb_root"], "" if s["wb_root_exists"] else "（不存在！）"))
    info("同步根    %s" % s["sync_root"])

    section("同步引擎 Syncthing")
    if s["syncthing_bin"]:
        ok("已安装     %s" % s["syncthing_bin"])
    else:
        bad("未安装     —— 跑 wbsync install")
    if s["procs"] is None:
        warn("无法枚举进程（本环境权限受限），不确定是否在跑")
    elif s["procs"]:
        ok("进程在跑   %d 个" % len(s["procs"]))
    else:
        warn("进程没跑   —— 跑 wbsync start")
    info("管理端口   %s" % ("%d 有响应" % gui_port() if s["port_open"] else "%d 无响应" % gui_port()))
    info("配置文件   %s" % (s["config_xml"] or "尚未生成（首次启动后才有）"))
    info("设备 ID    %s" % (s["my_id"] or "未知（需先启动 Syncthing）"))

    section("同步夹")
    for fid, label, p, exists in s["folders"]:
        out("  %s %s %s" % ("✓" if exists else "✗", wpad(label, 16), p))

    section("配对")
    if s["devices"]:
        others = [(k, v.get("name") or "") for k, v in s["devices"].items()
                  if k != s["my_id"]]
        if others:
            ok("已配对 %d 台" % len(others))
            for d, n in others:
                out("      %s  %s" % (d[:20] + "…", n))
        else:
            warn("只认识本机，还没配过对端")
    else:
        warn("读不到设备表（Syncthing 未运行或未初始化）")
    info("配置里记的对端 ID：%s" % (s["peer_id"] or "（空）"))

    section("登录自启")
    on, why = s["autostart"]
    (ok if on else warn)(why)

    section("登记层")
    info("文件      %s" % s["plugin_registry"])
    if s["plugin_registry_exists"]:
        r = heal_installed_plugins(False)
        if r.get("changed") or r["fixed"] or r["missing"]:
            warn("需要自愈：%d 条待重建，%d 条无法重建 —— 跑 wbsync expert"
                 % (len(r["fixed"]), len(r["missing"])))
        else:
            ok("路径正常，无需自愈")
    else:
        warn("登记文件不存在")

    section("下一步")
    steps = []
    if not s["syncthing_bin"]:
        steps.append("wbsync install      装 Syncthing")
    if s["syncthing_bin"] and not s["port_open"]:
        steps.append("wbsync start        启动 Syncthing")
    missing = [r for r in s["folders"] if not r[3]]
    if missing:
        steps.append("wbsync setup --apply  建立 %d 个同步夹并注册" % len(missing))
    if s["api_ok"] and len([d for d in s["devices"] if d != s["my_id"]]) == 0:
        steps.append("wbsync id           取本机设备 ID，拿去对端配对")
    steps.append("wbsync expert --apply  修登记层（专家同步过来后必做）")
    steps.append("wbsync autostart on --apply  登记开机自启")
    for i, t in enumerate(steps, 1):
        out("  %d. %s" % (i, t))
    return 0


def cmd_status(args):
    """只读：输出本账号身份 + 是否首次使用本 skill。供 AI 在 skill 运行时判断走完整引导还是跳过。"""
    cfg = load_config(quiet=True)
    initialized = bool(cfg.get("initialized"))
    payload = {
        "account_user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "home": str(HOME),
        "wb_root": str(WB_ROOT),
        "first_use": not initialized,
        "initialized": initialized,
        "initialized_at": cfg.get("initialized_at") or "",
        "disk_root": cfg.get("disk_root") or "",
        "workspace_root": cfg.get("workspace_root") or "",
        "sync_root": str(cfg_sync_root()),
        "config_exists": CONFIG_PATH.exists(),
    }
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    head("账号身份 · 是否首次使用")
    info("账号用户   %s" % (payload["account_user"] or "（未知）"))
    info("主目录     %s" % payload["home"])
    info("配置根     %s" % payload["wb_root"])
    if payload["first_use"]:
        warn("首次使用   是 —— 需走完整引导（问位置、装引擎、配对）")
    else:
        ok("首次使用   否 —— 已于 %s 完成过初始化" % (payload["initialized_at"] or "未知时间"))
    info("盘根目录   %s" % (payload["disk_root"] or "（未设置）"))
    info("工作空间根 %s" % (payload["workspace_root"] or "（未设置）"))
    info("同步根     %s" % payload["sync_root"])
    return 0


def cmd_identity(args):
    """输出本账号的 WorkBuddy identityKey（账号 UUID），用于区分「同一个人」的多台机器。"""
    ik = wb_identity_key()
    payload = {
        "identity_key": ik,
        "account_user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "device_id": _my_device_id() or "",
    }
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    head("WorkBuddy 账号身份")
    info("账号身份   %s" % (ik or "（未识别到 WorkBuddy 账号）"))
    info("系统用户   %s" % (payload["account_user"] or "（未知）"))
    info("本机设备   %s" % (payload["device_id"] or "（未知）"))
    out()
    if ik:
        info("同一个人在多台机器登录同一 WorkBuddy 账号，identityKey 相同；")
        info("配对时凭它区分「自己人」和「陌生人」。")
    else:
        warn("未识别到账号身份，配对时将无法自动区分同账号/陌生设备。")
    return 0


def cmd_mark_initialized(args):
    """把本账号标记为「已完成本 skill 初始化」。首次引导全部完成后由 AI 调用落盘。"""
    cfg = load_config(quiet=True)
    if not args.apply:
        info("演练：将把本账号标记为已初始化（initialized=true，记录当前时间）")
        return 0
    cfg["initialized"] = True
    cfg["initialized_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_config(cfg, True)
    ok("已标记本账号完成初始化（%s）" % cfg["initialized_at"])
    return 0


def cmd_probe(args):
    """只读：探测本机在同步组里该分到的后缀（第几台）。不写盘，供连接时先确认自己编号。"""
    reg = _read_ws_registry()
    st = Syncthing()
    my_id = _my_device_id()
    if not my_id:
        die("拿不到本机设备 ID。先 wbsync start 再试。")

    suffix, is_new = _resolve_my_suffix(reg, my_id)
    machines = reg.get("machines", {})
    occupied = sorted(machines.keys())

    payload = {
        "suffix": suffix,
        "is_new": is_new,
        "occupied": occupied,
        "total_machines": len(machines),
        "my_id": my_id,
        "hostname": HOSTNAME,
        "identity_key": wb_identity_key(),
    }
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    head("探测本机编号（第几台）")
    info("本机设备   %s" % my_id)
    info("主机名     %s" % HOSTNAME)
    if occupied:
        info("已占后缀   %s（共 %d 台）" % (", ".join(occupied), len(machines)))
    else:
        info("已占后缀   无（你是第一台）")
    if suffix is None:
        warn("后缀已用尽（超过 %d 台机器），无法再分配。" % len(_SUFFIXES))
    elif is_new:
        ok("你是第 %d 台，将分配后缀 %s" % (len(machines) + 1, suffix))
        info("默认盘名   sync-all-%s" % suffix)
    else:
        ok("你已分配后缀 %s（第 %d 台）" % (suffix, _SUFFIXES.index(suffix) + 1))
        info("默认盘名   sync-all-%s" % suffix)
    out()
    info("确认编号后，跑 wbsync newdisk --apply 或 wbsync workspace init --apply 正式建盘。")
    return 0


def cmd_registry(args):
    """输出「登记本机」所需的完整信息（含自动算好的编号 suffix），供 AI 写入资料库登记表。

    登记即命名：已登记 N 台 → 本机 = 第 N+1 台 = 对应字母（A/B/C/D…）。
    与 probe 的区别：probe 基于本地协同登记表；本命令输出的是「要写到资料库登记表的那一份」，
    suffix 由调用方传入（--suffix）或按 occupied 自动推算，二者都是登记时一次定好。
    """
    st = Syncthing()
    my_id = _my_device_id()
    if not my_id:
        die("拿不到本机设备 ID。先 wbsync start 再试。")

    # 登记编号：优先用调用方显式传入的 --suffix，否则按已占用自动推算
    occupied = sorted((args.occupied or "").upper().replace(" ", "").split(",")) if getattr(args, "occupied", None) else []
    occupied = [c for c in occupied if c]
    if getattr(args, "suffix", None):
        suffix = args.suffix.strip().upper()
    else:
        suffix = None
        for ch in _SUFFIXES:
            if ch not in occupied:
                suffix = ch
                break

    # 采集本机网络信息（ipconfig/ifconfig），一并写入登记表，方便后续机器查询与排查
    netinfo = _collect_netinfo()

    payload = {
        "deviceID": my_id,
        "hostname": HOSTNAME,
        "identityKey": wb_identity_key(),
        "suffix": suffix,
        "occupied": occupied,
        "registeredAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lanIP": netinfo.get("lanIP", ""),
        "gateway": netinfo.get("gateway", ""),
        "netmask": netinfo.get("netmask", ""),
        "ifaceType": netinfo.get("ifaceType", ""),
        "hasGlobalIPv6": netinfo.get("hasGlobalIPv6", False),
        "netinfoCommand": netinfo.get("command", ""),
        "netinfoRaw": netinfo.get("raw", ""),
    }
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    head("登记本机信息（登记即命名）")
    info("设备 ID   %s" % my_id)
    info("主机名     %s" % HOSTNAME)
    info("账号身份   %s" % (payload["identityKey"] or "（未识别）"))
    info("局域网 IP  %s" % (payload["lanIP"] or "（未识别）"))
    info("网关       %s" % (payload["gateway"] or "（未识别）"))
    info("网卡类型   %s" % (payload["ifaceType"] or "（未识别）"))
    info("全局 IPv6  %s" % ("有" if payload["hasGlobalIPv6"] else "无"))
    if suffix:
        ok("登记编号   %s（sync-all-%s）" % (suffix, suffix))
    else:
        warn("后缀用尽，无法分配编号")
    out()
    info("写入登记表时，请把下面「网络信息原文」一并写入，方便后续机器查询排查：")
    info("（也可用 wbsync netinfo 单独查看）")
    return 0


# 打包时必须排除的模式（分发到新电脑不能带：设备残留、编译缓存、本机元信息）
PACK_EXCLUDE_GLOBS = (
    "*sync-conflict*",       # Syncthing 冲突副本（含 . 开头，见下）
    ".*sync-conflict*",
    "*wbsync-bak-*",
    ".*wbsync-bak-*",
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    ".git",
    "_user_meta.json",       # 本机安装元信息（installedAt）——新机导入时会自建
)


def _pack_excluded(rel_path):
    """判断相对路径是否应在打包时排除（目录段 + 文件名两处都判，涵盖点开头的文件）。"""
    rp = Path(rel_path)
    for part in rp.parts:
        if part == "__pycache__" or part == ".git":
            return True
    name = rp.name
    return any(fnmatch.fnmatch(name, g) for g in PACK_EXCLUDE_GLOBS)


def _zip_skill(skill_dir, out_path, slim=False):
    """把 skill 目录打成干净 zip（排除残留 / 缓存 / 本机元信息）。
    slim=True 时额外排除 `vendor/`（内置的 Syncthing 离线二进制，约 34MB）——
    用于「上传有体积上限」的分发场景；二进制由 `wbsync install` 自动下载，离线才需要 vendor。
    返回 (文件数, 字节数, sha256)。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(skill_dir.rglob("*"), key=lambda x: str(x)):
            if not p.is_file():
                continue
            rel = p.relative_to(skill_dir)
            if _pack_excluded(rel):
                continue
            if slim and "vendor" in rel.parts:
                continue
            z.write(str(p), str(Path(skill_dir.name) / rel))
            n += 1
    h = hashlib.sha256()
    with open(str(out), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return n, out.stat().st_size, h.hexdigest()


# 设备残留的文件名模式（冲突副本 / wbsync 备份）
RESIDUE_PATTERNS = (
    "*.sync-conflict-*",      # Syncthing 冲突副本（文件名带设备 ID 片段）
    ".*.sync-conflict-*",     # 点开头的冲突副本（pathlib 的 * 与 shell 不同，显式补一条更稳）
    # 注：*.wbsync-bak-*（wbsync 改文件前的本机备份）不在此列——它们可能是用户想留的备份，
    # 且打包时 _do_zip_package 已显式排除出 zip，无需在 package 清理阶段删除（B 机复核 ⑤）。
)


def _scan_residue(skill_dir, scope="skill", include_versions=False):
    """扫描设备残留文件。

    scope="skill" ：只扫本 skill 目录（默认，安全）。
    scope="skills"：扫**整个 skills 夹**（skill_dir 的父目录），覆盖其它 skill 目录里
                    由三机互写产生的历史残留（如 `_skillhub_meta.sync-conflict-*`）——
                    C-g2 2026-10-15 反馈：本机 skills 夹内约 20 个残留，`package` 管不到。
    include_versions=False：默认**跳过 `.stversions/`**（那是 guard 版本控制的恢复仓库，
                    清掉等于放弃可恢复性），需要时显式 `--include-versions`。
    """
    if scope == "skills":
        base = skill_dir.parent
    else:
        base = skill_dir
    out_paths = []
    for pat in RESIDUE_PATTERNS:
        for p in base.rglob(pat):
            if not p.is_file():
                continue
            if not include_versions and ".stversions" in p.parts:
                continue
            out_paths.append(p)
    return sorted(set(out_paths), key=lambda x: str(x))


def cmd_package(args):
    """打包/分发前清理：扫掉 skill 目录里的设备残留（sync-conflict 文件、wbsync 备份、
    安装元信息的使用痕迹），保证把 skill 拷贝或同步到新电脑时，不带旧机器的设备 ID / 使用记录。

    `--zip <路径>`：清理后直接打一个**干净**的 zip（排除残留 / `__pycache__` / `.DS_Store` /
    本机 `_user_meta.json`），并打印文件数、大小、SHA256。**存在残留且未 --apply 时拒绝打包**
    —— 防止把冲突副本打进分发包（2026-10-14 实测踩过：v2.8.0 包误带 3 个冲突副本）。"""
    skill_dir = Path(__file__).resolve().parent.parent
    zip_out = getattr(args, "zip", None)
    scope = (getattr(args, "scope", None) or "skill").lower()
    include_versions = bool(getattr(args, "include_versions", False))
    slim = bool(getattr(args, "slim", False))

    targets = _scan_residue(skill_dir, scope=scope, include_versions=include_versions)

    # _user_meta.json 里若有 installedAt（本机安装时间戳）等使用痕迹，属于「本机残留」，
    # 打包分发到新电脑时应清掉，避免把上一台机器的安装记录带过去。
    meta_cleanup = None
    meta_path = skill_dir / "_user_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and "installedAt" in meta:
                meta_cleanup = meta_path
        except Exception:
            pass

    if not targets and meta_cleanup is None:
        ok("未发现需要清理的设备残留。skill 目录已干净。")
        if zip_out:
            return _do_zip_package(skill_dir, zip_out, slim=slim)
        return 0

    # 只有「文件级残留」（sync-conflict / wbsync-bak 等）才拦打包 —— 它们会被排除，
    # 但存在即说明目录不干净，属于「先清理再分发」的纪律。_user_meta.json 天然不入包，
    # 不阻塞（仅提示）。
    if zip_out and not targets:
        info("（本机 _user_meta.json 含 installedAt —— 打包时会自动排除，不影响分发）")
        return _do_zip_package(skill_dir, zip_out, slim=slim)

    if targets:
        base = skill_dir.parent if scope == "skills" else skill_dir
        section("将清理的设备残留（%d 个%s）" % (len(targets), "，扫全 skills 夹" if scope == "skills" else ""))
        for p in targets:
            try:
                info("  %s" % p.relative_to(base))
            except ValueError:
                info("  %s" % p)
    if meta_cleanup is not None:
        info("  清理 _user_meta.json 里的 installedAt 使用痕迹")

    if not args.apply:
        out()
        info("演练模式：未删除。加 %s--apply%s 真正删除。" % (C.B, C.R))
        if zip_out and targets:
            warn("检测到 %d 个文件级残留 —— **拒绝打包**（否则会把冲突副本打进分发包）。先 --apply 清理再打包。" % len(targets))
        return 0

    # 删除走**系统回收站**（可恢复）；不可用时降级到本机暂存目录
    fallback_dir = Path.home() / (".wbsync-package-trash-%s" % time.strftime("%Y%m%d-%H%M%S"))
    okl, faill = _trash_paths(targets, fallback_dir=fallback_dir)
    removed = len(okl)
    for p, e in faill:
        bad("删除失败 %s：%s" % (p, e))
    if okl:
        info("（已移入回收站，可恢复）")

    if meta_cleanup is not None:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                meta.pop("installedAt", None)
                atomic_write_json(str(meta_path), meta)
                removed += 1
        except Exception as e:
            bad("清理 _user_meta.json 失败：%s" % e)

    ok("已清理 %d 处残留。现在可以安全地把 skill 打包/同步到新电脑。" % removed)
    if zip_out:
        return _do_zip_package(skill_dir, zip_out, slim=slim)
    return 0


# 机器私有元数据：**不该跨机同步**（含本机绝对路径 / 本机生成），跨机必互写 → 冲突副本刷屏。
# 典型：_skillhub_meta.json 里 iconLocalPath = C:\Users\chenz\... (本机路径)、installedAt（本机时间）。
# 这与本 skill 的「登记层不能跨机复制」同源，所以用 Syncthing 忽略规则从根上不传。
MACHINE_LOCAL_IGNORE = (
    "_skillhub_meta.json",
    ".DS_Store",
    "__pycache__",
    "*.pyc",
)


def _folder_id_for_path(st, path):
    """按本地路径找同步夹 id（找不到返回 None）。"""
    try:
        target = Path(path).resolve()
    except Exception:
        return None
    for fid, f in (st.folders() or {}).items():
        try:
            if Path(f.get("path") or "").resolve() == target:
                return fid
        except Exception:
            continue
    return None


def _get_ignores(st, fid):
    """读某夹的忽略模式。字段名是 `ignore`（不是 patterns）——传错字段会静默无效。"""
    d = st.api("GET", "/rest/db/ignores?folder=%s" % fid) or {}
    if not isinstance(d, dict):
        return []
    return list(d.get("ignore") or d.get("patterns") or [])


def _set_ignores(st, fid, patterns):
    """写忽略模式：**夹 ID 走 query、模式走 body 的 `ignore` 字段**（实测唯一有效组合；
    body 用 `patterns` 会 200 但静默不生效，body 带 folder 会 500）。"""
    st.api("POST", "/rest/db/ignores?folder=%s" % fid, {"ignore": list(patterns)})


def cmd_ignore(args):
    """管理同步夹的 Syncthing 忽略规则：把**机器私有元数据**排除出同步。

    为什么：`_skillhub_meta.json`（含本机 `iconLocalPath` 绝对路径 + `installedAt`）、
    `.DS_Store`、`__pycache__`/`*.pyc` 属「本机生成、本机相关」——三机同写同同步必然
    互相覆盖、刷出 `*.sync-conflict-*` 副本（2026-10-15 C-g2 实测 skills 夹约 20 个）。
    忽略它们是**根治**：不传就不会冲突（清理残留只是治标）。

    默认作用于 `~/.workbuddy/skills` 所在的同步夹（wb-skills）；`--folder` 指定单个夹、
    `--all-folders` 作用于所有夹。默认演练，`--apply` 才落盘。`.stignore` 是**每台机本地**文件，
    不随同步走 → 三机各跑一次。
    """
    action = (getattr(args, "action", None) or "status").lower()
    apply_flag = bool(getattr(args, "apply", False))
    st = _st_client()
    if not (st and st.available):
        die("Syncthing 没有在运行（先 wbsync start）", 1)

    skill_dir = Path(__file__).resolve().parent.parent
    folders = st.folders() or {}
    if getattr(args, "all_folders", False):
        targets = sorted(folders.keys())
    elif getattr(args, "folder", None):
        targets = [args.folder]
    else:
        fid = _folder_id_for_path(st, skill_dir.parent)
        if not fid:
            die("找不到 skills 目录对应的同步夹（可加 --folder <夹ID> 指定；用 wbsync folders 看清单）", 1)
        targets = [fid]

    want = set(MACHINE_LOCAL_IGNORE)

    if action == "status":
        head("同步夹忽略规则（机器私有元数据）")
        for fid in targets:
            cur = _get_ignores(st, fid)
            have = [p for p in cur if p in want]
            info("%s（%s）" % (fid, (folders.get(fid) or {}).get("path", "?")))
            if have:
                ok("  已忽略：%s" % "、".join(have))
            else:
                warn("  未忽略（三机互写会刷冲突副本）")
            missing = sorted(want - set(cur))
            if missing:
                info("  待加：%s" % "、".join(missing))
        out()
        info("启用：wbsync ignore on --apply ；取消：wbsync ignore off --apply")
        return 0

    if action not in ("on", "off"):
        die("未知动作：%s（应为 on / off / status）" % action, 1)

    changed_any = False
    for fid in targets:
        cur = _get_ignores(st, fid)
        if action == "on":
            new = list(cur) + [p for p in MACHINE_LOCAL_IGNORE if p not in cur]
        else:
            new = [p for p in cur if p not in want]
        if new == cur:
            ok("%s：%s" % (fid, "已是目标状态（无需改动）" if action == "on" else "不含本规则的忽略项"))
            continue
        changed_any = True
        head("%s：%s" % ("加入忽略" if action == "on" else "移除忽略", fid))
        for p in sorted(set(new) - set(cur)):
            info("  + %s" % p)
        for p in sorted(set(cur) - set(new)):
            info("  - %s" % p)
        if apply_flag:
            _set_ignores(st, fid, new)
            ok("  已写入（Syncthing 立即生效；已存在的冲突副本不会自动消失，用 package --scope skills --apply 清）")
    out()
    if not changed_any:
        return 0
    if not apply_flag:
        info("演练模式：未写入。加 --apply 真正生效（.stignore 每台机本地，三机各跑一次）。")
    else:
        info("提示：`_skillhub_meta.json` 等被忽略后，各机保留自己那份、不再互写 → 冲突副本不再新增。")
    return 0


def _do_zip_package(skill_dir, zip_out, slim=False):
    """清理后打干净包，并打印文件数 / 大小 / SHA256。slim=True 额外排除 vendor/。"""
    # 清理后复查：只对「设备残留」（sync-conflict / wbsync-bak）报警——它们是目录不干净的信号
    _res_pats = ("*.sync-conflict-*", ".*.sync-conflict-*", "*.wbsync-bak-*", ".*.wbsync-bak-*")
    residue = []
    for pat in _res_pats:
        for p in skill_dir.rglob(pat):
            if p.is_file():
                residue.append(p)
    if residue:
        warn("打包前复查仍有 %d 个设备残留，已排除出包（但建议先跑 package --apply 清理）：" % len(residue))
        for p in residue:
            info("  %s" % p.relative_to(skill_dir))

    try:
        n, size, sha = _zip_skill(skill_dir, zip_out, slim=slim)
    except Exception as e:
        bad("打包失败：%s" % e)
        return 1
    out()
    ok("已打包（%s）：%s" % ("精简·不含 vendor" if slim else "干净", zip_out))
    info("  文件数 %d ｜ 大小 %.1f MB ｜ SHA256 %s" % (n, size / 1048576.0, sha))
    if slim:
        info("  已排除：vendor/（Syncthing 离线二进制）+ sync-conflict / 备份 / __pycache__ / *.pyc / .DS_Store / .git / _user_meta.json")
        info("  提示：目标机首次安装用 `wbsync install` 自动下载 Syncthing；已装好的机器直接用本包覆盖代码即可。")
    else:
        info("  已排除：sync-conflict 副本 / wbsync 备份 / __pycache__ / *.pyc / .DS_Store / .git / _user_meta.json")
    return 0


def cmd_setup(args):
    head("建立同步夹")
    st = Syncthing()
    if not st.available:
        die("Syncthing 没有在运行，先跑 wbsync start（或 wbsync install 再 start）")

    need = ensure_folders(args.apply, st)
    section("目录")
    for fid, label, p, exists in folder_table():
        if exists:
            ok("%s %s" % (wpad(label, 16), p))
        elif args.apply:
            try:
                p.mkdir(parents=True, exist_ok=True)
                ok("%s %s  已建立" % (wpad(label, 16), p))
            except Exception as e:
                bad("%s建立失败：%s" % (wpad(label, 16), e))
        else:
            warn("%s %s  待建立" % (wpad(label, 16), p))

    section("注册进 Syncthing")
    for fid, label, p, exists in folder_table():
        if not args.apply:
            info("演练：将注册 %s → %s" % (fid, p))
            continue
        if not p.exists():
            warn("跳过 %s（目录不存在）" % fid)
            continue
        try:
            act = st.ensure_folder(fid, label, p)
            mapping = {"created": "新建", "updated": "更新", "unchanged": "无需改动"}
            ok("%-16s %s" % (fid, mapping.get(act, act)))
        except Exception as e:
            bad("%-16s 失败：%s" % (fid, e))

    if not args.apply:
        out()
        info("演练结束。加 %s--apply%s 真正执行。" % (C.B, C.R))
    else:
        # 自动建立机器间沟通记事本（各机 Agent 跨机留言的固定通道）
        nb, created = ensure_comms_notebook(True)
        if created:
            ok("已建立机器间沟通记事本 %s" % nb.name)
        out()
        ok("完成。接下来跑 wbsync id 取设备 ID，去对端配对。")
    return 0


def cmd_id(args):
    head("本机设备 ID")
    st = Syncthing()
    myid = None
    if st.available:
        myid = st.my_id()
    if not myid:
        myid = read_my_id_from_config(find_config_xml())
    if not myid:
        die("取不到设备 ID。确认 Syncthing 已启动过至少一次。")
    out()
    out("  %s%s%s" % (C.B, myid, C.R))
    out()
    info("在另一台机器上执行：")
    out("      wbsync pair %s" % myid)
    info("或使用信箱跨网交换（两台机不在同一局域网时）：")
    out("      本机 wbsync mailbox put")
    out("      对端 wbsync mailbox get --apply")
    return 0


def normalize_device_id(s):
    s = (s or "").strip().upper().replace(" ", "")
    if not re.match(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$", s):
        return None
    return s


def is_valid_device_id(s):
    return normalize_device_id(s) is not None


def _my_device_id():
    st = Syncthing()
    mid = st.my_id() if st.available else None
    if not mid:
        mid = read_my_id_from_config(find_config_xml())
    return mid


# --------------------------------------------------------------------------
# 设备 ID 信箱（跨互联网配对，免手抄 ID）
# --------------------------------------------------------------------------

def cmd_mailbox(args):
    head("设备 ID 信箱")
    mailbox = Path(_mailbox_dir())
    me = _my_device_id()
    if not me:
        die("取不到本机设备 ID（Syncthing 未初始化？）")
    my_ik = wb_identity_key()
    if args.sub == "put":
        f = mailbox / (HOSTNAME + ".txt")
        content = json.dumps({
            "deviceID": me,
            "hostname": HOSTNAME,
            "identityKey": my_ik,
            "time": int(time.time()),
        }, ensure_ascii=False, indent=2)
        if not args.apply:
            info("（演练）将写入 %s" % f)
            info("  内容：%s" % content.replace("\n", " "))
            info("确认后加 --apply")
            return 0
        mailbox.mkdir(parents=True, exist_ok=True)
        backup_file(f)
        f.write_text(content, encoding="utf-8")
        ok("已把本机 ID 写入信箱：%s" % f)
        info("账号身份：%s" % (my_ik or "（未识别到 WorkBuddy 账号）"))
        info("对端机器收到后跑 `wbsync mailbox get --apply` 即自动配对。")
    elif args.sub == "get":
        if not mailbox.exists():
            info("信箱目录不存在（对端还没放 ID）：%s" % mailbox)
            return 0
        same = []       # 同账号（自己人）：自动配对
        foreign = []    # 不同账号（可能是别人）：标红提示，不自动连
        for f in mailbox.glob("*.txt"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            peer = d.get("deviceID")
            if not (peer and is_valid_device_id(peer) and peer != me):
                continue
            rec = (peer, d.get("hostname", "?"), d.get("identityKey", ""))
            if my_ik and rec[2] and rec[2] == my_ik:
                same.append(rec)
            else:
                foreign.append(rec)
        if not same and not foreign:
            info("信箱里没有新的对端 ID（已配对或尚未写入）")
            return 0

        st = Syncthing()
        if not st.available:
            die("Syncthing 未运行，先 wbsync start")

        # 1) 同账号：自动配对
        for peer, name, ik in same:
            info("发现同账号设备：%s（%s）" % (peer, name))
            if not args.apply:
                info("（演练）将配对并共享四个夹")
                continue
            st.add_device(peer, name)
            for fid, _label, _getter in FOLDER_SPECS:
                st.share_folder(fid, peer)
            ok("已配对并共享四夹：%s" % peer)

        # 2) 不同账号：不自动连，明确提示
        for peer, name, ik in foreign:
            warn("发现【非本账号】设备：%s（%s）" % (peer, name))
            info("  对方账号 identityKey=%s（与本机 %s 不一致）" % (ik or "未知", my_ik or "未知"))
            info("  已跳过，不会自动配对。若确认要连，用 `wbsync pair %s --apply` 手动连接。" % peer)

        if not args.apply:
            info("确认后加 --apply")
    else:
        die("mailbox 需要 put 或 get")
    return 0


# --------------------------------------------------------------------------
# 同步盘 / 工作空间（统一：A/B/C/D 后缀，位置首次询问后记住）
# --------------------------------------------------------------------------

def _disk_root():
    """同步盘根目录。优先环境变量 WBSYNC_DISK_ROOT，其次 cfg 里的 disk_root，
    最后 cfg_sync_root()。首次建盘由对话引导询问一次，写入 disk_root 记住。"""
    env = os.environ.get("WBSYNC_DISK_ROOT")
    if env:
        return Path(env).expanduser()
    c = load_config(quiet=True)
    dr = (c or {}).get("disk_root")
    if dr:
        return Path(dr).expanduser()
    return cfg_sync_root()


def _remember_disk_root(path, apply_flag):
    """把用户选定的盘根目录写进 config 的 disk_root，之后复用不再问。"""
    c = load_config(quiet=True)
    c["disk_root"] = str(Path(path).expanduser())
    save_config(c, apply_flag)


def cmd_newdisk(args):
    """新建同步盘：问一次根目录并记住 → 按 A/B/C/D 后缀分配 → 建目录 → 注册同步夹 → 共享 → 写登记表。

    与 workspace 共用同一套后缀与登记表（工作空间登记.json），
    命名规则统一为 A/B/C/D，位置统一走 disk_root。
    """
    st = Syncthing()
    if not st.available:
        die("Syncthing 未运行，先 wbsync start")

    my_id = _my_device_id()
    if not my_id:
        die("拿不到本机设备 ID。先 wbsync start 再试。")

    # 位置：--location 或环境变量优先；都没有且首次（未记住）则落到默认并提示由对话引导询问
    if getattr(args, "location", None):
        root = Path(args.location).expanduser()
        _remember_disk_root(root, args.apply)
        info("同步盘根目录设为：%s" % root)
    else:
        root = _disk_root()
        info("同步盘根目录：%s" % root)

    reg = _read_ws_registry()
    suffix, is_new = _resolve_my_suffix(reg, my_id)
    if suffix is None:
        die("后缀用尽（超过 %d 台机器）" % len(_SUFFIXES))
    if is_new:
        info("探测：你是第 %d 台，将分配后缀 %s" % (len(reg.get("machines", {})) + 1, suffix))
    else:
        info("探测：你已分配后缀 %s" % suffix)

    disk_path = root / ("sync-all-%s" % suffix)
    folder_id = "workspace-%s" % suffix
    label = ("同步盘 " + suffix) if not getattr(args, "name", None) else args.name.strip()

    info("默认盘名：sync-all-%s" % suffix)
    info("本地文件夹：%s" % disk_path)
    info("同步 ID：%s" % folder_id)

    if not args.apply:
        info("（演练）未创建。加 --apply 后：建目录 → 登记同步夹 → 共享对端 → 写登记表。")
        return 0

    # 1) 建目录（与 workspace 相同的 shared/private 结构）
    for sub in ("", "private", "shared"):
        d = disk_path if not sub else disk_path / sub
        d.mkdir(parents=True, exist_ok=True)

    # 2) 注册 Syncthing 同步夹（shared 跨机同步，private 私有）
    shared_dir = disk_path / "shared"
    st.ensure_folder(folder_id, label, shared_dir)

    # 3) 共享给所有对端
    shared_peers = []
    for dev_id in st.devices():
        if dev_id == my_id:
            continue
        ok_flag, msg = st.share_folder(folder_id, dev_id)
        if ok_flag:
            shared_peers.append(dev_id[:7])
        elif msg != "已共享":
            info("  共享给 %s：%s" % (dev_id[:7], msg))

    # 4) 写登记表（复用工作空间登记表）
    machines = dict(reg.get("machines", {}))
    machines[suffix] = {
        "deviceID": my_id,
        "hostname": HOSTNAME,
        "generation": _ws_next_generation(reg, suffix),
        "created": int(time.time()),
        "grants": machines.get(suffix, {}).get("grants", []),
    }
    reg["machines"] = machines
    # 本机路径只记在自己条目下：顶层 `root` 是「只写不读」的死字段，且各机 init 时
    # 都写自己的绝对路径 → 互相覆盖、制造无谓的跨机差异（C-g2 2026-10-16 报告）。
    machines[suffix]["root_local"] = str(root)
    # 写入前：与磁盘最新一份合并 + 守住 retired + 尝试跨机预约（三重防线，见 _ws_registry_write_guarded）
    _ws_registry_write_guarded(reg, suffix)
    _record_change(reg, suffix, "init", "建立同步盘 sync-all-%s" % suffix, my_id, True)

    ok("同步盘已创建：%s" % disk_path)
    info("私有目录：%s（只有本机写）" % (disk_path / "private"))
    info("共享目录：%s（所有机器可读写）" % shared_dir)
    if shared_peers:
        info("已共享给对端设备：%s" % ", ".join(shared_peers))
    return 0


def cmd_listdisks(args):
    head("已登记的同步盘")
    st = Syncthing()
    my_id = _my_device_id()
    reg = _read_ws_registry()
    return _ws_list(reg, st, my_id)


# --------------------------------------------------------------------------
# 工作空间（多机协作：A/B/C/D 各自工作空间 + shared 共享区 + 变更日志）
# --------------------------------------------------------------------------

_SUFFIXES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ws_marker_name(suffix=None):
    """识别「这是 wbsync 自己建的工作空间」的标记文件名。

    **按后缀区分**（`.wbsync-workspace-<后缀>`）：标记文件在工作空间根、跨机同步，
    三机同写同一路径必然冲突（A 写 suffix=A、C 写 suffix=C）——2026-10-14 C-g2 指出
    并建议按后缀或落私有区，故各机各写自己的后缀。
    """
    return ".wbsync-workspace" + (("-%s" % suffix) if suffix else "")


def _ws_marker_path(ws_dir, suffix=None):
    return Path(ws_dir) / _ws_marker_name(suffix)


def _ws_marker_payload(suffix):
    return json.dumps({"kind": "wbsync-workspace", "suffix": suffix,
                       "created": time.strftime("%Y-%m-%d %H:%M:%S")},
                      ensure_ascii=False, indent=1)


def _is_wbsync_workspace(ws_dir):
    """判断某目录是否是 wbsync 自己建的工作空间（存在任意 `.wbsync-workspace*` 标记即可）。"""
    try:
        return any(p.name.startswith(".wbsync-workspace") for p in Path(ws_dir).iterdir())
    except Exception:
        return False


def _ws_root():
    """工作空间根目录。优先环境变量 WBSYNC_WS_ROOT，其次 cfg 里的 workspace_root，最后 ~/WorkBuddy。

    默认放到 ~/WorkBuddy 下，因为 WorkBuddy 客户端的下拉菜单/工作空间选择器
    只扫描 ~/WorkBuddy 目录里的文件夹，放到这里新建任务时才能直接看到。
    """
    env = os.environ.get("WBSYNC_WS_ROOT")
    if env:
        return Path(env).expanduser()
    cfg = load_config(quiet=True)
    wr = (cfg or {}).get("workspace_root")
    if wr:
        return Path(wr).expanduser()
    return DEFAULT_WORKBUDDY_ROOT


def _ws_registry_path():
    """工作空间注册表：记录每台机器（deviceID）分配到哪个后缀，以及 shared 区信息。放 shared 区，随 Syncthing 同步。"""
    return coop_shared() / "工作空间登记.json"


def _ws_changelog_path():
    return coop_shared() / "工作空间变更.json"


def _ws_suffix_of_device(registry, device_id):
    """按 deviceID 反查本机后缀。

    先查现役 machines；未命中再回退查 retired[]（取 registeredAt 最近的一代）。
    回退是修「先 prune 后退」死锁的关键：机器被移入 retired 后仍能解析出自己的
    后缀，从而能跑 uninstall / prune / 正确落盘 tasks-<后缀> 与专线文件，而不是
    降级成 '?'。retired 回退命中的是「已退役」机器，调用方如需区分可自行判断
    该后缀当前是否还在 machines 里。
    """
    reg = registry or {}
    for suffix, e in reg.get("machines", {}).items():
        if e.get("deviceID") == device_id:
            return suffix
    # 回退：在 retired[] 里按 deviceID 找最近登记的一代
    best = None
    best_at = -1
    for e in reg.get("retired", []):
        if e.get("deviceID") == device_id:
            at = e.get("registeredAt", 0) or 0
            if at >= best_at:
                best, best_at = e.get("suffix"), at
    return best


def _ws_next_suffix(registry):
    machines = (registry or {}).get("machines", {})
    used = set(machines.keys())
    for ch in _SUFFIXES:
        if ch not in used:
            return ch
    return None


def _resolve_my_suffix(reg, my_id):
    """探测本机该分配的后缀。返回 (suffix, is_new)。

    - 若本机已在登记表里（按 deviceID 认）→ 返回已有后缀，is_new=False；
    - 否则按加入顺序取下一个空闲后缀（A/B/C/D…）→ is_new=True；
    - 后缀用尽返回 (None, False)。
    纯只读，不写盘。
    """
    existing = _ws_suffix_of_device(reg, my_id)
    if existing:
        return existing, False
    nxt = _ws_next_suffix(reg)
    return nxt, True


def _ws_next_generation(registry, suffix):
    """某槽位下一个世代号 = 该槽位历史最大世代 + 1。
    世代用于「序号复用」时区分继任者与前任（C 退出 → 新机仍叫 C，但世代=2）。"""
    gens = [e.get("generation", 1) for e in (registry or {}).get("retired", [])
            if e.get("suffix") == suffix]
    cur = (registry or {}).get("machines", {}).get(suffix)
    if cur:
        gens.append(cur.get("generation", 1))
    return (max(gens) if gens else 0) + 1


def _ws_registry_keep_retired(reg):
    """写入登记表前守住 `retired`（退役历史）——世代机制（`_ws_next_generation`）与
    prune（按 retired 反查 deviceID）都依赖它。

    ⚠️ 历史事故（2026-10-16 · D 机首次 init）：新机本地还没有 coop（从未同步过）、
    资料库登记 doc 也未带 retired，于是 `init` 直接写出一份**只有 machines** 的登记表；
    Syncthing 以「文件较新者胜」把它覆盖到 A/B/C → **C-g1 的退役记录整体丢失**
    （世代号可能回退、prune 反查不到 deviceID）。这类「新机一写就把共享状态写瘦」的
    破坏是**静默**的，故此处做三道兜底，**绝不写回比现有更少的历史**：
      ⓪ union **磁盘上当前那份**登记表的 retired（**最要紧**，见下）；
      ① 键缺失 → 至少补空表（保住键，避免后续 merge 逻辑整段跳过）；
      ② union 资料库登记里的 retired（**best-effort**：当前 `_read_library_registry()` 只返回
         `machines`，此路通常为空——**不再假装它是一道真防线**；留着是为资料库将来带上 retired）；
      ③ union 本地 `.stversions` 快照里的 retired（事故恢复源；但它**只存已被替换掉的旧版本**，
         兜不住「磁盘当前那份里的新退役记录」——那正是 ⓪ 要解决的）。
    """
    ret = list(reg.get("retired") or [])
    seen = {(x.get("suffix"), x.get("deviceID"), x.get("retiredAt")) for x in ret}

    def _add(items):
        for e in (items or []):
            k = (e.get("suffix"), e.get("deviceID"), e.get("retiredAt"))
            if k not in seen:
                ret.append(e)
                seen.add(k)

    # ⓪ 磁盘当前那份：内存里的 reg 可能是「别人写之前」的旧底稿，磁盘文件才是最新事实。
    #    缺这条有个真窗口（D 机 2026-10-16 审查 A1）：A 刚 clear/prune 退役某槽位并落盘 →
    #    D 拿旧底稿走 init 写回 → 那条退役记录被抹掉。`.stversions` 兜不住（它只存被替换过的版本）。
    try:
        _add(json.loads(Path(_ws_registry_path()).read_text(encoding="utf-8")).get("retired"))
    except Exception:
        pass
    try:                                              # ② 资料库登记（best-effort，见 docstring）
        _add(_read_library_registry().get("retired"))
    except Exception:
        pass
    try:                                              # ③ 版本归档（Syncthing trashcan）
        for p in sorted((cfg_sync_root() / "coop" / ".stversions").rglob("*登记*")):
            if not p.is_file():
                continue
            try:
                _add(json.loads(p.read_text(encoding="utf-8")).get("retired"))
            except Exception:
                continue
    except Exception:
        pass
    reg["retired"] = ret
    return ret


def _ws_registry_merge_fresh(reg):
    """把 `reg` 与**磁盘上最新一份**登记表合并（就地改 `reg`）——防「用陈旧底稿整份覆盖」。

    事故史（2026-10-16）：D 机首次 init 与我方 init 同时写登记表 →
    ① 一份把另一份的 `retired` 写丢；② Syncthing 判冲突，把文件归档、原位删除，
    本机当场认不出自己是哪台（显示「? 机」）。根因是**整份覆盖式 read-modify-write**。

    规则（D 机 2026-10-16 审查 A2 建议的**通用形态**，不再逐字段特判）：
      - **顶层键**：磁盘有而内存缺 → 直接补齐（避免「将来加新顶层键又重演静默丢数据」，
        顶层 `root` 已经踩过一次）；
      - `machines`：逐槽位按 `(generation, created)` 取更晚一份为主，再用另一份补齐缺失字段；
      - 其它**列表类**顶层键（如 `retired`）：**并集**，绝不覆盖（列表天生可累加）。
    """
    try:
        cur = json.loads(Path(_ws_registry_path()).read_text(encoding="utf-8"))
    except Exception:
        return reg
    if not isinstance(cur, dict):
        return reg
    # ① 顶层键补齐（内存缺失的从磁盘那份拿）
    for k, v in cur.items():
        if k not in reg:
            reg[k] = v
    # ② machines 逐槽位合并
    merged = dict(cur.get("machines") or {})
    for sfx, ent in (reg.get("machines") or {}).items():
        old = merged.get(sfx)
        if not isinstance(old, dict):
            merged[sfx] = ent
            continue
        base, other = (ent, old) if _ws_entry_key(ent) >= _ws_entry_key(old) else (old, ent)
        out = dict(base)
        for k, v in (other or {}).items():
            out.setdefault(k, v)
        merged[sfx] = out
    reg["machines"] = merged
    # ③ 其它列表类顶层键：并集（去重按 JSON 规范化）
    for k, v in cur.items():
        if k == "machines" or not isinstance(v, list):
            continue
        own = reg.get(k)
        if not isinstance(own, list):
            reg[k] = list(v)
            continue
        seen = {json.dumps(x, sort_keys=True, ensure_ascii=False, default=str) for x in own}
        for x in v:
            j = json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)
            if j not in seen:
                own.append(x)
                seen.add(j)
        reg[k] = own
    return reg


def _ws_entry_key(ent):
    """机器条目的新旧比较键：`generation` / `created` **先 int 归一**再比。

    原实现直接做元组比较：只要有机器写入字符串（如 `"created": "2026-10-16T…"`）就会
    `TypeError`，而调用处 `except: pass` 会把它吞掉 → 静默退回「未合并即写」（D 机审查 A3）。
    """
    def _i(v, d=0):
        try:
            return int(v)
        except Exception:
            return d
    return (_i(ent.get("generation"), 1), _i(ent.get("created"), 0))


def _ws_registry_write_guarded(reg, suffix):
    """登记表写入的**唯一推荐入口**：先合并磁盘最新 → 守 retired → 尝试跨机预约 → 写。

    三道防线（全部 fail-open，绝不阻断 init，只降低损坏概率）：
      ① `_ws_registry_merge_fresh`：与磁盘最新一份合并——**防数据丢失的主力**，
         即便预约没抢到，也不会用陈旧底稿抹掉对方条目；
      ② `_ws_registry_keep_retired`：守住退役历史（世代机制 / prune 依赖）；
      ③ 跨机预约（`reserve`，短 TTL）：降低「多机同时写」的窗口；抢不到就带警告继续。
    """
    try:
        _ws_registry_merge_fresh(reg)
    except Exception as e:
        # 不能静默吞：合并失败＝退回「未合并即写」这个**危险行为**，必须让用户看见（D 审查 A3）
        warn("登记表合并失败（%s）；本次**未**与磁盘最新一份合并，存在覆盖风险，写入后请核对" % e)
    try:
        _ws_registry_keep_retired(reg)
    except Exception as e:
        warn("retired 兜底失败（%s）；本次写入可能少退役历史，请核对登记表" % e)
    rp = str(_ws_registry_path())
    held = False
    try:
        got, msg = _reservation_claim(rp, reason="写登记表（%s）" % suffix, ttl=3)
        held = bool(got)
        if not got:
            warn("登记表被其它机器预约中（%s）；仍继续写，但已与磁盘最新一份合并过——"
                 "若随后出现 sync-conflict 副本，请用 `wbsync package --apply` 清理" % msg)
    except Exception:
        held = False
    try:
        return _write_ws_registry(reg)
    finally:
        if held:
            try:
                _reservation_release(rp)
            except Exception:
                pass


def _ws_my_generation(reg=None, my=None):
    """本机当前世代号。"""
    try:
        reg = reg if reg is not None else _read_ws_registry()
        my = my or _my_suffix_short()
        e = (reg or {}).get("machines", {}).get(my) or {}
        return e.get("generation", 1)
    except Exception:
        return 1


def _ws_retire(reg, suffix):
    """把某槽位当前使用者从 machines 移入 retired（记世代/deviceID/时间）。
    返回退役记录 dict 或 None（该槽位无现役记录时）。就地改 reg，不落盘。"""
    me = (reg.get("machines") or {}).pop(suffix, None)
    if not me:
        return None
    rec = {
        "suffix": suffix,
        "generation": me.get("generation", 1),
        "deviceID": me.get("deviceID", ""),
        "hostname": me.get("hostname", ""),
        "registeredAt": me.get("registeredAt") or me.get("created", ""),
        "retiredAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    reg.setdefault("retired", []).append(rec)
    return rec


# --------------------------------------------------------------------------
# 资料库登记表读取（跨机器自动判断 A/B/C/D）
# --------------------------------------------------------------------------

def _call_space_api(api_name, **kwargs):
    """调用 WorkBuddy 内置资料库 API 脚本 space_api.py，返回 JSON 数据。"""
    if not LIBRARY_SPACE_API.exists():
        return None
    cmd = [str(sys.executable or "python3"), str(LIBRARY_SPACE_API), api_name]
    for k, v in kwargs.items():
        if v is None:
            continue
        flag = "--" + k.replace("_", "-")
        cmd += [flag, str(v)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        if not out:
            return None
        # space_api.py 的响应外层是 {"api":..., "data": {...}, "traceid":...}
        env = json.loads(out)
        if isinstance(env, dict) and "data" in env:
            return env["data"]
        return env
    except Exception:
        return None


def _list_user_spaces():
    """列出当前用户能访问的资料库空间，返回 [(space_id, title), ...]。"""
    data = _call_space_api("list-user-spaces")
    if not data or "spaces" not in data:
        return []
    spaces = []
    for s in data["spaces"]:
        spaces.append((s.get("spaceId"), s.get("title")))
    return spaces


def _find_registry_folder_id(space_id):
    """在指定空间找到「多机同步登记（勿删）」文件夹 id。"""
    data = _call_space_api("list-nodes", space_id=space_id)
    if not data or "nodes" not in data:
        return None
    for n in data["nodes"]:
        if n.get("kind") == "folder" and "多机同步登记" in (n.get("title") or ""):
            return n.get("id")
    return None


def _find_registry_archive_folder_id(space_id):
    """在「多机同步登记」文件夹下找「已退出机器登记」归档子文件夹（标题含"退出"的 folder）。
    找不到返回 None —— 资料库 API 只支持建**文档**、不支持建**文件夹**，故该归档文件夹
    需人工在界面建一次；建好后退出流程会自动把退役机的登记 doc 移进去（move-node）。"""
    parent = _find_registry_folder_id(space_id)
    if not parent:
        return None
    try:
        for ch in _list_child_nodes(space_id, parent):
            t = ch.get("title") or ""
            if ch.get("kind") == "folder" and "退出" in t:
                return ch.get("id")
    except Exception:
        pass
    return None


def _library_create_folder(title, space_id=None, parent_id=None):
    """在资料库建一个空文件夹，返回 nodeBlockId 或 None。

    依赖 WorkBuddy 内置 library 的 manage/create_folder.py（走 nodeKind=folder），
    因为 space_api.py 的 create-doc 只能建文档、建不了文件夹。
    stdout 成功行：KS_FOLDER_CREATE\\t<nodeBlockId>\\t<kind>\\t<url>
    """
    script = LIBRARY_SPACE_API.parent / "manage" / "create_folder.py"
    if not script.exists():
        return None
    cmd = [str(sys.executable or "python3"), str(script), "--title", title]
    if space_id:
        cmd += ["--space-id", str(space_id)]
    if parent_id:
        cmd += ["--parent-id", str(parent_id)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        for line in (proc.stdout or "").splitlines():
            if line.startswith("KS_FOLDER_CREATE\t"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].strip():
                    return parts[1].strip()
        return None
    except Exception:
        return None


def _ensure_registry_archive_folder(space_id):
    """确保「多机同步登记」下存在「已退出机器登记」归档子文件夹；没有就自动建。
    返回 folder id 或 None（找不到登记文件夹 / 建文件夹失败时）。"""
    arch = _find_registry_archive_folder_id(space_id)
    if arch:
        return arch
    parent = _find_registry_folder_id(space_id)
    if not parent:
        return None
    return _library_create_folder("已退出机器登记", space_id=space_id, parent_id=parent)


def _registry_child_rank(child):
    """「多机同步登记」文件夹内节点的期望次序（升序）：
       0 = README/说明（置顶）
       1 = 现役机登记 doc（按后缀字母 A/B/C…）
       2 = 其它 doc（无法识别的，保持相对顺序）
       3 = 文件夹（如「已退出机器登记」——归档置末）
    返回值 (rank, 次键, 原标题)。"""
    title = (child.get("title") or "").strip()
    kind = child.get("kind")
    if kind == "folder":
        return (3, "", title)
    if "README" in title.upper() or "请勿手动" in title or "说明" in title:
        return (0, "", title)
    m = re.match(r"^\s*([A-Z])\s*机?\s*设备登记", title)
    if m and "已退出" not in title and "旧" not in title:
        return (1, m.group(1), title)
    return (2, "", title)


def _registry_desired_order(children):
    """按期望次序返回节点列表（稳定排序，不增删节点）。"""
    return sorted(children, key=lambda c: (_registry_child_rank(c)[0], _registry_child_rank(c)[1]))


def _reorder_registry_folder(space_id, parent_id=None, apply=True):
    """把「多机同步登记」重排为：README → 现役机登记(按后缀) → 其它 → 归档文件夹(末)。
    只调顺序，不增删任何节点。返回 (是否发生变化, 移动次数)。

    实现要点（2026-10-14 A 机实测）：
      - move-node 的定位靠 `--after-node-id`；**必须同时给 `--target-parent-id`**，
        否则节点会被移出文件夹、掉到空间根（曾把两个登记 doc 移飞）。
      - `--after-node-id=""`（空串）= 移到**最前**；给了具体 id = 移到该节点**紧后**。
      - 逐项「移到前一项之后」即可把整串顺序摆正（首项不必动，它会被后续移动自然顶到最前）。
    """
    parent = parent_id or _find_registry_folder_id(space_id)
    if not parent:
        return (False, 0)
    try:
        children = _list_child_nodes(space_id, parent)
    except Exception:
        return (False, 0)
    if len(children) < 2:
        return (False, 0)

    cur = [c.get("id") for c in children]
    want = [c.get("id") for c in _registry_desired_order(children)]
    if cur == want:
        return (False, 0)

    moved = 0
    if apply:
        prev = None
        for cid in want:
            after = "" if prev is None else prev
            try:
                if prev is None:
                    r = _call_space_api("space.workspace.move-node", node_id=cid,
                                        target_parent_id=parent, after_node_id="")
                else:
                    r = _call_space_api("space.workspace.move-node", node_id=cid,
                                        target_parent_id=parent, after_node_id=after)
                if r:
                    moved += 1
            except Exception:
                pass
            prev = cid
    return (True, moved if apply else len(want))


def _reorder_registry_all_spaces(apply=True, quiet=False):
    """对用户所有空间执行登记文件夹重排。返回移动次数合计。"""
    total = 0
    try:
        spaces = _list_user_spaces()
    except Exception:
        return 0
    for sid, _name in (spaces or []):
        try:
            changed, n = _reorder_registry_folder(sid, apply=apply)
            total += n
            if changed and not quiet:
                info("已重排「多机同步登记」：%s 空间（移动 %d 项）" % (_name or sid, n))
        except Exception:
            continue
    return total


def _list_child_nodes(space_id, parent_id):
    """列出某个文件夹下的直接子节点。

    必须用 parent_node_id 参数拿子级：不带该参数时 list-nodes 只返回空间根节点，
    且 folder 节点的 nodes 字段是「子节点 id 的字符串」，不是子节点对象列表
    （旧实现误把该字符串当对象列表遍历，导致资料库登记读取整体失效——静默、
    平时靠本地登记表兜底没暴露）。
    """
    data = _call_space_api("list-nodes", space_id=space_id, parent_node_id=parent_id)
    if not data or "nodes" not in data:
        return []
    return [n for n in data["nodes"] if isinstance(n, dict)]


def _read_doc_markdown(space_id, page_id):
    """读取文档 markdown 内容，返回字符串。"""
    data = _call_space_api("get-doc-review", space_id=space_id, page_id=page_id, output_mode="markdown")
    if not data:
        return ""
    return data.get("content", "")


def _parse_registry_doc(markdown):
    """从登记文档 markdown 中解析出 deviceID、hostname、suffix、identityKey。

    文档格式是一个两列表格：字段 | 值。
    """
    info = {}
    # 兼容 markdown 表格和简单的 "字段：值" 行
    # 先按行找 | 字段 | 值 | 这种模式
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            # 也试试 "字段: 值" 或 "字段 | 值"
            m = re.match(r"^(.+?)\s*[:：|]\s*(.+)$", line)
            if m:
                k = m.group(1).strip().lower()
                v = m.group(2).strip()
                if k in ("deviceid", "device id"):
                    info["deviceID"] = v
                elif k == "hostname":
                    info["hostname"] = v
                elif k == "suffix":
                    info["suffix"] = v.upper()
                elif k in ("identitykey", "identity key"):
                    info["identityKey"] = v
            continue
        cells = [c.strip() for c in line.split("|")]
        # 去掉首尾空单元格
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            k = cells[0].lower()
            v = cells[1]
            if k in ("deviceid", "device id"):
                info["deviceID"] = v
            elif k == "hostname":
                info["hostname"] = v
            elif k == "suffix":
                info["suffix"] = v.upper()
            elif k in ("identitykey", "identity key"):
                info["identityKey"] = v
    return info


def _read_library_registry():
    """从资料库「多机同步登记」文件夹读取所有设备登记，返回 {"machines": {suffix: entry}}。

    这是判断新机器该分配 A/B/C/D 的关键来源：资料库已经登记了哪些机器。
    """
    spaces = _list_user_spaces()
    if not spaces:
        return {"machines": {}}
    # 优先找「我的资料」空间；找不到就用第一个个人空间
    space_id = None
    for sid, title in spaces:
        if title and "我的资料" in title:
            space_id = sid
            break
    if not space_id and spaces:
        space_id = spaces[0][0]
    if not space_id:
        return {"machines": {}}

    folder_id = _find_registry_folder_id(space_id)
    if not folder_id:
        return {"machines": {}}

    children = _list_child_nodes(space_id, folder_id)
    machines = {}
    for child in children:
        title = child.get("title", "")
        kind = child.get("kind", "")
        page_id = child.get("id")
        if kind != "doc" or "设备登记" not in title:
            continue
        md = _read_doc_markdown(space_id, page_id)
        if not md:
            continue
        info = _parse_registry_doc(md)
        suffix = info.get("suffix")
        device_id = info.get("deviceID")
        if suffix and device_id:
            machines[suffix] = {
                "deviceID": device_id,
                "hostname": info.get("hostname", ""),
                "identityKey": info.get("identityKey", ""),
                "registeredAt": "",
                "source": "library",
            }
    return {"machines": machines}


def _read_ws_registry():
    """读取工作空间登记表。合并两源：本地协同目录 + 资料库登记表。

    资料库是「权威来源」—— 只要资料库里已经登记了 A/B/C/D，任何新机器都能
    立刻知道自己该分哪台，而不必等 Syncthing 同步完成。
    """
    # 本地登记表（来自 wb-coop 同步夹）
    p = Path(_ws_registry_path())
    local = {"machines": {}}
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("machines"), dict):
                local = d
        except Exception:
            pass

    # ⚡ 只读自愈（2026-10-16）：登记表可能因「多机同时 read-modify-write」被 Syncthing
    #    判定冲突后归档（实测 10:51 A 与 D 同时写 → 原文件被移入 .stversions、原位被删），
    #    此时本机连自己叫哪台都认不出（表现为 `wbsync` 显示「? 机」、A2A 归属全失效）。
    #    原位缺失/为空就从 .stversions 快照里**只读**取一份顶上——不写文件，避免与对端再撞。
    if not local.get("machines"):
        try:
            # 登记表位于 <同步根>/coop/shared/，其 Syncthing 版本归档在同级 <同步根>/coop/.stversions/
            # （从**登记表自身路径**推导，而不是重新取 cfg_sync_root——保持与调用方传入的路径一致）
            snaps_dir = p.parent.parent / ".stversions"
            snaps = sorted(snaps_dir.rglob("*%s*" % p.stem),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            for sp in snaps:
                try:
                    d = json.loads(sp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(d, dict) and isinstance(d.get("machines"), dict) and d.get("machines"):
                    local = d
                    break
        except Exception:
            pass

    # ⚡ 性能关键（2026-10-15）：本地登记表**已有机器记录**时直接返回，跳过资料库网络请求。
    #    本函数在「对话框生命周期 hook」里，每次用户消息都会被调用 1~2 次；而读资料库
    #    要数秒，曾把 UserPromptSubmit 卡到 60s 超时（实测 hook 13.8s / dialogue hb 9.4s）。
    #    资料库只在**引导期**必须读——本地还没有记录（新机尚未收到 wb-coop 同步）时，
    #    靠它知道自己该分哪台。本地文件经 wb-coop 同步到位后，即最新且权威。
    if local.get("machines"):
        return local

    # 资料库登记表（跨机器、同步前即可读取）
    try:
        library = _read_library_registry()
    except Exception:
        library = {"machines": {}}

    # 合并：资料库优先（它代表用户主动登记的意图），本地补充
    merged = {"machines": {}}
    for suffix in set(list(local.get("machines", {}).keys()) + list(library.get("machines", {}).keys())):
        lib_entry = library.get("machines", {}).get(suffix, {})
        local_entry = local.get("machines", {}).get(suffix, {})
        if lib_entry and lib_entry.get("deviceID"):
            merged["machines"][suffix] = lib_entry.copy()
            # 保留本地可能有的额外字段（如 shared 区信息）
            merged["machines"][suffix].update({k: v for k, v in local_entry.items() if k not in merged["machines"][suffix]})
        elif local_entry:
            merged["machines"][suffix] = local_entry.copy()
    # ⚠️ 必须保留 retired（退役历史）与 root —— 世代机制（_ws_next_generation）与
    #    prune（按 retired 反查 deviceID）都依赖它。历史 bug：这里只重建 machines，
    #    导致 retired 每次「读出→写回」都被清空（B 机 2026-10-14 报告的 prune-gap 根因）。
    ret = list(local.get("retired") or [])
    _seen = {(x.get("suffix"), x.get("deviceID"), x.get("retiredAt")) for x in ret}
    for e in (library.get("retired") or []):
        k = (e.get("suffix"), e.get("deviceID"), e.get("retiredAt"))
        if k not in _seen:
            ret.append(e)
            _seen.add(k)
    if ret:
        merged["retired"] = ret
    if local.get("root"):
        merged["root"] = local["root"]
    return merged


def _write_ws_registry(reg):
    p = Path(_ws_registry_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    backup_file(p)
    atomic_write_json(str(p), reg)


def _register_workbuddy_workspace(path):
    """把指定路径登记到 WorkBuddy 客户端的 workspaces 表，使其出现在「新建任务」下拉菜单中。

    WorkBuddy 在 ~/.workbuddy/workbuddy.db 的 workspaces 表中维护工作空间列表。

    健壮性要点：WorkBuddy 客户端运行时会以 WAL 模式持有该 db 的写连接，外部进程直接
    写入可能报「attempt to write a readonly database」。因此这里显式用 WAL 模式 + 长
    busy_timeout，让并发写入排队等待而非立即失败；仍失败时降级为警告（这是便利功能，
    不应阻断工作空间建立）。
    """
    # 守卫：绝不把「时间命名」目录登记为工作空间——那类是平台的会话目录，登进去只会
    # 在下拉里留下删不掉的脏项（2026-10-14 C 机实测）。skill 工作空间一律 sync-all-<后缀>。
    if _looks_like_time_dir(Path(str(path)).name):
        warn("拒绝把时间命名目录登记为工作空间：%s（应使用 sync-all-<后缀> 这类稳定命名）" % path)
        return False
    db_path = WB_ROOT / "workbuddy.db"
    if not db_path.exists():
        return False
    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        # 建表（不存在时）
        cur.execute("CREATE TABLE IF NOT EXISTS workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
        ts = int(time.time() * 1000)
        cur.execute("INSERT OR REPLACE INTO workspaces (path, last_opened_at) VALUES (?, ?)", (str(path), ts))
        conn.commit()
        return True
    except Exception as e:
        warn("登记 WorkBuddy 工作空间失败（可忽略，不影响同步）：%s" % e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_SEED_STATUS_FALLBACK = "completed"
# 占位会话用哪个 status？——**必须用本机库里真实存在、且非「进行中」的值**（先探测再选）。
# 缘由（C-g2 2026-10-16 P1 + D 机同日审查 B1，两侧均给出客户端源码证据）：
#   · 客户端 `status` 是**生命周期枚举**而非自由文本——`archived` 的语义就由它承载
#     （`isArchivedStatus(s)` ⇔ `s==='archived'`），普通列表会过滤掉 `status==='archived'` 的行；
#   · **凭空造值**（早期版本写的 `active` 只在个别 cloud 行出现过，并非不变量）在用户
#     **按状态筛选**时会落进一个「任何勾选桶都不含」的集合 → 该空间**第二次消失**；列默认值
#     `Pending` 同理。
# 故：探测本机库真实取值 → 优先 `completed` / `idle` → 否则取出现最多的非进行中值 → 兜底 `completed`。
_SEED_STATUS_AVOID = {"archived", "working", "planning", "running", "connecting",
                       "failed", "terminated", "error", "cancelled", "pending"}


def _seed_status_for(cur):
    """挑一个**本机库里真实存在**且语义合适的终态值（探测不到就退回 `completed`）。"""
    try:
        rows = cur.execute("SELECT status, COUNT(*) FROM sessions"
                           " WHERE status IS NOT NULL AND status<>'' GROUP BY status").fetchall()
    except Exception:
        return _SEED_STATUS_FALLBACK
    vals = {str(r[0]): int(r[1] or 0) for r in rows if r and r[0]}
    ok = {v.lower(): v for v in vals if v.lower() not in _SEED_STATUS_AVOID}
    for pref in ("completed", "idle"):
        if pref in ok:
            return ok[pref]
    if ok:
        return max(ok.values(), key=lambda v: vals.get(v, 0))
    return _SEED_STATUS_FALLBACK


def _norm_ws_path(p):
    """工作空间路径归一化（与客户端分组键同精神）：realpath + 去尾斜杠 + 折叠大小写。

    客户端分组键走 `normalizeWorkspacePathForCompare(...)`，而 seed 原先用**原始字符串**
    比较 → 根目录带尾斜杠 / 软链 / macOS 大小写不敏感卷下会重复种一条占位会话
    （D 机 2026-10-16 审查 B2）。
    """
    try:
        s = os.path.realpath(os.path.expanduser(str(p)))
    except Exception:
        s = str(p)
    return s.rstrip("/").lower()


def _client_uid():
    """取当前 WorkBuddy 账号的 user_id（客户端 sessions 表用）。取不到返回 ""。"""
    db_path = WB_ROOT / "workbuddy.db"
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=10000")
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM sessions WHERE user_id IS NOT NULL AND user_id<>'' LIMIT 1")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
            finally:
                conn.close()
        except Exception:
            pass
    # 兜底：从 ~/.workbuddy/user-<uid>-personal 目录名解析（客户端账号目录命名法）
    try:
        uids = []
        for p in sorted(WB_ROOT.glob("user-*-personal")):
            n = p.name
            if n.startswith("user-") and n.endswith("-personal"):
                u = n[len("user-"):-len("-personal")]
                if u:
                    uids.append(u)
        if uids:
            if len(uids) > 1:
                # 多账号机器：兜底取第一个可能**不是当前账号**（会话按 currentUserId 过滤，
                # 种到别的账号下本账号根本看不到）→ 明确提示，不装作没这回事（C-g2 P2）。
                warn("检测到 %d 个账号目录，回退取第一个（%s…）；若该空间没出现，"
                     "请在本机客户端于该目录手动新建一条会话" % (len(uids), uids[0][:8]))
            return uids[0]
    except Exception:
        pass
    return ""


def _seed_workspace_conversation(path, suffix, apply=True, quiet=False):
    """在工作空间目录里种一条**占位会话**，使客户端左侧「空间列表」能显示该空间。

    背景（D 机 2026-10-16 提案，A 复核并实测客户端源码）：
      - 左侧列表按**会话**分组；`filterTaskBlocks()` 只保留 `conversations.length > 0`
        的分组，空分组仅当 `kind==='project' && 搜索结果命中 && 无会话过滤` 时才入列
        → **没有会话的工作空间在左侧根本不显示**。
      - 输入框的工作空间下拉走另一套数据源（workspaces 表 ∪ 项目 ∪ 会话分组），
        所以会出现「下拉能看到、左侧看不到」。因此必须在 `sessions` 表里给它一条会话。

    ⚠️ 这是**客户端未公开接口**（表结构非官方契约，客户端升级可能变）：
      - **全程 fail-open**：任何异常只 warn，绝不阻断 `workspace init`；
      - **幂等**：该 cwd 已有未删除会话即跳过（本环境会重试同一条命令）；
      - 取不到 user_id → 跳过并给手动提示（降级），不猜不编。
    返回 True = 已存在或已种入；False = 未种（并已给出降级提示）。
    """
    ws_path = str(path)
    db_path = WB_ROOT / "workbuddy.db"
    if not db_path.exists():
        if not quiet:
            warn("未找到客户端数据库；跳过「空间自动入列」——可在客户端里于该目录手动新建一条会话")
        return False
    uid = _client_uid()
    if not uid:
        if not quiet:
            warn("取不到 WorkBuddy 账号 user_id；跳过「空间自动入列」——"
                 "请在客户端里于该目录手动新建一条会话，空间即会出现在左侧")
        return False
    try:
        import sqlite3
        import uuid as _uuid
        conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            # 幂等判定：
            #  ① **排除 archived**——客户端的「归档」只改 status、**不动 deleted_at**，但普通列表
            #     会过滤掉 archived 行 → 若这里只判 deleted_at：用户归档掉空间里最后一条会话后
            #     空间从左侧消失，而 seed 会命中该 archived 行判「已有会话」**拒绝补种**，
            #     症状与「没建成」完全一样且无自助手段（C-g2 P1，D 机独立复核确认）。
            #  ② **路径归一化**比较——客户端分组键走 normalizeWorkspacePathForCompare，原样字符串
            #     比较在根目录带尾斜杠 / 软链 / 大小写不敏感卷下会重复种（D 机 B2）。
            _base = os.path.basename(ws_path.rstrip("/")) or ws_path
            cur.execute("SELECT cwd FROM sessions WHERE deleted_at IS NULL"
                        " AND (status IS NULL OR lower(status) <> 'archived')"
                        " AND (cwd = ? OR cwd LIKE ?)", (ws_path, "%/" + _base + "%"))
            _want = _norm_ws_path(ws_path)
            if any(_norm_ws_path(r[0]) == _want for r in cur.fetchall() if r and r[0]):
                if not quiet:
                    info("该空间已有会话，无需补种")
                return True
            if not apply:
                info("（演练）将为该空间种一条占位会话（写客户端 sessions 表），使其出现在左侧列表")
                return True
            _status = _seed_status_for(cur)
            now = int(time.time() * 1000)
            cur.execute(
                "INSERT INTO sessions (id, cwd, user_id, title, status, created_at, updated_at,"
                " last_activity_at, is_playground, transport, unread)"
                " VALUES (?,?,?,?,?,?,?,?,0,'local',0)",
                (str(_uuid.uuid4()), ws_path, uid,
                 "sync-all-%s 空间（占位会话，可直接用）" % suffix,
                 _status, now, now, now))
            conn.commit()
            if not quiet:
                ok("已种一条占位会话（status=%s，取自本机库真实取值），使该空间出现在客户端左侧空间列表"
                   % _status)
                info("提示：需**重启一次客户端**才生效；这条空会话点开即可直接对话。"
                     "⚠️ 别删它——删了该空间会再次从左侧消失（可重跑 `wbsync workspace seed --apply` 补回）")
            return True
        finally:
            conn.close()
    except Exception as e:
        if not quiet:
            warn("种占位会话失败（不影响同步与协作，可在客户端手动新建一条会话）：%s" % e)
        return False


def _read_ws_changelog():
    p = Path(_ws_changelog_path())
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, list):
            return d
    except Exception:
        pass
    return []


def _append_ws_changelog(entry):
    p = Path(_ws_changelog_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    log = _read_ws_changelog()
    log.append(entry)
    atomic_write_json(str(p), log)


def _ws_my_suffix(reg, my_id):
    """本机已分配到的后缀；未分配返回 None。"""
    return _ws_suffix_of_device(reg, my_id)


def _ws_local_dir(reg, suffix):
    """某后缀工作空间在本机的落地目录名：sync-all-<suffix>。

    每台机器目录名不同（A/B/C/D），但底层用同一个 Syncthing folder_id
    做跨机同步，所以对端的内容也会出现在本机这个目录里。
    目录名统一用 sync-all-<suffix>，与登记表盘名、SKILL.md 文档一致。
    """
    return _ws_root() / ("sync-all-%s" % suffix)


def _ws_folder_id(suffix):
    """Syncthing 同步夹 ID：统一用 sync-workspace-all（历史锚点，勿改）。

    这是关键设计：A 机的 sync-all-A 和 B 机的 sync-all-B 是同一个
    Syncthing folder_id 在不同机器上的不同本地路径，因此两台机器的
    工作区内容会互相同步。

    注意：folder_id 保持 sync-workspace-all 不变——它是同步的锚点，
    已在 A/B 机 Syncthing 里注册。若改成 sync-all 会与对端夹 ID 不匹配，
    破坏同步。只有「目录名 + 显示名(label)」用 sync-all-<suffix>。
    """
    return "sync-workspace-all"


def _record_change(reg, my_suffix, action, detail, my_id, apply_flag):
    entry = {
        "at": int(time.time()),
        "hostname": HOSTNAME,
        "deviceID": my_id,
        "suffix": my_suffix,
        "action": action,
        "detail": detail,
    }
    if apply_flag:
        _append_ws_changelog(entry)
    return entry


def cmd_workspace(args):
    head("工作空间（多机协作）")
    action = args.action

    st = Syncthing()
    my_id = _my_device_id()
    if not my_id:
        die("拿不到本机设备 ID。先 wbsync start 再试。")

    reg = _read_ws_registry()

    if action == "init":
        return _ws_init(args, reg, st, my_id)
    if action == "list":
        return _ws_list(reg, st, my_id)
    if action == "grant":
        return _ws_grant(args, reg, st, my_id)
    if action == "revoke":
        return _ws_revoke(args, reg, st, my_id)
    if action == "apply-grants":
        return _ws_apply_grants(args, reg, st, my_id)
    if action == "seed":
        return _ws_seed(args, reg, st, my_id)
    if action == "log":
        return _ws_log(args, reg, my_id)
    if action == "cleanup":
        return _ws_cleanup(args, reg, my_id)
    if action == "hide":
        return _ws_hide(args, reg, my_id)
    die("未知动作：%s" % action)
    return 1


# 平台每次跑 skill/会话时，会在工作空间根（默认 ~/WorkBuddy）下建一个
# 「时间命名」的临时目录（如 2026-10-13-18-48-15），会话结束若没清理就遗弃成空壳。
# 更正（2026-10-14，依据 C 机实测）：这些时间目录**也可能**残留在 workbuddy.db 的
# workspaces 表里，导致 UI 工作空间下拉出现删不掉的「时间文件夹」。故 cleanup 除删空壳
# 外，还要清 db 里时间戳命名的残留登记（db 有 WAL 锁，需 busy_timeout + 先备份）。
_TIME_DIR_RES = (
    re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$"),  # 平台常见格式
    re.compile(r"^\d{8}-\d{6}$"),                           # 备用格式
)


def _looks_like_time_dir(name):
    """名字是否「纯时间戳命名」（平台会话工作区格式，如 2026-10-13-18-48-15）。
    skill 建的任何工作空间都不得用这种命名——见 _register_workbuddy_workspace 的守卫。"""
    return any(rx.match(str(name)) for rx in _TIME_DIR_RES)


def _time_shell_dirs():
    """返回平台遗弃的时间命名空壳目录列表（仅空目录、严格时间戳命名）。

    这些目录位于平台工作空间根 DEFAULT_WORKBUDDY_ROOT（默认 ~/WorkBuddy）下，
    是平台每次跑 skill / 会话时建的临时目录、会话结束遗弃成空壳。它们不登记进
    workbuddy.db，删之无 BEGIN IMMEDIATE / WAL 锁风险。
    """
    root = DEFAULT_WORKBUDDY_ROOT
    out_list = []
    if not root.exists():
        return out_list
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        if not any(rx.match(sub.name) for rx in _TIME_DIR_RES):
            continue
        try:
            if not any(sub.iterdir()):
                out_list.append(sub)
        except OSError:
            pass
    return out_list


def _stale_db_ws_records():
    """读 workbuddy.db 的 workspaces 表，返回「时间戳命名」的残留登记 [(rowid, path)]。

    这些是平台遗弃的会话工作区登记——UI 的工作空间下拉读的就是这张表，当前版本 UI
    没有「移除」入口，所以它们删不掉。只读，不写盘。
    """
    db = WB_ROOT / "workbuddy.db"
    if not db.exists():
        return []
    rows = []
    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=15000")
        cur = conn.cursor()
        cur.execute("SELECT rowid, path FROM workspaces")
        for rid, path in cur.fetchall():
            if any(rx.match(Path(str(path)).name) for rx in _TIME_DIR_RES):
                rows.append((rid, str(path)))
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def _purge_db_ws_records(apply_flag):
    """删除 workspaces 表里「时间戳命名」的残留登记（先备份 db）。返回 (命中数, 删除数)。

    安全：只删时间戳命名的记录（与空壳同源），绝不动 sync-all-X 等正常登记与用户项目。
    """
    recs = _stale_db_ws_records()
    if not recs or not apply_flag:
        return len(recs), 0
    db = WB_ROOT / "workbuddy.db"
    backup_file(str(db))
    deleted = 0
    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        for rid, _p in recs:
            try:
                cur.execute("DELETE FROM workspaces WHERE rowid=?", (rid,))
                deleted += 1
            except Exception:
                continue
        conn.commit()
    except Exception as e:
        warn("清 db 残留登记失败：%s" % e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return len(recs), deleted


def _ws_cleanup(args, reg, my_id):
    """清理平台遗弃的时间命名空壳目录 + workbuddy.db 里的时间戳残留登记。

    安全策略（宗旨：减少用户操作、必须操作时只弹确认）：
      - 空壳只认「严格时间戳命名」且「为空目录」的**直接子目录**；绝不误删用户项目。
      - db 只删「时间戳命名」的登记记录；绝不动 sync-all-X 等正常项与用户项目。
      - 默认只列不删（演练），加 --apply 才落盘；删 db 前自动备份 db。
    """
    shells = _time_shell_dirs()
    stale = _stale_db_ws_records()
    if not shells and not stale:
        ok("未发现时间命名目录 / db 残留登记，无需清理。")
        return 0

    if shells:
        head("可清理的时间命名空壳目录（%d 个）" % len(shells))
        for s in shells:
            info("  %s" % s)
    if stale:
        head("可清理的 workbuddy.db 残留登记（%d 条，UI 下拉里删不掉的「时间文件夹」）" % len(stale))
        for _rid, p in stale:
            info("  %s" % p)
        info("这些是平台遗弃的会话工作区登记；清后需**完全退出并重开 WorkBuddy**，下拉才刷新。")

    if not args.apply:
        out()
        info("（演练）以上未删除。加 --apply 后：空目录 rmdir + 删 db 残留登记（删前自动备份 db）。")
        return 0

    removed = 0
    for s in shells:
        try:
            s.rmdir()  # 仅当为空成功；非空抛异常被跳过
            removed += 1
            ok("已删除 %s" % s)
        except OSError as e:
            warn("删除失败（可能非空）：%s —— %s" % (s, e))
    hit, dele = _purge_db_ws_records(True)
    if hit:
        ok("已删 %d/%d 条 db 残留登记（db 已备份为 *.wbsync-bak-*）" % (dele, hit))
        info("提示：需完全退出并重开 WorkBuddy，下拉菜单才刷新。")
    out()
    ok("共清理 %d 个空壳 + %d 条 db 残留登记。" % (removed, dele))
    return 0


def _hide_windows(path):
    """Windows 下可靠加 Hidden：用 PowerShell 对 Attributes 位或，绕开 Git Bash 的
    `attrib +h` 中文编码 + 沙箱拦截导致的「看似成功实则不生效」坑（消化 B 反馈）。"""
    script = "$i=Get-Item -LiteralPath @'\n%s\n'@; " \
             "$i.Attributes = $i.Attributes -bor [IO.FileAttributes]::Hidden" % str(path)
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True)


def _ws_hide(args, reg, my_id):
    """隐藏本机磁盘上的 A/B/C（及后续 D…）工作空间目录，减少视觉干扰。

    消化 B/C 反馈：隐藏须每台机各自做一次，且 Windows 下 Git Bash 的 `attrib +h`
    中文编码 + 沙箱拦截会「看似成功实则不生效」。本命令封装各平台可靠隐藏法，
    一条命令替代手动敲，符合「减少用户操作」宗旨：
      - macOS: chflags hidden
      - Windows: PowerShell 对 Attributes 位或 +h（绕开 attrib 的坑）
    默认只列不执行（演练），--apply 才落盘。
    """
    suffix = _ws_suffix_of_device(reg, my_id) if my_id else None
    if not suffix:
        die("本机尚未分配后缀（先 wbsync workspace init）。")
    local_dir = _ws_local_dir(reg, suffix)
    if not local_dir.exists():
        die("工作空间目录不存在：%s" % local_dir)

    targets = [local_dir / ch for ch in _SUFFIXES if (local_dir / ch).is_dir()]
    if not targets:
        ok("未发现可隐藏的后缀目录（%s 下没有 A/B/C… 子目录）。" % local_dir)
        return 0

    head("将要隐藏的工作空间目录（%d 个）" % len(targets))
    for d in targets:
        info("  %s" % d)

    if not args.apply:
        out()
        info("（演练）未执行隐藏。加 --apply 后按平台可靠隐藏（macOS chflags / Windows PowerShell）。")
        return 0

    done = 0
    for d in targets:
        try:
            if IS_WIN:
                _hide_windows(d)
            else:
                subprocess.run(["chflags", "hidden", str(d)], check=True)
            done += 1
            ok("已隐藏 %s" % d)
        except Exception as e:
            warn("隐藏失败 %s：%s" % (d, e))
    out()
    ok("共隐藏 %d 个目录。" % done)
    return 0


def _apply_workspace_permissions(reg, my_suffix, apply_flag):
    """根据注册表里的授权关系，设置本地各工作区目录的读写权限。

    规则：
      - 本机自己的工作区（my_suffix/）：可写
      - 被授权可写的工作区（在 grants 列表里）：可写
      - 其他机器的工作区：不靠 chmod 锁定（行为约定：各机自觉不写他人目录，
        需要改先走 wbsync workspace grant）；目录统一保持 755，避免挡住 Syncthing 以同用户身份接收对端写入。
      - shared/：可写（公共区）
    """
    if not my_suffix:
        return False, "本机未分配后缀"
    local_dir = _ws_local_dir(reg, my_suffix)
    if not local_dir.exists():
        return False, "工作空间目录不存在：%s" % local_dir

    my_entry = reg.get("machines", {}).get(my_suffix, {})
    grants = set(my_entry.get("grants", []))

    changed = []
    for sub in local_dir.iterdir():
        if not sub.is_dir():
            continue
        name = sub.name
        if name == "shared":
            want_write = True
        elif name == my_suffix:
            want_write = True
        elif name in _SUFFIXES:
            want_write = name in grants
        else:
            continue

        try:
            mode = sub.stat().st_mode
            is_write = bool(mode & 0o200)  # owner write
            if want_write and not is_write:
                sub.chmod(0o755)
                changed.append("%s → 可写" % name)
            elif not want_write and not is_write:
                # 行为约定(2026-10-14 更正): 不再把他人工作区 chmod 成 555 只读,
                # 否则 Syncthing 以同用户身份接收对端写入时会 EACCES(permission denied),
                # 连带挡住同步接收。目录统一保持 755, 改他人目录先走 wbsync workspace grant。
                sub.chmod(0o755)
                changed.append("%s → 755(解除只读锁)" % name)
        except OSError as e:
            warn("无法设置 %s 权限：%s" % (sub, e))

    if not apply_flag:
        return True, "（演练）将调整权限：" + ("; ".join(changed) if changed else "无变化")
    return True, "已调整权限：" + ("; ".join(changed) if changed else "无变化")


def _ws_preinit_check(local_dir, suffix):
    """workspace init 前的识别 + 冲突/旧文件提示（只读，不写盘）。

    要防的坑（2026-10-14 用户提出）：本机磁盘上可能残留一个「同后缀/他后缀」的
    sync-all-* 目录（例如旧机器退出了、但本机同步根还留着它的目录），重接入或
    新建时容易和现有空间搅在一起。识别逻辑：
      - 有 .wbsync-workspace 标记 → 是 wbsync 自建空间（旧文件可安全处理/提示）；
      - 无标记 → 可能是平台建的、或用户手建的普通目录（不自动动它，只提示）。
    """
    if not local_dir.exists():
        return
    is_wbsync = _is_wbsync_workspace(local_dir)
    # 统计里面的东西，判断是否「非空 / 有旧文件 / 有 sync-conflict」
    try:
        children = [p for p in local_dir.iterdir()]
    except Exception:
        children = []
    conflicts = [p.name for p in children if "sync-conflict" in p.name]
    # 排除标记文件、本机私有目录、正常的同步元数据/配置、以及在役机器的私有目录后，
    # 还有别的才叫「旧文件」（残留/孤儿）。在役机器私有目录是同步来的正常内容。
    _WS_NOISE = {".stfolder", ".stignore", ".stversions", ".workbuddy", "README.md"}
    try:
        _active = set((_read_ws_registry() or {}).get("machines", {}).keys())
    except Exception:
        _active = set()
    _noise = _WS_NOISE | _active | {suffix}
    others = [p for p in children
              if p.name not in _noise and not p.name.startswith(".wbsync-workspace")]

    section("工作空间识别与冲突检查")
    if is_wbsync:
        info("识别：%s 是本 skill 自建的工作空间（有 .wbsync-workspace* 标记）。" % local_dir)
    else:
        if children:
            info("识别：%s 已存在，但**没有**本 skill 的标记（不是本 skill 建的）。" % local_dir)
            info("      它可能是平台建的、或你手建的普通目录，本 skill 不会自动清理它。")
        else:
            info("识别：%s 已存在但为空（无标记）。" % local_dir)

    if conflicts:
        warn("发现 %d 个 sync-conflict 冲突副本：" % len(conflicts))
        for c in conflicts:
            warn("  - %s" % c)
        info("  建议：确认哪份是最新的后，删掉旧的冲突副本（用 wbsync workspace cleanup 或手动）。")
    if others:
        info("发现 %d 个「非本机后缀」的旧内容（可能是已退出机器残留）：" % len(others))
        for p in others:
            info("  - %s%s" % (p.name, "（目录）" if p.is_dir() else ""))
        info("  提示：这些不会被本次 init 覆盖；若确认无用可手动清理，或跑 `wbsync workspace cleanup`。")
    if not is_wbsync and not others and not conflicts:
        info("未发现冲突或旧文件。")


def _ws_find_same_name_dirs(suffix, cfg_root=None):
    """在多个候选根下找同后缀的 `sync-all-<suffix>`，用于「同名不同根」检测。
    返回 [(root_str, path_str, is_wbsync)]。"""
    roots = []
    for r in (cfg_root, DEFAULT_WORKBUDDY_ROOT, _disk_root()):
        if not r:
            continue
        p = Path(str(r)).expanduser()
        if p not in roots:
            roots.append(p)
    out = []
    for r in roots:
        d = r / ("sync-all-%s" % suffix)
        if d.exists():
            out.append((str(r), str(d), _is_wbsync_workspace(d)))
    return out


def _ws_guard_existing(args, cfg, reg, suffix):
    """建夹前的旧工作空间检测。命中「同名不同根」等需确认场景 → 返回退出码（**不写盘**）；
    否则返回 None 继续。

    来源：C-g2 2026-10-14 的《workspace init 守卫》提案（v2.8.0 已实现，原方案文档已删除）。
    原则：**没确认就不动**（不阻塞式 input，只打印 + 非 0 退出码 + 可选 --json，交由 Agent 问用户）。
    """
    cur_root = (cfg or {}).get("workspace_root")
    my_local = str(Path(str(cur_root or DEFAULT_WORKBUDDY_ROOT)).expanduser()
                   / ("sync-all-%s" % suffix))
    cands = _ws_find_same_name_dirs(suffix, cur_root)
    others = [c for c in cands if os.path.normpath(c[1]) != os.path.normpath(my_local)]

    if not others:
        return None
    if getattr(args, "force_root", False):
        return None
    if getattr(args, "reuse_existing", False):
        tgt = others[0]
        cfg["workspace_root"] = tgt[0]
        save_config(cfg, getattr(args, "apply", False))
        info("沿用已存在的工作空间：%s（workspace_root 已指向 %s）" % (tgt[1], tgt[0]))
        return None

    head("检测到同名工作空间 —— 需你确认（本次未做任何改动）")
    for _r, p, isw in cands:
        info("  %s%s" % (p, "（本 skill 自建）" if isw else "（非本 skill 建，勿动）"))
    out()
    warn("存在多处同名目录，已**停止、未写盘**。请选择：")
    info("  · 沿用已存在的那处：加 `--reuse-existing`")
    info("  · 要使用当前根新建：加 `--force-root`（或显式 `--path <根>`）")
    if getattr(args, "json", False):
        print(json.dumps({"need_confirm": True, "suffix": suffix,
                          "candidates": [{"root": r, "path": p, "wbsync": w} for r, p, w in cands],
                          "recommended": "reuse_existing"}, ensure_ascii=False))
    return 2


def _rejoin_broadcast(suffix, my_id, reg):
    """入组 / 重入后向**全员**广播一条 `verify` 任务，随任务带上本机 deviceID。

    目的：让现役机器下次 `wake` 就能发现「本机设备表缺这个 ID」并一键反向配对
    （C-g2 2026-10-14 重入时是人工多轮排查才定位的；B 机 2026-10-15 提案固化为自动动作）。

    幂等：本机已有**未终结**的同类广播则跳过（重复 init 不会刷屏）。返回是否发出。
    """
    others = [s for s in (reg.get("machines") or {}).keys() if s != suffix]
    if not others or not my_id:
        return False
    try:
        for t in _a2a_all_tasks():
            if (t.get("from") == suffix and t.get("skill") == "verify"
                    and t.get("to") in ("全员", "all")
                    and t.get("state") not in A2A_TERMINAL_STATES):
                return False
    except Exception:
        pass
    gen = 1
    try:
        gen = int((reg.get("machines") or {}).get(suffix, {}).get("generation") or 1)
    except Exception:
        gen = 1
    try:
        tid = _a2a_next_task_id(suffix)
        _a2a_ensure_readme()
        _a2a_append_event(_a2a_journal_path(suffix), suffix, dict({
            "event": "task", "id": tid, "from": suffix, "to": "全员", "skill": "verify",
            "params": {"deviceID": my_id, "suffix": suffix, "generation": str(gen)},
            "state": "submitted", "at": _a2a_now(),
            "text": "【入组/重入广播】本机 %s 机（第 %d 代）deviceID=%s。"
                    "若你的设备表还没有这个 ID，请跑 wbsync accept-peer --device %s --apply。"
                    % (suffix, gen, my_id, my_id),
        }, **_a2a_identity(suffix)))
        return tid
    except Exception:
        return False


def _ws_seed(args, reg, st, my_id):
    """老机补种入口：给**本机**已建好的工作空间补一条占位会话（客户端左侧空间列表可见）。

    为什么单独给命令：`workspace init` 只在建空间那一次跑，已装好的机器不会重跑；
    而补种是**一次性**动作，**不塞进 `wake`**——wake 是每次会话都跑的常态路径，
    加重负担违背省 token 纪律。本命令幂等，可重复执行。
    """
    section("补种工作空间占位会话（客户端左侧可见）")
    suffix = _ws_my_suffix(reg, my_id)
    if not suffix:
        die("拿不到本机后缀。先跑 `wbsync workspace init --apply`。")
    own = _ws_local_dir(reg, suffix)
    given = getattr(args, "path", None)
    if given:
        local_dir = Path(given).expanduser()
        # 归属校验（C-g2 P2）：只允许补种**本机**工作空间——否则可把同步来的他机空间、
        # 甚至任意目录种进本机 sessions 表，在本机左侧凭空造出一个不属于自己的分组。
        try:
            inside = local_dir.resolve().is_relative_to(own.resolve())
        except Exception:
            inside = False
        if not inside:
            die("--path 只允许指向**本机**工作空间：%s（后缀 %s）；要补种他机目录请在**那台机器**上执行。"
                % (own, suffix))
    else:
        local_dir = own
    if not local_dir.exists():
        die("工作空间目录不存在：%s（先跑 `wbsync workspace init --apply`）" % local_dir)
    apply_ = bool(getattr(args, "apply", False))
    info("工作空间：%s（后缀 %s）" % (local_dir, suffix))
    if apply_:
        _register_workbuddy_workspace(local_dir)   # 与 init 行为一致：顺手补 workspaces 表
    ok_flag = _seed_workspace_conversation(local_dir, suffix, apply=apply_)
    if apply_ and not ok_flag:
        return 1
    return 0


def _ws_init(args, reg, st, my_id):
    """首次使用：给本机分配后缀 A/B/C/D，建自己的工作空间目录 + 注册 Syncthing 同步夹 + 写注册表。"""
    section("分配工作空间后缀")

    cfg = load_config(quiet=True)
    cur_root = (cfg or {}).get("workspace_root")
    want_path = getattr(args, "path", None)
    force_root = bool(getattr(args, "force_root", False))
    # ⚠️ 不无条件覆盖：缺 --path 时**保留已记住的根**（C-g2 2026-10-14 踩坑：曾被静默打回
    #    ~/WorkBuddy，与既有协作区脱节并误建游离目录，且**会复发**）。要改回默认根须显式 --force-root。
    if want_path:
        cfg["workspace_root"] = str(Path(want_path).expanduser())
    elif cur_root and not force_root:
        info("保留已记住的工作空间根：%s（如需改为默认 %s 请显式加 --force-root）"
             % (cur_root, DEFAULT_WORKBUDDY_ROOT))
    else:
        cfg["workspace_root"] = str(DEFAULT_WORKBUDDY_ROOT)
    save_config(cfg, args.apply)
    info("工作空间根目录：%s" % cfg["workspace_root"])

    suffix, is_new = _resolve_my_suffix(reg, my_id)
    if suffix is None:
        die("后缀用尽（超过 %d 台机器）" % len(_SUFFIXES))

    # 旧工作空间检测（同名不同根）——命中则不写盘、提示确认、退出码 2
    _rc_guard = _ws_guard_existing(args, cfg, reg, suffix)
    if _rc_guard is not None:
        return _rc_guard

    root = _ws_root()
    if is_new:
        info("探测：你是第 %d 台，将分配后缀 %s" % (len(reg.get("machines", {})) + 1, suffix))
    else:
        info("探测：你已分配后缀 %s" % suffix)

    local_dir = _ws_local_dir(reg, suffix)
    folder_id = _ws_folder_id(suffix)
    my_workspace = local_dir / suffix
    info("工作空间目录：%s" % local_dir)
    info("同步夹 ID：%s" % folder_id)

    # 冲突/旧文件检测：识别本目录是不是 wbsync 自建的，遇到冲突或旧文件先告诉用户
    _ws_preinit_check(local_dir, suffix)

    if not args.apply:
        info("（演练）未创建。加 --apply 后：建目录 → 建子目录({suffix}/) → 注册同步夹 → 写注册表。".format(suffix=suffix))
        return 0

    # 1) 建本地目录结构
    #    sync-all-<suffix>/
    #      <suffix>/   本机工作区（其他机器可读，改需授权）
    #    不再创建 shared/ 公共可写区 —— 2026-10-14 已取消（公共可写、无归属、
    #    最易变成乱写入口）。跨机广播/派活走 coop 通道（记事本 / A2A），
    #    放文件走盘根目录。
    for sub in ("", suffix):
        d = local_dir if not sub else local_dir / sub
        d.mkdir(parents=True, exist_ok=True)

    # 2) 写「本 skill 自建空间」标记文件（供识别 + 后续冲突检测）
    try:
        _ws_marker_path(local_dir, suffix).write_text(_ws_marker_payload(suffix), encoding="utf-8")
    except Exception:
        pass

    # 顺手补 .stfolder：改/指回既有目录时易缺，缺了 Syncthing 会报 folder marker missing、
    # state=error 且不扫描（C-g2 2026-10-14 踩坑）。
    try:
        _sf = local_dir / ".stfolder"
        if not _sf.exists():
            _sf.mkdir(parents=True, exist_ok=True)
            info("已补 .stfolder（Syncthing 目录标记）")
    except Exception as e:
        warn("补 .stfolder 失败：%s" % e)

    # 2) 写一份 README 说明读写约定
    readme = local_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 多机协作工作空间\n\n"
            "本目录跨机同步。约定：\n\n"
            "- `%s/` 是 **本机（%s 机）** 的工作区，其他机器**可读、不可改**。\n"
            "- 放文件：直接丢在**本盘根目录**（各机收件区）；跨机广播/派活走 coop 通道（记事本 / A2A）。\n"
            "- 若 B/C/D 机需要改 A 机的工作区，A 机应执行 `wbsync workspace grant B --apply` 授权。\n"
            "- 未授权擅自修改他人的工作区，可能产生 sync-conflict。\n" % (suffix, suffix),
            encoding="utf-8",
        )

    # 3) 注册 Syncthing 同步夹（整个目录跨机同步）
    if st.available:
        st.ensure_folder(folder_id, "sync-all-%s" % suffix, local_dir)
        # 关键：把已配对的所有对端设备都共享进这个工作空间夹。
        # ensure_folder 新建夹时默认只含本机，若不主动共享，对端机器就收不到
        # 这个工作空间（这是之前「B 机看不到 A 机工作空间」的根因）。
        for peer_id in st.devices().keys():
            if peer_id == my_id:
                continue
            try:
                done, why = st.share_folder(folder_id, peer_id)
                if done:
                    info("已把工作空间夹共享给 %s" % peer_id[:7] + "...")
            except Exception as e:
                warn("共享工作空间夹给 %s 失败：%s" % (peer_id[:7], e))

    # 4) 写注册表
    machines = dict(reg.get("machines", {}))
    machines[suffix] = {
        "deviceID": my_id,
        "hostname": HOSTNAME,
        "generation": _ws_next_generation(reg, suffix),
        "created": int(time.time()),
        "grants": machines.get(suffix, {}).get("grants", []),
    }
    reg["machines"] = machines
    # 本机路径只记在自己条目下：顶层 `root` 是「只写不读」的死字段，且各机 init 时
    # 都写自己的绝对路径 → 互相覆盖、制造无谓的跨机差异（C-g2 2026-10-16 报告）。
    machines[suffix]["root_local"] = str(root)
    # 写入前：与磁盘最新一份合并 + 守住 retired + 尝试跨机预约（三重防线，见 _ws_registry_write_guarded）
    _ws_registry_write_guarded(reg, suffix)

    _record_change(reg, suffix, "init", "建立工作空间 sync-all-%s" % suffix, my_id, True)

    # 一开始就把资料库的「已退出机器登记」归档文件夹建好（幂等，供日后退出时归档登记 doc）
    try:
        for _sid, _t in _list_user_spaces():
            _ensure_registry_archive_folder(_sid)
    except Exception:
        pass

    # 入组 / 重入广播：把本机 deviceID 随 A2A 任务发给全员 —— 现役机器下次 wake
    # 即可自动发现「本机设备表缺这个 ID」并一键 accept-peer（重入换 ID 场景）
    try:
        _tid = _rejoin_broadcast(suffix, my_id, reg)
        if _tid:
            info("已广播入组任务 %s（全员）——其他机器下次 `wbsync wake` 会看到配对提示。" % _tid)
    except Exception:
        pass

    # 新机登记后顺手把「多机同步登记」排好：README → 现役 A/B/C… → 归档文件夹（末）
    # （新机写完自己的登记 doc 后跑一次 `wbsync registry-order --apply` 也能达到同样效果）
    try:
        _n = _reorder_registry_all_spaces(apply=True, quiet=True)
        if _n:
            info("已重排资料库「多机同步登记」次序（README → 现役机 → 归档）")
    except Exception:
        pass

    # 5) 应用权限约定：本机工作区可写，其他机器工作区只读（能读不能改）
    _apply_workspace_permissions(reg, suffix, args.apply)

    # 6) 登记到 WorkBuddy 客户端工作空间列表，让下拉菜单能看到
    if _register_workbuddy_workspace(local_dir):
        info("已登记到 WorkBuddy 工作空间列表（新建任务下拉菜单可见）")

    # 6b) 再种一条占位会话：客户端左侧「空间列表」是按**会话**分组的，没有会话的空间不会显示
    #     （D 机 2026-10-16 提案 + A 实测客户端源码；属未公开接口，全程 fail-open）
    _seed_workspace_conversation(local_dir, suffix, apply=args.apply)

    ok("工作空间 sync-all-%s 已建立" % suffix)
    info("本机工作区：%s（其他机器可读，改需授权）" % my_workspace)
    if st.available:
        info("已注册同步夹 %s（本目录跨机同步）" % folder_id)
    return 0


def _ws_list(reg, st, my_id):
    section("已登记的工作空间")
    machines = reg.get("machines", {})
    if not machines:
        info("还没有任何工作空间。先跑 wbsync workspace init --apply。")
        return 0
    my_suffix = _ws_my_suffix(reg, my_id)
    for suffix in sorted(machines.keys()):
        e = machines[suffix]
        owner = e.get("hostname", "?")
        grants = e.get("grants", [])
        mark = "（本机）" if suffix == my_suffix else ""
        info("%s  sync-all-%s  owner=%s%s" % (suffix, suffix, owner, mark))
        if grants:
            info("      已授权可写：%s" % ", ".join(grants))
    return 0


def _ws_grant(args, reg, st, my_id):
    target = (args.target or "").strip().upper()
    if not target or target not in _SUFFIXES:
        die("用法：wbsync workspace grant <后缀>（如 B）")
    my_suffix = _ws_my_suffix(reg, my_id)
    if not my_suffix:
        die("本机还没建工作空间，先 wbsync workspace init --apply")
    if target == my_suffix:
        die("不能授权给自己")
    machines = reg.get("machines", {})
    if target not in machines:
        die("后缀 %s 还没有对应机器" % target)
    grants = set(machines.get(my_suffix, {}).get("grants", []))
    if not args.apply:
        info("演练：将授权 %s 可写你的 sync-all-%s（当前已授权：%s）" % (target, my_suffix, ", ".join(sorted(grants)) or "无"))
        return 0
    grants.add(target)
    machines[my_suffix]["grants"] = sorted(grants)
    reg["machines"] = machines
    _write_ws_registry(reg)
    _record_change(reg, my_suffix, "grant", "授权 %s 可写" % target, my_id, True)
    ok("已授权 %s 可写你的工作空间" % target)
    return 0


def _ws_revoke(args, reg, st, my_id):
    target = (args.target or "").strip().upper()
    if not target or target not in _SUFFIXES:
        die("用法：wbsync workspace revoke <后缀>（如 B）")
    my_suffix = _ws_my_suffix(reg, my_id)
    if not my_suffix:
        die("本机还没建工作空间，先 wbsync workspace init --apply")
    machines = reg.get("machines", {})
    grants = set(machines.get(my_suffix, {}).get("grants", []))
    if target not in grants:
        info("本来就没授权 %s，无需处理" % target)
        return 0
    if not args.apply:
        info("演练：将收回 %s 对你工作空间的写权限" % target)
        return 0
    grants.discard(target)
    machines[my_suffix]["grants"] = sorted(grants)
    reg["machines"] = machines
    _write_ws_registry(reg)
    _record_change(reg, my_suffix, "revoke", "收回 %s 的写权限" % target, my_id, True)
    ok("已收回 %s 的写权限" % target)
    return 0


def _ws_apply_grants(args, reg, st, my_id):
    my_suffix = _ws_my_suffix(reg, my_id)
    if not my_suffix:
        die("本机还没建工作空间，先 wbsync workspace init --apply")
    ok_flag, msg = _apply_workspace_permissions(reg, my_suffix, args.apply)
    (ok if ok_flag else bad)(msg)
    return 0 if ok_flag else 1


def _ws_log(args, reg, my_id):
    section("工作空间变更记录")
    log = _read_ws_changelog()
    if not log:
        info("还没有变更记录。")
        return 0
    my_suffix = _ws_my_suffix(reg, my_id)
    for e in sorted(log, key=lambda x: x.get("at", 0), reverse=True):
        ts = datetime.fromtimestamp(e.get("at", 0)).strftime("%Y-%m-%d %H:%M:%S")
        suffix = e.get("suffix", "?")
        mark = "  ← 你的" if suffix == my_suffix else ""
        info("%s  [%s] %s%s  %s" % (ts, e.get("hostname", "?"), e.get("action", ""), mark, e.get("detail", "")))
    return 0


def cmd_pair(args):
    """与对端设备配对（**已收敛为 `accept-peer` 的幂等语义**，不再有破坏性副作用）。

    历史坑（2026-10-14 C-g2 入组时踩到）：旧实现①只记一个 `peer_id`、②换对端时
    `_forget_peer` 会**连带删除旧对端**，三机以上会误删已配对的对端。现已改为：
    只做「add_device（幂等）+ share_folder（幂等）」，不写 peer_id、不删任何已有对端。
    与 `wbsync accept-peer` 等价，保留此命令仅为向后兼容旧习惯。
    """
    head("与对端设备配对（幂等，等价于 accept-peer）")
    raw = args.peer_id
    did = normalize_device_id(raw)
    if not did:
        die("设备 ID 格式不对。应为 8 组、每组 7 位（A-Z 和 2-7），用 - 连接。")
    st = Syncthing()
    if not st.available:
        die("Syncthing 没有在运行，先跑 wbsync start")
    if did == st.my_id():
        die("这是本机自己的设备 ID，不能和自己配对")

    name = args.name or ("peer-" + did[-7:])

    section("添加对端（幂等）")
    if did in st.devices():
        info("对端已在设备表里，跳过")
    elif not args.apply:
        info("演练：将添加设备 %s（名称 %s）" % (did, name))
    else:
        st.add_device(did, name)
        ok("已添加对端设备 %s" % name)

    section("共享同步夹")
    registered_folders = st.folders() if st.available else {}
    share_targets = registered_folders or {fid: None for fid, *_ in folder_table()}
    for fid in share_targets:
        label = (registered_folders.get(fid) or {}).get("label", fid)
        if not args.apply:
            info("演练：将把 %s（%s）共享给对端" % (fid, label))
            continue
        try:
            done, why = st.share_folder(fid, did)
            (ok if done else info)("%-22s %s" % (fid, why))
        except Exception as e:
            bad("%-22s 失败：%s" % (fid, e))

    if not args.apply:
        out()
        info("演练结束。加 %s--apply%s 真正执行。" % (C.B, C.R))
    else:
        out()
        ok("已添加对端并共享同步夹（未改动 peer_id、未删任何对端）。")
        info("对端也需把自己这边的设备表加上你：用 `wbsync accept-peer --device <你的ID> --apply`，")
        info("或在对端跑 `wbsync pair <你的ID> --apply`。双向都完成后同步开始。")
    return 0


def _forget_peer(cfg, st=None):
    """清除「旧对端」的完整痕迹：config 里的 peer_id/peer_name/dead_ids + Syncthing 设备表里的旧条目。

    返回被移除的设备 ID 列表（用于提示）。st 为 Syncthing 实例，传 None 则跳过设备表清理。
    """
    removed = []
    old_ids = []
    if cfg.get("peer_id"):
        old_ids.append(cfg["peer_id"])
    for i in cfg.get("dead_ids") or []:
        if i not in old_ids:
            old_ids.append(i)

    st = st or Syncthing()
    if st.available:
        my = st.my_id()
        for did in old_ids:
            # 只删「确实存在于设备表」的旧条目，且绝不删本机自己
            if did and did != my and did in st.devices():
                try:
                    st.remove_device(did)
                    removed.append(did)
                except Exception:
                    pass

    cfg["peer_id"] = ""
    cfg["peer_name"] = ""
    cfg["dead_ids"] = []
    return removed


def cmd_forget(args):
    """清空 config 里记住的对端设备 ID（peer_id/peer_name/dead_ids），
    并把它们在 Syncthing 设备表里的旧条目一并移除。

    场景：对端机器重建了 Syncthing、设备 ID 变了，本机 config 里还存着旧 ID、
    设备表里也挂着旧条目，导致配对/连接对不上。打包分发 skill、或更换对端设备时，
    用它一键清掉「深处记忆」+ 设备表残留。
    """
    head("清除已记住的对端设备 ID")
    cfg = load_config(quiet=True)
    had = bool(cfg.get("peer_id")) or bool(cfg.get("dead_ids"))
    section("将清除")
    info("  peer_id   %s" % (cfg.get("peer_id") or "（空）"))
    info("  peer_name %s" % (cfg.get("peer_name") or "（空）"))
    info("  dead_ids  %s" % (cfg.get("dead_ids") or []))
    if not had:
        ok("config 里没有记住的对端 ID，无需清除。")
        return 0

    if not args.apply:
        out()
        info("演练模式：未清除。加 %s--apply%s 真正清除。" % (C.B, C.R))
        return 0

    # P1 闸门：清掉记住的对端 ID + 设备表旧条目，会让在役对端直接断联 → 需放行票
    _stop_guard("forget --apply（清除对端设备 ID）")
    _require_authz("forget", "", "清除记住的对端设备 ID")

    removed = _forget_peer(cfg)
    save_config(cfg, True)
    ok("已清空记住的对端设备 ID。")
    if removed:
        for did in removed:
            info("  已从 Syncthing 设备表移除旧对端 %s" % did[:7] + "...")
    return 0


def cmd_doctor(args):
    head("全面体检")
    problems = []
    fixes = []

    section("1. 登记层（最容易静默出错的地方）")
    r1 = heal_installed_plugins(False)
    if r1["fixed"] or r1["missing"]:
        problems.append("登记层有 %d 条路径失效" % (len(r1["fixed"]) + len(r1["missing"])))
        fixes.append("wbsync expert --apply")
        for key, old, new in r1["fixed"][:5]:
            warn("%s 指向别处" % key)
    else:
        ok("登记路径正常")

    drift = version_drift_report()
    if drift:
        problems.append("有 %d 个专家版本漂移" % len(drift))
        for name, sv, rv, why in drift:
            warn("%s：%s" % (name, why))
    else:
        ok("没有版本漂移")

    section("2. 同步引擎")
    b = find_syncthing_bin()
    if not b:
        problems.append("Syncthing 未安装")
        fixes.append("wbsync install --apply")
        bad("没有 Syncthing")
    else:
        ok("已安装 %s" % b)
    shome = syncthing_home_effective()
    if not IS_WIN and b:
        if not os.access(str(b), os.X_OK):
            problems.append("Syncthing 没有可执行权限")
            fixes.append("chmod +x %s" % b)
            bad("没有可执行权限 —— 这正是原 Windows 版在 Mac 上静默失败的一类问题")

    if port_open():
        ok("管理端口有响应")
    else:
        problems.append("Syncthing 没有在跑")
        fixes.append("wbsync start")
        warn("管理端口无响应")

    procs = find_processes("syncthing")
    if procs is None:
        warn("无法枚举进程（本环境权限受限）—— 不能据此判断是否在跑")
    elif procs:
        ok("进程存活：%d 个（PID %s）"
           % (len(procs), ", ".join(str(p[0]) for p in procs[:5])))
        for pid, _, _ in procs[:5]:
            if not pid_alive(pid):
                bad("PID %d 报告为不存活 —— 探测有误" % pid)
                problems.append("进程存活探测异常")
    elif b and port_open():
        warn("没看到进程，但端口有响应（可能是权限限制，不影响使用）")

    section("3. 同步夹")
    st = Syncthing()
    reg = st.folders() if st.available else {}
    for fid, label, p, exists in folder_table():
        if not exists:
            problems.append("同步夹缺失：%s" % label)
            fixes.append("wbsync setup --apply")
            warn("%s目录不存在 %s" % (wpad(label, 16), p))
        elif fid not in reg:
            problems.append("同步夹未注册：%s" % label)
            fixes.append("wbsync setup --apply")
            warn("%s未注册进 Syncthing" % wpad(label, 16))
        else:
            devs = [d.get("deviceID") for d in (reg[fid].get("devices") or [])]
            others = [d for d in devs if d != st.my_id()]
            ok("%s正常，共享给 %d 台对端" % (wpad(label, 16), len(others)))
            if st.available:
                ds = st.db_status(fid)
                if ds and ds.get("errors"):
                    warn("      %s 有 %d 个同步错误" % (label, ds["errors"]))

    section("4. 配对状态")
    if st.available:
        others = [d for d in st.devices() if d != st.my_id()]
        if others:
            ok("已配对 %d 台" % len(others))
            try:
                conns = st.api("GET", "/rest/system/connections").get("connections") or {}
                for d in others:
                    c = conns.get(d) or {}
                    state = "在线" if c.get("connected") else "离线"
                    (ok if c.get("connected") else warn)("%s  %s" % (d[:20] + "…", state))
            except Exception:
                pass
        else:
            problems.append("还没和任何对端配对")
            fixes.append("wbsync id 取本机 ID，到对端跑 wbsync pair")
            warn("没有对端")

        # 设备表残留检测：config 里记的 peer_id 若已不在设备表里，或设备表里有
        # 一个「config 不再引用」的陌生旧设备，提示清理（预防「对端重建 ID」后旧条目残留）。
        cfg = load_config(quiet=True)
        cfg_peer = (cfg.get("peer_id") or "").strip()
        if cfg_peer and cfg_peer not in st.devices():
            warn("config 记住的对端 %s 已不在设备表里，建议重新 wbsync pair" % (cfg_peer[:20] + "…"))

    section("5. 随 WB 会话启停（hook）")
    _hook_status_probe()

    # 5.1 时间命名空壳预警（平台每次跑 skill 遗弃的临时目录，不碰库、可安全清理）
    _shells = _time_shell_dirs()
    if _shells:
        warn("发现 %d 个时间命名空壳目录（平台遗弃，未登记进库，可安全清理）：" % len(_shells))
        for _s in _shells[:10]:
            info("  %s" % _s)
        if len(_shells) > 10:
            info("  …… 还有 %d 个" % (len(_shells) - 10))
        problems.append("有 %d 个时间命名空壳目录待清理" % len(_shells))
        fixes.append("wbsync workspace cleanup --apply")

    # 5.2 他机目录保护（Syncthing trashcan 版本控制硬闸门）
    section("5.2 他机目录保护（guard 闸门）")
    _my = _my_suffix_short()
    if _my:
        _peers = _ws_peer_dirs(_my)
        _st = _st_client()
        _g_on = 0
        _g_tot = 0
        if _st and _st.available:
            _fids = _guard_target_folders(_st)
            _g_tot = len(_fids)
            for _fid in _fids:
                _v = _folder_versioning(_st, _fid) or {}
                if (_v.get("type") or "").strip() == "trashcan":
                    _g_on += 1
        if _g_tot and _g_on < _g_tot:
            warn("未全量启用 guard：%d/%d 个同步夹未开 trashcan 版本控制，误删（含三机传播删）不可恢复。" % (_g_tot - _g_on, _g_tot))
            problems.append("未全量启用他机目录保护（guard）")
            fixes.append("wbsync guard on --apply")
            if _peers:
                info("他机目录：%s" % "、".join("%s→%s" % (s, p) for s, p in _peers))
        elif _g_tot and _g_on == _g_tot:
            ok("已启用 guard：%d 个同步夹全部开启 trashcan 版本控制（删除可恢复）" % _g_on)
        else:
            info("未发现他机目录或 Syncthing 不可用，跳过 guard 检查。")
    else:
        info("无法确定本机后缀，跳过 guard 检查（先 wbsync id / workspace init）。")

    section("6. 登录自启")
    if b:
        on, why = autostart_status(b, shome)
        if on:
            ok(why)
        else:
            problems.append("登录自启未登记")
            fixes.append("wbsync autostart on --apply")
            warn(why)
    else:
        info("Syncthing 未装，跳过")

    section("7. 授权闸门 / STOP 刹车（P1）")
    if _stop_active():
        warn("🛑 STOP 生效中：写操作与高危动作全部拒绝（只读不受影响）；解除 `wbsync brake --off`")
        problems.append("STOP 刹车生效中")
    else:
        ok("🔓 无刹车（正常）")
    try:
        _tk = sorted(_authz_dir().glob("*.json"))
    except Exception:
        _tk = []
    if _tk:
        info("未消费的放行票 %d 张（TTL %d 秒，一次性）" % (len(_tk), AUTHZ_TTL_DEFAULT))
    else:
        ok("无待用放行票")

    section("结论")
    if not problems:
        ok("没发现问题。同步链路完整。")
        return 0
    warn("发现 %d 个问题" % len(problems))
    for p in problems:
        out("      · %s" % p)
    if fixes:
        out()
        info("建议按序执行：")
        seen = []
        for f in fixes:
            if f not in seen:
                seen.append(f)
        for i, f in enumerate(seen, 1):
            out("      %d. %s" % (i, f))
    if args.json:
        out(json.dumps({"problems": problems, "fixes": fixes},
                       ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args):
    """连接后自检：①连接是否建立 ②带序号工作空间是否建立 ③是否同步。

    输出结构化结果（含 JSON），供「以 A 为主」的 Agent 判断该让用户/对端做什么。
    三件事都健康 → 返回 0；有缺口 → 非 0 且列出待办。
    """
    head("多机同步连接自检")
    st = Syncthing()
    report = {
        "my_suffix": None,
        "connection": {},
        "workspaces": {},
        "issues": [],
    }

    my_id = _my_device_id()
    reg = _read_ws_registry()
    my_suffix = _ws_suffix_of_device(reg, my_id) if my_id else None
    report["my_suffix"] = my_suffix
    section("本机")
    info("后缀  %s" % (my_suffix or "（未分配）"))
    info("设备  %s" % (my_id[:20] + "…" if my_id else "（未知）"))

    # 1) 连接是否建立
    section("① 连接状态")
    devices = st.devices() if st.available else {}
    others = [d for d in devices if d != my_id]
    if not others:
        report["issues"].append("尚未与任何对端配对")
        warn("没有对端设备 —— 需先配对")
    else:
        try:
            conns = st.api("GET", "/rest/system/connections").get("connections") or {}
        except Exception:
            conns = {}
        for d in others:
            c = conns.get(d) or {}
            connected = bool(c.get("connected"))
            entry = {"deviceID": d, "connected": connected,
                     "address": c.get("address", "")}
            # 从登记表反查后缀名
            suffix = _ws_suffix_of_device(reg, d) or "?"
            entry["suffix"] = suffix
            report["connection"][d[:8]] = entry
            (ok if connected else warn)("%s机  %s  %s"
                                        % (suffix, "在线" if connected else "离线",
                                           c.get("address", "") or ""))
            if not connected:
                report["issues"].append("对端 %s 机离线，未建立连接" % suffix)

    # 2) 带序号工作空间是否建立
    section("② 工作空间（sync-all-A/B/C/D）")
    machines = reg.get("machines", {})
    if not machines:
        report["issues"].append("登记表里没有任何机器")
        warn("工作空间登记表为空")

    # 本机 sync-workspace-all 夹的落地目录（内容随同步夹 id 统一，对端内容会以 <后缀>/ 子目录出现）
    my_local_ws_dir = _ws_local_dir(reg, my_suffix) if my_suffix else None

    for suffix in sorted(machines.keys()):
        m = machines[suffix]
        did = m.get("deviceID", "")
        # 判断「该机器的工作空间是否就位」：
        #  - 本机自己：看本机 workspace 目录（sync-all-<本机后缀>）是否存在；
        #  - 对端机器：看本机 sync-workspace-all 夹里是否已同步出 <对端后缀>/ 子目录。
        if suffix == my_suffix:
            exists = bool(my_local_ws_dir and my_local_ws_dir.exists())
        else:
            exists = bool(my_local_ws_dir and (my_local_ws_dir / suffix).exists())
        # 该机器是否在线
        online = False
        if st.available and did:
            try:
                conns = st.api("GET", "/rest/system/connections").get("connections") or {}
                online = bool((conns.get(did) or {}).get("connected"))
            except Exception:
                pass
        ws = {"suffix": suffix, "hostname": m.get("hostname", ""),
              "deviceID": did[:8] + "…" if did else "", "online": online,
              "local_dir_exists": exists}
        report["workspaces"][suffix] = ws
        mark = "✓" if exists else "✗"
        (ok if exists else warn)("%s机  %s  %s%s"
                                 % (suffix, m.get("hostname", ""),
                                    "在线" if online else "离线", "  " + mark))
        if not exists:
            report["issues"].append("%s 机的工作空间目录未建立" % suffix)

    # 3) 是否同步（sync-workspace-all 夹的对端完成度）
    section("③ 工作空间同步（sync-workspace-all）")
    ws_folder = "sync-workspace-all"
    if st.available and ws_folder in st.folders():
        for d in others:
            suffix = _ws_suffix_of_device(reg, d) or "?"
            try:
                comp = st.api("GET", "/rest/db/completion?folder=%s&device=%s"
                              % (ws_folder, d)) or {}
            except Exception:
                comp = {}
            rstate = comp.get("remoteState", "")
            completion = comp.get("completion", 0)
            ok_ = (rstate == "valid" and completion >= 100)
            (ok if ok_ else warn)("%s机  remoteState=%s  completion=%s%%"
                                  % (suffix, rstate, completion))
            if rstate == "notSharing":
                report["issues"].append("%s 机未把 sync-workspace-all 共享回本机" % suffix)
            elif not ok_:
                report["issues"].append("%s 机工作空间未同步完成（%s%%）" % (suffix, completion))
    else:
        info("sync-workspace-all 夹尚未注册（本机还没建工作空间）")
        report["issues"].append("本机未建立工作空间 sync-workspace-all")

    # 汇总
    out()
    if args.json:
        out(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report["issues"] else 1

    if not report["issues"]:
        ok("自检通过：连接、工作空间、同步三者都健康。")
        return 0
    warn("发现 %d 个待办：" % len(report["issues"]))
    for it in report["issues"]:
        out("      · %s" % it)
    return 1


def cmd_brief(args):
    """精简状态摘要：一条命令拿全「引擎/对端/A2A 待办/同步」信息。

    目的是**降低 Agent 自检成本**——不再需要跑多条命令（查连接、查目录、查任务……）。
    """
    my = _my_suffix_short()
    gen = _ws_my_generation() if my else 1
    _dialogue_announce()
    lines = []
    st = Syncthing()
    running = False
    conns = {}
    try:
        if st.available:
            st.api("GET", "/rest/noauth/health", timeout=4)
            running = True
            conns = (st.api("GET", "/rest/system/connections", timeout=6) or {}).get("connections", {})
    except Exception:
        pass

    devname = {}
    try:
        for did, d in (st.devices() or {}).items():
            devname[did] = d.get("name") or did[:7]
    except Exception:
        pass
    me = st.my_id() if running else ""
    online, offline = [], []
    for did, name in devname.items():
        if did == me:
            continue
        (online if (conns.get(did) or {}).get("connected") else offline).append(name)

    tasks_inc, tasks_out = [], []
    try:
        for t in _a2a_all_tasks():
            if t.get("to") in (my, "全员", "all") and t.get("state") not in A2A_TERMINAL_STATES:
                tasks_inc.append(t)
            if t.get("from") == my and t.get("state") not in A2A_TERMINAL_STATES:
                tasks_out.append(t)
    except Exception:
        pass

    if getattr(args, "json", False):
        out(json.dumps({"my_suffix": my, "engine_running": running,
                        "online": online, "offline": offline,
                        "a2a_incoming": len(tasks_inc), "a2a_outgoing": len(tasks_out)},
                       ensure_ascii=False))
        return 0

    head("多机同步状态")
    _context_note()                       # 省 token：本对话过长时提醒收口（常态零行）
    _fat_log_note()                       # 省 token：每日日志膨胀时提醒蒸馏（常态零行）
    _drift_note()                         # #12：队列 open 但 changelog 已修 → 一行提醒（常态零行）
    out("  本机 %s 机（第%d代）   |   引擎 Syncthing %s" % (my or "?", gen, "运行中" if running else "未运行"))
    try:
        _mach = sorted((_read_ws_registry() or {}).get("machines", {}) or {})
    except Exception:
        _mach = []
    out("  ⚠️「在册」≠「在线」：在册 %d 台（%s）｜ 同步在线 %s%s" % (
        len(_mach), "/".join(_mach) or "?",
        ", ".join(online) or "无", ("  离线 " + ", ".join(offline)) if offline else ""))
    try:
        fs = []
        # ③ 健壮性：遍历 Syncthing 实际配置的同步夹，不再写死 folder id（旧版 id 不同会静默跳过）。
        for fid in (list((st.folders() or {}).keys()) if running else []):
            d = st.db_status(fid) or {}
            fs.append("%s e=%s n=%s" % (fid, d.get("errors"), d.get("needFiles")))
        out("  同步 " + " | ".join(fs))
    except Exception:
        pass
    out("  A2A 待我处理 %d / 待对方确认 %d" % (len(tasks_inc), len(tasks_out)))
    _ro_n, _ro_last, _ro_leg = _reviews_open()
    if _ro_n:
        out("  未处置报告 %d 份（state: open）  最近：%s%s"
            % (_ro_n, _ro_last, ("；另有 %d 份无 state 头" % _ro_leg) if _ro_leg else ""))
    for t in tasks_inc[:3]:
        txt = ((t.get("messages") or [""])[0] or "")[:40]
        out("     ← %s  %s  %s: %s" % (t.get("id"), _a2a_dir_tag(t), t.get("skill"), txt))
    for t in tasks_out[:3]:
        out("     → %s  %s  skill=%s  [%s]" % (t.get("id"), _a2a_dir_tag(t), t.get("skill"), t.get("state")))
    # #10：超时提醒（**只算不写**；`--full` 显示全部）
    _od = _a2a_overdue()
    if _od:
        _lim = len(_od) if getattr(args, "full", False) else 3
        warn("  ⚠️ 超时任务 %d 条（只提示、不自动重发）" % len(_od))
        for _t, _age, _lm in _od[:_lim]:
            out("     ⏰ %s  %s  %s：%d 分钟未动（阈值 %d）"
                % (_t.get("id"), _a2a_dir_tag(_t), _t.get("skill"), _age, _lm))
        if len(_od) > _lim:
            out("     …（另有 %d 条，`wake --full` 看全）" % (len(_od) - _lim))
    return 0


def _wake_state_line(peer, ws, maxlen=72):
    """读对端状态卡片 `STATE-<peer>.md`，抽一行摘要（降 token：只读前 12 行）。"""
    if not ws:
        return ""
    p = ws / peer / "memory" / ("STATE-%s.md" % peer)
    try:
        if not p.exists():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    pick = ""
    for key in ("刚做完", "最新", "状态", "待办", "待我处理"):
        for ln in lines[:12]:
            s = ln.strip().lstrip("-").strip()
            if s.startswith(key):
                pick = s
                break
        if pick:
            break
    if not pick:
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#"):
                pick = s
                break
    return re.sub(r"^\W+", "", pick)[:maxlen]


def _wake_pending_peers(st, tasks, me, my_suffix=None):
    """找出「对端已声明、本机设备表却没有」的 deviceID —— 即**待反向配对的重入对端**。

    场景：某机重装 / 换 deviceID 后重入（C-g2 2026-10-14 实例）。Syncthing 配对是双向的，
    对端已把本机加进设备表，但本机还不知道它的新 ID → 连接被拒。原靠人工多轮排查
    （a2a/comms check + curl REST + 读 config.xml），token 消耗大。

    token 安全（B 机提案 2026-10-15 的红线）：
      - **只比对、只输出差集**：常态 0 行额外输出，有 pending 才 +1 行/对端。
      - **不读资料库登记表**：新 ID 直接取自待处理 A2A 任务的 `params.deviceID`
        （重入机派发时自带）。
    返回 [{"peer": 发起方后缀, "device_id": 完整ID, "short": 前12位, "task_id": 任务ID}]，按 ID 去重排序。
    """
    if not (st and st.available):
        return []
    try:
        known = set((st.devices() or {}).keys())
    except Exception:
        return []
    pending = {}
    for t in (tasks or []):
        # ② 误报风险：只认「入组/重入广播」类的 verify 任务，避免任何带 deviceID 的普通
        #    任务（如 report / setup）被误当成待配对对端并触发自动 accept-peer。
        # ④ 边界：跳过本机自己发出的广播——Syncthing 未换新证前重跑本机 wake，
        #    会把自身新 id 当成「待反向配对」的对端（dev==me 过滤要等换新证后才生效，
        #    而任务里的 from 是本机后缀，故用 my_suffix 判定）。
        if t.get("skill") != "verify":
            continue
        if my_suffix is not None and t.get("from") == my_suffix:
            continue
        p = t.get("params") or {}
        dev = (p.get("deviceID") or p.get("device_id") or "").strip()
        if not dev or len(dev) < 20:
            continue
        if dev in known or dev == me:
            continue
        # 记住首个触发该 deviceID 的任务 id，供自动配对后闭环广播任务（见 ①）。
        pending.setdefault(dev, (t.get("from") or "?", t.get("id")))
    return [{"peer": peer, "device_id": dev, "short": dev[:12], "task_id": tid}
            for dev, (peer, tid) in sorted(pending.items())]


def _wake_ack_pending_peer(my, tid, peer, short):
    """① 自动反向配对后，闭环广播方的 verify 任务：在本机 journal 写一条 status=completed，
    让广播方（对端）下次 `wbsync a2a check` 能看到 ack；否则其「待对方确认」与接收方
    「待我处理」会永久 +1。只写本机自己的 journal（A2A 标准回执模式），不动对端文件。"""
    if not tid:
        return False
    try:
        _a2a_append_event(_a2a_journal_path(my or "?"), my, {
            "event": "status", "id": tid, "from": my, "state": "completed",
            "text": "已反向配对 %s…（%s 机声明）" % (short, peer),
            "at": _a2a_now(),
        })
        return True
    except Exception:
        return False


def cmd_wake(args):
    """一体化唤醒摘要：一条命令拿全「本机 + 同步健康 + A2A 待办 + 未读 + 对端状态卡片」。

    目的是**替代会话开始时的多次命令往返**（brief + comms check + a2a check + 读 STATE 卡片），
    输出控制在 ~20 行内。降 token 三件套之二（2026-10-14 A 机）。
    """
    my = _my_suffix_short()
    gen = _ws_my_generation() if my else 1
    _dialogue_announce()
    st = Syncthing()
    running = False
    conns = {}
    try:
        if st.available:
            st.api("GET", "/rest/noauth/health", timeout=4)
            running = True
            conns = (st.api("GET", "/rest/system/connections", timeout=6) or {}).get("connections", {})
    except Exception:
        pass

    devname = {}
    try:
        for did, d in (st.devices() or {}).items():
            devname[did] = d.get("name") or did[:7]
    except Exception:
        pass
    me = st.my_id() if running else ""
    online, offline = [], []
    for did, name in devname.items():
        if did == me:
            continue
        (online if (conns.get(did) or {}).get("connected") else offline).append(name)

    folders = []
    try:
        # ③ 健壮性：不再写死 folder id（旧版 id 不同会静默跳过），改为遍历 Syncthing
        #    实际配置的同步夹，逐个查健康；单夹缺字段/查失败不影响其它夹。
        _fids = list((st.folders() or {}).keys()) if running else []
        for fid in _fids:
            d = st.db_status(fid) or {}
            folders.append((fid, d.get("errors"), d.get("needFiles")))
    except Exception:
        pass

    tasks_inc, tasks_out = [], []
    try:
        for t in _a2a_all_tasks():
            if t.get("to") in (my, "全员", "all") and t.get("state") not in A2A_TERMINAL_STATES:
                tasks_inc.append(t)
            if t.get("from") == my and t.get("state") not in A2A_TERMINAL_STATES:
                tasks_out.append(t)
    except Exception:
        pass

    # #15：顺手刷一次本机看板快照（内容没变则不写；引擎健康/告警只有 wake 侧能拿到）
    try:
        _board_touch("wake", state="online", running=running, folders=folders)
        _board_ensure_views()      # 「一开 skill 就建立看板」：主维护机补齐 md/html
    except Exception:
        pass

    # 待反向配对的对端（重入机声明了新 deviceID、本机设备表还没有）
    pending_peers = []
    try:
        pending_peers = _wake_pending_peers(st, tasks_inc, me, my)
    except Exception:
        pending_peers = []
    if pending_peers and getattr(args, "apply", False):
        for pp in pending_peers:
            try:
                _accept_peer_apply(st, pp["device_id"], "peer-%s" % pp["short"][:7])
                out("  ✓ 已自动反向配对：%s…（%s 机声明）" % (pp["short"], pp["peer"]))
                # ① 闭环：自动配对成功后，把广播方的 verify 任务标 completed（写本机 journal），
                #    否则广播方「待对方确认」与接收方「待我处理」永久 +1。
                if _wake_ack_pending_peer(my, pp.get("task_id"), pp["peer"], pp["short"]):
                    out("  ✓ 已回执广播方 verify 任务 %s（待对方确认可消）" % (pp.get("task_id")))
            except Exception as e:
                out("  ! 自动反向配对失败 %s…：%s" % (pp["short"], e))
        try:
            pending_peers = _wake_pending_peers(st, tasks_inc, me, my)
        except Exception:
            pending_peers = []

    try:
        comms_rows, _ = _comms_peer_status()
    except Exception:
        comms_rows = []
    try:
        mem_rows, _ = _mem_peer_status()
    except Exception:
        mem_rows = []

    # 对端状态卡片
    ws = None
    try:
        ws = _ws_local_dir(_read_ws_registry(), my)
    except Exception:
        ws = None
    want_peers = (getattr(args, "peers", None) or "").strip().upper()
    peer_set = {p.strip() for p in want_peers.split(",") if p.strip()} if want_peers else set()
    ws_peers = set()
    if ws and ws.exists():
        for p in ws.iterdir():
            # 只认后缀目录（A/B/C/D…），跳过 .stfolder / .workbuddy 这类隐藏目录
            if p.is_dir() and not p.name.startswith(".") and re.match(r"^[A-Za-z0-9]{1,4}$", p.name):
                ws_peers.add(p.name.upper())
    peers = sorted({r["peer"] for r in comms_rows} | {r["peer"] for r in mem_rows} | ws_peers)
    states = []
    for peer in peers:
        if peer == my or (peer_set and peer not in peer_set):
            continue
        states.append((peer, _wake_state_line(peer, ws)))

    if getattr(args, "json", False):
        out(json.dumps({
            "my_suffix": my, "engine_running": running,
            "online": online, "offline": offline,
            "folders": [{"id": f, "errors": e, "need_files": n} for f, e, n in folders],
            "a2a_incoming": [{"id": t.get("id"), "skill": t.get("skill"),
                              "from": t.get("from"), "state": t.get("state")} for t in tasks_inc],
            "a2a_outgoing": [{"id": t.get("id"), "to": t.get("to"), "skill": t.get("skill")} for t in tasks_out],
            "comms": [{"peer": r["peer"], "unread": r["unread"], "initialized": r["initialized"]} for r in comms_rows],
            "mem": [{"peer": r["peer"], "unread": r["unread"], "initialized": r["initialized"]} for r in mem_rows],
            "peer_states": [{"peer": p, "state": s} for p, s in states],
            "pending_peers": pending_peers,
        }, ensure_ascii=False))
        return 0

    head("多机唤醒摘要")
    out("  本机 %s 机（第%d代）   |   引擎 Syncthing %s" % (my or "?", gen, "运行中" if running else "未运行"))
    try:
        _mach = sorted((_read_ws_registry() or {}).get("machines", {}) or {})
    except Exception:
        _mach = []
    out("  ⚠️「在册」≠「在线」：在册 %d 台（%s）｜ 同步在线 %s%s" % (
        len(_mach), "/".join(_mach) or "?",
        ", ".join(online) or "无", ("  离线 " + ", ".join(offline)) if offline else ""))
    if folders:
        out("  同步 " + " | ".join("%s e=%s n=%s" % (f, e, n) for f, e, n in folders))
    out("  A2A 待我处理 %d / 待对方确认 %d" % (len(tasks_inc), len(tasks_out)))
    _ro_n, _ro_last, _ro_leg = _reviews_open()
    if _ro_n:
        out("  未处置报告 %d 份（state: open）  最近：%s%s"
            % (_ro_n, _ro_last, ("；另有 %d 份无 state 头" % _ro_leg) if _ro_leg else ""))
    if _stop_active():
        warn("  🛑 STOP 刹车生效中：A2A 写操作与高危动作全部拒绝（只读不受影响）")
    _p0 = [t for t in tasks_inc if (t.get("priority") or "").upper() == "P0"]
    if _p0:
        warn("  ⚠️ P0 未回执 %d 条：%s" % (len(_p0), "、".join(str(t.get("id")) for t in _p0[:3])))
    _gated = [t for t in tasks_inc if (t.get("requires") or "none").lower() == A2A_GATED_REQUIRES]
    if _gated:
        info("  🔒 高危任务 %d 条（requires=delete）：认领前需人签票 `wbsync authorize a2a-task --peer <ID> --apply`"
             % len(_gated))
    for t in tasks_inc[:3]:
        txt = ((t.get("messages") or [""])[-1] or "")[:40]
        out("     ← %s  %s  %s: %s" % (t.get("id"), _a2a_dir_tag(t), t.get("skill"), txt))
    for t in tasks_out[:3]:
        out("     → %s  %s  skill=%s  [%s]" % (t.get("id"), _a2a_dir_tag(t), t.get("skill"), t.get("state")))
    # #10：超时提醒（**只算不写**；`--full` 显示全部）
    _od = _a2a_overdue()
    if _od:
        _lim = len(_od) if getattr(args, "full", False) else 3
        warn("  ⚠️ 超时任务 %d 条（只提示、不自动重发）" % len(_od))
        for _t, _age, _lm in _od[:_lim]:
            out("     ⏰ %s  %s  %s：%d 分钟未动（阈值 %d）"
                % (_t.get("id"), _a2a_dir_tag(_t), _t.get("skill"), _age, _lm))
        if len(_od) > _lim:
            out("     …（另有 %d 条，`wake --full` 看全）" % (len(_od) - _lim))

    if comms_rows:
        tip = " / ".join("%s %d条%s" % (r["peer"], r["unread"], "" if r["initialized"] else "(未初始化)")
                         for r in comms_rows)
        out("  未读专线 %s  → wbsync comms read" % tip)
    if mem_rows:
        tip = " / ".join("%s %d段%s" % (r["peer"], r["unread"], "" if r["initialized"] else "(未初始化)")
                         for r in mem_rows)
        out("  未读记忆 %s  → wbsync mem read" % tip)
    for peer, sline in states:
        out("  %s 机状态：%s" % (peer, sline or "(无状态卡片)"))
    for pp in pending_peers:
        out("  ⚠️ %s 机声明新设备ID %s… 但本机设备表无 → wbsync accept-peer --device %s --apply"
            % (pp["peer"], pp["short"], pp["device_id"]))
    return 0


def cmd_fixnames(args):
    """补改名：Syncthing 下载完成后若无法把 `.syncthing.<name>.tmp` 改名为正式文件
    （本机 Syncthing 进程被限制 rename 时的兜底），由本命令代劳。

    扫描各同步夹，把「已存在且已停止写入（mtime 超过阈值）的 .syncthing.*.tmp」
    改名为去掉 `.syncthing.` 前缀与 `.tmp` 后缀的正式文件名。默认执行；--dry-run 只列不改。
    """
    head("补改名（Syncthing 未完成的 .tmp → 正式文件）")
    st = Syncthing()
    paths = []
    if st.available:
        try:
            for fid, fcfg in (st.folders() or {}).items():
                p = fcfg.get("path")
                if p:
                    paths.append(Path(p))
        except Exception:
            pass
    if not paths:
        # 回退：常见同步根
        paths = [cfg_sync_root() / "coop", cfg_sync_root() / "workspaces",
                 Path.home() / ".workbuddy" / "skills",
                 Path.home() / ".workbuddy" / "experts"]
    min_age = getattr(args, "min_age", 5)
    if min_age is None:
        min_age = 5
    now = time.time()
    fixed, skipped = [], []
    for root in paths:
        try:
            if not root.exists():
                continue
        except Exception:
            continue
        try:
            tmps = list(root.rglob(".syncthing.*.tmp"))
        except Exception:
            continue
        for tmp in tmps:
            try:
                if now - tmp.stat().st_mtime < min_age:
                    skipped.append(tmp)
                    continue
                name = tmp.name
                if not (name.startswith(".syncthing.") and name.endswith(".tmp")):
                    continue
                final = name[len(".syncthing."):-len(".tmp")]
                dst = tmp.parent / final
                if getattr(args, "dry_run", False):
                    fixed.append((tmp, dst))
                    continue
                os.replace(str(tmp), str(dst))
                fixed.append((tmp, dst))
            except Exception as e:
                bad("改名失败 %s：%s" % (tmp, e))
    for t, d in fixed:
        info("  %s → %s" % (t.name, d.name))

    # 注意：**不在此处删除任何文件**。删除会触发 WorkBuddy「文件安全守护」的授权弹窗；
    # 且本机 rename 受限时也删不掉。临时文件问题已在源头（atomic_write_json 直接写入）解决。
    ok("补改名 %d 个（跳过 %d 个可能仍在写入的）" % (len(fixed), len(skipped)))
    return 0


def cmd_autostart(args):
    head("登录自启")
    if args.action == "status":
        b = find_syncthing_bin()
        if not b:
            die("Syncthing 未安装，先跑 wbsync install")
        on, why = autostart_status(b, syncthing_home_effective())
        (ok if on else warn)(why)
        info("Syncthing：%s" % b)
        info("配置目录：%s" % syncthing_home_effective())
        if IS_MAC:
            info("登记位置：%s" % mac_plist_path())
        elif IS_WIN:
            info("登记位置：注册表 HKCU\\%s\\%s" % (WIN_RUN_KEY, WIN_RUN_NAME))
        else:
            info("登记位置：%s" % linux_unit_path())
        return 0

    b = find_syncthing_bin()
    if args.action == "on":
        if not b:
            die("Syncthing 未安装，先跑 wbsync install")
        autostart_enable(b, syncthing_home_effective(), args.apply)
        if not args.apply:
            out()
            info("加 %s--apply%s 真正写入" % (C.B, C.R))
        return 0

    if args.action == "off":
        autostart_disable(args.apply)
        if not args.apply:
            out()
            info("加 %s--apply%s 真正执行" % (C.B, C.R))
        return 0
    return 0


# --------------------------------------------------------------------------
# 网络连接：中继 / 全局发现 / 局域网发现 / AP 隔离诊断
# --------------------------------------------------------------------------
# 背景（2026-10-13 实战沉淀）：
#   1. Syncthing 官方公共中继池 relays.syncthing.net 与发现服务器 discovery.syncthing.net
#      在国内常被墙（TLS 握手超时），不能依赖。
#   2. 中继地址要写在 config.xml 的 options 里，且**多个地址必须用「重复的 <listenAddress>
#      标签」，不能用逗号/空格塞进同一个标签**——否则报 malformed listener URL，中继一个都挂不上。
#   3. 光配中继不够，必须同时打开 globalAnnounceEnabled（全局发现）：双方各自上报「我在哪个中继」，
#      对方经发现服务器查到后才握手。二者缺一，中继返回 not found / 发现 404。
#   4. 即便被 AP 隔离（有线↔无线二层不通），经「发现服务器撮合 + NAT 打洞」也可能建成 P2P 直连。

# 免费公共中继池（国内/亚太可达，实测 22067 端口通 + TLS 握手 CN=strelaysrv 验证通过）。
# 说明：公共中继节点动态变化、可用性无保证，仅作「能连上」的兜底；长期稳定请自建中继。
RELAY_POOL = [
    "relay://123.51.24.51:22067/?id=GTP6TJQ-QQSSRL4-XJOIHDV-ZQM3KDK-S2F73MC-C6WL3MD-V6VKIFH-VTPDGAB",
    "relay://180.149.44.55:22067/?id=S2CVKXC-4VATLVF-GR7XNKN-FG2PT2G-CIBF4IR-KEO3HCH-AQWOTNT-ENPO2QH",
    "relay://203.196.33.16:22067/?id=3I2Q6CE-QIRWGQW-CUMHI45-7WIMSVS-ZQ4BKAY-YTP2OOA-3EMOF6C-ZCC7QAY",
    "relay://103.214.6.110:22067/?id=BLGVNL7-ETCLXWH-5W4QBE6-NGXU7PB-DGOWCKX-LNOZHWN-QHEOK3O-U7S57Q2",
]


def _run_quiet(cmd, timeout=10):
    """跑一条命令，返回 (returncode, stdout, stderr)；任何异常都返回 (None, '', '')。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except Exception:
        return None, "", ""


def _collect_netinfo():
    """采集本机网络信息，供写入资料库登记表、后续机器查询与排查。

    返回 dict：包含 ipconfig/ifconfig 的原始文本 + 解析出的关键字段。
    三平台各取各的：Windows=ipconfig，macOS/Linux=ifconfig(+ip route)。
    """
    info_block = {"platform": PLATFORM_LABEL, "collectedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")}
    raw = ""
    if IS_WIN:
        _, raw, _ = _run_quiet(["ipconfig", "/all"])
        info_block["command"] = "ipconfig /all"
    else:
        _, raw, _ = _run_quiet(["ifconfig"])
        info_block["command"] = "ifconfig"

    info_block["raw"] = raw.strip() if raw else ""

    def _hex_mask_to_dotted(v):
        """把 0xffffff00 之类十六进制掩码转成 255.255.255.0。"""
        v = (v or "").strip()
        if v.lower().startswith("0x"):
            try:
                n = int(v, 16)
                if n == 0:
                    return v
                return ".".join(str((n >> (24 - i * 8)) & 0xFF) for i in range(4))
            except Exception:
                return v
        return v

    # 解析 IPv4 / 掩码 / 网关 / 网卡类型（尽力而为，解析不到就留空）
    ipv4s = []
    netmask = ""
    gateway = ""
    iface_type = ""
    # 用状态机跳过回环接口（lo0/lo/回环适配器），避免把 127.x 的掩码当成本机掩码
    cur_iface = ""
    for line in (raw or "").splitlines():
        s = line.strip()
        # 接口名（ifconfig 风格：en0: flags=... / lo0: flags=...）
        m_if = re.match(r"^([A-Za-z0-9]+):\s+flags=", s)
        if m_if:
            cur_iface = m_if.group(1)
        is_loop = cur_iface.lower().startswith("lo") or ("回环" in s)
        # IPv4 地址（排除回环接口与 127.x）
        m = re.search(r"\b(?:inet|IPv4|IP Address)[^\d]*(\d{1,3}(?:\.\d{1,3}){3})", s)
        if m and not m.group(1).startswith("127.") and not is_loop:
            ipv4s.append(m.group(1))
        # 掩码（排除回环接口）
        if not netmask:
            m2 = re.search(r"(?:netmask|Subnet Mask|子网掩码)[^\d]*([0-9a-fA-Fx.:]+)", s)
            if m2 and not is_loop:
                netmask = _hex_mask_to_dotted(m2.group(1))
        # 网卡类型：无线 / 有线（排除回环）
        if not iface_type and not is_loop:
            if re.search(r"wireless|wi-fi|wlan|802\.11|RTL8|WiFi", s, re.I):
                iface_type = "无线"
            elif re.search(r"ethernet|以太网|en\d+|eth\d+|Realtek PCIe GbE|GbE", s, re.I):
                iface_type = "有线"

    # 网关：ifconfig 不含网关，macOS 用 route，Linux 用 ip route，Windows 用 ipconfig（已在上方）
    if not gateway:
        if IS_MAC:
            _, r_net, _ = _run_quiet(["route", "-n", "get", "default"])
            m = re.search(r"gateway:\s*(\d{1,3}(?:\.\d{1,3}){3})", r_net or "")
            if m:
                gateway = m.group(1)
        elif IS_LINUX:
            _, r_net, _ = _run_quiet(["ip", "route"])
            m = re.search(r"default via (\d{1,3}(?:\.\d{1,3}){3})", r_net or "")
            if m:
                gateway = m.group(1)
    # Windows 网关从 ipconfig 里抓（默认网关 行）
    if not gateway:
        m3 = re.search(r"(?:默认网关|Default Gateway)[^\d]*(\d{1,3}(?:\.\d{1,3}){3})", raw or "")
        if m3:
            gateway = m3.group(1)

    info_block["ipv4"] = sorted(set(ipv4s))
    info_block["netmask"] = netmask
    info_block["gateway"] = gateway
    info_block["ifaceType"] = iface_type

    # 取第一个非回环 IPv4 作为 lanIP
    info_block["lanIP"] = info_block["ipv4"][0] if info_block["ipv4"] else ""

    # IPv6 是否有全局可路由地址（2xxx: 开头，且不是 ::1 / fe80 链路本地）
    has_gua = False
    for line in (raw or "").splitlines():
        m = re.search(r"\binet6\s+(2[0-9a-fA-F]{3}:[0-9a-fA-F:]+)", line)
        if m:
            has_gua = True
            break
    info_block["hasGlobalIPv6"] = has_gua

    return info_block


def _read_config_xml_path():
    return find_config_xml()


def _apply_network_config(relays, apply_flag, enable_global=True, enable_relay=True):
    """把中继地址 + 全局发现开关写进 config.xml。

    - relays: 要写入的中继 URI 列表（relay://...）
    - enable_global: 是否开启全局发现（globalAnnounceEnabled）
    - enable_relay: 是否开启中继（relaysEnabled）

    关键正确做法：中继地址**每个写一个独立的 <listenAddress> 标签**，
    与 <listenAddress>default</listenAddress> 并列，绝不能用逗号/空格拼进同一个标签。
    """
    cfg_path = _read_config_xml_path()
    if not cfg_path:
        return False, "找不到 config.xml（Syncthing 可能还没启动过）"

    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(cfg_path))
        root = tree.getroot()
        opts = root.find("options")
        if opts is None:
            return False, "config.xml 里没有 <options> 节点"
    except Exception as e:
        return False, "解析 config.xml 失败：%s" % e

    # 预览要做的改动
    changes = []
    if enable_global:
        ge = opts.find("globalAnnounceEnabled")
        if ge is None or (ge.text or "").strip().lower() != "true":
            changes.append("globalAnnounceEnabled → true（开启全局发现）")
    if enable_relay:
        re_ = opts.find("relaysEnabled")
        if re_ is None or (re_.text or "").strip().lower() != "true":
            changes.append("relaysEnabled → true（开启中继）")

    # 计算当前已有的 listenAddress 标签
    existing_la = [e.text.strip() if e.text else "" for e in opts.findall("listenAddress")]
    for r in relays:
        if r not in existing_la:
            changes.append("新增中继：%s" % r)

    if not changes:
        return True, "网络配置已是最新，无需改动"

    if not apply_flag:
        # 演练：只打印
        return True, "演练模式（未落盘）。将做 %d 处改动：%s" % (len(changes), "；".join(changes))

    # 落盘：备份 → 改 → 写回
    backup_file(cfg_path)
    try:
        import xml.etree.ElementTree as ET
        if enable_global:
            ge = opts.find("globalAnnounceEnabled")
            if ge is None:
                ge = ET.SubElement(opts, "globalAnnounceEnabled")
            ge.text = "true"
        if enable_relay:
            re_ = opts.find("relaysEnabled")
            if re_ is None:
                re_ = ET.SubElement(opts, "relaysEnabled")
            re_.text = "true"

        # 保证有一个 default 标签；然后为每个中继补一个独立标签
        has_default = any((e.text or "").strip() == "default" for e in opts.findall("listenAddress"))
        if not has_default:
            d = ET.Element("listenAddress")
            d.text = "default"
            opts.append(d)

        existing_la = [e.text.strip() if e.text else "" for e in opts.findall("listenAddress")]
        for r in relays:
            if r not in existing_la:
                e = ET.Element("listenAddress")
                e.text = r
                opts.append(e)

        tree.write(str(cfg_path), encoding="utf-8", xml_declaration=True)
        return True, "已写入 %d 处网络配置（中继 %d 个 + 全局发现/中继开关）" % (len(changes), len(relays))
    except Exception as e:
        return False, "写 config.xml 失败：%s" % e


def _probe_relay_reachability(relays, timeout=4):
    """逐个测中继 TCP 端口连通性（用 socket，不依赖 nc）。返回 [(uri, reachable), ...]。"""
    results = []
    for uri in relays:
        host, port = "", 22067
        m = re.match(r"relay://([^:/]+)(?::(\d+))?", uri)
        if m:
            host = m.group(1)
            if m.group(2):
                port = int(m.group(2))
        reachable = False
        if host:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    reachable = True
            except Exception:
                reachable = False
        results.append((uri, host, reachable))
    return results


def _check_ap_isolation():
    """启发式判断本机是否处于 AP 隔离 / 与对端二层不通。

    返回 (is_likely, evidence)：evidence 是给用户看的说明。
    依据：本机 IP 与网关同网段、能 ping 网关，但对端 IP 的 ARP 表项是 (incomplete)
    （或 ping 不通），而同一子网的其他机器能正常 ARP 应答。
    """
    # 拿本机默认网关
    gateway = ""
    if IS_MAC:
        _, out_, _ = _run_quiet(["route", "-n", "get", "default"])
        m = re.search(r"gateway:\s*(\d{1,3}(?:\.\d{1,3}){3})", out_ or "")
        if m:
            gateway = m.group(1)
    elif IS_LINUX:
        _, out_, _ = _run_quiet(["ip", "route"])
        m = re.search(r"default via (\d{1,3}(?:\.\d{1,3}){3})", out_ or "")
        if m:
            gateway = m.group(1)
    # 网关 ping 是否通（网关能通说明本机出网正常）
    gw_ok = False
    if gateway:
        if IS_WIN:
            _, out_, _ = _run_quiet(["ping", "-n", "2", "-w", "2000", gateway])
            gw_ok = "TTL=" in (out_ or "")
        else:
            _, out_, _ = _run_quiet(["ping", "-c", "2", "-t", "2", gateway])
            gw_ok = "0% packet loss" in (out_ or "") or ", 0.0% packet loss" in (out_ or "")

    return {
        "gateway": gateway,
        "gatewayReachable": gw_ok,
        "note": "网关可达 + 本机与对端同网段却互 ping 不通/ARP 表 (incomplete)，"
                "通常意味着无线 AP 隔离（有线↔无线被接入层隔离）或访客网络隔离。",
    }


def _hook_script_stale():
    """已装的 hook 脚本是否与内置版本不一致（wbsync 更新后需重装）。

    消化 C 反馈的「看门狗修复后须本机重装」痛点：本工具更新后，每台机器只需重跑
    `wbsync hook on --apply` 即可刷新脚本，settings.json 条目不变、无需重新 /hooks 确认。
    此函数让 doctor / status 能提示「脚本已过期」。
    """
    skill_dir = Path(__file__).resolve().parent.parent
    hook_src = skill_dir / "scripts" / "wbsync-autostart-hook.sh"
    hook_dst = WB_ROOT / "scripts" / "wbsync-autostart-hook.sh"
    if not hook_src.exists() or not hook_dst.exists():
        return False
    try:
        return hook_src.read_bytes() != hook_dst.read_bytes()
    except OSError:
        return False


def _hook_status_probe():
    """在 doctor 里输出 hook 状态：脚本是否已装、SessionStart hook 是否已写入 settings.json。

    只做只读探测，不写任何文件。用户是否在 /hooks 面板确认，程序无法探知，只做提示。
    """
    skill_dir = Path(__file__).resolve().parent.parent
    hook_dst = WB_ROOT / "scripts" / "wbsync-autostart-hook.sh"
    settings_path = WB_ROOT / "settings.json"

    installed = hook_dst.exists()
    written = False
    if settings_path.exists():
        try:
            d = json.loads(settings_path.read_text(encoding="utf-8"))
            ss = (d or {}).get("hooks", {}).get("SessionStart", [])
            for group in ss:
                for h in (group or {}).get("hooks", []):
                    if "wbsync-autostart-hook" in str(h.get("command", "")):
                        written = True
        except Exception:
            pass

    if installed and written:
        if _hook_script_stale():
            warn("hook 脚本已装、SessionStart 已写入，但【脚本已过期】—— 重跑 "
                 "wbsync hook on --apply 刷新（无需重新 /hooks 确认）")
        else:
            ok("hook 脚本已装、SessionStart 已写入（若尚未在 /hooks 确认，需确认一次才生效）")
    elif installed or written:
        warn("hook 状态不完整（脚本 %s / 写入 %s），建议 wbsync hook on --apply"
             % ("已装" if installed else "未装", "已写入" if written else "未写入"))
    else:
        info("未启用「随 WB 会话启停同步」（wbsync hook on --apply 可开启）")


def cmd_hook(args):
    """随 WorkBuddy 会话启停同步：打开对话自动启动 Syncthing、关闭 WB 自动停止。

    子命令：
      on     —— 把 skill 自带的 hook 脚本装到 ~/.workbuddy/scripts/，并写入 settings.json
              的 SessionStart hook（合并保留原字段）。
      off    —— 从 settings.json 移除本 skill 的 SessionStart hook（保留其他 hook）。
      status —— 查看当前 hook 配置状态。

    「关闭 WB 即停」靠脚本内置看门狗实现（WB 没有「退出应用」hook 事件）：
    检测 WB 主进程消失 → 约 30 秒后 wbsync stop。故无需在 settings.json 里挂 SessionEnd。
    """
    skill_dir = Path(__file__).resolve().parent.parent
    hook_src = skill_dir / "scripts" / "wbsync-autostart-hook.sh"
    scripts_dir = WB_ROOT / "scripts"
    hook_dst = scripts_dir / "wbsync-autostart-hook.sh"
    settings_path = WB_ROOT / "settings.json"

    action = getattr(args, "action", "status")

    if action == "status":
        head("随 WB 会话启停同步（hook）状态")
        info("脚本已装  %s" % ("是" if hook_dst.exists() else "否（未装）"))
        has_hook = False
        if settings_path.exists():
            try:
                d = json.loads(settings_path.read_text(encoding="utf-8"))
                hooks = (d or {}).get("hooks", {})
                ss = hooks.get("SessionStart", [])
                for group in ss:
                    for h in (group or {}).get("hooks", []):
                        if "wbsync-autostart-hook" in str(h.get("command", "")):
                            has_hook = True
            except Exception:
                pass
        info("SessionStart hook 已写入  %s" % ("是" if has_hook else "否"))
        if has_hook:
            if _hook_script_stale():
                warn("但【脚本已过期】：重跑 wbsync hook on --apply 刷新（无需重新 /hooks 确认）。")
            else:
                info("提示：想要「开关 WB 自动启停 Syncthing」的便利，可在 WB 输入 /hooks 对本 skill 点【确认】")
                info("（仅一次，可选；跳过也不影响核心同步，核心同步靠系统登录自启）。")
        return 0

    if action == "off":
        head("关闭随 WB 会话启停同步")
        if not settings_path.exists():
            ok("settings.json 不存在，无需处理。")
            return 0
        d = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = (d or {}).get("hooks", {})
        ss = hooks.get("SessionStart", [])
        kept = []
        removed_any = False
        for group in ss:
            hs = (group or {}).get("hooks", [])
            new_hs = [h for h in hs if "wbsync-autostart-hook" not in str(h.get("command", ""))]
            if len(new_hs) != len(hs):
                removed_any = True
            if new_hs:
                new_group = dict(group)
                new_group["hooks"] = new_hs
                kept.append(new_group)
        if removed_any:
            if kept:
                hooks["SessionStart"] = kept
            else:
                hooks.pop("SessionStart", None)
            d["hooks"] = hooks
            if not args.apply:
                info("演练：将从 settings.json 移除本 skill 的 SessionStart hook。")
                return 0
            backup_file(settings_path)
            atomic_write_json(str(settings_path), d)
            ok("已移除 SessionStart hook。")
        else:
            ok("settings.json 里没有本 skill 的 hook，无需移除。")
        return 0

    # action == "on"
    head("随 WB 会话启停同步（打开对话自启、关闭 WB 自动停）")

    # 1) 装 hook 脚本
    if not hook_src.exists():
        die("找不到 hook 脚本：%s" % hook_src)
    if not args.apply:
        info("演练：将安装脚本到 %s" % hook_dst)
    else:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(hook_src), str(hook_dst))
        try:
            os.chmod(hook_dst, 0o755)
        except OSError:
            pass
        ok("已安装 hook 脚本 %s" % hook_dst)

    # 2) 写 settings.json 的 SessionStart hook（合并保留原字段）
    d = {}
    if settings_path.exists():
        try:
            d = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            warn("settings.json 解析失败，将新建。")
            d = {}
    if not isinstance(d, dict):
        d = {}

    hook_cmd = str(hook_dst)
    new_entry = {"hooks": [{"type": "command", "command": hook_cmd}]}

    hooks = d.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    ss = hooks.get("SessionStart", [])
    if not isinstance(ss, list):
        ss = []

    # 幂等：已存在则跳过
    exists = any(
        any("wbsync-autostart-hook" in str(h.get("command", "")) for h in (g or {}).get("hooks", []))
        for g in ss
    )
    if exists:
        ok("SessionStart hook 已存在，跳过。")
    else:
        ss.append(new_entry)
        hooks["SessionStart"] = ss
        d["hooks"] = hooks
        if not args.apply:
            info("演练：将写入 SessionStart hook（command=%s）" % hook_cmd)
        else:
            backup_file(settings_path)
            atomic_write_json(str(settings_path), d)
            ok("已写入 SessionStart hook。")

    out()
    ok("Hook 已就绪：脚本装到 %s 并写入 settings.json 的 SessionStart。" % hook_dst)
    info("说明：核心同步靠系统登录自启（launchd / systemd），【不依赖】此 hook；")
    info("此 hook 仅提供「打开 WB 对话→自启 Syncthing / 关闭 WB→自动停」的便利。")
    info("想要该便利：在 WB 对话框输入 /hooks，对本 skill 的 SessionStart 点【确认】（仅一次，可选）。")
    info("脚本已更新时无需重新确认（settings 条目不变）。不想确认也可直接跳过，不影响同步。")
    return 0


# --------------------------------------------------------------------------
# 写护栏（PreToolUse）：把「同机多对话静默覆盖共享文件」变成「显式拦截」
#   与 `wbsync lock`（软锁）配套——lock 声明意图，wguard 把意图变成硬闸门。
# --------------------------------------------------------------------------

WGUARD_MARK = "wbsync-write-guard.sh"
WGUARD_POST_MARK = "wbsync-write-guard-post.sh"   # PostToolUse · 覆盖告知记账（对标借鉴 A）


def _wguard_groups(d):
    hooks = (d or {}).get("hooks", {}) if isinstance(d, dict) else {}
    pt = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
    return pt if isinstance(pt, list) else []


def _wguard_has(d, mark=WGUARD_MARK):
    for g in _wguard_groups(d):
        for h in (g or {}).get("hooks", []):
            if mark in str(h.get("command", "")):
                return True
    return False


def _wguard_stale(src, dst):
    try:
        return src.exists() and dst.exists() and src.read_bytes() != dst.read_bytes()
    except Exception:
        return False


def cmd_wguard(args):
    """写护栏（PreToolUse hook）：他人持锁时，阻止本机对话写共享文件。

    解决的问题：Syncthing 只在**跨设备**分歧时产生 `*.sync-conflict-*`；**同一台机器**上
    两个对话改同一份共享文件**不会有冲突副本，而是后写静默覆盖先写**。`wbsync lock` 是
    软约定（不守协议的对话照样覆盖），本护栏用平台的 PreToolUse 钩子把它变成硬闸门。

    判定（三条全中才阻止）：① 工具是 Write/Edit/MultiEdit/NotebookEdit；
    ② file_path 落在协作共享区（`~/WorkBuddySync/workspaces/*/`、`~/WorkBuddySync/coop/shared/`、
    skill 目录；可用 `WBSYNC_SHARED_ROOTS` 追加）；③ 存在**他人**持有且未过期的编辑锁。
    否则**静默放行**（exit 0、零输出 → 不消耗 token）。任何异常一律放行（fail-open），
    绝不因本护栏故障瘫痪文件写入。

    子命令：on（装脚本 + 写 settings.json）/ off（移除）/ status（查看）。
    已知限制：不拦用户在编辑器里的手工修改，也不拦 Bash 内的 `sed -i` 等。
    """
    skill_dir = Path(__file__).resolve().parent.parent
    guard_src = skill_dir / "scripts" / "wbsync-write-guard.sh"
    guard_post_src = skill_dir / "scripts" / "wbsync-write-guard-post.sh"
    scripts_dir = WB_ROOT / "scripts"
    guard_dst = scripts_dir / "wbsync-write-guard.sh"
    guard_post_dst = scripts_dir / "wbsync-write-guard-post.sh"
    settings_path = WB_ROOT / "settings.json"
    action = getattr(args, "action", "status")

    def _load():
        d = {}
        if settings_path.exists():
            try:
                d = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                warn("settings.json 解析失败，按空配置处理。")
                d = {}
        return d if isinstance(d, dict) else {}

    if action == "status":
        head("写护栏（PreToolUse 拦截 + PostToolUse 记账）状态")
        info("PreToolUse 脚本已装   %s" % ("是" if guard_dst.exists() else "否（未装）"))
        info("PostToolUse 脚本已装  %s" % ("是" if guard_post_dst.exists() else "否（未装）"))
        d = _load()
        has = _wguard_has(d)
        has_post = _hook_event_has(d, "PostToolUse", WGUARD_POST_MARK)
        info("PreToolUse hook 已写入   %s" % ("是" if has else "否"))
        info("PostToolUse hook 已写入  %s" % ("是" if has_post else "否"))
        if guard_dst.exists() and _wguard_stale(guard_src, guard_dst):
            warn("但【脚本已过期】：重跑 wbsync wguard on --apply 刷新（无需重新 /hooks 确认）。")
        elif has:
            ok("生效中：他人持锁 → 阻止写并提示对方；写后被改 → 下次写前给『覆盖提醒』（不阻断）。")
            info("卸载：wbsync wguard off --apply")
        return 0

    if action == "off":
        head("关闭写护栏")
        if not settings_path.exists():
            ok("settings.json 不存在，无需处理。")
            return 0
        d = _load()
        hooks = d.get("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}
        removed = False

        def _strip(event):
            nonlocal removed
            gs = hooks.get(event, [])
            if not isinstance(gs, list):
                gs = []
            kept = []
            for g in gs:
                hs = (g or {}).get("hooks", [])
                new_hs = [h for h in hs if WGUARD_MARK not in str(h.get("command", ""))
                          and WGUARD_POST_MARK not in str(h.get("command", ""))]
                if len(new_hs) != len(hs):
                    removed = True
                if new_hs:
                    ng = dict(g)
                    ng["hooks"] = new_hs
                    kept.append(ng)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)

        _strip("PreToolUse")
        _strip("PostToolUse")
        if not removed:
            ok("settings.json 里没有本 skill 的写护栏，无需移除。")
            return 0
        d["hooks"] = hooks
        if not args.apply:
            info("演练：将从 settings.json 移除写护栏（Pre + Post）。")
            return 0
        backup_file(settings_path)
        atomic_write_json(str(settings_path), d)
        ok("已移除写护栏（Pre + Post）。")
        return 0

    # action == "on"
    head("写护栏：同机多对话改共享文件时显式拦截（替代静默覆盖）")
    if not guard_src.exists():
        die("找不到护栏脚本：%s" % guard_src)
    if args.apply:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for _s, _dd in ((guard_src, guard_dst), (guard_post_src, guard_post_dst)):
            if _s.exists():
                shutil.copy2(str(_s), str(_dd))
                try:
                    os.chmod(_dd, 0o755)
                except OSError:
                    pass
        ok("已安装护栏脚本（Pre + Post）→ %s" % scripts_dir)
    else:
        info("演练：将安装 Pre/Post 护栏脚本到 %s" % scripts_dir)

    d = _load()
    hooks = d.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    changed = False
    pt = hooks.get("PreToolUse", [])
    if not isinstance(pt, list):
        pt = []
    if _wguard_has(d):
        ok("PreToolUse 写护栏已存在，跳过。")
    else:
        pt.append({"hooks": [{"type": "command", "command": str(guard_dst)}]})
        hooks["PreToolUse"] = pt
        changed = True
    po = hooks.get("PostToolUse", [])
    if not isinstance(po, list):
        po = []
    if _hook_event_has(d, "PostToolUse", WGUARD_POST_MARK):
        ok("PostToolUse 记账 hook 已存在，跳过。")
    elif guard_post_dst.exists() or not args.apply:
        po.append({"hooks": [{"type": "command", "command": str(guard_post_dst)}]})
        hooks["PostToolUse"] = po
        changed = True
    d["hooks"] = hooks
    if changed:
        if args.apply:
            backup_file(settings_path)
            atomic_write_json(str(settings_path), d)
            ok("已写入写护栏（PreToolUse + PostToolUse）。")
        else:
            info("演练：将写入 PreToolUse + PostToolUse hook")

    out()
    if args.apply:
        ok("写护栏已就绪。")
        info("效果：他人持锁时，本机写共享文件被阻止（exit 2）并回传对方身份/用途；无锁时静默放行。")
        info("配合：改共享文件前先 `wbsync lock claim \"<用途>\"`；改完 `wbsync lock release`。")
        info("卸载：wbsync wguard off --apply")
    return 0


# --------------------------------------------------------------------------
# 对话框生命周期 hook 注册（SessionStart / UserPromptSubmit / SessionEnd）
#   让「开着 / 已关闭」由显式事件判定，而不是猜文件时间。
# --------------------------------------------------------------------------

DLIFE_MARK = "wbsync-dialogue-lifecycle"
DLIFE_EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")


def _hook_event_groups(d, event):
    hooks = (d or {}).get("hooks", {}) if isinstance(d, dict) else {}
    g = hooks.get(event, []) if isinstance(hooks, dict) else []
    return g if isinstance(g, list) else []


def _hook_event_has(d, event, mark):
    for g in _hook_event_groups(d, event):
        for h in (g or {}).get("hooks", []):
            if mark in str(h.get("command", "")):
                return True
    return False


def cmd_dialogue_hook(args):
    """注册「对话框生命周期」hook：把每个对话框的 开着/已关闭 变成显式状态。

    为什么用 hook 而不是猜文件时间：`SessionStart` 明确表示开窗；`SessionEnd` 明确表示关窗
    （客户端带 clear / logout / prompt_input_exit 等原因）；`UserPromptSubmit` 用于保活。
    比「文件多久没动」可靠得多——后者分不出「开着但空闲」与「已关闭」。

    装什么：把 `scripts/wbsync-dialogue-lifecycle.sh` 复制到 `~/.workbuddy/scripts/`，并注册
    上述三个事件。脚本**完全静默、恒 0 退出**（不污染上下文、不影响会话）。

    子命令：on（装）/ off（卸）/ status（查看）。**每台机器各注册一次。**
    """
    skill_dir = Path(__file__).resolve().parent.parent
    src = skill_dir / "scripts" / "wbsync-dialogue-lifecycle.sh"
    scripts_dir = WB_ROOT / "scripts"
    dst = scripts_dir / "wbsync-dialogue-lifecycle.sh"
    settings_path = WB_ROOT / "settings.json"
    cmd_str = str(dst)
    action = getattr(args, "action", "status")

    def _load():
        d = {}
        if settings_path.exists():
            try:
                d = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                warn("settings.json 解析失败，按空配置处理。")
                d = {}
        return d if isinstance(d, dict) else {}

    if action == "status":
        head("对话框生命周期 hook 状态")
        info("脚本已装  %s" % ("是" if dst.exists() else "否（未装）"))
        d = _load()
        for ev in DLIFE_EVENTS:
            info("  %-17s %s" % (ev, "已注册" if _hook_event_has(d, ev, DLIFE_MARK) else "未注册"))
        if dst.exists() and _wguard_stale(src, dst):
            warn("但【脚本已过期】：重跑 wbsync dialogue-hook on --apply 刷新。")
        info("效果：开着/已关闭由显式事件判定；未收到关闭事件的按时间兜底显示「疑似已关闭」。")
        return 0

    if action == "off":
        head("卸载对话框生命周期 hook")
        if not settings_path.exists():
            ok("settings.json 不存在，无需处理。")
            return 0
        d = _load()
        hooks = d.get("hooks", {})
        changed = False
        if isinstance(hooks, dict):
            for ev in DLIFE_EVENTS:
                gs = hooks.get(ev, [])
                if not isinstance(gs, list):
                    continue
                kept = []
                for g in gs:
                    hs = (g or {}).get("hooks", [])
                    nh = [h for h in hs if DLIFE_MARK not in str(h.get("command", ""))]
                    if len(nh) != len(hs):
                        changed = True
                    if nh:
                        ng = dict(g)
                        ng["hooks"] = nh
                        kept.append(ng)
                if kept:
                    hooks[ev] = kept
                else:
                    hooks.pop(ev, None)
        if not changed:
            ok("settings.json 里没有本 skill 的生命周期 hook，无需移除。")
            return 0
        d["hooks"] = hooks
        if not args.apply:
            info("演练：将移除上述三个事件的 hook。")
            return 0
        backup_file(settings_path)
        atomic_write_json(str(settings_path), d)
        ok("已移除对话框生命周期 hook。")
        return 0

    # on
    head("注册对话框生命周期 hook（区分 开着 / 已关闭）")
    if not src.exists():
        die("找不到脚本：%s" % src)
    if args.apply:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        try:
            os.chmod(dst, 0o755)
        except OSError:
            pass
        ok("已安装脚本 %s" % dst)
    else:
        info("演练：将安装脚本到 %s" % dst)

    d = _load()
    hooks = d.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    pending = False
    for ev in DLIFE_EVENTS:
        if _hook_event_has(d, ev, DLIFE_MARK):
            info("  %-17s 已注册，跳过" % ev)
            continue
        gs = hooks.get(ev, [])
        if not isinstance(gs, list):
            gs = []
        gs.append({"hooks": [{"type": "command", "command": cmd_str}]})
        hooks[ev] = gs
        d["hooks"] = hooks
        pending = True
        info("  %-17s %s" % (ev, "已注册" if args.apply else "演练：将注册"))
    if pending and args.apply:
        backup_file(settings_path)
        atomic_write_json(str(settings_path), d)
        ok("已写入 settings.json。")
    out()
    if args.apply:
        ok("生命周期 hook 已就绪。")
        info("效果：新开/关闭对话框即时更新登记状态；每次提问保活。")
        info("卸载：wbsync dialogue-hook off --apply")
    return 0


# --------------------------------------------------------------------------
# 机器间沟通记事本：check（看发给自己的留言）/ send（给别的机器留言）
# --------------------------------------------------------------------------

def _parse_comms_messages(text):
    """解析记事本正文，返回消息块列表。

    每条消息以「## [A→B] 标题（状态）（时间：…）」开头，正文到下一个 '##' 或 '---' 为止。
    返回 [{direction, from_suffix, to_suffix, title, status, body}, ...]
    """
    msgs = []
    # 消息头：## [方向] 标题（状态）（时间：…）
    # 方向 = 方括号内容；标题+状态+时间 = 方括号后的内容
    head_re = re.compile(r"^##\s*\[([^\]]+)\]\s*(.*)$")
    # 从「标题（状态）（时间：…）」里分别抽状态和标题
    status_re = re.compile(r"（([^（）]*?)）")  # 匹配任意中文括号内容

    lines = text.splitlines()
    i = 0
    in_code_block = False
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        # 跳过代码块（``` 围起来的内容，如模板里的格式示例）
        if line.startswith("```"):
            in_code_block = not in_code_block
            i += 1
            continue
        if in_code_block:
            i += 1
            continue

        m = head_re.match(line)
        if not m:
            i += 1
            continue

        direction = m.group(1).strip()
        rest = m.group(2).strip()

        # 抽出所有「（…）」括号内容
        parens = status_re.findall(rest)
        # 状态 = 第一个「标准状态词」的括号内容；非标准词（如「请遵守」「准实时」）跳过
        status = ""
        for p in parens:
            p = p.strip()
            if p.startswith("时间"):
                continue
            if p in _STANDARD_STATUS:
                status = p
                break
        # 标题 = 去掉所有「（…）」括号后的剩余文字
        title = status_re.sub("", rest).strip()

        # 收集正文，直到下一个 '##' 消息头
        body_lines = []
        j = i + 1
        while j < len(lines):
            nl = lines[j].strip()
            if head_re.match(nl) or nl.startswith("#"):
                break
            if nl:
                body_lines.append(nl)
            j += 1
        i = j

        # 解析方向：A→B 或 全员
        from_suffix, to_suffix = None, None
        if "→" in direction or "->" in direction:
            parts = re.split(r"→|->", direction)
            from_suffix = parts[0].strip() or None
            to_suffix = parts[1].strip() if len(parts) > 1 else None
        else:
            to_suffix = direction or None

        msgs.append({
            "direction": direction,
            "from_suffix": from_suffix,
            "to_suffix": to_suffix,
            "title": title,
            "status": status,
            "body": "\n".join(body_lines),
        })

    return msgs


def _my_suffix_short():
    """返回本机后缀（A/B/C/D…）；取不到返回 None。"""
    st = Syncthing()
    my_id = _my_device_id()
    if not my_id:
        return None
    reg = _read_ws_registry()
    return _ws_suffix_of_device(reg, my_id)


def _comms_cleanup(args):
    """归档已闭环的旧消息：把「状态已闭环（已解决/已回复/已完成…）」的消息移到
    单独的归档文件 `机器间沟通-归档.md`，主记事本只保留未闭环的消息，避免无限增长。

    默认演练（--apply 才真正归档）。归档是「移动」而非「删除」，可回溯。
    """
    head("归档已闭环的旧消息")
    p = comms_notebook_path()
    if not p.exists():
        info("记事本不存在，无需归档。")
        return 0

    text = p.read_text(encoding="utf-8", errors="replace")
    msgs = _parse_comms_messages(text)

    # 找出已闭环的消息标题
    closed_titles = set()
    for m in msgs:
        if m["status"] in _CLOSED_STATUS and m["title"]:
            closed_titles.add(m["title"].strip())

    # 用行扫描切分，保留「未闭环」的消息块 + 文件头（标题/约定/说明）
    lines = text.split("\n")
    # 文件头 = 第一个「## 消息历史」或第一个消息「---」之前的部分
    # 简化：保留到「## 消息历史」之前的头部（含约定），其余按消息块切分
    header_end = 0
    for idx, ln in enumerate(lines):
        if ln.strip().startswith("## 消息历史"):
            header_end = idx + 1
            break

    header = "\n".join(lines[:header_end]) if header_end else ""

    # 逐块处理消息历史部分
    body_lines = lines[header_end:]
    keep_blocks = []
    archive_blocks = []
    cur_block = []
    in_code = False

    def flush():
        nonlocal cur_block
        if not cur_block:
            return
        blk = "\n".join(cur_block).strip()
        # 判断该块的消息标题是否已闭环
        title = ""
        for ln in cur_block:
            m = re.match(r"^##\s*\[[^\]]+\]\s*(.*)$", ln.strip())
            if m:
                title = re.sub(r"（[^（）]*?）", "", m.group(1)).strip()
                break
        if title in closed_titles:
            archive_blocks.append(blk)
        else:
            keep_blocks.append(blk)
        cur_block = []

    for ln in body_lines:
        # 消息块以「---」+「## [..]」为界；遇到「---」且后面是消息头则切换
        if ln.strip() == "---":
            # 先看下一行是否是消息头
            pass
        cur_block.append(ln)
        # 遇到新的「## [..]」消息头（非文件标题），切分
        if re.match(r"^##\s*\[[^\]]+\]", ln.strip()):
            # 当前块已经包含上一个消息的结尾，需要把上一个消息先 flush（去掉当前这行）
            if cur_block and len(cur_block) > 1:
                # 当前行是新消息头，之前的块 = cur_block[:-1]
                prev = cur_block[:-1]
                cur_block = [ln]
                # flush prev
                blk = "\n".join(prev).strip()
                if blk:
                    t = ""
                    for x in prev:
                        mm = re.match(r"^##\s*\[[^\]]+\]\s*(.*)$", x.strip())
                        if mm:
                            t = re.sub(r"（[^（）]*?）", "", mm.group(1)).strip()
                            break
                    if t in closed_titles:
                        archive_blocks.append(blk)
                    else:
                        keep_blocks.append(blk)
    # 最后一块
    if cur_block:
        blk = "\n".join(cur_block).strip()
        if blk:
            t = ""
            for x in cur_block:
                mm = re.match(r"^##\s*\[[^\]]+\]\s*(.*)$", x.strip())
                if mm:
                    t = re.sub(r"（[^（）]*?）", "", mm.group(1)).strip()
                    break
            if t in closed_titles:
                archive_blocks.append(blk)
            else:
                keep_blocks.append(blk)

    info("未闭环消息 %d 块，已闭环 %d 块" % (len(keep_blocks), len(archive_blocks)))
    if not args.apply:
        info("演练：将把 %d 块已闭环消息归档到 机器间沟通-归档.md" % len(archive_blocks))
        info("加 --apply 真正执行。")
        return 0

    # 写回主文件（头部 + 未闭环块）
    new_main = header.rstrip() + "\n\n" + "\n\n".join(keep_blocks) + "\n"
    p.write_text(new_main, encoding="utf-8")

    # 追加归档
    if archive_blocks:
        arch_path = coop_shared() / "机器间沟通-归档.md"
        stamp = time.strftime("%Y-%m-%d %H:%M")
        arch_header = ("# 机器间沟通归档\n\n> 由 wbsync comms cleanup 自动归档的已闭环消息。\n\n")
        if arch_path.exists():
            arch_text = arch_path.read_text(encoding="utf-8", errors="replace")
        else:
            arch_text = arch_header
        arch_text = arch_text.rstrip() + "\n\n---\n归档于 %s\n" % stamp + "\n\n".join(archive_blocks) + "\n"
        arch_path.write_text(arch_text, encoding="utf-8")
        ok("已归档 %d 块消息到 %s" % (len(archive_blocks), arch_path.name))
    ok("cleanup 完成。")
    return 0


def _a2a_dir():
    return coop_shared() / A2A_DIR


def _a2a_card_path(suffix):
    return _a2a_dir() / ("agent-card-%s.json" % suffix)


def _a2a_journal_path(suffix):
    return _a2a_dir() / ("tasks-%s.json" % suffix)


def _a2a_all_journals():
    d = _a2a_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.glob("tasks-*.json"))


def _a2a_now():
    """事件时间戳（**毫秒级**）。

    #13 的教训：原先只有秒级精度 → 「同一秒内完成 + 打回」两条事件跨文件折叠时
    无法定序（按 journal 文件名排 → 可能把 completed 排在 rejected 之后），
    状态机就"打回了又变回完成"。加毫秒后同机连发的次序稳定可判。
    """
    t = time.time()
    lt = time.localtime(t)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + (".%03d" % int((t % 1) * 1000)) + time.strftime("%z", lt)


def _a2a_build_card(my):
    return {
        "name": "wbsync-%s" % (my or "?"),
        "description": "%s 机的 WorkBuddy 多机同步 Agent（文件通道版 A2A）" % (my or "?"),
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "preferredTransport": "file",
        "url": "file://wb-coop/a2a/tasks-%s.json" % (my or "?"),
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": A2A_SKILLS,
        "generatedAt": _a2a_now(),
    }


# 每个 journal 上一次**成功**解析的内容。用途：Syncthing 原子替换 / 对端正整文件重写时，
# 读取会经历一个「文件暂缺或只读到半截」的窗口 —— 一次解析失败就把该机全部任务当成不存在，
# 看板上就会出现「待我 6 → 3 → 6」的跳动（用户 2026-10-17 实测反馈）。
_A2A_JOURNAL_LAST_GOOD = {}


def _a2a_read_journal(path):
    key = str(path)
    for i in range(3):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _A2A_JOURNAL_LAST_GOOD[key] = data
            return data
        except Exception:
            if i < 2:
                time.sleep(0.05 * (i + 1))      # 50ms、100ms —— 跨过替换窗口即可
    # 三次都失败：退回上次成功内容（宁可用旧数据，也不要让任务凭空消失）
    return _A2A_JOURNAL_LAST_GOOD.get(key) or {"machine": None, "events": []}


def _a2a_claims_dir():
    d = _a2a_dir() / "claims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _a2a_claim_path(task_id):
    """任务 ID 含 '-' 等字符，claim 文件名做安全化（保留可读前缀）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(task_id))
    return _a2a_claims_dir() / ("%s.json" % safe)


def _a2a_claim_info(task_id):
    """读任务认领（claim 文件）。过期视为未认领，返回 None。"""
    try:
        p = _a2a_claim_path(task_id)
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        exp = float(d.get("expires_at") or 0)
        if exp and exp < time.time():
            return None
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _a2a_claim_tag(task_id, state=""):
    """任务行尾的认领标记：`🔒 B@16:14` / `未认领`。

    claim 机制落地后如果界面看不见，两个并行对话会各自认领同一批任务
    （B 机 2026-10-17 实测，见 R-20261017-B-代码v4 §3）。
    """
    d = _a2a_claim_info(task_id)
    if d:
        who = str(d.get("owner") or "?")
        if len(who) > 12:
            who = who[:8]
        at = d.get("renewed_at") or d.get("created_at")
        try:
            hm = time.strftime("%H:%M", time.localtime(float(at)))
        except Exception:
            hm = "?"
        return "🔒 %s@%s" % (who, hm)
    if (state or "") in ("submitted", "working", "rework"):
        return "未认领"
    return ""


def _a2a_claim_acquire(task_id, owner, ttl=600, fence=None):
    """原子认领一个 A2A 任务（O_EXCL 创建，read-then-write 不做）。

    返回 (ok, msg)：
      ok=True  → 本次获得（或本人续期成功）；msg 含 fence
      ok=False → 已被他人持有且未过期；msg 含持有者
    fence：单调递增整数（每次接管 +1）；旧 fence 持有者后续 update 会被拒
    （防「假死对话复活后继续写」的脑裂）。
    原子性：用 open(..., "x")（O_EXCL）创建 claim 文件——文件已存在则抛 FileExistsError，
    这是真正的原子互斥（不依赖 tempfile，符合本环境「禁临时文件+rename」约束）。
    """
    p = _a2a_claim_path(task_id)
    me = "%s" % (owner or "?")
    now = time.time()
    cur = None
    if p.exists():
        try:
            cur = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cur = None
    if cur and cur.get("owner") == me:
        # 本人续期：不抢锁，刷过期时间，保留原 fence
        cur["expires_at"] = now + ttl
        cur["renewed_at"] = now
        atomic_write_json(str(p), cur)
        return True, "renewed(fence=%s)" % cur.get("fence")
    if cur and cur.get("owner") != me and cur.get("expires_at", 0) > now:
        # 他人持有且未过期 → 拒绝
        return False, "已被 %s 认领（fence=%s，%.0fs 后过期）" % (
            cur.get("owner"), cur.get("fence"), cur["expires_at"] - now)
    # 无 claim 或他人已过期 → 原子创建（O_EXCL）
    base_fence = cur.get("fence", 0) if cur else 0
    for _ in range(20):
        new_fence = base_fence + 1
        payload = {"task_id": str(task_id), "owner": me, "fence": new_fence,
                   "created_at": cur.get("created_at") if cur and cur.get("owner") != me else now,
                   "renewed_at": now, "expires_at": now + ttl, "ttl": ttl}
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return True, "acquired(fence=%d)" % new_fence
        except FileExistsError:
            # 同 ms 另一进程抢先创建：重读判定
            try:
                other = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                other = None
            if other and other.get("owner") == me:
                return True, "acquired(fence=%s)" % other.get("fence")
            if other and other.get("owner") != me and other.get("expires_at", 0) > time.time():
                return False, "已被 %s 认领（并发抢先）" % other.get("owner")
            # 他人过期/损坏：删旧再重试
            try:
                p.unlink()
            except OSError:
                pass
            time.sleep(0.05)
    return False, "认领失败（并发冲突，稍后重试）"


def _a2a_claim_read(task_id):
    p = _a2a_claim_path(task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _a2a_claim_release(task_id, owner=None):
    """释放 claim；owner 不匹配则拒绝（防误释他人）。"""
    p = _a2a_claim_path(task_id)
    if not p.exists():
        return False, "无认领"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False, "claim 文件损坏"
    if owner and cur.get("owner") != owner:
        return False, "非你持有（当前 %s），拒释" % cur.get("owner")
    try:
        p.unlink()
    except OSError:
        return False, "删除失败"
    return True, "released"


def _a2a_claim_status(task_id):
    """返回 (active, claim)；active=False 表示未认领或已过期。"""
    c = _a2a_claim_read(task_id)
    if not c:
        return False, None
    if c.get("expires_at", 0) <= time.time():
        return False, c
    return True, c


def _a2a_write_guarded(task_id, owner, allow_missing_claim=True):
    """a2a update 前的围栏校验：
    - 任务有**有效** claim 且调用方不是 owner → 拒（防脑裂）
    - 无 claim / allow_missing_claim → 放行（兼容未启用 claim 的历史流程）
    返回 (ok, msg)。
    """
    active, c = _a2a_claim_status(task_id)
    if not active:
        return True, ""
    if c.get("owner") == (owner or "?"):
        return True, ""
    return False, "任务 %s 已被 %s 认领（fence=%s）——请等其完成或先 `a2a release-claim --id %s --force`" % (
        task_id, c.get("owner"), c.get("fence"), task_id)


def _a2a_claim_void(task_id, note=""):
    """#13：进入 `rejected` 时**交回认领**——保留 fence 计数（写成已过期墓碑），
    使原执行方失去写权，同时让下一轮 `rework` 接管时 fence 继续单调递增。

    不直接删文件：删掉会把 fence 归零，「旧持有者复活写」就失去了序号依据。
    """
    p = _a2a_claim_path(task_id)
    cur = _a2a_claim_read(task_id) or {}
    payload = {
        "task_id": str(task_id),
        "owner": "",                       # 无持有者
        "fence": int(cur.get("fence") or 0) + 1,   # bump：写者变了
        "created_at": cur.get("created_at") or time.time(),
        "renewed_at": cur.get("renewed_at") or 0,
        "expires_at": 0,                   # 已过期 → 任何人不算持有
        "ttl": 0,
        "voided": note or "rejected",
    }
    try:
        atomic_write_json(str(p), payload)
        return True
    except Exception:
        return False


def _a2a_role(task, actor):
    """判定本次操作者在该任务里的角色：assigner（派活方）/ receiver（接活方）/ other。"""
    if not actor:
        return "other"
    if actor == task.get("from"):
        return "assigner"
    if task.get("to") == A2A_POOL_TARGET:
        return "receiver"       # 池任务：除创建者外谁都能接（具体归属由 claim 的 fence 仲裁）
    if task.get("to") in (actor, "全员", "all"):
        return "receiver"
    return "other"


def _a2a_transition_check(task, to_state, actor):
    """#13 状态机校验：返回 (ok, msg)。非法转换必须**拒并说明原因**。

    规则来源（问题队列 #13）：
      submitted → working / completed / canceled
      working   → completed / rejected / canceled
      completed → rejected                    ⚠️ 完成可被打回
      rejected  → rework / canceled           （只允许二选一，不许申辩）
      rework    → completed / rejected / canceled
      canceled / failed → 不可复活（要重来请重发新任务）
    """
    cur = (task.get("state") or "submitted")
    if to_state not in A2A_STATES:
        return False, "未知状态 %s（合法：%s）" % (to_state, " / ".join(A2A_STATES))
    if to_state == cur:
        return True, ""                       # 幂等：同状态重复更新（补产物/补说明）放行
    if cur in A2A_FINAL_STATES:
        return False, "任务已是真终态 %s，不可再变（要重来请重发新任务）" % cur
    allowed = A2A_TRANSITIONS.get(cur, set())
    if to_state not in allowed:
        return False, "非法转换 %s → %s（%s 允许：%s）" % (
            cur, to_state, cur, " / ".join(sorted(allowed)) or "无（真终态）")
    need = A2A_TRANSITION_ROLE.get((cur, to_state))
    role = _a2a_role(task, actor)
    if need and role != need:
        want = "派活方" if need == "assigner" else "接活方"
        return False, "角色不符：%s → %s 只能由%s发起（你是 %s）" % (
            cur, to_state, want,
            "派活方" if role == "assigner" else ("接活方" if role == "receiver" else "无关方"))
    if to_state == "rework" and int(task.get("attempt") or 1) >= A2A_REWORK_MAX:
        return False, "已达返工上限（attempt=%s）——只能 canceled 后重发新任务" % task.get("attempt")
    return True, ""


def _a2a_read_journal_strict(path, tries=3):
    """写入前的严格读：**读失败就抛异常**，绝不返回空事件表。

    事故（2026-10-17 19:41）：批量关任务连发 18 个 CLI 进程，其中一次撞上
    Syncthing 替换窗口 → `_a2a_read_journal()` 返回 `{"events": []}`（宽容读，
    且新进程没有 last-good 缓存）→ `_a2a_append_event` 把它当"空日志"写回，
    **A 机整份 A2A 日志被覆盖成 1 条事件**，历史全丢。
    结论：宽容读只可用于展示（board/wake）；**改动前必须严格读，读不出就中止**。
    """
    last = None
    for i in range(tries):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("journal 结构非法（缺 events 数组）")
            return data
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(0.05 * (i + 1))
    raise RuntimeError("A2A 日志暂时读不出来（%s）：%s。已中止写入，"
                       "避免把历史事件覆盖掉——请稍后重试。" % (path.name, last))


A2A_JOURNAL_LOCK_WAIT = 2.0      # 秒：等锁上限（正常一次写入是毫秒级）
A2A_JOURNAL_LOCK_STALE = 15.0    # 秒：锁文件超过这么久视为持锁进程已崩


def _a2a_journal_lock_path(journal):
    """journal 的跨进程锁文件路径。

    刻意放在 `~/.workbuddy/.wbsync-locks/`（**本机、不参与同步**），不放 `a2a/` 里：
    `a2a/` 是同步目录，锁文件高频建删会像 `board/*.render-pending` 那样反复产生
    `*.sync-conflict-*` 副本。锁只需要**同机跨进程**互斥——每台机只写自己那份 journal。
    """
    h = hashlib.sha256(str(Path(journal).resolve()).encode("utf-8")).hexdigest()[:16]
    d = WB_ROOT / ".wbsync-locks"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return d / ("a2a-%s.lock" % h)


def _a2a_journal_lock(journal, wait=A2A_JOURNAL_LOCK_WAIT):
    """取 journal 的跨进程互斥锁（O_EXCL 创建）。返回锁路径，或 None（锁机制不可用）。

    为什么需要（问题队列 #21，2026-10-18 实测复现）：`_a2a_append_event` 是
    「读整份 journal → 追加 1 条 → 整份写回」。两个进程并发时都读到 N 条、各自写回
    N+1 条 → **后写覆盖先写**，派活静默丢失。原有两道防线都拦不住：
      · 「事件数只能增」比对的是**各自读到的那份**（N→N+1，合法）；
      · 「写后回读」只证明**自己写的那一刻**在文件里，通过之后照样能被整份覆盖。
    所以把「读-改-写」整段放进互斥。

    fail-open 边界：**只有等锁超时才抛错**（宁可报错让人重试，也不能静默丢）；
    锁机制本身不可用（目录建不了、权限问题）→ 返回 None，退化回旧的无锁行为。
    """
    lock = _a2a_journal_lock_path(journal)
    if lock is None:
        return None
    deadline = time.time() + max(0.1, float(wait))
    delay = 0.02
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, ('{"pid": %d, "at": %.3f}' % (os.getpid(), time.time())).encode("utf-8"))
            finally:
                os.close(fd)
            return lock
        except FileExistsError:
            # 持锁进程崩了不会删锁 → 超过 stale 秒数就强行清掉
            try:
                if time.time() - lock.stat().st_mtime > A2A_JOURNAL_LOCK_STALE:
                    lock.unlink()
                    continue
            except Exception:
                pass
            if time.time() >= deadline:
                raise RuntimeError(
                    "写 A2A 日志等锁超时（%.1fs）：%s 被占用，可能另一个进程正在写。"
                    "请稍后重试；**不要并行跑 a2a send/update**（问题队列 #21）。"
                    % (wait, lock.name))
            time.sleep(delay)
            delay = min(delay * 2, 0.2)      # 指数退避，最多 200ms 一次
        except Exception:
            return None                       # 锁不可用 → 放行，保持旧行为（不瘫痪协作）


def _a2a_journal_unlock(lock):
    if not lock:
        return
    try:
        Path(lock).unlink()
    except Exception:
        pass


def _a2a_append_event(path, machine, event):
    """追加一条事件到 journal（**跨进程互斥**，见 `_a2a_journal_lock`）。"""
    lock = _a2a_journal_lock(path)
    try:
        return _a2a_append_event_unlocked(path, machine, event)
    finally:
        _a2a_journal_unlock(lock)


def _a2a_append_event_unlocked(path, machine, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = _a2a_read_journal_strict(path)     # ← 严格读：失败即抛，宁可不写
    else:
        data = {"machine": machine, "protocolVersion": A2A_PROTOCOL_VERSION, "events": []}
    _n_before = len(data.get("events") or [])
    data.setdefault("events", []).append(event)
    # 防回退守卫（v4.30）：事件数**只能增不能减**。任何"越写越少"都是覆盖事故的形状
    # （2026-10-17 A 的日志曾从 148 条被写成 1 条）。宁可报错让人重试，也不能静默丢历史。
    if _n_before and len(data["events"]) <= _n_before:
        raise RuntimeError("拒绝写入 %s：事件数未增长（%d → %d），疑似被并发覆盖"
                           % (path.name, _n_before, len(data["events"])))
    # 写前留一份上一版（只保留 1 份，体积可控）——真出事时是唯一的回滚点
    if path.exists():
        try:
            shutil.copyfile(str(path), str(path) + ".bak")
        except Exception:
            pass
    atomic_write_json(str(path), data)
    # ⑥ 写后回读校验：本次追加的事件 ID 必须仍在文件里（防「读-改-写」被并发覆盖）
    # 命中则原地重试 1 次（重读最新 → 再追加 → 再校验）；仍失败返回 False 让调用方决定。
    want_id = (event or {}).get("id")
    if not want_id:
        return True
    for attempt in range(2):
        try:
            after = _a2a_read_journal(path)
            if any(str(e.get("id")) == want_id for e in after.get("events", [])):
                return True
        except Exception:
            return False
        if attempt == 0:
            # 被并发覆盖：重读最新，再追加一次（幂等：先剔除同 id 的旧残影）
            fresh = _a2a_read_journal(path)
            fresh_events = [e for e in fresh.get("events", []) if str(e.get("id")) != want_id]
            fresh_events.append(event)
            fresh["events"] = fresh_events
            atomic_write_json(str(path), fresh)
    try:
        after = _a2a_read_journal(path)
        return any(str(e.get("id")) == want_id for e in after.get("events", []))
    except Exception:
        return False


# --------------------------------------------------------------------------
# P1 · 授权闸门 / STOP 刹车
# --------------------------------------------------------------------------
def _authz_dir():
    return coop_shared() / AUTHZ_DIRNAME


def _stop_path():
    return _authz_dir() / STOP_FILENAME


def _stop_active():
    try:
        return _stop_path().exists()
    except Exception:
        return False


def _stop_guard(what=""):
    """写操作 / 高危动作入口：STOP 存在即拒。**只读命令不调用本函数**。"""
    if not _stop_active():
        return True
    die("🛑 STOP 刹车生效中，「%s」已拒绝。\n"
        "  全组被人为急停（%s）。确认可以继续后，由**人**手动解除：\n"
        "    rm \"%s\"\n"
        "  只读命令（wake / brief / a2a check|list / board / stop）不受影响。"
        % (what or "该动作", _stop_path(), _stop_path()), 1)
    return False


def _authz_key(action, peer=""):
    """票指纹 = sha12(action|peer)。peer 为空表示「不限定对象」。"""
    raw = "%s|%s" % ((action or "").strip().lower(), (peer or "").strip().upper())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _authz_ticket_path(action, peer=""):
    return _authz_dir() / ("%s.json" % _authz_key(action, peer))


def _authz_issue(action, peer="", ttl=None):
    """签发一张一次性放行票（**应由人在终端手动跑**，agent 不该代签）。"""
    ttl = AUTHZ_TTL_DEFAULT if ttl is None else int(ttl)   # 注意：0 是合法值（立即过期），不能 `or`
    d = _authz_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _authz_ticket_path(action, peer)
    rec = {
        "action": (action or "").strip().lower(), "peer": (peer or "").strip().upper(),
        "key": _authz_key(action, peer),
        "ts": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ttl": ttl,
        "issuedBy": _my_suffix_short() or "?", "note": "一次性；执行即消费（文件被删除）",
    }
    atomic_write_json(p, rec)
    return p, rec


def _authz_check(action, peer="", consume=True):
    """校验票：STOP → 票存在 → 未过期 → （一次性）消费。返回 (ok, msg)。"""
    if _stop_active():
        return False, "STOP 刹车生效中"
    p = _authz_ticket_path(action, peer)
    if not p.exists() and peer:
        # 放宽：authorize 时没写 --peer 的票，对任意对象都算数（仍是人签的）
        p2 = _authz_ticket_path(action, "")
        if p2.exists():
            p = p2
    if not p.exists():
        return False, "没有放行票"
    try:
        rec = json.loads(p.read_text("utf-8") or "{}")
    except Exception as e:
        return False, "票文件损坏（%s）" % e
    try:
        age = time.time() - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        age = 10 ** 9
    ttl = int(rec["ttl"]) if rec.get("ttl") is not None else AUTHZ_TTL_DEFAULT
    if age > ttl:
        try:
            p.unlink()
        except Exception:
            pass
        return False, "票已过期（%.0f 秒前签发，有效期 %d 秒）" % (age, ttl)
    if consume:
        # 一次性：直接删除文件（比写 used 标记更原子，防并发双用）
        try:
            p.unlink()
        except Exception:
            pass
    return True, "票有效（%.0f 秒前签发）" % age


def _require_authz(action, peer="", what=""):
    """高危动作执行前的统一闸门。未过则打印「怎么补票」并退出。"""
    okv, msg = _authz_check(action, peer, consume=True)
    if okv:
        info("🔓 放行票校验通过：%s" % msg)
        return True
    if msg.startswith("STOP"):
        _stop_guard(what)
    cmd = "wbsync authorize %s%s --apply" % (action, (" --peer %s" % peer) if peer else "")
    die("🔒 「%s」需要放行票：%s。\n"
        "  这是高危动作（删除会经 Syncthing 传播给全组）。请**由人**在终端手动执行：\n"
        "    %s\n"
        "  票 %d 秒内有效、一次性；agent 不应代为签发。"
        % (what or action, msg, cmd, AUTHZ_TTL_DEFAULT), 1)
    return False


def cmd_authorize(args):
    """签发高危动作放行票（应人工执行）。"""
    action = (getattr(args, "action", "") or "").strip()
    if not action:
        die("用法：wbsync authorize <动作> [--peer X] [--ttl 300] [--apply]\n"
            "  动作：prune / uninstall / forget / a2a-task", 1)
    peer = (getattr(args, "peer", "") or "").strip().upper()
    ttl = int(getattr(args, "ttl", AUTHZ_TTL_DEFAULT) or AUTHZ_TTL_DEFAULT)
    head("高危动作放行票")
    info("动作：%s%s" % (action, ("   对象：%s" % peer) if peer else "   （不限定对象）"))
    info("有效期：%d 秒 · 一次性（执行即消费）" % ttl)
    info("票路径：%s" % _authz_ticket_path(action, peer))
    if _stop_active():
        warn("🛑 STOP 生效中：此时即使有票也会被拒，先解除刹车。")
    if not args.apply:
        out()
        info("演练：未签发。确认后用 --apply 签发（**应由人手动执行**）。")
        return 0
    p, rec = _authz_issue(action, peer, ttl)
    ok("已签发 %s（%d 秒内有效）" % (p.name, rec["ttl"]))
    info("执行高危命令后票自动失效；过期自动作废。")
    return 0


def cmd_brake(args):
    """STOP 刹车：存在即急停（全组经 Syncthing 传播）。

    ⚠️ 2026-10-18 由 C 发现并修：这个函数原来也叫 `cmd_stop`，而它在文件里**定义更靠后**
    → Python 用后者覆盖前者，`wbsync stop` 实际执行的是刹车逻辑，`stop --apply` 还因为
    argparse 不认该参数直接报错（autostart-hook 的看门狗停机调用因此失效）。
    真正的 `stop`（停 Syncthing）在文件前部，改名后各归各位。
    """
    p = _stop_path()
    if getattr(args, "on", False):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("STOP %s by %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                          _my_suffix_short() or "?"), encoding="utf-8")
        ok("🛑 已刹车：%s" % p)
        info("全组经 Syncthing 秒级生效：A2A 写操作 + prune/uninstall/forget 全部拒绝；只读不受影响。")
        info("解除：wbsync brake --off")
        return 0
    if getattr(args, "off", False):
        if p.exists():
            try:
                p.unlink()
            except Exception as e:
                die("解除失败：%s" % e, 1)
            ok("🔓 已解除刹车")
        else:
            info("本来就没刹车。")
        return 0
    head("STOP 刹车")
    if p.exists():
        bad("🛑 生效中 —— 写操作与高危动作全部拒绝（只读命令不受影响）")
        try:
            info("内容：%s" % (p.read_text("utf-8") or "").strip()[:200])
        except Exception:
            pass
    else:
        ok("🔓 无刹车（正常）")
    try:
        tickets = sorted(_authz_dir().glob("*.json"))
    except Exception:
        tickets = []
    if tickets:
        info("未消费的放行票 %d 张（%s）：" % (len(tickets), _authz_dir()))
        for t in tickets:
            try:
                rec = json.loads(t.read_text("utf-8") or "{}")
                age = time.time() - float(rec.get("ts") or 0)
                out("  · %s  %s%s  剩 %ds" % (
                    t.name, rec.get("action"), ("→" + rec.get("peer")) if rec.get("peer") else "",
                    max(0, int(int(rec.get("ttl") or AUTHZ_TTL_DEFAULT) - age))))
            except Exception:
                out("  · %s（读不出内容）" % t.name)
    else:
        info("当前无未消费的放行票。")
    return 0


def _a2a_pri_rank(t):
    """排序键：P0 最前 → P3 最后，同级按创建时间。
    注意：priority 只影响**排序与提示**，不触发放行票闸门（闸门只看 requires）。"""
    pr = (t.get("priority") or A2A_PRIORITY_DEFAULT).upper()
    rank = A2A_PRIORITIES.index(pr) if pr in A2A_PRIORITIES else len(A2A_PRIORITIES) - 1
    return (rank, t.get("createdAt") or "")


def _a2a_pri_tag(t):
    """展示用标记：🔴 P0 / 🟠 P1 / 🟡 P2 + requires 提示（P3 不标，避免噪声）。"""
    pr = (t.get("priority") or A2A_PRIORITY_DEFAULT).upper()
    if pr not in A2A_PRIORITIES:
        pr = A2A_PRIORITY_DEFAULT
    tag = {"P0": "  🔴 P0", "P1": "  🟠 P1", "P2": "  🟡 P2"}.get(pr, "")
    req = (t.get("requires") or "none").lower()
    if req == A2A_GATED_REQUIRES:
        tag += "  ⚠️ requires=delete（认领需 authorize 票）"
    elif req == "write":
        tag += "  requires=write"
    return tag


def _a2a_fold(events):
    """把事件日志折叠成任务列表（task 创建 + status 状态迁移）。"""
    tasks = {}
    order = []
    for e in events:
        kind = e.get("event")
        if kind == "task":
            tid = e.get("id")
            if not tid:
                continue
            tasks[tid] = {
                "id": tid, "from": e.get("from"), "to": e.get("to"),
                "fromDevice": e.get("fromDevice", ""), "fromGen": e.get("fromGen", 1),
                # v4.35：任务来源的对话短码（老事件没有 → 空，展示时静默跳过）
                "fromSession": e.get("fromSession", ""),
                "skill": e.get("skill"), "params": e.get("params", {}),
                "files": e.get("files", []),
                "state": e.get("state", "submitted"),
                "createdAt": e.get("at"), "updatedAt": e.get("at"),
                "messages": [], "artifacts": [],
                "attempt": 1,          # #13：第几轮（rework 时 +1；>1 即「返工后」）
                "reason": "",          # #13：最近一次打回理由（≤100 字）
                # P1：任务风险分级（老事件没这两字段 → 按默认 P3 / none 兼容）
                "priority": (e.get("priority") or A2A_PRIORITY_DEFAULT),
                "requires": (e.get("requires") or "none"),
            }
            if e.get("text"):
                tasks[tid]["messages"].append(e["text"])
            if tid not in order:  # 同一 id 只列一次（历史重号事件会造成重复行）
                order.append(tid)
        elif kind == "retag":
            # 给存量任务补「要做的事」（--do）。**不动 state / updatedAt**：
            # 补一句话不等于有人推进过，不能让超时计时被重置。
            t = tasks.get(e.get("id"))
            if not t:
                continue
            _prm = e.get("params") or {}
            if isinstance(_prm, dict) and _prm:
                t["params"] = dict(t.get("params") or {})
                t["params"].update({k: v for k, v in _prm.items() if v})
        elif kind == "status":
            t = tasks.get(e.get("id"))
            if not t:
                continue
            if e.get("state"):
                t["state"] = e["state"]
                # #13：接受打回 = 新一轮（attempt 记第几轮；同一 id 内演进，不新建 id）
                if e["state"] == "rework":
                    t["attempt"] = int(t.get("attempt") or 1) + 1
                if e["state"] == "rejected" and e.get("reason"):
                    t["reason"] = e["reason"]
            t["updatedAt"] = e.get("at", t["updatedAt"])
            if e.get("text"):
                t["messages"].append(e["text"])
            for a in (e.get("artifacts") or []):
                t["artifacts"].append(a)
    return [tasks[t] for t in order]


def _a2a_next_task_id(my, now=None):
    """生成本机新的任务 ID：`t-<YYYYmmdd-HHMMSS>-<后缀>-<NNN>`。

    ① 原子序号：用 `a2a/.seq/<前缀>-<NNN>` O_EXCL 占位文件做**原子**自增——
    文件已存在则 FileExistsError，重试下一个号。这根治「同秒两条 send 读-改-写
    各算出同一个 -001 → 后写覆盖先写 → 对方整条收不到」的事故（2026-10-16 实测）。
    先扫既有 journal 的序号取 max，再从 max+1 起 O_EXCL 占位，兼顾「重启不重号」
    与「同秒并发不撞号」。占位文件保留（同目录内自增，不清理，体积可忽略）。
    """
    ts = (now or time.strftime("%Y%m%d-%H%M%S"))
    prefix = "t-%s-%s-" % (ts, my or "?")
    # 1) 扫既有 journal，取本前缀已用到的最大序号（重启后接着编，不重号）
    used_max = 0
    for jp in _a2a_all_journals():
        try:
            for e in _a2a_read_journal(jp).get("events", []):
                eid = str(e.get("id", ""))
                if e.get("event") == "task" and eid.startswith(prefix):
                    try:
                        used_max = max(used_max, int(eid[len(prefix):]))
                    except ValueError:
                        pass
        except Exception:
            continue
    # 2) 从 used_max+1 起，用 O_EXCL 原子占位（同秒并发各拿不同号）
    seq_dir = _a2a_dir() / ".seq"
    seq_dir.mkdir(parents=True, exist_ok=True)
    n = used_max + 1
    for _ in range(1000):
        marker = seq_dir / ("%s%03d" % (prefix, n))
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return "%s%03d" % (prefix, n)
        except FileExistsError:
            n += 1
            continue
    # 兜底（极罕见：同前缀占满 1000）：随机后缀确保唯一
    import secrets
    return "%s%03d-%s" % (prefix, n, secrets.token_hex(2))


def _a2a_identity(my):
    """本机在 A2A 任务里的身份标注（deviceID 短码 + 世代）。
    序号（A/B/C）复用后，靠这两个字段区分「退出的旧机」与「接替的新机」。"""
    dev = ""
    try:
        dev = (read_my_id_from_config(find_config_xml()) or "")[:7]
    except Exception:
        pass
    sid = ""
    try:
        sid = (_session_id() or "")[:8]      # 只存短码：够区分对话，又不把整份 uuid 塞进每条事件
    except Exception:
        sid = ""
    return {"fromDevice": dev, "fromGen": _ws_my_generation(my=my),
            **({"fromSession": sid} if sid else {})}


def _a2a_label(suffix, gen):
    """任务显示用机名：世代 >1 时标注 (g2/g3)，以区分同序号的历代机器。"""
    try:
        g = int(gen)
    except (TypeError, ValueError):
        g = 1
    return "%s(第%d代)" % (suffix, g) if g > 1 else suffix


_DEV_SUFFIX_CACHE = {"at": 0.0, "reg": None}


def _dev_suffix_cached(dev):
    """deviceID → 后缀（60 秒缓存，避免逐任务读登记表）。查不到返回 None。"""
    if not dev:
        return None
    now = time.time()
    if _DEV_SUFFIX_CACHE["reg"] is None or now - _DEV_SUFFIX_CACHE["at"] > 60:
        try:
            _DEV_SUFFIX_CACHE["reg"] = _read_ws_registry() or {}
        except Exception:
            _DEV_SUFFIX_CACHE["reg"] = {}
        _DEV_SUFFIX_CACHE["at"] = now
    reg = _DEV_SUFFIX_CACHE["reg"] or {}
    try:
        hit = _ws_suffix_of_device(reg, dev)
    except Exception:
        hit = None
    if hit:
        return hit
    # 任务里有时只存 7 位短 ID（`Q6XGQMY`），登记表存全 ID（`Q6XGQMY-AJRF`）
    # → 精确匹配不到就退一步按前缀找，别把 D 显示成「未知」。
    _d = str(dev).strip()
    if len(_d) < 7:
        return None
    cands = [e for e in (list(reg.get("machines", {}).values()) + list(reg.get("retired", [])))
             if str(e.get("deviceID") or "").startswith(_d)]
    if not cands:
        return None
    cands.sort(key=lambda e: e.get("registeredAt", 0) or 0, reverse=True)
    return cands[0].get("suffix")


def _a2a_sess_tag(t):
    """任务来源的**对话**短码（`〔会话 790fbc63〕`）；老任务没这字段 → 空串。

    A2A 以前只能分辨「哪台机」，同一台机的两个对话发的任务长得一模一样（用户 2026-10-17 提问）。
    v4.35 起发任务时写入 `fromSession`，这里负责展示；缺字段的历史任务保持安静，不显示占位符。
    """
    try:
        sid = str((t or {}).get("fromSession") or "").strip()
    except Exception:
        return ""
    return ("〔会话 %s〕" % sid) if sid else ""


A2A_POOL_TARGET = "*"      # #20 池化：`--to any` 归一成 `*`，谁空闲谁认领（创建者不参与）


def _a2a_norm_target(to):
    """把 `--to` 的别名收敛成规范值：池 → `*`；广播 → `全员`；单机 → 大写后缀。

    为什么必须归一（2026-10-18 #20）：`--to` 原本直收字符串，写 `any` 会造出一个
    谁都不匹配的任务（`check` 过滤按 `to in (my,'全员','all')`），任务静默躺在
    日志里没人看见。所以入口处收敛一次，后面所有比较都拿规范值。
    """
    t = (to or "").strip()
    tl = t.lower()
    if tl in ("any", "anyone", "pool", "*", "池", "池化"):
        return A2A_POOL_TARGET
    if tl in ("all", "everyone", "全员", "广播"):
        return "全员"
    if len(t) == 1 and t.isalpha():
        return t.upper()
    return t


def _a2a_dir_tag(t):
    """任务方向标签：`A → B`（世代 >1 标 gN）。

    看任务第一眼要知道「**谁给谁的**」——所以先给箭头，再给 skill / 状态。
    `from` 为空时按 `fromDevice` 反查登记表补上（历史上打不出后缀会显示成 `?`，
    用户看到问号只会困惑）；两条路都查不到才显示「未知」。
    """
    if not isinstance(t, dict):      # 防御：畸形/被替换的输入不该让看板整块崩
        return "未知 → 未知"
    frm = (t.get("from") or "").strip() if isinstance(t.get("from"), str) else t.get("from")
    if not frm:
        frm = _dev_suffix_cached(t.get("fromDevice")) or ""
    to = (t.get("to") or "").strip() if isinstance(t.get("to"), str) else t.get("to")
    _to_disp = "池" if to == A2A_POOL_TARGET else (to if to else "未知")
    return "%s → %s" % (_a2a_label(frm, t.get("fromGen")) if frm else "未知", _to_disp)


def _a2a_art_text(a):
    """从产物对象提取文本。标准结构 {name, parts:[{type,text}]}；兼容旧的 {name,text}。"""
    if not isinstance(a, dict):
        return str(a)
    ps = a.get("parts")
    if isinstance(ps, list):
        return "\n".join(str(p.get("text", "")) for p in ps if isinstance(p, dict))
    return str(a.get("text") or "")


def _a2a_note_attempt(t, indent="      "):
    """#13：返工轮次（attempt>1 即「返工后」），首次尝试不占行。"""
    try:
        n = int(t.get("attempt") or 1)
    except (TypeError, ValueError):
        n = 1
    if n > 1:
        out("%s第 %d 轮（返工 %d 次）" % (indent, n, n - 1))


def _a2a_note_reason(t, indent="      "):
    """#13：打回理由（1 行，≤100 字）。"""
    if (t.get("state") or "") == "rejected" and t.get("reason"):
        out("%s↩ 打回理由：%s" % (indent, t.get("reason")))


def _a2a_print_artifacts(t, full, indent="      "):
    """打印一个任务的产物：--full 显示全文，否则只提示件数。"""
    arts = t.get("artifacts") or []
    if not arts:
        return
    if not full:
        out("%s（产物 %d 件，用 --full 查看）" % (indent, len(arts)))
        return
    for a in arts:
        nm = a.get("name") if isinstance(a, dict) else ""
        out("%s▸ 产物「%s」：" % (indent, nm or "result"))
        for ln in _a2a_art_text(a).splitlines() or [""]:
            out("%s  %s" % (indent, ln))


def _a2a_all_tasks():
    """汇总所有机器日志里的事件，按时间排序后**统一折叠**。

    必须跨文件合并后折叠：任务的 `task` 事件在本机日志、对端对该任务的 `status`
    回执在对方日志——若按文件分别折叠，跨机的状态更新会丢失。
    """
    events = []
    for p in _a2a_all_journals():
        events.extend(_a2a_read_journal(p).get("events", []))
    events.sort(key=lambda e: e.get("at") or "")
    return _a2a_fold(events)


_A2A_README = """# 文件通道版 A2A（Agent ↔ Agent）

本目录用「共享文件」承载 A2A 语义，**不开放网络端口**，传输走 Syncthing 文件同步。
每台机器只写自己的文件，互不覆盖（避免并发冲突）。

## 文件
- `agent-card-<后缀>.json`：各机的**能力卡**（Agent Card），声明身份、能力(skills)、传输方式。由该机自己发布。
- `tasks-<后缀>.json`：各机的**任务事件日志**（append-only）。含两类事件：
  - `{"event":"task", ...}`：本机**创建**一个任务（委派给对端）。
  - `{"event":"status", ...}`：本机对某任务的**状态更新/回执**（含产物 artifacts）。
  任务的当前状态 = 汇总所有机器日志里同一 `id` 的事件后折叠得出。

## 与 A2A 标准的关系
概念对齐 A2A（Agent Card / Task / state / Message / Artifact），但传输为 `file` 而非 HTTP。
状态机（#13）：
```
submitted → working / completed / canceled
working   → completed / rejected / canceled
completed → rejected                    （完成可被打回）
rejected  → rework / canceled           （只许二选一，不接受文字申辩）
rework    → completed / rejected / canceled
canceled / failed → 不可复活（要重来请重发新任务）
```
- `rejected` = 派活方打回，**必带理由 `--reason`（≤100 字）**；进入时自动交回认领。
- `rework` = 接活方接受打回、正在重做（同一任务 id 内演进，`attempt` 记第几轮，上限 3 轮）。
- 返工后完成：`attempt>1`（统计上可与「一次通过」区分）。
- 风险分级（P1）：`priority`（P0–P3，默认 P3）只影响 `a2a check` 排序与提示；
  `requires`（none/write/delete，默认 none）里 **delete 才触发放行票闸门**——接活方 `claim` 前需人签票。

## 授权闸门 / STOP 刹车（P1）
- 高危动作（`prune --apply` / `uninstall --apply` / `forget --apply` / `requires=delete` 的任务认领）
  除 `--apply` 外**还需要一张人签的一次性放行票**（`--apply` 只防手滑，不防「agent 自信地判定」）。
- 签票（**由人在终端执行**，agent 不应代签）：`wbsync authorize <动作> [--peer X] --apply`，TTL 300 秒、用一次即失效。
- 急停：`wbsync brake --on` 写 `coop/shared/.a2a-auth/STOP` → 全组经 Syncthing 秒级生效，
  A2A 写操作与高危动作全部拒绝，只读（wake/brief/check/list/board）不受影响；`wbsync brake --off` 解除。

## 命令
```bash
wbsync a2a card --apply                      # 发布/更新本机能力卡
wbsync a2a send --to B --skill verify --text "跑一下自检"   # 创建任务
wbsync a2a send --to B --skill deploy --priority P0 --requires delete --text "…"
wbsync a2a check                             # 看待我处理 / 待对方确认的任务（P0 在前）
wbsync a2a list                              # 列出所有任务
wbsync a2a update --id <ID> --state completed --text "已完成" --artifact "result=...\n"
wbsync a2a update --id <ID> --state rejected --reason "第 3 项没覆盖到"
wbsync a2a update --id <ID> --state rework   # 接受打回，重新做
wbsync a2a claim --id <ID>                   # 认领（一任务一对话；requires=delete 需先 authorize）
wbsync authorize prune --peer C --apply      # 人签放行票（300 秒 · 一次性）
wbsync brake --on / --off                    # 全组急停 / 解除
```
"""


def _a2a_ensure_readme():
    d = _a2a_dir()
    p = d / "README.md"
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        old = ""
    # 版本标记：老 README 缺 P1 段 → 重写（机制生成文件，重写安全、幂等）
    if old and "STOP 刹车（P1）" in old:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        p.write_text(_A2A_README, encoding="utf-8")
    except Exception:
        pass


def _a2a_notify_hint(skill, text, do):
    """`notify` 里写了「回执/回我一句」时给一句提示。

    机制上 notify 是**单向告知**：对方标 completed 即闭环，不需要再回一句「收到」——
    两边都回就变成 ack 往返。要对方明确回话，应改用 `verify`。
    """
    if (skill or "") != "notify":
        return ""
    blob = "%s %s" % (text or "", do or "")
    if re.search(r"回执|回一句|回落|回个话|回我|确认一下|请回复|ack", blob, re.I):
        return ("notify 是单向告知（对方标 completed 即闭环，不需要回「收到」）；"
                "要对方明确回话请改用 --skill verify")
    return ""


def cmd_a2a(args):
    """文件通道版 A2A：card（能力卡）/ send（建任务）/ check（看任务）/ update（更新状态）/ list。"""
    action = getattr(args, "action", "check")
    my = _my_suffix_short()

    if action == "card":
        card = _a2a_build_card(my)
        if getattr(args, "apply", False) and my:
            _a2a_ensure_readme()
            p = _a2a_card_path(my)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(str(p), card)
            ok("能力卡已发布：%s" % p.name)
        if args.json:
            out(json.dumps(card, ensure_ascii=False, indent=2))
        else:
            head("本机 A2A 能力卡")
            out(json.dumps(card, ensure_ascii=False, indent=2))
            if my and not getattr(args, "apply", False):
                info("加 --apply 可发布到 %s" % _a2a_card_path(my).name)
        return 0

    if action == "retag":
        # 给存量任务补一句「要做的事」（不改状态、不改时间）——历史任务正文多是背景交代，
        # 看板上读不出「要对方干什么」。只允许补自己派出去的。
        _tid = (getattr(args, "id", None) or "").strip()
        _do = (getattr(args, "do", None) or "").strip()
        if not _tid or not _do:
            die("retag 需要 --id 与 --do（一句话说明要对方做什么）")
        _map = {t["id"]: t for t in _a2a_all_tasks()}
        if _tid not in _map:
            die("找不到任务：%s" % _tid)
        if (_map[_tid].get("from") or "") != my:
            die("只能给自己派出的任务补 --do（%s 是 %s 派的）"
                % (_tid, _map[_tid].get("from") or "?"))
        _a2a_append_event(_a2a_journal_path(my or "?"), my,
                          {"event": "retag", "id": _tid,
                           "params": {"do": _do}, "at": _a2a_now()})
        ok("已补「要做的事」：%s → %s" % (_tid, _do))
        return 0

    if action == "send":
        _stop_guard("a2a send（派活）")           # P1：STOP 时停一切协作写入
        to = _a2a_norm_target(args.to)
        skill = (args.skill or "").strip()
        if not to or not skill:
            die("用法：wbsync a2a send --to B|any|全员 --skill verify [--text ...] [--params k=v,...] "
                "[--priority P0-P3] [--requires none|write|delete] [--file <路径>]")
        # P1：任务风险分级（priority 只影响排序/提示；requires=delete 才触发放行票闸门）
        priority = (getattr(args, "priority", None) or A2A_PRIORITY_DEFAULT).strip().upper()
        if priority not in A2A_PRIORITIES:
            die("--priority 只接受 %s（默认 %s）" % ("/".join(A2A_PRIORITIES), A2A_PRIORITY_DEFAULT), 1)
        requires = (getattr(args, "requires", None) or "none").strip().lower()
        if requires not in A2A_REQUIRES_VALUES:
            die("--requires 只接受 %s（默认 none）" % "/".join(A2A_REQUIRES_VALUES), 1)
        if requires == A2A_GATED_REQUIRES and priority == "P3":
            warn("requires=delete 的任务却标了 P3：高危任务建议 P0/P1（对端会先看到）。")
        params = {}
        for kv in (getattr(args, "params", None) or "").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()
        _do = (getattr(args, "do", None) or "").strip()
        if _do:
            params["do"] = _do          # 「要做的事」：看板概况 / check 优先显示这一句
        text_val = (args.text or "").strip()
        # 涉及文件：`--file` 的语义是「给对端的**跨机指针**」（纪律 §14 要求写相对同步根的路径），
        # 所以**不能**按 CWD 补全——否则本机 CWD 一换，指针就被重写成三机都不存在的绝对路径
        # （B 机 2026-10-17 实测，见 R-20261017-B-代码v4 §2）。`_to_syncroot_rel` 正是这个语义：
        # 已是相对路径就原样保留，绝对路径才相对化。
        files = []
        for _f in (getattr(args, "file", None) or []):
            _f = (_f or "").strip()
            if not _f:
                continue
            try:
                _key = _to_syncroot_rel(_f)
                files.append(_key)
                if not (cfg_sync_root() / _key).exists() and not Path(_key).is_absolute():
                    warn("涉及文件本机不存在：%s（对端很可能也找不到，建议核对路径）" % _key)
            except Exception:
                files.append(_f)
        _hint = _a2a_notify_hint(skill, text_val, (params or {}).get("do"))
        if _hint:
            warn(_hint)
        tid = _a2a_next_task_id(my)
        _a2a_ensure_readme()
        _a2a_append_event(_a2a_journal_path(my or "?"), my, dict({
            "event": "task", "id": tid, "from": my, "to": to, "skill": skill,
            "params": params, "text": text_val,
            "files": files, "priority": priority, "requires": requires,
            "state": "submitted", "at": _a2a_now(),
        }, **_a2a_identity(my)))
        ok("已创建任务 %s（%s → %s，skill=%s，%s%s）"
           % (tid, my, ("池" if to == A2A_POOL_TARGET else to), skill, priority,
              "" if requires == "none" else "，requires=%s" % requires))
        if to == A2A_POOL_TARGET:
            info("池任务：任何一台空闲机器都能认领（**创建机不参与**，免得自己派的活自己抢）。"
                 "对端 `a2a check` 在「无人认领 / 自己已认领」时才显示；认领后别机就看不到，"
                 "除非租约过期（`a2a claim --ttl %d`，长任务记得续租）。" % 1800)
        if requires == A2A_GATED_REQUIRES:
            info("该任务要求高危动作：对端**认领时**必须有人签的放行票"
                 "（`wbsync authorize a2a-task --peer %s --apply`），否则 `a2a claim` 会被拒。" % tid)
        if files:
            info("涉及文件：%s（对端 `a2a check` 会显示，照路径即可定位）" % "、".join(files))
        if len(text_val) > 60:
            warn("正文 %d 字：对端 `a2a check` 默认只看前 60 字（要全文得加 --full）。"
                 "长正文建议走「短指针 + 正文放专线/共享文件」。" % len(text_val))
        # 纪律 §14：交代了细节却没给文件指针 → 对端只能满目录翻一遍。只提示、不阻断。
        _ptr = any(k in text_val for k in ("/", ".md", ".json", ".py"))
        if text_val and not _ptr and not files and len(text_val) > 40:
            info("提示（纪律 §14）：正文没写文件指针。建议写成「先读 coop/shared/方案-X.md"
                 "（头部状态块 + §N）→ 实施」，对端可少翻一遍；涉及文件加 --file。")
        info("对端 `wbsync a2a check` 可看到；处理完用 `wbsync a2a update --id %s --state completed` 回执。" % tid)
        _board_touch("a2a send", state="online")     # #15：任务变化即刷看板
        return 0

    if action == "claim":
        _stop_guard("a2a claim（认领任务）")       # P1：STOP 时停一切协作写入
        tid = (args.id or "").strip()
        if not tid:
            die("用法：wbsync a2a claim --id <任务ID> [--ttl 600]")
        # P1：高危任务（requires=delete）认领时须人签放行票 —— 防「自信地执行危险派活」
        _t = next((x for x in _a2a_all_tasks() if x.get("id") == tid), None)
        if _t and _t.get("to") == A2A_POOL_TARGET and (_t.get("from") or "") == (my or ""):
            die("池任务 %s 是本机派出的：创建机不认领自己的池任务（要自己做就别投池）。" % tid, 1)
        if _t and _t.get("requires") == A2A_GATED_REQUIRES:
            _require_authz("a2a-task", tid, "认领高危任务 %s（requires=delete）" % tid)
        ttl = int(getattr(args, "ttl", 600) or 600)
        ok_flag, msg = _a2a_claim_acquire(tid, my or "?", ttl=ttl)
        if ok_flag:
            ok("已认领 %s（%s · %s）" % (tid, my or "?", msg))
            info("处理完成：`wbsync a2a update --id %s --state completed`；放弃：`wbsync a2a release-claim --id %s`" % (tid, tid))
        else:
            warn("认领失败：%s" % msg)
        _board_touch("a2a claim", state="online")    # #15
        return 0

    if action == "release-claim":
        _stop_guard("a2a release-claim")
        tid = (args.id or "").strip()
        if not tid:
            die("用法：wbsync a2a release-claim --id <任务ID> [--force]")
        force = getattr(args, "force", False)
        # --force：释放他人持有的认领（确认对方已停再用于接管）；否则只允许释自己的
        if force:
            ok_flag, msg = _a2a_claim_release(tid, owner=None)
        else:
            ok_flag, msg = _a2a_claim_release(tid, owner=(my or "?"))
        (ok if ok_flag else warn)("释放认领 %s：%s%s" % (tid, msg, "（--force）" if force else ""))
        _board_touch("a2a release-claim", state="online")   # #15
        return 0

    if action == "update":
        _stop_guard("a2a update（回执/改状态）")
        tid = (args.id or "").strip()
        state = (args.state or "").strip()
        if not tid or not state:
            die("用法：wbsync a2a update --id <任务ID> --state <working|completed|rejected|rework|failed|canceled> "
                "[--reason \"≤100字\"] [--text ...] [--artifact name=text]")
        # #13 状态机校验：非法转换直接拒并说明原因（含「谁来做」）
        task = next((t for t in _a2a_all_tasks() if t.get("id") == tid), None)
        if not task:
            die("找不到任务 %s（用 `wbsync a2a list` 确认 ID）" % tid, 1)
        t_ok, t_msg = _a2a_transition_check(task, state, my or "?")
        if not t_ok:
            die(t_msg, 1)
        reason = (getattr(args, "reason", "") or "").strip()
        if state == "rejected":
            if not reason:
                die("打回（rejected）必须带 --reason \"≤100 字理由\"（对方照理由重做，不接受文字申辩）", 1)
            if len(reason) > A2A_REASON_MAX:
                reason = reason[:A2A_REASON_MAX]
                warn("理由超过 %d 字，已截断（要详细说明请写进共享文件，A2A 只留结论）" % A2A_REASON_MAX)
        # ③ fence 校验：有有效 claim 且非本人 → 拒（防脑裂）
        #    #13 例外：派活方打回（completed/working → rejected）必须能穿透接活方的认领，
        #    否则「做完了但 claim 没释放」就永远打不回；打回后会 void 掉原 claim。
        guard_ok, guard_msg = _a2a_write_guarded(tid, my or "?")
        if not guard_ok and not (state == "rejected" and _a2a_role(task, my) == "assigner"):
            die(guard_msg, 1)
        arts = []
        raw = getattr(args, "artifact", None)
        if raw:
            for a in raw.split("|"):
                a = a.strip()
                if not a:
                    continue
                if "=" in a:
                    n, v = a.split("=", 1)
                    arts.append({"name": n.strip() or "result",
                                 "parts": [{"type": "text", "text": v.strip()}]})
                else:
                    arts.append({"name": "result", "parts": [{"type": "text", "text": a}]})
        _a2a_ensure_readme()
        _a2a_append_event(_a2a_journal_path(my or "?"), my, {
            "event": "status", "id": tid, "from": my, "state": state,
            "reason": reason, "attempt": int(task.get("attempt") or 1),
            "text": (args.text or "").strip(), "artifacts": arts, "at": _a2a_now(),
        })
        ok("已更新任务 %s → %s%s" % (
            tid, state, ("（第 %d 轮）" % (int(task.get("attempt") or 1) + 1)) if state == "rework" else ""))
        if state == "rejected":
            # #13：进入打回 → 交回认领（原执行方失去写权，fence bump 防复活写）
            _a2a_claim_void(tid, note="rejected by %s" % (my or "?"))
            info("理由：%s" % reason)
            info("已交回认领（原执行方不再持有）。对方 `a2a check` 会看到「待我重做」，"
                 "用 `--state rework` 接受或 `--state canceled` 拒绝。")
        elif state == "rework":
            # #13：接受打回 → 重做方重新认领（fence 继续递增）
            c_ok, c_msg = _a2a_claim_acquire(tid, my or "?", ttl=600)
            if c_ok:
                info("已重新认领（%s）。做完 `--state completed`。" % c_msg)
            else:
                warn("重新认领失败：%s —— 请先 `a2a release-claim --id %s --force` 再试" % (c_msg, tid))
        _board_touch("a2a update", state="online")   # #15：**每次状态变化都刷**（不能只刷发布）
        return 0

    # check / list
    tasks = _a2a_all_tasks()
    incoming, outgoing = [], []
    for t in tasks:
        st = t.get("state")
        _to = t.get("to")
        _receives = _to in (my, "全员", "all")
        if _to == A2A_POOL_TARGET:
            # #20 池化收件规则：① 创建者本人不看成「待我处理」（自己派的活自己抢 = 没池化）；
            # ② 已被别人有效认领 → 不显示，免得两台机同时读同一任务做重复工作；
            # ③ 无人认领 / 已认领到本机 → 显示，谁空闲谁接。
            # 第 ② 条是「第三台机不打搅」的落点：认领本身就是过滤条件，不用额外机制。
            if (t.get("from") or "") != (my or ""):
                _holder = (_a2a_claim_info(t["id"]) or {}).get("owner") or ""
                _receives = (not _holder) or _holder == (my or "")
            else:
                _receives = False
        if _receives and st not in A2A_TERMINAL_STATES:
            incoming.append(t)
        if t.get("from") == my and st not in A2A_TERMINAL_STATES:
            outgoing.append(t)
    # P1：高危/紧急任务先看到（P0 → P3；同级按创建时间）
    incoming.sort(key=_a2a_pri_rank)
    outgoing.sort(key=_a2a_pri_rank)

    if args.json:
        out(json.dumps({"my_suffix": my, "incoming": incoming,
                        "outgoing": outgoing, "all": tasks},
                       ensure_ascii=False, indent=2))
        return 0

    head("文件通道版 A2A · 任务")
    if _stop_active():
        warn("🛑 STOP 刹车生效中：只读可看，写操作（send/claim/update/release-claim）一律被拒。")
    full = getattr(args, "full", False)

    def _tr(s, n=60):
        s = s or ""
        if full or len(s) <= n:
            return s
        return "%s… ⋯【本消息共 %d 字，已截断，务必加 --full 看全文再执行】" % (s[:n], len(s))

    # 「被用一次就自己对齐」：check/list 之前先刷本机快照（幂等，内容没变不写）。
    # 跨机任务发布后，对端没有守护进程，唯一能自动刷新的时机就是它下次跑命令；
    # 把 check/list 算进来 = 只要它在处理协作，自己那张卡就不会落后。
    _board_touch("a2a %s" % action, state="online")

    if action == "list":
        _want_pri = (getattr(args, "priority", None) or "").strip().upper()
        if _want_pri:
            tasks = [t for t in tasks if (t.get("priority") or A2A_PRIORITY_DEFAULT).upper() == _want_pri]
            if not tasks:
                ok("暂无 %s 任务。" % _want_pri)
                return 0
            info("只列 %s 任务（%d 条）" % (_want_pri, len(tasks)))
        if not tasks:
            ok("暂无任务。")
            return 0
        for t in tasks:
            _at = int(t.get("attempt") or 1)
            out("  · %s [%s%s] %s  skill=%s%s%s"
                % (t["id"], t.get("state"), (" attempt#%d" % _at) if _at > 1 else "",
                   _a2a_dir_tag(t), t.get("skill"), _a2a_pri_tag(t),
                   ("  " + _a2a_sess_tag(t)) if _a2a_sess_tag(t) else ""))
            _a2a_note_reason(t)
            for m in t.get("messages", []):
                out("      %s" % _tr(m))
            _a2a_print_artifacts(t, full)
        return 0

    if not incoming and not outgoing:
        ok("没有待处理的 A2A 任务。")
        return 0
    if incoming:
        info("待我处理（%d 条）" % len(incoming))
        for t in incoming:
            _st = t.get("state") or "submitted"
            _ct = _a2a_claim_tag(t["id"], _st)
            out("  ─ %s  %s  skill=%s  状态 %s%s%s%s%s" % (
                t["id"], _a2a_dir_tag(t), t.get("skill"), _st,
                "  ⚠️ 被打回，待我重做" if _st == "rejected" else "", _a2a_pri_tag(t),
                ("  " + _ct) if _ct else "", ("  " + _a2a_sess_tag(t)) if _a2a_sess_tag(t) else ""))
            if (t.get("requires") or "none").lower() == A2A_GATED_REQUIRES:
                out("      🔒 高危：认领前需人签票 → wbsync authorize a2a-task --peer %s --apply" % t["id"])
            _a2a_note_attempt(t)
            if t.get("files"):
                out("      文件：%s" % "、".join(str(x) for x in t["files"]))
            _prm = t.get("params") if isinstance(t.get("params"), dict) else {}
            if _prm.get("do"):
                out("      ▶ 要做：%s" % _prm["do"])
            if t.get("params"):
                out("      params=%s" % json.dumps(t["params"], ensure_ascii=False))
            _a2a_note_reason(t)
            for m in t.get("messages", []):
                out("      %s" % _tr(m))
            _a2a_print_artifacts(t, full)
    if outgoing:
        info("待对方确认（%d 条）" % len(outgoing))
        for t in outgoing:
            _st = t.get("state") or "submitted"
            _ct = _a2a_claim_tag(t["id"], _st)
            out("  ─ %s  %s  skill=%s  状态 %s%s%s%s" % (
                t["id"], _a2a_dir_tag(t), t.get("skill"), _st,
                "（待对方重做）" if _st == "rejected" else "", _a2a_pri_tag(t),
                ("  " + _ct) if _ct else ""))
            _a2a_note_attempt(t)
            if t.get("files"):
                out("      文件：%s" % "、".join(str(x) for x in t["files"]))
            _a2a_note_reason(t)
            _a2a_print_artifacts(t, full)
    return 0


def _comms_cursor_cmd(args):
    """查看 / 推进「本机读到各机专线的位置」。"""
    rows, cursor = _comms_peer_status()
    my = _my_suffix_short() or "?"
    want = (getattr(args, "peer", None) or "").strip().upper()

    if getattr(args, "mark", False):
        peers = {want} if want else set()
        if want and want not in {r["peer"] for r in rows}:
            die("找不到对端专线：%s（现有：%s）"
                % (want, ", ".join(r["peer"] for r in rows) or "无"))
        touched = _comms_push_cursor(cursor, rows, peers)
        if not touched:
            bad("没有可推进的对端（无对端专线）。")
            return 1
        if not write_comms_cursor(cursor):
            bad("读位写入失败（本机专线文件不可写？）")
            return 1
        head("读位已推进")
        info("本机 = %s 机；下表为推进后的位置：" % my)
        for peer, n in touched:
            out("  ─ %s 机：已读 %d 条" % (peer, n))
        return 0

    if args.json:
        out(json.dumps({"my_suffix": my,
                        "rows": [{k: v for k, v in r.items()
                                  if k not in ("msgs", "unread_msgs")} for r in rows]},
                       ensure_ascii=False, indent=2))
        return 0

    head("专线读位")
    if not rows:
        info("还没有对端专线文件（无未读）。")
        return 0
    info("本机 = %s 机；读位只记在本机专线文件里，不写对方文件。" % my)
    for r in rows:
        line = "  ─ %s 机：共 %d 条，已读 %d，未读 %d" % (r["peer"], r["total"], r["read"], r["unread"])
        out(line)
        if r["unread"]:
            out("    未读起：%s" % r["unread_first"])
            out("    读未读：wbsync comms read --peer %s" % r["peer"])
        else:
            out("    已读完")
        if not r["initialized"]:
            warn("%s 机的读位未初始化 → 全部视为未读；确认已读过可用 "
                 "wbsync comms cursor --mark --peer %s 置零" % (r["peer"], r["peer"]))
        if r["drift"]:
            warn("%s 机读位漂移（文件被归档/重排？）→ 本次按「已读 %d 条」保守使用，"
                 "建议重读一遍后用 --mark 重置" % (r["peer"], r["read"]))
    return 0


def _comms_read_cmd(args):
    """输出对端专线里本机还没读过的消息（默认读完自动推进读位）。"""
    rows, cursor = _comms_peer_status()
    want = (getattr(args, "peer", None) or "").strip().upper()
    picked = [r for r in rows if (not want or r["peer"] == want)]
    if not picked:
        die("找不到对端专线：%s（现有：%s）"
            % (want or "无", ", ".join(r["peer"] for r in rows) or "无"))

    show_all = bool(getattr(args, "all", False))
    do_mark = not bool(getattr(args, "no_mark", False))
    head("读专线（%s）" % ("全部" if show_all else "仅未读"))

    printed_total = 0
    to_push = []
    for r in picked:
        if r["drift"]:
            warn("%s 机读位漂移，本次按保守位置（已读 %d/%d）输出" % (r["peer"], r["read"], r["total"]))
        if not r["initialized"]:
            warn("%s 机读位未初始化 → 视为全部未读（读完会自动置位）" % r["peer"])
        sel = r["msgs"] if show_all else r["unread_msgs"]
        info("%s 机：%d 条%s" % (r["peer"], len(sel), "（全部）" if show_all else "（未读）"))
        if not sel:
            out("  （没有新留言）")
        for m in sel:
            printed_total += 1
            out("  ─ %s" % (m.get("title") or "(无标题)"))
            out("    来自 %s   状态 %s" % (m.get("from_suffix") or "?", m.get("status") or "?"))
            for bl in (m.get("body") or "").splitlines():
                if bl.strip():
                    out("    %s" % bl)
        if do_mark:
            to_push.append(r)

    if do_mark and to_push and printed_total:
        touched = _comms_push_cursor(cursor, picked, {r["peer"] for r in to_push})
        if write_comms_cursor(cursor):
            ok("读位已推进：%s" % "，".join("%s→%d/%d" % (p, n, next(
                x["total"] for x in picked if x["peer"] == p)) for p, n in touched))
        else:
            bad("读位写入失败（本机专线文件不可写？）")
            return 1
    elif do_mark and not printed_total:
        info("没有新留言，读位未变。")
    return 0


def cmd_tell(args):
    """组外留言（薄封装）：**其他工作空间**的对话 → 协作小组，一条命令零决策。

    见 `coop/shared/纪律-协作行为.md` §12。自动：
      · 标题加 `[外部·<窗口名>] ` 前缀（让小组一眼分出是哪个窗口在说话）
      · `--to 全员`
      · 复用 comms 通道（写本机专线、单写者、无多机冲突面）
    **只发不碰**：组外对话不应跑 a2a / lock / reserve / 写协作文件。
    """
    text = (getattr(args, "text", "") or "").strip()
    if not text:
        die("用法：wbsync tell \"<一句话>\" [--from \"<窗口名>\"] [--body \"<正文>\"]", 1)
    win = (getattr(args, "from_window", "") or "").strip() or "外部窗口"
    title = "[外部·%s] %s" % (win, text[:60])
    body = (getattr(args, "body", "") or "").strip() or text
    head("组外留言 → 协作小组")
    d = append_comms_message("全员", title, body, "待回复")
    if d:
        ok("已留言 [%s] %s" % (d, title))
        info("小组下次 `wbsync wake` / `comms check` 会看到；本窗口也可自行 `wbsync comms check` 看回信。")
    else:
        bad("留言失败")
    return 0


def cmd_comms(args):
    action = getattr(args, "action", "check")

    if action == "cursor":
        return _comms_cursor_cmd(args)

    if action == "read":
        return _comms_read_cmd(args)

    if action == "cleanup":
        return _comms_cleanup(args)

    if action == "send":
        head("给别的机器留言")
        direction = (args.to or "").strip() or "全员"
        title = (args.title or "").strip()
        body = (args.body or "").strip()
        if not title and not body:
            die("请用 --title 和 --body 提供标题与正文，或 --body 单条消息")
        if not body:
            body = title
            title = body[:20]
        reply_to = getattr(args, "reply_to", None) or None
        final_dir = append_comms_message(direction, title, body,
                                         args.status or "待回复", reply_to=reply_to)
        if final_dir:
            ok("已追加消息 [%s] %s" % (final_dir, title))
            if reply_to:
                info("已在正文标注「↳ 回复：%s」；对方下次 comms check 会据此把该消息自闭环。" % reply_to)
        else:
            bad("追加失败")
        return 0

    # check：查看发给本机的、未处理的消息 + 本机发出但未获对方确认的消息
    # --json 时不打横幅：SessionStart hook 会把本命令输出直接落盘成
    # $TMPDIR/wbsync-comms-unread.json，掺入人类可读的标题行就不是合法 JSON 了。
    if not args.json:
        head("查看机器间留言")
    # 每机专线机制：扫描所有沟通文件（本机 + 各对端 + 历史），跨文件合并解析
    files = _all_comms_files()
    if not files:
        if args.json:
            out(json.dumps({"my_suffix": _my_suffix_short(), "messages": [], "awaiting_ack": [],
                            "unread": []}, ensure_ascii=False))
        else:
            info("还没有任何沟通文件（无机器留言）。")
        return 0

    msgs = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            msgs.extend(_parse_comms_messages(text))
        except Exception:
            pass
    my = _my_suffix_short()

    done_states = _CLOSED_STATUS

    # 预构建「已闭环标题」索引：闭环判定有两个来源——
    #  ① 消息标题本身（若对方回执沿用了原标题）；
    #  ② 正文里的「↳ 回复：<原标题>」引用（--reply-to 生成的回执），据此把原标题视为已闭环。
    # 命中任一即视为该标题已闭环，不再列「待我回复」——解决「回了仍显示待回复」的假待办。
    acked_titles = set()
    for m in msgs:
        if m["status"] in done_states and m["title"]:
            acked_titles.add(m["title"].strip())
        # 解析正文里的「↳ 回复：X」
        for bl in (m["body"] or "").splitlines():
            bl = bl.strip()
            if bl.startswith("↳ 回复："):
                ref = bl[len("↳ 回复："):].strip()
                if ref:
                    acked_titles.add(ref)

    # 本机自查闭环：本机自己文件里「已获对方回执」的待回复消息，标记为已回复（只写本机文件）
    try:
        _self_close_local(acked_titles)
    except Exception:
        pass

    # 1) 待我回复：别人发给本机、状态仍待回复，且没有对方的闭环回执
    pending = []
    # 2) 待对方确认：本机发出、对方还没给回执（用于主节点跟进，直到闭环）
    awaiting_ack = []
    for m in msgs:
        to = m["to_suffix"]
        frm = m["from_suffix"]
        title = (m["title"] or "").strip()
        # 发给本机（或全员）、且状态未闭环、且无同标题闭环回执 → 待我处理
        if (to in (my, "全员", "所有机器", "all")
                and m["status"] not in done_states
                and title not in acked_titles):
            pending.append(m)
        # 本机发出、且对方还没给回执（无同标题闭环回执）→ 待对方确认
        if frm == my and m["status"] not in done_states and title not in acked_titles:
            awaiting_ack.append(m)

    if args.json:
        _rows, _ = _comms_peer_status()
        out(json.dumps({"my_suffix": my, "messages": pending,
                        "awaiting_ack": awaiting_ack,
                        "unread": [{"peer": r["peer"], "total": r["total"], "read": r["read"],
                                    "unread": r["unread"], "initialized": r["initialized"],
                                    "drift": r["drift"]} for r in _rows]},
                       ensure_ascii=False, indent=2))
        return 0

    if not pending and not awaiting_ack:
        ok("没有待处理的新留言。")
        _comms_print_unread_summary()
        return 0

    if pending:
        info("待我回复（%d 条）：%s" % (len(pending), "" if my is None else "（本机 = %s 机）" % my))
        for m in pending:
            _ext = "  🔔组外留言" if "[外部·" in (m.get("title") or "") else ""
            out("  ─ %s%s%s%s" % (C.B, m["title"], C.R, _ext))
            out("    来自 %s   状态 %s" % (m["from_suffix"] or "?", m["status"]))
            if m["body"]:
                for bl in m["body"].splitlines()[:5]:
                    out("    %s" % bl)
    if awaiting_ack:
        info("待对方确认（%d 条，需跟进直到闭环）：" % len(awaiting_ack))
        for m in awaiting_ack:
            _ext = "  🔔组外留言" if "[外部·" in (m.get("title") or "") else ""
            out("  ─ %s%s%s%s   （发往 %s，状态 %s）"
                % (C.B, m["title"], C.R, _ext, m["to_suffix"] or "?", m["status"]))
    _comms_print_unread_summary()
    return 0


def cmd_netinfo(args):
    """采集并输出本机网络信息（含 ipconfig/ifconfig 原始文本），供写入资料库登记表。"""
    ni = _collect_netinfo()
    if args.json:
        out(json.dumps(ni, ensure_ascii=False, indent=2))
        return 0
    head("本机网络信息（供登记 / 排查）")
    info("采集命令   %s" % ni["command"])
    info("IPv4       %s" % (", ".join(ni["ipv4"]) if ni["ipv4"] else "（未识别）"))
    info("掩码       %s" % (ni["netmask"] or "（未识别）"))
    info("网关       %s" % (ni["gateway"] or "（未识别）"))
    info("网卡类型   %s" % (ni["ifaceType"] or "（未识别）"))
    info("全局 IPv6  %s" % ("有" if ni["hasGlobalIPv6"] else "无（仅链路本地 fe80::）"))
    out()
    info("原始输出（写入登记表时带上，方便后续机器查询）：")
    out("─" * 60)
    out(ni["raw"] or "（采集不到原始输出）")
    return 0


def cmd_network(args):
    """配置中继 + 全局发现，并做连接诊断。默认演练，--apply 才落盘。

    连接策略（按优先级）：
      1) 免费中继（优先）——绕开 AP 隔离 / 被墙发现服务器；
      2) 局域网发现 / IPv6 直连（其次）；
      3) 都不行 → 提示可能 AP 隔离，给出解决思路。
    """
    if args.action == "config":
        relays = RELAY_POOL
        if args.relay:
            relays = [r.strip() for r in args.relay.split(",") if r.strip()]
        if not args.apply:
            head("演练：配置中继 + 全局发现（不落盘）")
        else:
            head("配置中继 + 全局发现")
        ok_, msg = _apply_network_config(relays, args.apply,
                                         enable_global=not args.no_global,
                                         enable_relay=not args.no_relay)
        (ok if ok_ else bad)(msg)
        if ok_ and args.apply:
            info("配置已落盘。请重启 Syncthing 生效：wbsync stop --apply 后再 wbsync start")
            info("（或经 Web 界面 /rest/system/restart）")
        elif ok_ and not args.apply:
            info("确认无误后加 --apply 落盘。")
        return 0

    if args.action == "test-relays":
        head("测试免费中继可达性")
        results = _probe_relay_reachability(RELAY_POOL)
        for uri, host, reachable in results:
            (ok if reachable else bad)("%s  %s" % (host, "可达" if reachable else "不可达"))
        return 0

    if args.action == "diag":
        head("连接诊断（AP 隔离 / 中继 / 发现）")
        ni = _collect_netinfo()
        info("本机 IPv4：%s" % (", ".join(ni["ipv4"]) or "未知"))
        info("网关：%s" % (ni["gateway"] or "未知"))
        iso = _check_ap_isolation()
        info("网关可达：%s" % ("是" if iso["gatewayReachable"] else "否/未知"))
        out()
        # 检查 config 里的中继与全局发现状态
        cfg_path = _read_config_xml_path()
        if cfg_path:
            try:
                import xml.etree.ElementTree as ET
                t = ET.parse(str(cfg_path))
                opts = t.getroot().find("options")
                ge = opts.find("globalAnnounceEnabled")
                re_ = opts.find("relaysEnabled")
                las = [e.text.strip() for e in opts.findall("listenAddress") if e.text]
                relays = [x for x in las if x.startswith("relay://")]
                info("全局发现：%s" % ("开启" if (ge is not None and (ge.text or "").lower() == "true") else "关闭（建议开启）"))
                info("中继开关：%s" % ("开启" if (re_ is not None and (re_.text or "").lower() == "true") else "关闭（建议开启）"))
                info("已配中继：%d 个%s" % (len(relays), "" if relays else "（无，建议 wbsync network config --apply）"))
            except Exception:
                info("config.xml 解析失败，无法读取网络配置")
        else:
            info("config.xml 不存在（Syncthing 未启动过）")
        out()
        warn("若与对端同网段却互 ping 不通、ARP 表显示 (incomplete)，")
        info("极可能是无线 AP 隔离 / 访客网络隔离。解决思路：")
        info("  1) 进路由器关「AP 隔离 / 无线隔离 / 客户端隔离」；")
        info("  2) 或让走无线的机器改插网线；")
        info("  3) 或继续用中继（wbsync network config --apply），绕开隔离。")
        return 0

    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=APP,
        description="WorkBuddy 多电脑同步助手（macOS / Windows / Linux 通用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型流程：\n"
            "  1. wbsync plan                  看现状\n"
            "  2. wbsync install --apply       装 Syncthing\n"
            "  3. wbsync setup --apply         建四个夹并注册\n"
            "  4. wbsync id                    取本机设备 ID\n"
            "  5. wbsync pair <对端ID> --apply 配对（对端也要反向做一次）\n"
            "     或 wbsync mailbox put/get --apply  跨网用信箱交换 ID 自动配对\n"
            "  6. wbsync expert --apply        专家/连接器同步过来后修登记层\n"
            "  7. wbsync autostart on --apply  登记开机自启\n"
            "  8. wbsync doctor                随时体检\n"
            "\n"
            "新建文件同步盘（可选）：\n"
            "  wbsync newdisk <名> --apply      新建一个文件同步盘，对端同名自动 -B 对接\n"
            "  wbsync listdisks                 看登记表里有哪些同步盘\n"
            "\n"
            "多机协作工作空间（可选）：\n"
            "  wbsync workspace init --apply    首次使用自动分配后缀 A/B/C/D 建工作空间\n"
            "  wbsync workspace list            看各机器工作空间\n"
            "  wbsync workspace grant B --apply 授权 B 可写你的工作空间\n"
            "  wbsync workspace revoke B --apply 收回 B 的写权限\n"
            "  wbsync workspace log             看变更记录（谁何时改了啥）\n"
        ),
    )
    p.add_argument("--version", action="version", version="%s %s" % (APP, VERSION))
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("plan", help="看本机现状（只读）")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("status", help="看本账号身份 + 是否首次使用本 skill（只读）")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("identity", help="看本账号 WorkBuddy identityKey（区分同人多机 vs 陌生人）")
    sp.set_defaults(func=cmd_identity)

    sp = sub.add_parser("probe", help="探测本机是同步组第几台（后缀 A/B/C/D，只读不写盘）")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("registry", help="输出登记本机所需信息（登记即命名，含自动算好的编号）")
    sp.add_argument("--suffix", help="显式指定编号（如 B），缺省按已占用自动推算")
    sp.add_argument("--occupied", help="已占用的编号列表，逗号分隔（如 A,B），用于自动推算下一编号")
    sp.set_defaults(func=cmd_registry)

    sp = sub.add_parser("package", help="打包/分发前清理 skill 目录里的设备残留，并可打一个干净 zip（--zip）")
    sp.add_argument("--apply", action="store_true", help="真正删除残留文件（走系统回收站，可恢复）")
    sp.add_argument("--zip", metavar="路径", help="清理后打干净 zip（排除残留/__pycache__/.DS_Store/本机 _user_meta.json），打印文件数/大小/SHA256")
    sp.add_argument("--scope", choices=["skill", "skills"], default="skill",
                    help="扫描范围：skill=只扫本 skill 目录（默认）；skills=扫整个 skills 夹（清其它 skill 目录里的历史残留）")
    sp.add_argument("--include-versions", action="store_true",
                    help="连同 .stversions/ 里的旧版本一起清（默认跳过——那是 guard 版本控制的恢复仓库）")
    sp.add_argument("--slim", action="store_true",
                    help="精简包：额外排除 vendor/（Syncthing 离线二进制 ~34MB）—— 用于上传有体积上限的分发场景；二进制由 `wbsync install` 自动下载")
    sp.set_defaults(func=cmd_package)

    ig = sub.add_parser("ignore", help="忽略机器私有元数据（_skillhub_meta.json/.DS_Store/__pycache__/*.pyc）——根治跨机互写冲突")
    ig.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    ig.add_argument("--folder", help="指定同步夹 ID（默认自动定位 ~/.workbuddy/skills 所在夹）")
    ig.add_argument("--all-folders", action="store_true", help="作用于所有同步夹")
    ig.add_argument("--apply", action="store_true", help="真正写入（默认只演练）")
    ig.set_defaults(func=cmd_ignore)

    sp = sub.add_parser("mark-initialized", help="把本账号标记为已完成初始化（首次引导后调用）")
    sp.add_argument("--apply", action="store_true", help="真正落盘")
    sp.set_defaults(func=cmd_mark_initialized)

    sp = sub.add_parser("install", help="安装 Syncthing")
    sp.add_argument("--apply", action="store_true", help="真正下载安装")
    sp.add_argument("--force", action="store_true", help="已装也重装")
    sp.add_argument("--from-file", metavar="压缩包", help="用本地安装包离线装（零下载，最快；如从 U 盘/资料库拿到的 syncthing-*.zip）")
    sp.add_argument("--mirror", metavar="前缀", help="下载加速镜像前缀（GitHub 慢/被墙时用；也可设环境变量 WBSYNC_DL_MIRROR）")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("setup", help="建立同步夹并注册")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("folders", help="列出同步夹")
    sp.set_defaults(func=cmd_folders)

    sp = sub.add_parser("secure", help="安全加固：同步夹密钥忽略（status/on）+ 密钥扫描（scan）；全盘加密归 OS")
    sp.add_argument("action", choices=["status", "on", "scan"], nargs="?", default="status")
    sp.add_argument("--apply", action="store_true", help="on：真正写入 .stignore（默认演练）")
    sp.set_defaults(func=cmd_secure)

    sp = sub.add_parser("id", help="显示本机设备 ID")
    sp.set_defaults(func=cmd_id)

    sp = sub.add_parser("pair", help="与对端设备配对")
    sp.add_argument("peer_id", help="对端设备 ID")
    sp.add_argument("--name", help="给对端起个名字")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_pair)

    sp = sub.add_parser("forget", help="清除 config 里记住的对端设备 ID（peer_id/dead_ids）")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("mailbox", help="通过 wb-coop 信箱跨网交换设备 ID")
    sp.add_argument("sub", choices=["put", "get"])
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_mailbox)

    sp = sub.add_parser("newdisk", help="新建一个文件同步盘（统一 A/B/C/D 后缀，位置首次询问后记住）")
    sp.add_argument("--name", help="盘的可读备注名（可选，默认「同步盘 <后缀>」）")
    sp.add_argument("--location", help="盘所在根目录（首次询问后写入 config 记住，之后复用）")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_newdisk)

    sp = sub.add_parser("listdisks", help="列出已登记的同步盘")
    sp.set_defaults(func=cmd_listdisks)

    sp = sub.add_parser("workspace", help="多机协作工作空间（A/B/C/D 各自空间 + 共享区 + 变更记录）")
    wsp = sp.add_subparsers(dest="action")

    w = wsp.add_parser("init", help="首次使用：分配后缀 A/B/C/D 并建自己的工作空间（默认 ~/WorkBuddySync/workspaces/sync-all-{suffix}）")
    w.add_argument("--path", help="工作空间根目录（默认保留 config 现值；无现值才用 ~/WorkBuddy）")
    w.add_argument("--reuse-existing", action="store_true",
                   help="沿用已存在的同名工作空间（并把 workspace_root 指过去），不新建")
    w.add_argument("--force-root", action="store_true",
                   help="明确要求改根（否则缺 --path 时保留已记住的 workspace_root，不覆盖为默认）")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("seed", help="为已建好的工作空间补一条占位会话，使其出现在客户端左侧空间列表（幂等；老机一次性补种）")
    w.add_argument("--path", help="要补种的工作空间目录（默认本机 sync-all-<后缀>）")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("list", help="列出所有机器的工作空间")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("grant", help="授权某台机器（后缀）可写你的工作空间")
    w.add_argument("target", help="目标机器后缀，如 B")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("revoke", help="收回某台机器（后缀）的写权限")
    w.add_argument("target", help="目标机器后缀，如 B")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("apply-grants", help="根据登记表里的授权关系，重置本地各工作区目录的读写权限")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("log", help="查看工作空间变更记录（谁、何时、改了什么）")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("cleanup", help="清理平台遗弃的时间命名空壳目录（默认只列，--apply 才删）")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    w = wsp.add_parser("hide", help="隐藏本机磁盘上的 A/B/C（及后续）工作空间目录（默认只列，--apply 才隐藏）")
    w.add_argument("--apply", action="store_true")
    w.set_defaults(func=cmd_workspace)

    sp = sub.add_parser("expert", help="专家/连接器登记层自愈")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_expert)

    sp = sub.add_parser("autostart", help="登录自启")
    sp.add_argument("action", choices=["status", "on", "off"])
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_autostart)

    sp = sub.add_parser("start", help="启动 Syncthing")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="停止 Syncthing")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("doctor", help="全面体检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("verify", help="连接后自检（连接/工作空间/同步三查）")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("fixnames", help="补改名：把 Syncthing 未完成的 .tmp 改成正式文件（兜底）")
    sp.add_argument("--dry-run", action="store_true", help="只列出，不改")
    sp.add_argument("--min-age", type=int, default=5, help="只处理 mtime 超过该秒数的 .tmp（默认 5）")
    sp.set_defaults(func=cmd_fixnames)

    sp = sub.add_parser("netinfo", help="采集本机网络信息（ipconfig/ifconfig，供写入登记表）")
    sp.set_defaults(func=cmd_netinfo)

    sp = sub.add_parser("hook", help="随 WB 会话启停同步（打开对话自启、关闭 WB 自动停）")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("wguard", help="写护栏（PreToolUse）：他人持锁时阻止本机写共享文件，把静默覆盖变成显式拦截")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    sp.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    sp.set_defaults(func=cmd_wguard)

    sp = sub.add_parser("comms", help="机器间沟通记事本（check 看留言 / send 发留言 / cleanup 归档 / cursor 读位 / read 读未读）")
    sp.add_argument("action", choices=["check", "send", "cleanup", "cursor", "read"], nargs="?", default="check")
    sp.add_argument("--to", help="发给谁：A/B/C/D 或「全员」（send 用）")
    sp.add_argument("--title", help="消息标题（send 用）")
    sp.add_argument("--body", help="消息正文（send 用）")
    sp.add_argument("--status", help="状态标记，默认「待回复」")
    sp.add_argument("--reply-to", help="回执时标注「回复哪条消息」（传原消息标题）")
    sp.add_argument("--apply", action="store_true", help="cleanup 时真正归档")
    sp.add_argument("--peer", help="对端机后缀，如 B（cursor / read 用；不给则含全部对端）")
    sp.add_argument("--mark", action="store_true", help="cursor：把读位推进到最新（标记为已读）")
    sp.add_argument("--no-mark", action="store_true", help="read：读完不推进读位（默认会推进）")
    sp.add_argument("--all", action="store_true", help="read：输出全部消息（默认只输出未读部分）")
    sp.set_defaults(func=cmd_comms)

    sp = sub.add_parser("tell", help="组外留言（其他工作空间的对话 → 小组）：自动补 --to 全员 + [外部·] 前缀，一条命令零决策")
    sp.add_argument("text", nargs="?", help="一句话摘要（也作标题）")
    sp.add_argument("--from", dest="from_window", help="窗口名（如「求职库」「短剧外」）；不填则用「外部窗口」")
    sp.add_argument("--body", help="正文（可选，默认同摘要）")
    sp.set_defaults(func=cmd_tell)

    sp = sub.add_parser("mem", help="机器间 memory 日志增量读（read 只读未读段 / cursor 看读位 / index 看全貌索引；降 token）")
    sp.add_argument("action", choices=["read", "cursor", "index"], nargs="?", default="cursor")
    sp.add_argument("--peer", help="对端机后缀，如 C（不给则含全部对端；index 含本机+各对端）")
    sp.add_argument("--mark", action="store_true", help="cursor：把读位推进到最新（置零）")
    sp.add_argument("--no-mark", action="store_true", help="read：读完不推进读位")
    sp.add_argument("--all", action="store_true", help="read：输出全部段（默认只未读）")
    sp.add_argument("--json", action="store_true", help="cursor/index：输出 JSON")
    sp.add_argument("--days", type=int, help="index：只保留距今 ≤N 天的带日期条目（longterm/state 不受影响）")
    sp.add_argument("--logs-only", action="store_true", help="index：只列日志/归档，不列 longterm/state")
    sp.add_argument("--titles", type=int, default=8, help="index：每条目最多列 N 个段标题（默认 8）")
    sp.set_defaults(func=cmd_mem)

    sp = sub.add_parser("a2a", help="文件通道版 A2A（card 能力卡 / send 建任务 / check / update / list / claim 认领）")
    sp.add_argument("action", choices=["card", "send", "check", "update", "list", "claim",
                                      "release-claim", "retag"], nargs="?", default="check")
    sp.add_argument("--to", help="任务发给谁（send 用）：A/B/C/D 指定机；any = 投池（谁空闲谁认领，创建机除外）；全员 = 广播")
    sp.add_argument("--skill", help="委派的能力（send 用），如 verify / fixnames / report")
    sp.add_argument("--text", help="任务说明或状态说明")
    sp.add_argument("--do", dest="do", metavar="一句话",
                    help="**要对方具体做什么**（≤30 字中文）。看板/check 直接显示这句；"
                         "不写就退回正文首句——正文多是背景交代，读不出「要干什么」")
    sp.add_argument("--params", help="参数，k=v,k2=v2（send 用）")
    sp.add_argument("--file", action="append", default=None,
                    help="涉及的文件（可重复；自动归一为跨机可比路径——相对同步根 / ~/…，对端照键即可定位）")
    sp.add_argument("--id", help="任务 ID（update / claim / release-claim 用）")
    sp.add_argument("--state", help="新状态：submitted/working/completed/rejected/rework/failed/canceled")
    sp.add_argument("--reason", help="打回理由（--state rejected 必填，≤100 字，超截断）")
    sp.add_argument("--artifact", help="产物，name=text（多个用 | 分隔）")
    sp.add_argument("--ttl", type=int, default=600, help="认领租约秒数（claim 用，默认 600）")
    sp.add_argument("--priority", default=None,
                    help="任务优先级 P0–P3（send 用，默认 P3；list 时作为**过滤**，如 --priority P0）")
    sp.add_argument("--requires", default="none", help="任务所需权限 none|write|delete（send 用；delete 认领需放行票）")
    sp.add_argument("--force", action="store_true", help="release-claim 时强制释放他人认领（确认对方已停再用于接管）")
    sp.add_argument("--apply", action="store_true", help="card 时真正写入能力卡")
    sp.add_argument("--full", action="store_true", help="list/check 时显示消息与产物全文（默认截断）")
    sp.set_defaults(func=cmd_a2a)

    sp = sub.add_parser("authorize", help="签发高危动作放行票（一次性·TTL 300s；**应由人手动执行**）")
    sp.add_argument("action", nargs="?", help="动作：prune / uninstall / forget / a2a-task")
    sp.add_argument("--peer", help="对象（prune 的后缀 / a2a-task 的任务 ID；缺省表示不限定）")
    sp.add_argument("--ttl", type=int, default=AUTHZ_TTL_DEFAULT, help="有效期秒数（默认 300）")
    sp.add_argument("--apply", action="store_true", help="真正签发（默认只演练）")
    sp.set_defaults(func=cmd_authorize)

    sp = sub.add_parser("brake", help="STOP 刹车：存在即全组急停（A2A 写操作 + 高危动作全拒；只读不受影响）")
    sp.add_argument("--on", action="store_true", help="上刹车（写 STOP 文件，经 Syncthing 传播全组）")
    sp.add_argument("--off", action="store_true", help="解除刹车（删 STOP 文件）")
    sp.set_defaults(func=cmd_brake)

    sp = sub.add_parser("board", help="机群看板：一份数据（board/<后缀>.json）+ 终端/md/HTML 三视图")
    sp.add_argument("action", choices=["show", "write", "render", "push"], nargs="?", default="show")
    sp.add_argument("--json", action="store_true", help="show：输出全部机器的快照（JSON）")
    sp.add_argument("--force", action="store_true", help="render：越过「只有主维护机（序号最小的在册机）渲染共享视图」的限制")
    sp.add_argument("--no-push", dest="no_push", action="store_true",
                    help="write：只写本地快照，不顺手推资料库数据表（默认会推）")
    sp.add_argument("--if-stale", dest="if_stale", action="store_true",
                    help="render：只有 board/*.json 比现有视图新时才渲染（给定时器用，没变化就什么都不做）")
    sp.set_defaults(func=cmd_board)

    sp = sub.add_parser("health", help="通道自检：各机自报/视图/日志/票/退出闭环/数据表（只读）")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("brief", help="精简状态摘要（引擎/对端/A2A 待办/同步，一条命令拿全）")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("do", help="意图分发：一句话 → 该跑的命令（纯关键词，不调模型；只建议不执行）")
    sp.add_argument("intent", nargs="*", help='你的意图，例如：wbsync do "帮我把这活派给 B"（不带则列出全部规则）')
    sp.add_argument("--json", action="store_true", help="机器可读输出")
    sp.set_defaults(func=cmd_do)

    sp = sub.add_parser("wake", help="一体化唤醒摘要（本机/同步/A2A 待办/未读/对端状态卡片，≤20 行）")
    sp.add_argument("--peers", help="只看指定对端的状态卡片（逗号分隔，缺省全部）")
    sp.add_argument("--apply", action="store_true",
                    help="自动对「已声明但本机设备表没有」的对端执行 accept-peer（重入机换 ID 场景；幂等）")
    sp.add_argument("--full", action="store_true", help="完整一屏：含超时任务明细（≤25 行；`#10`）")
    sp.set_defaults(func=cmd_wake)

    sp = sub.add_parser("network", help="配置中继/全局发现 + 连接诊断（config / test-relays / diag）")
    nsp = sp.add_subparsers(dest="action")
    n = nsp.add_parser("config", help="把免费中继地址 + 全局发现写进 config.xml")
    n.add_argument("--relay", help="自定义中继地址，逗号分隔（缺省用内置免费中继池）")
    n.add_argument("--no-global", action="store_true", help="不开启全局发现")
    n.add_argument("--no-relay", action="store_true", help="不开启中继开关")
    n.add_argument("--apply", action="store_true", help="真正落盘")
    n.set_defaults(func=cmd_network)
    n = nsp.add_parser("test-relays", help="测试内置免费中继的可达性")
    n.set_defaults(func=cmd_network)
    n = nsp.add_parser("diag", help="诊断 AP 隔离 / 中继 / 全局发现状态")
    n.set_defaults(func=cmd_network)

    sp = sub.add_parser("guard", help="硬闸门：给同步夹开 Syncthing trashcan 版本控制，让删除可恢复（堵三机同删）")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    sp.add_argument("--apply", action="store_true", help="真正写入 Syncthing config.xml（默认只演练）")
    sp.add_argument("--cleanout", type=int, default=30, help="删除后保留天数（默认 30，trashcan 用）")
    sp.set_defaults(func=cmd_guard)

    up = sub.add_parser("uninstall", help="退出机制：停同步→通知对端→清本机自己的产物→资料库标记→归档/删记忆（默认演练，删除走回收站）")
    up.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    up.add_argument("--by", help="退出发起者（只允许 A 或本机；默认本机）。A 机可远程委派他人退出，B 不能踢 C")
    up.add_argument("--memory", choices=["delete", "archive"],
                    help="长期记忆/日志：delete=删除 / archive=归档到协作共享区（默认运行时询问，非交互默认 archive）")
    up.add_argument("--purge", action="store_true", help="额外删除 Syncthing 二进制目录（默认已清配置 / plist / 各夹同步元数据）")
    up.add_argument("--wait", type=int, default=90, help="停同步前等待变更送达对端的秒数（默认 90，至少 60）")
    up.set_defaults(func=cmd_uninstall)

    pp = sub.add_parser("prune", help="对端侧收尾：清理已退出机器在本机的痕迹（协作区残留/登记表/设备表+授权）")
    pp.add_argument("--peer", required=True, help="要清理的已退出机器后缀（单个大写字母，如 C）")
    pp.add_argument("--device", help="显式指定要移除的设备 ID（登记表取不到时兜底，配合 --peer 使用）")
    pp.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    pp.set_defaults(func=cmd_prune)

    ap = sub.add_parser("accept-peer", help="反向配对：把对端设备加入本机设备表并共享所有同步夹（对端重装/换 ID 后入组用；勿用 pair）")
    ap.add_argument("--device", required=True, help="对端完整 deviceID（56 位）")
    ap.add_argument("--name", help="设备名（默认取 ID 前缀）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    ap.set_defaults(func=cmd_accept_peer)

    ss = sub.add_parser("sessions", help="列出本机 · 本工作空间的对话（并行会话；上下文不共享、记忆共享）")
    ss.add_argument("--ws", help="指定工作空间路径（默认当前目录）")
    ss.add_argument("--limit", type=int, default=8, help="显示最近 N 个会话（默认 8）")
    ss.set_defaults(func=cmd_sessions)

    lk = sub.add_parser("lock", help="同机编辑锁：改共享文件（记忆/skill 代码/coop 文档）前先占锁，防同机多对话静默覆盖")
    lk.add_argument("action", choices=["status", "claim", "release"], nargs="?", default="status")
    lk.add_argument("purpose", nargs="?", help="claim 时说明用途，如 \"改 wbsync.py\"")
    lk.add_argument("--ttl", type=int, default=LOCK_DEFAULT_TTL_MIN, help="锁自动过期分钟数（默认 30）")
    lk.add_argument("--force", action="store_true", help="他人持锁时强行占锁（确认无冲突才用）")
    lk.set_defaults(func=cmd_lock)

    rs = sub.add_parser("reserve", help="跨机器文件预约锁：改任意共享文件前先占，防 A/B/C 同时改同一份（写 coop/shared，随同步可见）")
    rs.add_argument("action", choices=["status", "claim", "verify", "release"], nargs="?", default="status")
    rs.add_argument("path", nargs="?", help="claim / verify / release 的文件路径")
    rs.add_argument("--ttl", type=int, default=RESERVE_DEFAULT_TTL_MIN,
                    help="预约自动过期分钟数（默认 %d；机器会随手关，别设太长）" % RESERVE_DEFAULT_TTL_MIN)
    rs.add_argument("--fence", type=int, default=None,
                    help="claim 时打印的序号：verify 用它复核是否仍是你的锁；release 带它可防误删他人的锁")
    rs.add_argument("--reason", default="", help="claim 时说明用途")
    rs.add_argument("--force", action="store_true", help="他人持预约时强行占（确认无冲突才用）")
    rs.add_argument("--session", default="", help="release --session <sid>：释放某对话持有的全部预约（SessionEnd 自动调用）")
    rs.set_defaults(func=cmd_reserve)

    hf = sub.add_parser("handoff", help="指针式交接：生成带 log_ref+摘要+自动 reserve 的 A2A 任务，让对端自己翻你的日志段")
    hf.add_argument("log_ref", help="日志指针：相对同步根的路径，可加 ::标题 指定段落，如 workspaces/sync-all-A/A/memory/2026-10-15-A.md::视频X")
    hf.add_argument("--summary", required=True, help="TL;DR：这次干了啥/剪到哪/未决点（保底摘要，进 A2A）")
    hf.add_argument("--to", required=True, help="交给哪台机（后缀）")
    hf.add_argument("--file", default="", help="本次占用的共享文件（自动 reserve；接手方接管）")
    hf.add_argument("--skill", default="handoff", help="A2A 任务 skill 标签（默认 handoff）")
    hf.add_argument("--state", default="", help="from_state：从哪接着做")
    hf.set_defaults(func=cmd_handoff)

    pk = sub.add_parser("pickup", help="B 接手：读 handoff 摘要、跳转到 A 的日志段、接管 reserve")
    pk.add_argument("id", help="handoff 任务 ID")
    pk.add_argument("--full", action="store_true", help="日志段过长时仍显示全文")
    pk.set_defaults(func=cmd_pickup)

    dl = sub.add_parser("dialogue", help="跨对话框协作身份：登记/关闭/列出（区分 开着 与 已关闭；本机+跨机+各世代）")
    dl.add_argument("action", choices=["register", "leave", "ls", "hb"], nargs="?", default="ls",
                    help="hb 为 hook 事件入口（读 stdin JSON，静默；由 dialogue-hook 注册后自动调用）")
    dl.add_argument("--topic", help="register 时声明本对话框在做什么（协作信号）")
    dl.add_argument("--locked", nargs="*", help="register 时标注本对话框正在改的共享文件（如 MEMORY.md）")
    dl.add_argument("--json", action="store_true", help="ls 输出 JSON")
    dl.set_defaults(func=cmd_dialogue)

    sp = sub.add_parser("dialogue-hook", help="注册对话框生命周期 hook（SessionStart/UserPromptSubmit/SessionEnd）——让 开着/已关闭 由显式事件判定")
    sp.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    sp.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    sp.set_defaults(func=cmd_dialogue_hook)

    ad = sub.add_parser("archive-doc", help="把 coop/shared 里已消化/已闭环/已作废的协作文档移入「历史协作/」并加标记+登记索引")
    ad.add_argument("name", nargs="?", help="文件名或相对 coop/shared 的路径")
    ad.add_argument("--reason", help="归档原因（默认「已闭环 / 已消化」）")
    ad.add_argument("--into", help="消化去向（如 `纪律-协作行为.md §4`；默认「无需」）")
    ad.add_argument("--list", action="store_true", help="列出已归档清单（读 历史协作/README.md）")
    ad.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    ad.set_defaults(func=cmd_archive_doc)

    ro = sub.add_parser("registry-order", help="重排资料库「多机同步登记」次序：README → 现役机(按后缀) → 归档文件夹(末)")
    ro.add_argument("--apply", action="store_true", help="真正执行（默认只演练）")
    ro.set_defaults(func=cmd_registry_order)

    return p


# ---------------------------------------------------------------------------
# guard：Syncthing 版本控制硬闸门 —— 让「误删」可恢复
#   为什么不是平台 sandbox 规则：实测平台会重写 ~/.workbuddy/settings.json 里的
#         sandbox.orderedRules.file.rules，外部写入的 custom 规则不持久（17:21 被覆盖）。
#   机制：给 5 个同步夹在 Syncthing 侧开 trashcan 版本控制。此后本机/他机任何
#         删除（含误删、三机传播删）都会被 Syncthing 移到 .stversions 目录，
#         保留 cleanoutDays 天，可整夹/整文件找回——从根上堵住「删了就没」。
#   写哪：Syncthing 自己的 config.xml（~/.workbuddy 管不到它），可持久、平台不覆盖。
#   注意：版本控制是各机各自的本地开关（config.xml 不同步），需三机各跑一次
#         `guard on --apply`；但即便只本机开，本机产生的删除也已可恢复。
# ---------------------------------------------------------------------------

def _ws_peer_dirs(my_suffix):
    """本机工作空间根下、除本机外的其它单字母后缀目录（他机私有区）。
    结构：sync-all-A/{A,B,C,...} 同夹兄弟；本机只写自己的 <后缀>/，他机只读。"""
    root = _ws_local_dir(_read_ws_registry(), my_suffix)
    peers = []
    if not root or not root.exists():
        return peers
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        if not re.fullmatch(r"[A-Z]", sub.name):
            continue
        if sub.name == my_suffix:
            continue
        peers.append((sub.name, sub))
    return peers


def _st_client(config_xml=None):
    try:
        return Syncthing(config_xml=config_xml)
    except Exception:
        return None


def _guard_target_folders(st):
    """guard 要保护的同步夹 id 列表（5 夹全护）。
    REST 抖动时返回空列表，绝不抛异常——doctor 等诊断路径不能被 guard 检查带崩。"""
    try:
        return sorted((st.folders() or {}).keys())
    except Exception:
        return []


def _folder_versioning(st, folder_id):
    try:
        f = (st.folders() or {}).get(folder_id)
    except Exception:
        f = None
    if not f:
        return None
    v = f.get("versioning") or {}
    return v


def _trashcan_spec(cleanout_days=30):
    return {
        "type": "trashcan",
        "params": {"cleanoutDays": str(int(cleanout_days))},
        "cleanupIntervalS": 3600,
        "fsPath": "",
        "fsType": "basic",
    }


def _set_folder_versioning(st, folder_id, versioning, apply_flag=False):
    """给某同步夹设 versioning。返回 (是否变化, 现有 versioning)。"""
    f = (st.folders() or {}).get(folder_id)
    if not f:
        return False, None
    cur = f.get("versioning") or {}
    if not apply_flag:
        return (cur.get("type") != versioning.get("type")
                or dict(cur.get("params") or {}) != dict(versioning.get("params") or {})), cur
    body = dict(f)
    body["versioning"] = versioning
    st.api("PUT", "/rest/config/folders/%s" % folder_id, body)
    return True, cur


def cmd_guard(args):
    """guard：给同步夹开 Syncthing trashcan 版本控制，让删除可恢复。
    on/off 默认演练（只列将改什么），--apply 才真正写 Syncthing config.xml。"""
    my = _my_suffix_short()
    peers = _ws_peer_dirs(my) if my else []

    action = getattr(args, "action", "status")
    cleanout = getattr(args, "cleanout", 30) or 30
    spec = _trashcan_spec(cleanout)

    st = _st_client()
    if not st or not st.available:
        die("Syncthing 不可用（未运行或拿不到 API key）。先 wbsync id / workspace init 确认 Syncthing 在跑。", 1)

    folder_ids = _guard_target_folders(st)
    if not folder_ids:
        info("Syncthing 里没有注册任何同步夹。")
        return 0

    # 现有各夹 versioning
    cur = {fid: _folder_versioning(st, fid) for fid in folder_ids}

    if action == "status":
        head("guard 状态（Syncthing 版本控制硬闸门）")
        on_cnt = 0
        for fid in folder_ids:
            v = cur[fid] or {}
            t = (v.get("type") or "").strip()
            if t == "trashcan":
                on_cnt += 1
                info("  [on ] %-22s trashcan 保留 %s 天" % (fid, (v.get("params") or {}).get("cleanoutDays", "?")))
            elif t:
                info("  [?? ] %-22s 已开 %s（非 trashcan，参数 %s）" % (fid, t, v.get("params")))
            else:
                info("  [off] %-22s 无版本控制（删除即永久）" % fid)
        out()
        if on_cnt == len(folder_ids):
            ok("已全部启用 trashcan 版本控制（%d/%d 夹）。" % (on_cnt, len(folder_ids)))
        else:
            warn("有 %d 个夹未启用 trashcan。跑 `wbsync guard on --apply` 全量开启。" % (len(folder_ids) - on_cnt))
        if peers:
            info("本机他机目录：%s（其删除也受版本控制保护，可恢复）" % "、".join(s for s, _ in peers))
        return 0

    if action == "on":
        targets = [fid for fid in folder_ids if (cur[fid] or {}).get("type") != "trashcan"]
        if not args.apply:
            section("演练：将为 %d 个同步夹开启 trashcan 版本控制（保留 %d 天）" % (len(targets), cleanout))
            for fid in folder_ids:
                t = (cur[fid] or {}).get("type") or "off"
                mark = "→ trashcan" if fid in targets else "已是 trashcan"
                info("  %-22s %s %s" % (fid, t, mark))
            out()
            info("开启后：任何删除（含误删/三机传播删）进 .stversions，%d 天内可找回。" % cleanout)
            info("加 --apply 真正写入 Syncthing config.xml（可持久，平台不覆盖）。")
            return 0
        changed = 0
        for fid in targets:
            _, _old = _set_folder_versioning(st, fid, spec, apply_flag=True)
            changed += 1
            info("  [on] %s" % fid)
        ok("已为 %d 个同步夹开启 trashcan 版本控制（保留 %d 天）。" % (changed, cleanout))
        out()
        info("各机需各跑一次 `wbsync guard on --apply`（版本控制是本地开关，config.xml 不同步）。")
        return 0

    if action == "off":
        targets = [fid for fid in folder_ids if (cur[fid] or {}).get("type")]
        empty = {"type": "", "params": {}, "cleanupIntervalS": 3600, "fsPath": "", "fsType": "basic"}
        if not args.apply:
            section("演练：将关闭 %d 个同步夹的版本控制" % len(targets))
            for fid in folder_ids:
                t = (cur[fid] or {}).get("type") or "off"
                info("  %-22s %s" % (fid, t))
            out()
            info("加 --apply 真正关闭（删除将恢复为「即删即没」，慎用）。")
            return 0
        changed = 0
        for fid in targets:
            _set_folder_versioning(st, fid, empty, apply_flag=True)
            changed += 1
            info("  [off] %s" % fid)
        ok("已关闭 %d 个同步夹的版本控制。" % changed)
        return 0

    die("未知动作：%s" % action, 1)


# ---------------------------------------------------------------------------
# uninstall：退出机制（卸载 / 离开协作组）
#   让一台机器干净退出多机协作，且不伤其他机器。三个关键点：
#     1) 删除会传播（所有夹 sendreceive）→ 必须先脱离同步再删（stop 后本地删除不传播）。
#     2) 通知靠同步送达 → 必须先发通知、等送达、再停同步。
#     3) 资料库无删节点 API → 只能重命名标记 + 列清单供手动删。
#   安全默认：默认只演练；删除一律走系统回收站（可恢复）；只清本机自己的东西。
# ---------------------------------------------------------------------------

UNINSTALL_ARCHIVE_PREFIX = "_退出归档"


def _trash_one(p):
    """把单个路径移入系统回收站；成功返回 True，平台不支持则抛异常。"""
    p = Path(p)
    if not p.exists():
        return True
    if IS_MAC and Path("/usr/bin/trash").exists():
        subprocess.run(["/usr/bin/trash", str(p)], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if IS_WIN:
        fn = "DeleteDirectory" if p.is_dir() else "DeleteFile"
        arg = "'%s','OnlyErrorDialogs','SendToRecycleBin'" % str(p).replace("'", "''")
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::%s(%s)" % (fn, arg))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    if IS_LINUX:
        subprocess.run(["gio", "trash", str(p)], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    raise RuntimeError("此平台无系统回收站命令")


def _trash_paths(paths, fallback_dir=None):
    """批量移入回收站；系统回收站不可用时降级到 fallback_dir（本机暂存，可手动恢复）。
    返回 (成功列表, 失败列表[(path, err)])。"""
    okl, faill = [], []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        try:
            _trash_one(p)
            okl.append(str(p))
        except Exception as e:
            moved = False
            if fallback_dir:
                try:
                    fb = Path(fallback_dir)
                    fb.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(fb / p.name))
                    okl.append(str(p))
                    moved = True
                except Exception as e2:
                    faill.append((str(p), "%s / 降级失败：%s" % (e, e2)))
            if not moved:
                faill.append((str(p), str(e)))
    return okl, faill


def _uninstall_scan(my, purge=False):
    """只读预检：列出退出的影响面（不写盘）。"""
    plan = {"my": my, "peers": [], "region": [], "coop_own": [], "a2a_own": [],
            "library_docs": [], "memory_dir": None, "memory_files": [],
            "folders": [], "infra": [], "ws": None}
    try:
        reg = _read_ws_registry()
        ws = _ws_local_dir(reg, my)
    except Exception:
        reg, ws = {"machines": {}}, None
    plan["ws"] = str(ws) if ws else None
    plan["peers"] = [s for s, _ in _ws_peer_dirs(my)]
    if ws:
        mine = ws / my
        if mine.exists():
            plan["region"].append(str(mine))
    nf = _my_comms_file()
    if nf and Path(nf).exists():
        plan["coop_own"].append(str(nf))
    try:
        card = _a2a_dir() / ("agent-card-%s.json" % my)
        if card.exists():
            plan["a2a_own"].append(str(card))
    except Exception:
        pass
    if ws:
        mem = ws / my / "memory"
        if mem.exists():
            plan["memory_dir"] = str(mem)
            plan["memory_files"] = sorted(p.name for p in mem.rglob("*.md"))
    st = _st_client()
    if st and st.available:
        try:
            plan["folders"] = [(f.get("id"), f.get("path")) for f in st.config().get("folders", [])]
        except Exception:
            pass
    try:
        for sid, _title in _list_user_spaces():
            fid = _find_registry_folder_id(sid)
            if not fid:
                continue
            for child in _list_child_nodes(sid, fid):
                t = child.get("title") or ""
                if child.get("kind") != "doc" or "设备登记" not in t:
                    continue
                # 用「标题」判断归属本机（doc 标题形如「A 机设备登记」），不依赖内容解析：
                # get-doc-review 返回的是 Table/Paragraph 富文本，不是 markdown 表格。
                if re.match(r"^\s*%s\s*机?\s*设备登记" % re.escape(my), t):
                    plan["library_docs"].append((sid, child.get("id"), t))
    except Exception:
        pass
    plan["infra"].append("随 WB 会话启停 hook（SessionStart）")
    plan["infra"].append("开机自启（launchd / 注册表 / systemd）")
    plan["infra"].append("Syncthing 进程（stop）")
    if purge:
        plan["infra"].append("Syncthing 配置 / 自启 plist / 二进制（--purge）")
        plan["infra"].append("各同步夹同步元数据 .stfolder/.stignore/.stversions（--purge）")
    return plan


def _uninstall_report(plan, apply_flag, mem_action):
    my = plan["my"] or "?"
    head("退出机制预检（本机 %s 机）%s" % (my, "" if apply_flag else " · 演练"))
    info("对端机器：%s" % ("、".join(plan["peers"]) or "（无）"))
    out()
    info("协作区里【本机做的】文件（将清，且删除会传播到其他机器一并清除）：")
    _mine = list(plan["region"]) + list(plan["coop_own"]) + list(plan["a2a_own"])
    for p in _mine:
        out("    - %s" % p)
    if not _mine:
        out("    （无）")
    info("对端自己写的文件不受影响（本机只删自己名下的产物）。")
    out()
    if plan["memory_dir"]:
        ma = mem_action or "（会问用户：留在其他机器上=archive / 一并删掉=delete）"
        info("长期记忆/工作日志：%s（%d 个文件）→ %s" % (plan["memory_dir"], len(plan["memory_files"]), ma))
    else:
        info("长期记忆/工作日志：未发现 memory 目录")
    out()
    if plan["library_docs"]:
        info("资料库自己的「设备登记 doc」（无删节点 API，将重命名标记 + 列清单）：")
        for _sid, _pid, title in plan["library_docs"]:
            out("    - %s" % title)
    else:
        info("资料库：未发现本机登记 doc（或读取不可用）")
    out()
    info("将停止/脱离：")
    for s in plan["infra"]:
        out("    - %s" % s)
    out()
    info("通知：A2A 广播给 %s + 记事本公告（先发，等同步送达后再停）" % ("、".join(plan["peers"]) or "无对端"))


def _uninstall_ask_memory(plan):
    """运行时询问长期记忆/日志是否留在其他机器上。非交互时默认 archive（安全优先）。"""
    if not plan.get("memory_dir"):
        return "keep-none"
    if not sys.stdin.isatty():
        warn("非交互环境，默认「留在其他机器上（归档）」。")
        return "archive"
    try:
        out()
        info("长期记忆/工作日志：%d 个文件" % len(plan["memory_files"]))
        ans = input("  这些记忆/日志要【留在其他机器上】吗？"
                    "留→输入 keep（归档到共享区）；一并删掉→输入 delete（默认 keep）：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "archive"
    return "delete" if ans.startswith("d") else "archive"


def _uninstall_notify(my, peers):
    """发退出通知：A2A 广播 + 记事本公告。返回 (a2a 条数, comms 路径 or None)。"""
    ts_iso = _a2a_now()
    ts_hm = time.strftime("%Y-%m-%d %H:%M")
    n = 0
    journal = _a2a_dir() / ("tasks-%s.json" % my)
    for peer in peers:
        tid = _a2a_next_task_id(my)
        ev = dict({
              "event": "task", "id": tid, "from": my, "to": peer, "skill": "uninstall",
              "state": "submitted", "at": ts_iso, "params": {},
              "text": "%s 机即将退出协作组并卸载同步。请在本机运行 `wbsync prune --peer %s --apply` 完成对端侧收尾"
                      "（清理协作区残留 + 登记表退役 + 从 Syncthing 设备表与各共享夹移除本机）。"
                      "长期记忆/日志已按选择归档/删除。" % (my, my)},
              **_a2a_identity(my))
        _a2a_append_event(journal, my, ev)
        n += 1
    comms_path = None
    try:
        nf = _my_comms_file()
        block = ("\n\n## 【退出通告】%s 机退出协作组（%s）\n\n"
                 "%s 机已执行退出：停用同步、清理本机协作产物、移除资料库登记。\n"
                 "各机请在收到后运行：`wbsync prune --peer %s --apply`"
                 "（清理协作区残留、登记表退役、从设备表与共享夹移除本机）。\n" % (my, ts_hm, my, my))
        with open(nf, "a", encoding="utf-8") as f:
            f.write(block)
        comms_path = str(nf)
    except Exception as e:
        warn("记事本公告写入失败：%s" % e)
    return n, comms_path


def _uninstall_wait_delivery(st, seconds=90, min_grace=20):
    """等本机变更（删/归档）真正送达对端后，才允许停同步。

    原实现只查本机各夹 state==idle——删完文件本机立刻 idle，会「秒过」，
    导致随后停同步时变更还没宣布给对端就被断了（2026-10-14 C 机实测踩坑：
    删了却没传出去，A/B 残留）。修正为：
      ① 先触发全夹 rescan，确保本机变更被索引；
      ② 等本机 idle 且 need=0；
      ③ 等**在线**对端该夹 completion 达 100%（离线/未知的对端跳过，避免死等）；
      ④ 满足 ②③ 后再保底等 min_grace 秒，让 announce 落地。
    返回是否达成；未达成也不要停——由调用方警告。
    """
    if not (st and st.available):
        return False
    try:
        my_id = st.my_id()
    except Exception:
        my_id = None
    folders = [f.get("id") for f in (st.config().get("folders") or [])]
    # ① 触发一次全夹 rescan
    for fid in folders:
        try:
            st.api("POST", "/rest/db/scan?folder=%s" % fid)
        except Exception:
            pass
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        try:
            peers = [d for d in (st.devices() or {}) if d != my_id]
            ok_all = True
            for fid in folders:
                s = st.db_status(fid) or {}
                if s.get("state") != "idle" or (s.get("needTotalItems") or 0) > 0:
                    ok_all = False
                    break
                for p in peers:
                    c = st.api("GET", "/rest/db/completion?folder=%s&device=%s" % (fid, p)) or {}
                    if c.get("remoteState") in (None, "unknown"):
                        continue  # 离线/未知对端跳过，不阻塞
                    if (c.get("completion") or 0) < 100:
                        ok_all = False
                        break
                if not ok_all:
                    break
            if ok_all:
                time.sleep(max(0, min_grace))  # ④ 保底宽限，确保 announce 落地
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _uninstall_archive_memory(my, plan, fallback):
    """把本机 memory/ 归档到协作共享区 + 写标记（目录名带世代）。返回归档目录 or None。"""
    ws = Path(plan["ws"]) if plan.get("ws") else None
    src = Path(plan["memory_dir"]) if plan.get("memory_dir") else None
    if not ws or not src or not src.exists():
        return None
    gen = _ws_my_generation()
    try:
        dev = (read_my_id_from_config(find_config_xml()) or "")[:7]
    except Exception:
        dev = ""
    dst = ws / ("%s-%s-gen%d-%s" % (UNINSTALL_ARCHIVE_PREFIX, my, gen, time.strftime("%Y%m%d")))
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst / "memory"))
        (dst / "README-归档说明.md").write_text(
            "# 退出归档说明\n\n"
            "- 原机器：%s 机（第 %d 代）\n- 设备 ID：%s\n- 归档时间：%s\n"
            "- 来源：本机 memory/（长期记忆 + 工作日志）\n"
            "- 说明：该机已退出协作组，此为其自愿保留的记忆归档，仅供查阅，勿改。\n"
            "  若后续有同序号（%s）的新机器加入，其世代会递增（%s-g2、g3…），可据此与本归档区分。\n"
            % (my, gen, dev or "（未知）", time.strftime("%Y-%m-%d %H:%M"), my, my),
            encoding="utf-8")
        return dst
    except Exception as e:
        warn("记忆归档失败（将保留原地不动）：%s" % e)
        return None


def _uninstall_clear_region(my, plan, fallback, archive_dir=None):
    """清协作区里自己的产物（私有区/专线/能力卡）+ 把本机从登记表「退役」（移入 retired 保留世代）。
    返回 (成功列表, 失败列表, 退役记录 or None)。"""
    targets = list(plan["region"]) + list(plan["coop_own"]) + list(plan["a2a_own"])
    if archive_dir:
        targets = [t for t in targets if str(archive_dir) not in t]
    okl, faill = _trash_paths(targets, fallback_dir=fallback)
    rec = None
    try:
        reg = _read_ws_registry()
        if isinstance(reg, dict):
            rec = _ws_retire(reg, my)   # 移入 retired（记世代/deviceID/时间），而非直接抹掉
            if rec:
                _write_ws_registry(reg)
    except Exception as e:
        warn("登记退役（retired）失败：%s" % e)
    return okl, faill, rec


def _uninstall_mark_library(my, docs):
    """资料库：把本机登记 doc **移入**「已退出机器登记」归档子文件夹，并重命名标记。

    资料库无删除节点 API，但支持 `move-node`——「移走」等于从主登记里清出去
    （绕过"不能删"的限制）。归档子文件夹不存在时降级为「仅重命名标记」。
    返回 (成功数, 移入归档数)。

    注意：rename-node / move-node 都只需 node_id（**不要传 space_id**，否则 API 报
    unknown argument 而静默失败）；且必须检查返回，不能「调用即算成功」。
    """
    n = 0
    moved = 0
    for sid, pid, title in docs:
        new_title = "%s（旧 · 已退出 %s）" % (title, time.strftime("%Y-%m-%d"))
        arch = _ensure_registry_archive_folder(sid)
        did_move = False
        if arch:
            try:
                r = _call_space_api("space.workspace.move-node", node_id=pid, target_parent_id=arch)
                did_move = bool(r)
            except Exception:
                did_move = False
        try:
            r2 = _call_space_api("space.workspace.rename-node", node_id=pid, title=new_title)
        except Exception:
            r2 = None
        if did_move or r2:
            n += 1
        if did_move:
            moved += 1
    # 本机登记 doc 移走后，顺势把主登记排好（现役在前、归档夹置末）
    if moved:
        try:
            _reorder_registry_all_spaces(apply=True, quiet=True)
        except Exception:
            pass
    return n, moved


def _exit_mark_path():
    """本机「已退出」标记（幂等判断用；不在任何同步夹内，纯本机）。"""
    return WB_ROOT / ".wbsync-exited.json"


def _write_exit_mark(my):
    """退出成功后落一个本机标记：重跑/会话恢复时给幂等提示，而非「无法确定后缀」硬失败。"""
    try:
        WB_ROOT.mkdir(parents=True, exist_ok=True)
        _exit_mark_path().write_text(json.dumps(
            {"suffix": my, "hostname": HOSTNAME,
             "exitedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _read_exit_mark():
    p = _exit_mark_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _hook_script_path():
    """随 WB 会话启停用的 hook 脚本落地位置。"""
    return WB_ROOT / "scripts" / "wbsync-autostart-hook.sh"


def _hook_registered():
    """settings.json 里是否仍挂着本 skill 的 SessionStart hook。"""
    p = WB_ROOT / "settings.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        for group in ((d or {}).get("hooks") or {}).get("SessionStart", []):
            for h in (group or {}).get("hooks", []):
                if "wbsync-autostart-hook" in str(h.get("command", "")):
                    return True
    except Exception:
        pass
    return False


def _uninstall_detach(apply_flag, fallback_dir=None):
    """脱离同步组（阶段二第一步——必须在所有「需传播」的动作都完成并送达之后）：
      ① 关开机自启 → ② 注销 SessionStart hook（**先移除注册项、再移走脚本本体**）
         → ③ 停 Syncthing 进程。

    为什么必须移走脚本本体：会话启动 hook 会执行 `wbsync start`。若只改
    settings.json 注册（平台可能保留/恢复该注册），hook 仍在 → 引擎被重新拉起；
    而此时 Syncthing 配置已入暂存 → 引擎会**重新生成默认配置与新证书**，
    产生「新身份的幽灵实例」（2026-10-14 C 机实测踩坑 P0-2）。移走脚本后可
    彻底阻断这条拉起路径。
    返回自检结果 dict。
    """
    notes = {}
    try:
        autostart_disable(apply_flag)
    except Exception as e:
        warn("关闭开机自启失败：%s" % e)

    # ② 注销 hook：注册项 + 脚本本体，双保险
    try:
        class _HookArg:
            pass
        ha = _HookArg()
        ha.action = "off"
        ha.apply = apply_flag
        cmd_hook(ha)
    except Exception as e:
        warn("关闭 hook（移除注册项）失败：%s" % e)
    if apply_flag:
        hs = _hook_script_path()
        if hs.exists():
            try:
                okl, faill = _trash_paths([str(hs)], fallback_dir=fallback_dir)
                if okl:
                    ok("已移走 hook 脚本（防会话启动把引擎拉回）：%s" % hs)
                for p, e in faill:
                    warn("移走 hook 脚本失败 %s（%s）" % (p, e))
            except Exception as e:
                warn("移走 hook 脚本失败：%s" % e)

    # ③ 停 Syncthing
    try:
        st = _st_client()
        if st and st.available:
            st.api("POST", "/rest/system/shutdown")
    except Exception:
        pass

    if apply_flag:
        try:
            notes["hook_script_left"] = _hook_script_path().exists()
            notes["hook_registered"] = _hook_registered()
            st2 = _st_client()
            notes["engine_up"] = bool(st2 and st2.available)
        except Exception:
            pass
    return notes


def _uninstall_purge_infra(plan, fallback, include_bin=False):
    """清理本地同步基础设施：Syncthing 配置目录 / 自启 plist / 各同步夹的同步元数据
    （.stfolder/.stignore/.stversions）。include_bin=True 时额外删 Syncthing 二进制目录。

    守卫（P0-2）：hook 脚本若仍在（会话启动仍可能拉起引擎），则**跳过清理
    Syncthing 配置目录**——否则引擎在无配置状态下启动会重新生成默认配置与
    新证书，制造「新身份的幽灵实例」。
    """
    targets = []
    hook_left = _hook_script_path().exists()
    try:
        cx = find_config_xml()
        if cx:
            if hook_left:
                warn("hook 脚本仍在 %s：跳过清理 Syncthing 配置（避免会话启动后重新生成新身份）。"
                     % _hook_script_path())
            else:
                targets.append(str(Path(cx).parent))
    except Exception:
        pass
    try:
        targets.append(str(mac_plist_path()))
    except Exception:
        pass
    if include_bin:
        try:
            b = find_syncthing_bin()
            if b:
                targets.append(str(Path(b).parent))
        except Exception:
            pass
    for fid, path in plan.get("folders", []):
        if not path:
            continue
        for meta in (".stfolder", ".stignore", ".stversions"):
            p = Path(path) / meta
            if p.exists():
                targets.append(str(p))
    removed, failed = _trash_paths(targets, fallback_dir=fallback)
    return removed, failed


# ---------------------------------------------------------------------------
# prune：清理「已退出对端」在本机的痕迹（对端侧收尾）
#   退出机的 uninstall 只能清自己、并通知对端；设备表条目在**每台机本地**、
#   无法由退出机远程改。所以对端收到退出通知后，需要用 prune 把自己这边
#   该清的一次性清干净：① 协作区里退出机的残留文件（同步态删除 → 传播给
#   其他对端一并清；兜底防 P0-1 未送达时留残）② 登记表把该槽位移入 retired
#   ③ Syncthing 设备表 + 各夹授权移除该设备。
#   默认演练，--apply 才执行；删除走系统回收站。
# ---------------------------------------------------------------------------

def _st_unshare_device(st, device_id):
    """把某设备从所有同步夹的授权列表移除。返回处理的夹数。"""
    n = 0
    try:
        for f in (st.config().get("folders") or []):
            devs = f.get("devices") or []
            if any((d.get("deviceID") == device_id) for d in devs):
                f2 = dict(f)
                f2["devices"] = [d for d in devs if d.get("deviceID") != device_id]
                try:
                    st.api("PUT", "/rest/config/folders/%s" % f.get("id"), f2)
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def _prune_scan(peer):
    """只读：列出清理「已退出对端 <peer>」在本机的痕迹。"""
    my = _my_suffix_short()
    try:
        reg = _read_ws_registry()
    except Exception:
        reg = {"machines": {}}
    plan = {"peer": peer, "my": my, "region": [], "coop_own": [], "a2a_own": [],
            "device_id": None, "in_machines": False, "retired_gen": None,
            "folders": [], "ws": None, "candidate_devices": []}
    try:
        ws = _ws_local_dir(reg, my)
    except Exception:
        ws = None
    plan["ws"] = str(ws) if ws else None
    if ws:
        p = ws / peer
        if p.exists():
            plan["region"].append(str(p))
    try:
        nf = coop_shared() / COMMS_PER_MACHINE.format(suffix=peer)
        if nf.exists():
            plan["coop_own"].append(str(nf))
    except Exception:
        pass
    try:
        card = _a2a_card_path(peer)
        if card.exists():
            plan["a2a_own"].append(str(card))
    except Exception:
        pass
    m = (reg.get("machines") or {}).get(peer)
    if m:
        plan["in_machines"] = True
        plan["device_id"] = m.get("deviceID")
    else:
        for r in sorted((reg.get("retired") or []), key=lambda x: x.get("retiredAt") or ""):
            if r.get("suffix") == peer:
                plan["device_id"] = r.get("deviceID")
                plan["retired_gen"] = r.get("generation")
    st = _st_client()
    if st and st.available:
        if plan["device_id"]:
            try:
                for f in (st.config().get("folders") or []):
                    ids = [d.get("deviceID") for d in (f.get("devices") or [])]
                    if plan["device_id"] in ids:
                        plan["folders"].append(f.get("id"))
            except Exception:
                pass
        else:
            # 登记表取不到 deviceID（已完全退役/从未登记）→ 从 Syncthing 设备表反查
            # 「不在登记表里的设备」作为候选，避免**静默漏清**（B 机 2026-10-14 报 prune-gap）。
            try:
                known = {e.get("deviceID") for e in (reg.get("machines") or {}).values()}
                known |= {e.get("deviceID") for e in (reg.get("retired") or [])}
                try:
                    my_id = st.my_id()
                except Exception:
                    my_id = None
                for dev in (st.devices() or {}).keys():
                    if dev in known or dev == my_id:
                        continue
                    plan["candidate_devices"].append(dev)
            except Exception:
                pass
    return plan


def _accept_peer_apply(st, dev, name=None):
    """把对端 deviceID 加进本机设备表 + 共享所有同步夹（**幂等**）。
    返回 (是否新增设备, 共享成功夹数)。供 `accept-peer` 与 `wake --apply` 复用。"""
    name = name or ("peer-%s" % dev[:7])
    added = False
    if dev not in (st.devices() or {}):
        st.add_device(dev, name)
        added = True
    n = 0
    for fid in sorted((st.folders() or {}).keys()):
        try:
            done, _why = st.share_folder(fid, dev)
            if done:
                n += 1
        except Exception:
            pass
    return added, n


def cmd_accept_peer(args):
    """反向配对：把某对端设备加入本机 Syncthing 设备表，并共享所有同步夹。

    用于「对端已把你加入设备表、但你这边还没有它」的场景（典型：某机重装 / 换设备 ID
    后重新入组）。幂等；默认演练、`--apply` 才落盘。
    注意：**不要用 `wbsync pair`** —— 它只记一个 peer_id，换对端时会连带删除旧对端，
    三机以上会误删已配对的对端（2026-10-14 C 机第 2 代入组时踩到）。
    """
    dev = (getattr(args, "device", "") or "").strip()
    if not dev or len(dev) < 20:
        die("请用 --device 指定对端完整 deviceID（56 位，形如 XXXXXXX-XXXXXXX-…）", 1)
    name = (getattr(args, "name", "") or "").strip() or ("peer-%s" % dev[:7])
    apply_flag = bool(getattr(args, "apply", False))

    st = _st_client()
    if not st or not st.available:
        die("Syncthing 不可用（未运行？先 wbsync start）", 1)

    already = dev in (st.devices() or {})
    folders = sorted((st.folders() or {}).keys())

    head("反向配对：把对端加入本机设备表并共享所有同步夹")
    info("设备 ID：%s" % dev)
    info("设备名：%s" % name)
    info("设备表：%s" % ("已存在（幂等，跳过新增）" if already else "将新增"))
    out()
    info("将共享的同步夹（%d 个）：" % len(folders))
    for fid in folders:
        fobj = (st.folders() or {}).get(fid) or {}
        ids = [d.get("deviceID") for d in (fobj.get("devices") or [])]
        out("    - %-22s %s" % (fid, "已含" if dev in ids else "将追加"))

    if not apply_flag:
        out()
        info("以上为演练（未执行任何改动）。加 --apply 真正执行（幂等）。")
        return 0

    added, n = _accept_peer_apply(st, dev, name)
    if added:
        ok("已加入设备表。")
    else:
        info("设备表已含该设备，跳过。")
    ok("已把设备共享进 %d 个夹。" % n)
    out()
    ok("反向配对完成。对端应很快连上并开始同步（用 `wbsync wake` 看同步完成度）。")
    return 0


def cmd_registry_order(args):
    """重排资料库「多机同步登记」文件夹次序：
       README(置顶) → 现役机登记（按后缀 A/B/C…）→ 其它 doc → 归档文件夹「已退出机器登记」(置末)。
    只调顺序，不增删节点。默认演练，--apply 才执行。
    用途：每台新机写完自己的登记 doc 后跑一次，保证列表始终有序。"""
    apply_flag = bool(getattr(args, "apply", False))
    try:
        spaces = _list_user_spaces()
    except Exception as e:
        die("读取资料库空间失败：%s" % e, 1)
    if not spaces:
        info("未发现可用资料库空间（未登录 / 无权限？），跳过。")
        return 0

    head("重排资料库「多机同步登记」次序")
    total_changed = 0
    total_moves = 0
    for sid, name in spaces:
        parent = _find_registry_folder_id(sid)
        if not parent:
            continue
        try:
            children = _list_child_nodes(sid, parent)
        except Exception:
            continue
        if not children:
            continue
        want = _registry_desired_order(children)
        cur_ids = [c.get("id") for c in children]
        want_ids = [c.get("id") for c in want]
        changed = cur_ids != want_ids
        out()
        info("空间「%s」：" % (name or sid))
        if not changed:
            ok("  次序已是目标（README → 现役机 → 归档），无需调整。")
            continue
        info("  当前 → 目标：")
        for c in children:
            mark = "" if c.get("id") in want_ids else "  (?)"
            info("      现状 [%s] %s%s" % (c.get("kind"), c.get("title"), mark))
        for c in want:
            info("      目标 [%s] %s" % (c.get("kind"), c.get("title")))
        if not apply_flag:
            continue
        c_, n_ = _reorder_registry_folder(sid, parent_id=parent, apply=True)
        total_changed += 1 if c_ else 0
        total_moves += n_
        ok("  已重排（移动 %d 项）。" % n_)

    out()
    if not apply_flag:
        info("以上为演练（未执行任何改动）。加 --apply 真正重排。")
    else:
        ok("完成：%d 个空间发生调整，共移动 %d 项。" % (total_changed, total_moves))
    return 0


SESSION_ENV_VARS = ("CLAUDE_SESSION_ID", "WB_SESSION_ID", "WORKBUDDY_SESSION_ID")
SESSION_CACHE = Path(os.path.expanduser("~/.workbuddy/.wbsync-dialogue-id"))


def _log_sid_degraded(sid, how):
    """记一条"会话身份走了降级路径"（不写 stdout，避免污染每次命令的输出）。

    为什么要记：互斥（锁/预约）全靠 sid 区分"我"和"别人"。一旦降级，两个对话可能互相认错。
    日志只保留最近 20 行，给排查用，不参与任何判定。
    """
    try:
        p = SESSION_CACHE.parent / ".wbsync-sid-degraded.log"
        line = "%s %s pid=%s cwd=%s sid=%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), how, os.getpid(), os.getcwd(), sid)
        lines = []
        if p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-19:]
        lines.append(line.rstrip("\n"))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _session_id():
    """本对话的稳定标识 —— **唯一出口**，lock / reserve / dialogue 三处共用同一口径。

    取数顺序：
      1. 平台注入的环境变量：CLAUDE_SESSION_ID → WB_SESSION_ID → WORKBUDDY_SESSION_ID
      2. 本机稳定缓存文件 `~/.workbuddy/.wbsync-dialogue-id`（跨进程一致）
      3. 生成 uuid 并落盘

    为什么不能退化为 `pid-<pid>`：同一对话的后台任务 / 子 agent / 续跑都是**新进程**，
    pid 必然不同 → 之前 `reserve` 的预约持有者认不出来：写护栏把自己当「别人」拦下、
    心跳续期也续不到（长任务超 TTL 后预约静默失效）。
    """
    for env in SESSION_ENV_VARS:
        v = (os.environ.get(env) or "").strip()
        if v:
            return v
    # 降级路径（env 全缺失：hook / 子进程 / 定时脚本 / 外部终端）
    # 2026-10-17 加固：**缓存文件按工作目录隔离**——原来全机共用一个文件，两个对话都没 env 时
    # 会算出同一个 sid → 锁把它们当同一个人 → **互斥静默失效**。
    # 现在按 cwd 哈希分文件，并留一条降级日志（不写 stdout，避免污染上下文）。
    try:
        _key = hashlib.sha1(str(Path.cwd()).encode("utf-8")).hexdigest()[:8]
        _cache = SESSION_CACHE.with_name(SESSION_CACHE.name + "-" + _key)
    except Exception:
        _cache = SESSION_CACHE
    try:
        if _cache.exists():
            v = _cache.read_text(encoding="utf-8").strip()
            if v:
                _log_sid_degraded(v, "cache")
                return v
    except Exception:
        pass
    import uuid
    sid = uuid.uuid4().hex
    try:
        _cache.write_text(sid, encoding="utf-8")
    except Exception:
        pass
    _log_sid_degraded(sid, "generated")
    return sid


def _my_session_id():
    """本次对话的 session id（= `_session_id()` 唯一出口）。"""
    return _session_id()


def _ws_projects_dir(ws_path=None):
    """WorkBuddy 把「每个工作空间的会话记录」放在 `~/.workbuddy/projects/<编码路径>/`。

    编码规则：把工作空间绝对路径里的**非字母数字字符替换成 `-`**、再去掉首尾 `-`
    （例：`/Users/<用户名>/WorkBuddySync/workspaces/sync-all-A`
     → `Users-<用户名>-WorkBuddySync-workspaces-sync-all-A`）。
    找不到时退化为「去掉所有非字母数字后模糊匹配」。
    """
    base = Path.home() / ".workbuddy" / "projects"
    if not base.exists():
        return None
    target = Path(ws_path or os.getcwd()).resolve()
    cand = base / re.sub(r"[^A-Za-z0-9]", "-", str(target)).strip("-")
    if cand.exists():
        return cand
    key = re.sub(r"[^A-Za-z0-9]", "", str(target)).lower()
    try:
        for d in base.iterdir():
            if d.is_dir() and re.sub(r"[^A-Za-z0-9]", "", d.name).lower() == key:
                return d
    except Exception:
        pass
    return None


def _content_to_text(content):
    """把会话记录里的 content 字段（str 或 block 列表）压成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") in (None, "text") and isinstance(b.get("text"), str):
                    parts.append(b["text"])
        return " ".join(p for p in parts if p).strip()
    return ""


_SESSION_META_HINT = re.compile(r"(OS Version|Shell:|IDE Theme|Workspace Folder|system-reminder)")


def _session_summary(jsonl, max_lines=400, limit=90):
    """给会话起一个人读的标签：优先用平台生成的 `aiTitle`，退化用「首条真人用户消息」。

    只扫 jsonl 头部若干行（不读全文、不清空）。用户消息前通常挂着
    `<system-reminder>` 注入块（含 OS/Shell 等），要剥掉标签再判是不是"真人的话"。
    """
    title, first = "", ""
    try:
        with open(str(jsonl), "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not title and obj.get("aiTitle"):
                    title = re.sub(r"\s+", " ", str(obj["aiTitle"])).strip()
                if first:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                if (msg.get("role") or obj.get("role")) != "user":
                    continue
                t = _content_to_text(msg.get("content"))
                if not t:
                    continue
                cleaned = re.sub(r"<[^>]{0,400}>", " ", t)      # 剥 HTML/XML 标签
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                if len(cleaned) < 6 or _SESSION_META_HINT.search(cleaned):
                    continue                                     # 全是元信息 → 看下一条
                first = cleaned[:limit]
    except Exception:
        pass
    return title or first, title, first


# ---- 省 token 相关（会话体量 / 日志膨胀）----------------------------------
# 结构层提醒：同一对话里历史**每轮都会重放**，对话越长每轮越贵。此前只能靠自觉，
# 现在由心跳顺带记录体量（一次 stat，零额外开销），并在超阈值时给出明确提示。
def _env_num(name, default):
    """读数值型环境变量（供阈值可调）。非法 / 缺失 → 用默认值，绝不抛。"""
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return default
        return float(v) if isinstance(default, float) else int(float(v))
    except Exception:
        return default


CONTEXT_WARN_MB = _env_num("WBSYNC_CONTEXT_WARN_MB", 5.0)      # 本对话超此 MB → 建议收口
CONTEXT_HARD_MB = _env_num("WBSYNC_CONTEXT_HARD_MB", 20.0)     # 超此 MB → 明确要求收口
MEM_LOG_WARN_KB = _env_num("WBSYNC_MEM_LOG_WARN_KB", 50)       # 单份每日日志超此 KB → 提示蒸馏

_TRANSCRIPT_CACHE = {}


def _transcript_path(sid=None):
    """本对话 transcript（`~/.workbuddy/projects/<编码路径>/<sid>.jsonl`）路径；找不到返回 None。

    结果**会缓存（含「找不到」）**——本函数在每条消息的心跳里都会跑，不能每次全盘扫。
    """
    sid = sid or _my_session_id()
    if not sid:
        return None
    if sid in _TRANSCRIPT_CACHE:
        v = _TRANSCRIPT_CACHE[sid]
        return Path(v) if v else None
    cands = []
    try:
        d = _ws_projects_dir()
        if d:
            cands.append(d / (sid + ".jsonl"))
    except Exception:
        pass
    try:
        cands.extend(sorted((WB_ROOT / "projects").glob("*/" + sid + ".jsonl")))
    except Exception:
        pass
    found = ""
    for c in cands:
        try:
            if c.is_file():
                found = str(c)
                break
        except Exception:
            continue
    _TRANSCRIPT_CACHE[sid] = found
    return Path(found) if found else None


def _transcript_bytes(sid=None):
    """本对话 transcript 字节数（拿不到按 0，绝不抛）。"""
    p = _transcript_path(sid)
    if not p:
        return 0
    try:
        return p.stat().st_size
    except Exception:
        return 0


def _cur_ws_dir():
    """当前工作空间目录：从 cwd 向上找 `.../workspaces/<名>` 形态。"""
    try:
        p = Path.cwd().resolve()
    except Exception:
        return Path.cwd()
    for cand in [p] + list(p.parents):
        if cand.parent.name == "workspaces":
            return cand
    return p


def _fat_daily_logs(limit_kb=MEM_LOG_WARN_KB):
    """列出偏大的**每日日志**（`YYYY-MM-DD*.md`），返回 [(文件名, KB)]，按大小降序（最多 5 条）。

    只盯每日日志：长期记忆 / 状态卡片 / DETAIL 是 curated（小且稳定），
    只有每日日志是**只追加**的、会自然膨胀；膨胀后每次「翻日志」都容易整份读 → 浪费。
    """
    ws = _cur_ws_dir()
    suffix = _my_suffix_short() or ""
    dirs = [ws / ".workbuddy" / "memory"]
    if suffix:
        dirs.append(ws / suffix / "memory")
    rows = []
    for d in dirs:
        try:
            for f in d.glob("*.md"):
                n = f.name
                if not (len(n) >= 10 and n[:4].isdigit() and n[4] == "-" and n[7] == "-"):
                    continue                      # 非每日日志（长期记忆 / 状态卡片等）
                kb = int(f.stat().st_size / 1024)
                if kb >= limit_kb:
                    rows.append((n, kb))
        except Exception:
            continue
    rows.sort(key=lambda x: -x[1])
    return rows[:5]


def _context_note():
    """本对话上下文体量提示（未超阈值则零行输出）。"""
    b = _transcript_bytes()
    if b <= 0:
        return
    mb = b / 1048576.0
    if mb < CONTEXT_WARN_MB:
        return
    if mb >= CONTEXT_HARD_MB:
        warn("本对话上下文已 %.1f MB —— 明显偏大：手头这件事办完就另起新对话"
             "（记忆在文件里，不会丢）。" % mb)
    else:
        info("本对话上下文 %.1f MB —— 建议办完当前这件事后另起新对话。" % mb)


# #10：超时阈值（分钟）—— 按 skill 分级；**只算不写**（红线：不自动重发、不发消息，防 agent 互相点火）
# 超时阈值（分钟）：**统一 6 小时**（用户 2026-10-17 定：分级太碎、天天误报）。
# 原来按 skill 分级（notify 10 / verify 15 / review 60 / setup·install·uninstall 240），
# 结果一条"通知"10 分钟就红，红色失去区分度。以后要分级再往这个 dict 里加。
OVERDUE_MIN = {}
OVERDUE_DEFAULT_MIN = 360


def _parse_a2a_ts(s):
    """解析 A2A 时间戳（'2026-10-17T10:00:00+0800'）→ epoch 秒；失败返回 0。"""
    try:
        from datetime import datetime
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except Exception:
        return 0


def _a2a_overdue(now=None):
    """#10：非终态任务中超时的（**只算不写**）。返回 [(task, age_min, limit_min)]。"""
    now = now or time.time()
    rows = []
    try:
        for t in _a2a_all_tasks():
            if (t.get("state") or "") in A2A_TERMINAL_STATES:
                continue
            ts = _parse_a2a_ts(t.get("updatedAt") or t.get("createdAt"))
            if not ts:
                continue
            age = int((now - ts) // 60)
            # #13：rejected 用默认 60m（催对方接受/拒绝）；rework 仍按原 skill 阈值
            if (t.get("state") or "") == "rejected":
                lim = OVERDUE_DEFAULT_MIN
            else:
                lim = OVERDUE_MIN.get((t.get("skill") or "").lower(), OVERDUE_DEFAULT_MIN)
            if age > lim:
                rows.append((t, age, lim))
    except Exception:
        pass
    rows.sort(key=lambda x: -x[1])
    return rows


# --------------------------------------------------------------------------
# 机群状态看板（#15）：一份数据（`board/<后缀>.json`）+ 多视图渲染
# 方案：coop/shared/方案-机群状态看板-20261017.md
# 四条红线：❌心跳刷盘（只在状态真的变了才写）❌中心机汇总（每机只写自己那份）
#          ❌为每个视图造数据源（md/html 都是渲染）❌假实时（必须自曝「数据截至」）
# --------------------------------------------------------------------------
BOARD_SCHEMA = "wbsync-board/1"
BOARD_DIRNAME = "board"
BOARD_STALE_MIN = 60        # ≤60min：🟢 在线（页面不变灰）；>60min：🟡 可能已过时 + 整页变灰
BOARD_OFFLINE_MIN = 120     # >2h：⚪ 疑似离线
BOARD_VIEW_MD = "机群看板.md"
BOARD_VIEW_HTML = "机群看板.html"
BOARD_DIR_SKILLS = 3        # 每个方向后面带几个 skill 名做「任务概况」

# skill 名 → 看板上的中文简述（「稍微多说几个字」但没有长句；未收录的原样显示）
BOARD_SKILL_ZH = {
    "setup": "环境搭建", "install": "安装", "pair": "设备配对",
    "review": "评审文档", "verify": "自检复核", "report": "状态回报",
    "fix": "修问题", "fixnames": "修正文件名", "guard": "闸门设置",
    "uninstall": "卸载退出", "prune": "清理残留痕迹", "accept-peer": "反向配对",
    "notify": "通知同步", "update": "更新版本", "handoff": "交接给对端",
    "deploy": "部署上线", "sync": "同步数据", "test": "跑测试",
    "memory.update": "更新记忆", "token.diet": "精简上下文", "state.card": "更新状态卡",
    "skill.update": "更新 skill", "skill.package": "打包 skill",
    "workspace.apply-grants": "工作空间授权", "workspace.memory-relocate": "迁移记忆目录",
    "workspace.layout": "整理工作空间", "comms.send": "发留言", "comms.read-cursor": "读留言",
    "hook.reinstall": "重装 hook", "a2a-task": "高危任务认领",
}



def _reviews_open(my=None):
    """扫 `reviews/R-*.md` 的 `state:` / `to:` 头，返回 (未处置份数, 最近一份, 无 state 头份数)。

    为什么（R-20261017-B-方案v0.2 [P1-2] + 问题队列 #22）：`reviews/` 是纯文件目录，
    `wake` 从不扫它 → `state: open` 的报告能躺一天没人看见（实测两份各躺 16 / 22 小时）。
    「看不见」比「修得慢」更贵：对方以为问题还坏着，会绕路。

    口径（避免「永远报 8 份」的狼来了）：
    - 只算 **`state:` 不是 closed/resolved/merged/done** 的；
    - `to:` 写了且**不含本机/全员/用户**的 → 不算（那是别人的账）；
    - **没有 state 头的老文件单独计数**，只作提示，不计入「未处置」。
    """
    try:
        d = coop_shared() / "reviews"
        if not d.is_dir():
            return 0, "", 0
        my = (my or _my_suffix_short() or "").strip()
        opened, legacy = [], 0
        for p in sorted(d.glob("R-*.md")):
            try:
                head = p.read_text(encoding="utf-8", errors="replace").splitlines()[:8]
            except Exception:
                continue
            st, to = "", ""
            for line in head:
                t = line.strip().lstrip("-").strip()
                low = t.lower()
                if low.startswith("state:") and not st:
                    st = t.split(":", 1)[1].strip().lower()
                elif low.startswith("to:") and not to:
                    to = t.split(":", 1)[1].strip()
            mine = (not to) or (not my) or (my in to) or any(k in to for k in ("全员", "用户", "all"))
            if not st:
                if mine:
                    legacy += 1
                continue
            if any(k in st for k in ("closed", "resolved", "merged", "done")):
                continue
            if not mine:
                continue
            try:
                opened.append((p.stat().st_mtime, p.name))
            except Exception:
                opened.append((0, p.name))
        opened.sort(reverse=True)
        return len(opened), (opened[0][1] if opened else ""), legacy
    except Exception:
        return 0, "", 0


def _board_primary():
    """共享视图的**主维护机** = 登记表里后缀序号最小的在册机器（A > B > C …）。

    用户 2026-10-17 定：看板由「序号第一个」的机器维护。取活的登记表而非硬编码
    "A"，是为了 A 退役/不在册时能自动交给下一台（B→C…），不会出现没人维护的
    死看板。登记表读不到时兜底返回 "A"（唯一确定值，避免多机都不渲染）。
    """
    try:
        reg = _read_ws_registry() or {}
        keys = [k for k in (reg.get("machines") or {}).keys() if k in _SUFFIXES]
        if keys:
            return sorted(keys, key=lambda s: _SUFFIXES.index(s))[0]
    except Exception:
        pass
    return "A"


def _board_is_primary(my=None):
    my = my or _my_suffix_short()
    return bool(my) and my == _board_primary()


def _board_dir():
    return coop_shared() / BOARD_DIRNAME


def _board_path(suffix):
    return _board_dir() / ("%s.json" % (suffix or "?"))


def _board_view_md():
    return coop_shared() / BOARD_VIEW_MD


def _board_view_html():
    return coop_shared() / BOARD_VIEW_HTML


def _board_probe():
    """探测同步引擎与各夹健康（返回 (running, [(fid, errors, needFiles)])）。"""
    running, folders = False, []
    try:
        st = Syncthing()
        if st.available:
            st.api("GET", "/rest/noauth/health", timeout=3)
            running = True
            for fid in list((st.folders() or {}).keys()):
                d = st.db_status(fid) or {}
                folders.append((fid, d.get("errors"), d.get("needFiles")))
    except Exception:
        pass
    return running, folders


def _board_doing(my):
    """「正在做」= 本机最近活跃对话框的主题（对话上下文不共享，但登记表共享）。"""
    try:
        recs = [r for r in _dialogue_registry_read()
                if (r.get("machine") or "").upper() == (my or "").upper()
                and _dialogue_status(r) != "closed"]
    except Exception:
        return "", ""
    if not recs:
        return "", ""
    recs.sort(key=lambda r: r.get("last_seen_ts") or r.get("hb_ts") or 0, reverse=True)
    # 优先取**声明过主题**的那个对话：hook 会不断登记 topic 为空的记录（对话 id 形如
    # `hook-sid`），若只按「最新」取，认真的主题会被一条空记录盖掉，看板就只剩
    # 「（未声明主题）」——2026-10-17 实际发生过两次。
    # 注意：占位文案「（未声明主题）」本身也是非空字符串，不能只判空 —— 否则 hook
    # 登记的空主题记录（topic 被写成这个占位符）仍会盖掉真实主题。
    _placeholder = ("（未声明主题）", "(未声明主题)")
    _declared = [r for r in recs
                 if (r.get("topic") or "").strip() not in ("",) + _placeholder]
    r = _declared[0] if _declared else recs[0]
    return ((r.get("topic") or "").strip() or "（未声明主题）"), (r.get("dialogue_id") or "")[:8]


def _a2a_relates_to(t, my):
    """这个任务跟 `my` 机有没有关系（我是派活方，或我是接活方）。

    看板的「超时」只列**涉及本机**的：别机之间卡住的任务（如 C→D）放到本机看板上，
    除了制造焦虑没有信息量（用户 2026-10-17：「不涉及本机的话就不要放进去」）。
    判不了（畸形输入）就保留 —— 宁可多显示，也不静默吞掉。
    """
    if not isinstance(t, dict):
        return True
    if not my:
        return True
    if (t.get("from") or "") == my:
        return True
    return (t.get("to") or "") in (my, "全员", "all")


def _board_brief(skills):
    """skill 名 → 看板上的**中文简述**（去重后最多 BOARD_DIR_SKILLS 个，超出标「等 N 类」）。
    未收录的 skill 原样保留——宁可露出英文原名，也不要吞掉信息。"""
    # 保持「先出现的在前」：主任务类型排在前面，比按码点排序更可读（顺序由任务列表决定，稳定）
    zh = list(dict.fromkeys(BOARD_SKILL_ZH.get(x, x) for x in (skills or []) if x))
    brief = "、".join(zh[:BOARD_DIR_SKILLS])
    if len(zh) > BOARD_DIR_SKILLS:
        brief += " 等 %d 类" % len(zh)
    return brief


def _board_snapshot(my=None, state=None, running=None, folders=None):
    """算本机当前快照（只读本地；running/folders 由调用方给，避免每次写都打 REST）。"""
    my = my or _my_suffix_short() or "?"
    tasks = []
    try:
        tasks = _a2a_all_tasks()
    except Exception:
        pass
    mine = [t for t in tasks if t.get("to") in (my, "全员", "all")
            and t.get("state") not in A2A_TERMINAL_STATES]
    theirs = [t for t in tasks if t.get("from") == my
              and t.get("state") not in A2A_TERMINAL_STATES]
    try:
        _od = [x for x in _a2a_overdue() if _a2a_relates_to(x[0], my)]
    except Exception:
        _od = []
    overdue = len(_od)
    # 任务逐条列出（**一行一条**）：用户明确要求「不要省略」——
    # 数字与行数天然一致，不再需要「×N」「另 N 组」这类换算。
    def _items(rows):
        # 防御：畸形/被 stub 的行（None）不该让整块看板崩
        return [[_a2a_dir_tag(t), _board_summary(t)] for t in rows]

    mine_items = _items(mine)
    theirs_items = _items(theirs)
    od_items = _items([_t for _t, _a, _l in _od])

    doing, did = _board_doing(my)
    alerts = []
    if running is False:
        alerts.append("同步引擎未运行")
    for fid, errs, need in (folders or []):
        if errs:
            alerts.append("%s e=%s" % (fid, errs))
        if need:
            alerts.append("%s 待传 %s" % (fid, need))
    try:
        nconf = len(list(coop_shared().glob("*.sync-conflict-*")))
        if nconf:
            alerts.append("coop 冲突副本 %d" % nconf)
    except Exception:
        pass
    try:
        gen = _ws_my_generation(my=my)
    except Exception:
        gen = 1
    return {
        "schema": BOARD_SCHEMA, "machine": my, "gen": gen,
        "state": state or "online", "updated_at": _a2a_now(),
        "doing": doing, "dialogue": did,
        "tasks": {"mine": len(mine), "theirs": len(theirs), "overdue": overdue,
                  "mine_items": mine_items, "theirs_items": theirs_items,
                  "overdue_items": od_items},
        "alerts": alerts,
    }


def _board_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _board_pending_path():
    return _board_dir() / ".render-pending"


def _board_mark_pending(my, reason=""):
    """非主维护机自报后写「请主维护机重渲染」标记（覆盖写，天然无冲突）。

    C 2026-10-17 提的 [P1-1]：自报是"推"，渲染是"拉"，两者之间没有触发桥——主维护机
    不产生事件，共享视图就冻结（用户实测滞后 6 分钟，若 A 收工会到数小时）。
    标记文件让「谁自报、谁举手」，主维护机下次任何动作时顺手消费，不引入守护进程。
    """
    try:
        p = _board_pending_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("%s %s %s\n" % (my or "?", _board_now_iso(), reason or ""), encoding="utf-8")
        return True
    except Exception:
        return False


def _board_drain_pending(my=None):
    """主维护机消费「待渲染」标记：渲染一次并删除标记。返回是否真的做了。

    ⚠️ 必须限定主维护机（2026-10-18 修 14:34 的冲突）：原来任何机器都会 drain，
    于是「B 举手 → B 下次动作自己兑现」+「A 也在渲染」→ 两台同时写 `机群看板.md/html`
    → Syncthing 各留一份 `*.sync-conflict-*`。举手是为了让**主维护机**动手，不是谁都能动。
    """
    try:
        if not _board_is_primary(my):
            return False
        p = _board_pending_path()
        if not p.exists():
            return False
        _board_render(quiet=True)
        try:
            p.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False


BOARD_AUTOPUSH_MIN = 1       # 分钟：沟通后最多这么久内把本机那行推一次资料库表（60s 合并同一分钟的多次动作）
BOARD_AUTOPUSH_SKIP_PREFIXES = ("a2a check", "a2a list", "wake", "board show")


def _board_lastpush_path():
    return WB_ROOT / ".wbsync-board-lastpush.json"


def _board_mark_pushed(my):
    """记「这台机刚刚推过表」——自动推表按它做 60 秒防抖，显式 `board push` 也要记，
    否则手动推完 60 秒内再来一次沟通动作会重复推一遍（多 ~1 秒，无意义）。"""
    try:
        p = _board_lastpush_path()
        d = {}
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            d = {}
        d[str(my)] = time.time()
        atomic_write_json(str(p), d)
    except Exception:
        pass


def _board_maybe_autopush(my, reason=""):
    """顺手推表：本机那行超过 `BOARD_AUTOPUSH_MIN` 分钟没推过就推一次。

    为什么需要（2026-10-18 实测）：`_board_touch` 只写**本地快照**，推资料库表只发生在
    显式的 `board write` / `board push`。没有定时任务时，表里某台机的行会一直停在最后一次
    手动推送的时刻——A 机实测 B 停在 10-17 22:21、C 停在 22:14（13 小时），而本地快照是新的，
    所以 `board show` / `机群看板.md` 看着正常，**只有资料库页面是旧的**，很难发现。

    设计取舍：
    - 有防抖（`BOARD_AUTOPUSH_MIN=1` 分钟），同一分钟的多次沟通合并成一次；纯查询直接跳过；
    - 失败**不抛**，登记到 `.pending-push.json`（已有机制，下次 board 动作会补推）；
    - 失败也记时间戳，避免数据库不可用时每条命令都白试一次；
    - `WBSYNC_BOARD_AUTOPUSH=0` 可关（纯 CLI 无授权环境建议关）。
    """
    try:
        if (os.environ.get("WBSYNC_BOARD_AUTOPUSH") or "1").strip() in ("0", "false", "off", "no"):
            return
        # 纯查询不推：`check` / `list` / `wake` / `show` 只是一次读，不该为它们多付 1.5s 并写一次库。
        # 真正算「沟通」的是会改状态的动作：send / claim / update / release-claim / 上下线 / 登记主题。
        if (reason or "").startswith(BOARD_AUTOPUSH_SKIP_PREFIXES):
            return
        p = _board_lastpush_path()
        last = 0.0
        if p.exists():
            try:
                last = float((json.loads(p.read_text(encoding="utf-8")) or {}).get(str(my)) or 0)
            except Exception:
                last = 0.0
        if time.time() - last < BOARD_AUTOPUSH_MIN * 60:
            return
        try:
            _board_push(my)
            _board_push_pending_clear(my)
        except Exception as e:
            _board_push_pending_set(my, "自动推表失败：%s" % str(e)[:90])
        _board_mark_pushed(my)      # 成功失败都记：失败时不至于每条命令都白试一次
    except Exception:
        pass


def _board_touch(reason="", state=None, running=None, folders=None, force=False):
    """事件驱动落盘本机快照。**失败静默**（看板绝不能拖累主流程）。返回是否真的写了。

    写少读多：内容（除 updated_at）没变且上次写距今 <1h → 不写（防 Syncthing churn）；
    但 >1h 且仍有开着对话 → 刷新一次时间戳（活跃自证，**不是**每秒心跳）。
    """
    try:
        my = _my_suffix_short()
        if not my:
            return False
        p = _board_path(my)
        old = {}
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        if state is None:
            state = old.get("state") or "online"
        snap = _board_snapshot(my, state=state, running=running, folders=folders)
        a = {k: v for k, v in old.items() if k != "updated_at"}
        b = {k: v for k, v in snap.items() if k != "updated_at"}
        if not force and a == b:
            try:
                age = time.time() - os.path.getmtime(str(p))
            except Exception:
                age = 10 ** 9
            if age < 3600:
                _board_autorender(my)   # 快照没变，但共享视图可能被删/首次建立 → 顺手补齐
                _board_maybe_autopush(my, reason)   # 快照没变也刷一下「更新于」（防表长期不动）
                return False
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(p), snap)
        if _board_is_primary(my):
            _board_autorender(my)
        else:
            _board_mark_pending(my, reason)     # 非主维护机：举手，等主维护机渲染
        _board_maybe_autopush(my, reason)       # 顺手推本机那行（60 秒防抖；纯查询跳过）
        return True
    except Exception:
        return False


def _board_read_all():
    rows = []
    d = _board_dir()
    try:
        files = sorted(d.glob("*.json")) if d.exists() else []
    except Exception:
        files = []
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("schema") != BOARD_SCHEMA:
            continue          # 别的东西（损坏/旧格式）不参与渲染
        rec["_file"] = p.name
        rows.append(rec)
    rows.sort(key=lambda r: (str(r.get("machine") or ""), int(r.get("gen") or 1)))
    return rows


def _board_age_min(rec):
    ts = _parse_a2a_ts(rec.get("updated_at"))
    if not ts:
        return 10 ** 6
    return max(0, int((time.time() - ts) // 60))


def _board_live(rec):
    """判活：自述 offline 优先；否则按 updated_at 年龄分档。

    原理：「我下线了」往往正是设备发不出来的那条消息 → 在线靠事件，离线靠超时推断。
    """
    age = _board_age_min(rec)
    if (rec.get("state") or "") == "offline":
        return "⚪ 离线", age
    if age <= BOARD_STALE_MIN:
        return "🟢 在线", age
    if age <= BOARD_OFFLINE_MIN:
        return "🟡 可能已过时", age
    return "⚪ 疑似离线", age


def _board_fmt_age(age):
    if age >= 10 ** 6:
        return "时间未知"
    if age < 60:
        return "%d 分钟前" % age
    if age < 1440:
        return "%d 小时前" % (age // 60)
    return "%d 天前" % (age // 1440)


def _board_stamp(ts_str):
    ts = _parse_a2a_ts(ts_str)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "时间未知"


BOARD_DIGEST_N = 15      # 任务概况最多几个字（用户定：15 字以内，一句话说个大概）


def _board_summary(t, n=BOARD_DIGEST_N):
    """任务概况：**15 字以内的中文短句**（用户 2026-10-17：不要省略号、尽量别出现英文）。

    做法是「清洗 + 取第一个像样的分句」，而不是硬截断：
    ① 去开头标签（【…】/[…]）、去反引号代码、去路径；
    ② 去英文单词（≥2 字母；单个大写字母是机器代号，保留，如 D 机）；
    ③ 按句读切分，取第一个够长的分句（优先 ≥5 个汉字的那句，避免只剩「请」）；
    ④ 仍超长就在标点处收口，**不加省略号**——它是一句概括，不是被剪断的原文。
    ⑤ 清洗后太短（英文被删空）→ 退回 skill 中文名（如「卸载退出」）。
    """
    if not isinstance(t, dict):
        return ""
    cand = ""
    prm = t.get("params")
    if isinstance(prm, dict):
        # 「要做的事」优先：派活时显式写的 --do 最贴合看板需要的口径
        cand = str(prm.get("do") or "").strip() or str(prm.get("summary") or "").strip()
    if not cand:
        msgs = t.get("messages")
        if isinstance(msgs, list) and msgs:
            cand = str(msgs[0] or "")
    if not cand:
        cand = str(t.get("text") or "")

    cand = re.sub(r"^\s*(?:【[^】]*】|\[[^\]]*\])\s*", "", cand)   # 开头标签
    cand = re.sub(r"`[^`]*`", " ", cand)                              # 行内代码
    cand = re.sub(r"\S*[/\\]\S*", " ", cand)                        # 路径
    # 只删「≥2 个字母」的英文单词：单字母是机器代号（D 机 / A 机），删了句子就断
    cand = re.sub(r"(?<![A-Za-z])[A-Za-z]{2,}[A-Za-z0-9_.\-]*", " ", cand)
    cand = re.sub(r"(?<![A-Za-z0-9])v?\d+(?:\.\d+)+(?![0-9])", "", cand)    # v0.1 这类版本号
    cand = re.sub(r"\s*[-–—]{1,}\s*", " ", cand)                     # 残留的 -- / — 破折号
    cand = re.sub(r"\s+", " ", cand).strip()
    cand = re.sub(r"\s+", " ", cand).strip()
    # 汉字之间多余的半角空格去掉（删英文词后常见「闸门 已评审」）
    cand = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cand)

    # 切分句：先按句号/分号/感叹问号，再按逗号顿号
    parts = [x.strip(" \t，,、；;：:。.！!？?") for x in re.split(r"[。；;！!？?\n]", cand)]
    parts = [x for x in parts if x]
    pick = ""
    for x in parts:                       # 优先第一个「有 4 个以上汉字」的分句
        if len(re.findall(r"[\u4e00-\u9fff]", x)) >= 4:
            pick = x
            break
    if not pick:
        pick = parts[0] if parts else ""
    # 贪心合并后面的分句，把 15 字用起来（单句只有 5 个字时太干，比如「解析应回退」）
    else:
        _idx = parts.index(pick)
        for _nx in parts[_idx + 1:]:
            _try = pick + "，" + _nx
            if len(_try) > n:
                break
            pick = _try
    # 还太长 → 在逗号处再切一刀
    if len(pick) > n:
        subs = [y.strip(" ，,、：:") for y in re.split(r"[，,、：:]", pick)]
        acc = ""
        for y in subs:
            if not y:
                continue
            if len(acc) + len(y) <= n:
                acc = (acc + "，" + y) if acc else y
            else:
                break
        pick = acc or pick[:n]
    pick = pick.strip(" \t，,、；;：:。.！!？?-–—")
    # 括号没闭合就把最后那个半截括号连同后面一起丢掉（`用户已授权 彻底退出同步（记忆`）
    if pick.count("（") > pick.count("）"):
        pick = pick[:pick.rfind("（")].strip()
    if pick.count("(") > pick.count(")"):
        pick = pick[:pick.rfind("(")].strip()
    pick = re.sub(r"[，,、\s]*(?:见|详见|参见|请|另|参见)\s*$", "", pick).strip()
    _cjk = len(re.findall(r"[\u4e00-\u9fff]", pick))
    # 残片守卫（B 2026-10-17 实测）：`board push在Win必失败` 删掉 Win 后只剩「在必失败」，
    # 看着像中文其实是断句残渣。规则：汉字太少（<5）或以虚词开头 → 退回 skill 中文名。
    if _cjk < 5 or re.match(r"^[在的了和与及或而就都也还被把对从]", pick):
        return _board_brief([t.get("skill")])
    if _cjk < 2:
        return _board_brief([t.get("skill")])
    return pick[:n].rstrip(" 的了")


def _board_card_note(rec, age=None):
    """每张卡底部的口径说明：这张卡的数字是「该机最后一次自报」，不是实时。

    用户 2026-10-17 两次把「他人行没变」当成 bug —— 所以这句话放在**每张卡下面**
    （页头没人看，卡片底下一定会被看到）。

    两条硬约束：
    ① **不带相对时间**（「N 分钟前」每分钟都变 → 渲染不幂等 → 每次重写 + Syncthing churn）；
    ② **不依赖「谁是渲染者」**（`_my_suffix_short()` 换机就换文案 → 非主维护机 `--force`
       补渲染后，主维护机再渲染会被判成「内容变了」而重写）。
    卡上「更新于 …（N 分钟前）」那行已经给了时间，这里只讲口径。
    """
    return ("该机最后一次自报 · 非实时：对它的任务操作，"
            "要等它下次自报才会反映在这张卡上")


def _board_task_lines(rec, per_group=None):
    """任务区：**三行计数** + 每组下面**一行一条**列全（不折叠、不省略）。

        待我 6
          B → A：状态回报
          B → A：状态回报
          ...
        待对方 3
          A → D：环境搭建

    用户定过的两条硬规矩：① 标题数字必须等于下面的行数（一行一条最省心，
    不用「×N」「另 N 组」让读者自己换算）；② 不要省略——超时也全列出来。
    """
    t = rec.get("tasks") or {}
    lines = []
    for label, cnt_key, item_key, dir_key in (
            ("待我", "mine", "mine_items", "mine_dirs"),
            ("待对方", "theirs", "theirs_items", "theirs_dirs"),
            ("超时", "overdue", "overdue_items", "overdue_dir")):
        n = t.get(cnt_key, 0)
        head = "%s %s" % (label, n)
        if label == "超时" and n:
            head = "⚠️" + head
        lines.append(head)
        items = t.get(item_key) or []
        if items:
            for it in items:
                if isinstance(it, dict):        # 兼容 v4.2–v4.3 的明细对象
                    _dir, _brief = it.get("dir"), BOARD_SKILL_ZH.get(
                        it.get("skill") or "", it.get("skill") or "")
                else:
                    _dir = (it[0] if it else "") or ""
                    _brief = it[1] if len(it) > 1 else ""
                lines.append("  %s%s" % (_dir or "未知",
                                         ("：" + str(_brief)) if _brief else ""))
        else:
            # 旧版对端快照只有聚合（dirs）没有逐条 → 退回按组显示，别留空白
            for d in (t.get(dir_key) or []):
                _dir, _n = d[0], d[1]
                _sk = d[2] if len(d) > 2 else ""
                lines.append("  %s %s 条%s" % (_dir, _n, ("：" + _sk) if _sk else ""))
    return lines


def _board_task_line(rec):
    """任务行：三个计数 + 超时按方向拆开（`A → B 5 · D → A 3`），一眼看出哪两台机卡住。"""
    t = rec.get("tasks") or {}
    line = "待我 %s / 待对方 %s / ⚠️超时 %s" % (
        t.get("mine", 0), t.get("theirs", 0), t.get("overdue", 0))
    dirs = t.get("overdue_dir") or []
    if dirs:
        line += "（%s）" % " · ".join("%s %s" % (k, n) for k, n in dirs)
    return line


def _board_terminal(rows):
    """终端视图（约 40 行内；任务区按「待我/待对方/超时」三组细分，多机时截断）。"""
    head("机群看板")
    if not rows:
        info("还没有任何机器发布快照 → 各机跑一次 `wbsync board write`（之后由事件自动刷新）")
        return
    newest = max((_parse_a2a_ts(r.get("updated_at")) or 0) for r in rows)
    out("  数据截至 %s（各机自报时间取最新；>%d 分钟标「可能已过时」）"
        % (time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "未知",
           BOARD_STALE_MIN))
    show = rows[:6]
    for r in show:
        live, age = _board_live(r)
        out("  %s 机 第%s代  %s  · %s" % (r.get("machine") or "?", r.get("gen") or 1,
                                        live, _board_fmt_age(age)))
        dline = r.get("doing") or "（未声明）"
        if r.get("dialogue"):
            dline += "（对话 %s）" % r["dialogue"]
        out("     正在做：%s" % dline)
        out("     任务：")
        for _tl in _board_task_lines(r):
            out("       %s" % _tl)
        out("     告警：%s" % ("；".join(r.get("alerts") or []) or "无"))
        out("     · %s" % _board_card_note(r, age))
    if len(rows) > len(show):
        out("  …另有 %d 台（`wbsync board --json` 看全）" % (len(rows) - len(show)))


def _board_md(rows):
    newest = max([_parse_a2a_ts(r.get("updated_at")) or 0 for r in rows] or [0])
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "未知"
    L = ["# 机群看板（多机状态）", "",
         "> **数据截至 %s** · 本文件由 A 机渲染（单写者），请勿手改。" % stamp,
         "> 超过 %d 分钟未更新的机器标「可能已过时」，超过 %d 分钟标「疑似离线」——"
         "看板只承诺「最后已知」，不承诺实时。" % (BOARD_STALE_MIN, BOARD_OFFLINE_MIN),
         "> 数据源：`coop/shared/board/<后缀>.json`（每台机只写自己那一份，零写冲突）。",
         "> 渲染于 %s（本视图生成时刻；各机数据以各自「更新于」为准——视图可能落后于最新自报）。" %
         time.strftime("%Y-%m-%d %H:%M"), ""]
    if not rows:
        L.append("_暂无机器发布快照。_")
    for r in rows:
        live, age = _board_live(r)
        L.append("## %s 机 · 第%s代 · %s" % (r.get("machine") or "?", r.get("gen") or 1, live))
        L.append("- 更新于 %s" % _board_stamp(r.get("updated_at")))
        doing = r.get("doing") or "（未声明）"
        if r.get("dialogue"):
            doing += "（对话 %s）" % r["dialogue"]
        L.append("- 正在做：%s" % doing)
        L.append("- 任务：")
        for _tl in _board_task_lines(r):
            L.append(("    - %s" % _tl.strip()) if _tl.startswith("  ")
                     else ("  - **%s**" % _tl.strip()))
        L.append("- 告警：%s" % ("；".join(r.get("alerts") or []) or "无"))
        L.append("  _%s_" % _board_card_note(r, age))
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def _board_html(rows):
    import html as _html

    newest = max([_parse_a2a_ts(r.get("updated_at")) or 0 for r in rows] or [0])
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "未知"
    cards = []
    for r in rows:
        live, age = _board_live(r)
        cls = ("off" if (r.get("state") or "") == "offline"
               else ("ok" if age <= BOARD_STALE_MIN
                     else ("warn" if age <= BOARD_OFFLINE_MIN else "off")))
        doing = _html.escape(r.get("doing") or "（未声明）")
        did = ("（对话 %s）" % r["dialogue"]) if r.get("dialogue") else ""
        alerts = "；".join(r.get("alerts") or []) or "无"
        tasks_html = '<p class="tk-h"><span class="k">任务</span></p><ul class="tk">%s</ul>' % "".join(
            '<li%s>%s</li>' % ("" if x.startswith("  ") else ' class="grp"', _html.escape(x.strip()))
            for x in _board_task_lines(r))
        cards.append(
            '<section class="card {cls}">'
            '<h2>{m} 机<span class="gen">第{g}代</span><span class="live">{live}</span></h2>'
            '<p class="meta">更新于 {stamp}</p>'
            '<p><span class="k">正在做</span>{doing}<span class="dim">{did}</span></p>'
            '{tasks}'
            '<p><span class="k">告警</span>{alerts}</p>'
            '<p class="note">{note}</p>'
            '</section>'.format(
                cls=cls, m=_html.escape(str(r.get("machine") or "?")), g=r.get("gen") or 1,
                live=_html.escape(live), stamp=_html.escape(_board_stamp(r.get("updated_at"))),
                age=_html.escape(_board_fmt_age(age)), doing=doing,
                did=_html.escape(did), tasks=tasks_html,
                note=_html.escape(_board_card_note(r, age)),
                alerts=_html.escape(alerts)))
    body = "\n".join(cards) or '<p class="empty">暂无机器发布快照。</p>'
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>机群看板</title>
<style>
:root{--bg:#f6f7f9;--fg:#1b1f24;--card:#fff;--line:#e4e7ec;--dim:#6b7280;
--ok:#1e9e5a;--warn:#b7791f;--off:#8a8f98;}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e8eaed;--card:#1c2027;
--line:#2b3038;--dim:#9aa3ae;--ok:#4ade80;--warn:#fbbf24;--off:#7b828c;}}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:22px;margin:0 0 6px}
.cut{color:var(--dim);margin:0 0 4px;font-size:13px}
.warnbar{background:rgba(183,121,31,.14);border:1px solid var(--warn);color:var(--fg);
padding:8px 12px;border-radius:8px;margin:12px 0 0;font-size:13px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));margin-top:18px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ok);
border-radius:10px;padding:14px 16px}
.card.warn{border-left-color:var(--warn)}
.card.off{border-left-color:var(--off);opacity:.72}
.card h2{font-size:16px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.gen{font-size:12px;color:var(--dim);font-weight:400}
.live{margin-left:auto;font-size:12px;font-weight:500}
.tk{margin:2px 0 0;padding-left:18px;font-size:13px;list-style:none}
.tk li{margin:1px 0;padding-left:12px;color:var(--dim)}
.tk li.grp{font-weight:600;color:var(--fg);padding-left:0;margin-top:4px}
.tk-h{margin:6px 0 0}
.card.warn .live{color:var(--warn)} .card.ok .live{color:var(--ok)} .card.off .live{color:var(--off)}
.card p{margin:6px 0;font-size:14px}
.card p.note{font-size:11.5px;color:var(--dim);margin:8px 0 0;padding-top:6px;
border-top:1px dashed var(--line);line-height:1.45}
.meta{color:var(--dim);font-size:12px!important}
.k{display:inline-block;min-width:52px;color:var(--dim);font-size:12.5px}
.dim{color:var(--dim);font-size:12.5px}
.empty{color:var(--dim)}
body.stale{filter:grayscale(.55)}
body.stale .grid{opacity:.75}
</style></head>
<body>
<h1>机群看板</h1>
<p class="cut">数据截至 <b>%(stamp)s</b> · 超过 %(stale)d 分钟未更新 → 本页变灰</p>
<p class="cut">渲染于 %(rendered)s（本视图生成时刻；各机数据以各自「更新于」为准）</p>
<div class="warnbar" id="warn" hidden>⚠️ 本页数据已超过 %(stale)d 分钟未更新，可能已过时（看板只承诺「最后已知」，不承诺实时）</div>
<div class="grid">
%(body)s
</div>
<script>
var cutoff = %(epoch)d;
var ageMin = (Date.now() / 1000 - cutoff) / 60;
if (cutoff && ageMin > %(stale)d) { document.body.classList.add('stale');
  document.getElementById('warn').hidden = false; }
</script>
</body></html>
""" % {"stamp": stamp, "stale": BOARD_STALE_MIN, "body": body,
       "rendered": time.strftime("%Y-%m-%d %H:%M"),
       "epoch": int(newest) if newest else 0}


def _board_view_is_stale():
    """有任意一台机快照比共享视图新 → True（供 `board render --if-stale` 定时器用）。

    存在意义：**他人更新不会触发本机渲染**——C 20:00 更新了 C.json，但 A 没事件就不会
    重渲染，页面上就一直停在旧时间（用户 2026-10-17 实测「C 为什么还是 19:44」）。
    定时器每 5 分钟问一次"有新的吗"，有才干活、没有就退出，零写入。
    """
    try:
        md = _board_view_md()
        if not md.exists():
            return True
        base = md.stat().st_mtime
        for p in _board_dir().glob("*.json"):
            try:
                if p.stat().st_mtime > base + 0.5:
                    return True
            except Exception:
                continue
    except Exception:
        return True
    return False


def _board_lock_path():
    return _board_dir() / ".render-lock"


def _board_lease_acquire(holder, ttl=180):
    """渲染租约：**任何机器都能渲染**，但同一时刻只许一台（O_EXCL 原子建锁）。

    为什么不再"只许 A 渲染"：A 不在/不动作时共享视图就冻结（用户实测滞后数小时）。
    改成租约后，谁手上数据最新谁渲染；并发由锁挡住，崩溃由 TTL 兜底（过期可抢占）。
    """
    p = _board_lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    payload = "%s %d\n" % (holder or "?", int(time.time()) + ttl)
    for _ in range(2):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                parts = p.read_text(encoding="utf-8").strip().split()
                exp = int(parts[1]) if len(parts) > 1 else 0
            except Exception:
                exp = 0
            if exp and exp > time.time():
                return False            # 别人在渲，且租约未过期
            try:
                p.unlink()              # 过期租约：抢占
            except Exception:
                return False
    return False


def _board_lease_release():
    try:
        p = _board_lock_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _board_render(force=False, quiet=False):
    """渲染共享视图（md + html）。

    v4.23 起**不再限定主维护机**：任何机器发现"快照比视图新"都可以渲染，靠 `.render-lock`
    租约保证同一时刻只有一个写者（TTL 180 秒，崩溃后可被抢占）。`--force` 仍然越过
    「没变就不重写」与新鲜度判断，用于应急修视图。
    """
    my = _my_suffix_short()
    if not force:
        if not _board_view_is_stale():
            if not quiet:
                info("共享视图已是最新的（各机快照都没有更新），未重写。")
            return False
    if not force and not _board_lease_acquire(my or "?"):
        if not quiet:
            info("另一台机正在渲染共享视图（租约占用中），本次跳过；稍后任何动作会自动重试。")
        return False
    try:
        rows = _board_read_all()
        md = _board_md(rows)
        html = _board_html(rows)
        wrote = False

        def _strip_stamp(t):
            return re.sub(r"^.*渲染于.*$", "", t, flags=re.M)

        for path, text in ((_board_view_md(), md), (_board_view_html(), html)):
            old = ""
            try:
                if path.exists():
                    old = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                old = ""
            if force or _strip_stamp(old) != _strip_stamp(text):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                wrote = True
        if not quiet:
            (ok if wrote else info)("共享视图已%s：%s、%s" % (
                "更新" if wrote else "是最新的（未重写）", BOARD_VIEW_MD, BOARD_VIEW_HTML))
        if wrote:
            # 本机已经把所有人的「举手」兑现了 → 顺手清标记（C 2026-10-17 P3：
            # 直接跑 render 不走 drain，标记会留到下次白跑一遍）
            try:
                _p = _board_pending_path()
                if _p.exists():
                    _p.unlink()
            except Exception:
                pass
        return wrote
    except Exception as e:
        if not quiet:
            warn("渲染共享视图失败：%s" % e)
        return False
    finally:
        _board_lease_release()


def _board_autorender(my):
    """事件落盘后顺带渲染（仅主维护机；内容没变不重写 → 不会 churn）。

    顺带消费「待渲染」标记：别的机器自报后举手，主维护机下次动作时把它兑现，
    这样跨机自报不再需要人催（C 2026-10-17 [P1-1]）。
    """
    try:
        _board_drain_pending(my)    # 只有主维护机能兑现「举手」（否则多机同时渲染 → 冲突副本）
    except Exception:
        pass
    try:
        if _board_is_primary(my):
            _board_render(quiet=True)
    except Exception:
        pass


def _board_ensure_views():
    """主维护机保证共享视图**存在**（首次加载 skill / 文件被删后自愈）。非主维护机不动。

    「一开 skill 就建立看板」的落点：开场必跑 `wake` → 这里补齐 md/html，即使
    各机快照内容没变（`_board_touch` 早退不写）也能把视图建出来。
    """
    try:
        if not _board_is_primary():
            return False
        if _board_view_md().exists() and _board_view_html().exists():
            return False
        return _board_render(quiet=True)
    except Exception:
        return False


BOARD_DB_ID = "KocqqEfVzZRz6xrjzUhDrr"     # 资料库里的「机群看板数据」表
def _library_db_dir():
    """找到 WorkBuddy 内置 `library/database` 脚本目录（跨平台）。

    走 `_library_script()` 的统一探测（env → 各平台默认 → 各盘符根），与 `space_api.py`
    用同一套规则——两处曾经各写一套，改了一个漏另一个（R-20261017-B-board-push-win）。
    """
    env = (os.environ.get("WBSYNC_LIBRARY_DB") or "").strip()
    if env and os.path.isdir(env):
        return env
    return str(_library_script("database", ""))


LIBRARY_DB_DIR = _library_db_dir()


def _board_row_from_snapshot(snap):
    """本机快照 → 数据表一行的 PropertyValue 结构（口径与卡片一致）。"""
    t = snap.get("tasks") or {}
    age = None
    try:
        ts = _parse_a2a_ts(snap.get("updated_at"))
        age = (time.time() - ts) / 60 if ts else None
    except Exception:
        age = None
    state = snap.get("state") or "unknown"
    if age is not None and age >= 120:
        online = "⚪ 疑似离线"
    elif age is not None and age >= 60:
        online = "🟡 可能已过时"
    elif state == "online":
        online = "🟢 在线"
    else:
        online = "⚪ " + state

    def _join(key):
        out = []
        for it in (t.get(key) or []):
            if isinstance(it, list) and len(it) >= 2:
                out.append("%s：%s" % (it[0], it[1]))
            elif isinstance(it, str):
                out.append(it)
        return "\n".join(out)

    _upd = str(snap.get("updated_at") or "")
    return {
        "机器": {"text": snap.get("machine") or "?"},
        "第几代": {"number": int(snap.get("gen") or 1)},
        "在线": {"text": online},
        "更新于": {"text": _upd[:16].replace("T", " ") if len(_upd) >= 16 else _upd},
        "正在做": {"text": snap.get("doing") or ""},
        "对话": {"text": ("对话 " + str(snap.get("dialogue"))) if snap.get("dialogue") else ""},
        "告警": {"text": "；".join(snap.get("alerts") or []) or "无"},
        "待我": {"number": int(t.get("mine") or 0)},
        "待对方": {"number": int(t.get("theirs") or 0)},
        "超时": {"number": int(t.get("overdue") or 0)},
        "待我明细": {"text": _join("mine_items")},
        "待对方明细": {"text": _join("theirs_items")},
        "超时明细": {"text": _join("overdue_items")},
    }


def _lib_db(script, *args):
    """调资料库数据表脚本（需要 WorkBuddy 会话授权；纯 CLI 环境会 E_AUTH_FAILED）。"""
    _dir = _library_db_dir()
    if not os.path.isdir(_dir):
        raise RuntimeError("找不到 WorkBuddy 的 library/database 脚本目录；"
                           "请设置环境变量 WBSYNC_LIBRARY_DB 指向它（当前：%s）" % _dir)
    cmd = [str(sys.executable or "python3"), os.path.join(_dir, script)] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip().splitlines()
    for line in out:
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    raise RuntimeError((r.stderr or r.stdout or "调用失败").strip()[:200])


def _board_pending_push_path():
    return _board_dir() / ".pending-push.json"


def _board_push_pending_set(my, reason=""):
    """推表失败时登记一条待补推（下次任何 board 动作自动重试，不再静默丢）。"""
    try:
        p = _board_pending_push_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        data[str(my)] = {"at": _board_now_iso(), "reason": str(reason)[:120]}
        atomic_write_json(str(p), data)
        return True
    except Exception:
        return False


def _board_push_pending_clear(my):
    try:
        p = _board_pending_push_path()
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        if str(my) in data:
            data.pop(str(my), None)
            atomic_write_json(str(p), data)
    except Exception:
        pass


def _board_flush_pending():
    """有别人的待补推就一起补（**只为本机能写的那行**，不越权写别人的）。"""
    try:
        p = _board_pending_push_path()
        if not p.exists():
            return 0
        my = _my_suffix_short()
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        if str(my) not in data:
            return 0
        _board_push(my)
        _board_push_pending_clear(my)
        return 1
    except Exception:
        return 0


def _board_push(my):
    """把本机快照推进资料库数据表（**有则更新、无则新增**）。

    这是「省 token 的更新方式」：页面永远读这张表，所以不需要发版；
    各机在自己已有的会话里跑一句 `wbsync board push` 就够（1 次命令，不再起新会话）。
    """
    snap = _board_snapshot(my, state="online")
    row = _board_row_from_snapshot(snap)
    res = _lib_db("query_database_record.py", "--database-id", BOARD_DB_ID, "--page-size", "100")
    rid = None
    for rec in (res.get("results") or []):
        if str(rec.get("机器") or "") == str(my):
            rid = rec.get("record_id")
            break
    if rid:
        _lib_db("batch_update_database_records.py", "--database-id", BOARD_DB_ID,
                "--records", json.dumps([{"record_id": rid, "properties": row}], ensure_ascii=False))
        _board_push_pending_clear(my)
        _board_mark_pushed(my)
        return "updated"
    _lib_db("batch_add_database_records.py", "--database-id", BOARD_DB_ID,
            "--records", json.dumps([row], ensure_ascii=False))
    _board_mark_pushed(my)
    return "added"


def _doctor_exit_closure():
    """退出闭环三件套（D 2026-10-16 复盘「建议3」）：产物 / retired / 本机无残留。

    缺一项就只报"进行中，缺 X"——不许报"已完成"。这是防"手段验证当成已执行"的机制化。
    """
    rows = []
    reg = _read_ws_registry() or {}
    retired = {str(e.get("suffix")): e for e in (reg.get("retired") or [])}
    for sfx in sorted(retired):
        prod = sorted(coop_shared().glob("退出-%s-*.md" % sfx))
        try:
            plan = _prune_scan(sfx)
            left = bool(plan.get("residual_files")) or bool(plan.get("shared_folders")) \
                or bool(plan.get("device_entries"))
            # 同一后缀还有新一代在役（如 C 第2代）→ "本机残留"不适用于退役的那一代，
            # 不能因为 machines 里有 C 就判它残留（按后缀扫描看不到 deviceID 代差）。
            still_live = bool(plan.get("in_machines"))
        except Exception:
            left, still_live = None, False
        miss = []
        if not prod:
            miss.append("退出产物")
        if not retired[sfx].get("generation"):
            miss.append("retired 记录")
        if left:
            miss.append("本机残留")
        rows.append((sfx, bool(prod), bool(retired[sfx].get("generation")), left, miss, still_live))
    return rows


def cmd_health(args):
    """自检：一眼看出"哪条通道断了"。只读，不修任何东西。"""
    my = _my_suffix_short()
    head("wbsync 自检（只读）")
    warns = []

    # ① 各机最后自报
    out()
    out("① 各机自报")
    for rec in _board_read_all():
        age = _board_age_min(rec)
        who = rec.get("machine") or "?"
        txt = _board_fmt_age(age) if age is not None else "时间未知"
        flag = "✅" if (age is not None and age < 60) else ("⚠️" if age is None or age < 120 else "❌")
        out("   %s %s 机 · %s · %s" % (flag, who, txt, _board_live(rec)))
        if flag != "✅":
            warns.append("%s 机最后自报 %s" % (who, txt))

    # ② 共享视图是否落后
    out()
    out("② 共享视图")
    if _board_view_is_stale():
        try:
            base = _board_view_md().stat().st_mtime
            lag = int((time.time() - base) / 60)
        except Exception:
            lag = -1
        out("   ⚠️ 有快照比视图新（视图约落后 %s 分钟）→ 跑 `wbsync board render`" % (lag if lag >= 0 else "?"))
        warns.append("共享视图落后")
    else:
        out("   ✅ 视图不落后")

    # ③ A2A 日志健康
    out()
    out("③ A2A 日志")
    for j in sorted(_a2a_all_journals()):
        n = -1
        try:
            n = len(_a2a_read_journal_strict(j).get("events") or [])
            state = "✅"
        except Exception as e:
            state = "❌ 读不出（%s）" % str(e)[:40]
            warns.append("%s 日志读不出" % j.name)
        bak = j.with_name(j.name + ".bak")
        out("   %s %-14s 事件 %s%s" % (state, j.name, n if n >= 0 else "?",
                                       " · 有 .bak" if bak.exists() else ""))
        wiped = list(j.parent.glob(j.stem + ".wiped-*.json"))
        if wiped:
            out("      · 存在事故留证 %d 份（%s）" % (len(wiped), wiped[-1].name))

    # ④ 未消费的放行票
    out()
    out("④ 放行票")
    try:
        tickets = sorted(_authz_dir().glob("*.json"))
    except Exception:
        tickets = []
    if tickets:
        alive = 0
        for t in tickets:
            try:
                rec = json.loads(t.read_text(encoding="utf-8"))
                exp = float(rec.get("expiresAt") or 0)
                if exp > time.time():
                    alive += 1
            except Exception:
                pass
        out("   ⚠️ 有 %d 张票（未过期 %d 张）→ 不用就删掉，避免被误用" % (len(tickets), alive))
        warns.append("残留放行票 %d 张" % len(tickets))
    else:
        out("   ✅ 无残留票")

    # ⑤ 退出闭环（三件套）
    out()
    out("⑤ 已退机器的闭环（产物 / retired / 本机残留）")
    rows = _doctor_exit_closure()
    if not rows:
        out("   · 没有 retired 记录")
    for sfx, prod, reg_ok, left, miss, still_live in rows:
        if still_live:
            # 后缀已有新一代在役：只报"这一代的历史留证"，不当成待办
            out("   ⏭ %s 机（旧代）：产物%s · retired有 · 现役是新一代 → 残留判定不适用"
                % (sfx, "有" if prod else "缺（历史缺证，不阻断）"))
            continue
        okall = prod and reg_ok and (left is False)
        out("   %s %s 机：产物%s · retired%s · 本机残留%s"
            % ("✅" if okall else "⚠️", sfx,
               "有" if prod else "缺", "有" if reg_ok else "缺",
               "无" if left is False else ("有" if left else "未知")))
        if not okall:
            warns.append("%s 机退出闭环缺：%s" % (sfx, "、".join(miss) or "未知"))

    # ⑥ 数据表（需要会话授权，失败不报错）
    out()
    out("⑥ 资料库数据表")
    try:
        res = _lib_db("query_database_record.py", "--database-id", BOARD_DB_ID, "--page-size", "10")
        rows2 = res.get("results") or []
        out("   ✅ 可读，%d 行" % len(rows2))
        latest = max([str(r.get("更新于") or "") for r in rows2] or [""])
        if latest:
            out("      · 最新一行「更新于」%s" % latest)
    except Exception as e:
        out("   · 跳过（%s）" % str(e)[:60])

    out()
    if warns:
        warn("发现 %d 处需要处理：" % len(warns))
        for w in warns:
            out("   · %s" % w)
        return 1
    ok("全部正常。")
    return 0


def cmd_board(args):
    """机群看板：一份数据（board/<后缀>.json）+ 三视图（终端 / md / HTML）。

    - `board`（默认）：**纯读**，合并各机快照现场渲染终端视图（不改任何文件）
    - `board write`：探测本机状态并发布 `board/<后缀>.json`（事件之外的手动刷新）
    - `board render`：渲染共享视图 md + html（仅 A 机；`--force` 越过单写者限制）
    """
    action = (getattr(args, "action", None) or "show").lower()
    my = _my_suffix_short()

    if action == "write":
        running, folders = _board_probe()
        wrote = _board_touch("board write", state="online", running=running,
                             folders=folders, force=True)
        snap = {}
        try:
            snap = json.loads(_board_path(my).read_text(encoding="utf-8"))
        except Exception:
            snap = {}
        if wrote:
            ok("已发布本机快照：board/%s.json（%s）" % (my, snap.get("updated_at") or ""))
            # 顺手推数据表：**快照与表是两条通道**，只写快照的话，页面（读表）看不到变化
            # —— 用户 2026-10-17 就撞上过「B 明明 21:09 更新了，页面还是 16:45」。
            if not getattr(args, "no_push", False):
                try:
                    _what = _board_push(my)
                    info("已同步数据表（%s）：页面「机群看板（实时）」会立即反映。"
                         % ("更新" if _what == "updated" else "新增"))
                except Exception as e:
                    _board_push_pending_set(my, e)
                    info("数据表未同步（%s）；已记入待补推队列，下次任意 board 动作会自动重试。"
                         % str(e)[:80])
            if not _board_is_primary(my):
                info("已请主维护机（%s 机）重渲染共享视图；它下次跑任意命令时生效。"
                     "要看现算合并视图可 `wbsync board`。" % _board_primary())
        else:
            warn("快照写入失败（看板不可用不影响同步与协作）")
        return 0

    _board_flush_pending()      # 上次推表失败的话，这次先补（只补本机那行）

    if action == "push":
        try:
            what = _board_push(my)
        except Exception as e:
            die("推送数据表失败：%s\n  提示：需要 WorkBuddy 会话授权（纯终端/定时器会 E_AUTH_FAILED）" % e)
        ok("已%s资料库「机群看板数据」：%s 这一行" % ("更新" if what == "updated" else "新增", my))
        info("页面「机群看板（实时）」会立即反映（它读这张表，不需要发版）。")
        return 0

    if action == "render":
        if getattr(args, "if_stale", False) and not _board_view_is_stale():
            return 0                       # 没有任何快照比视图新 → 静默跳过（零写入、零 churn）
        _board_render(force=bool(getattr(args, "force", False)))
        return 0

    # 兜底渲染：他人更新（C/D 写自己的快照）**不会触发本机渲染**，视图会停在旧时间。
    # 看之前若发现快照比视图新，先补一次（仅主维护机），失败不影响查看。
    if _board_view_is_stale() and _board_is_primary(my):
        try:
            _board_render(quiet=True)
        except Exception:
            pass

    rows = _board_read_all()
    if getattr(args, "json", False):
        out(json.dumps({"view": "board", "machines": len(rows), "rows": rows},
                       ensure_ascii=False, indent=2))
        return 0
    _board_terminal(rows)
    return 0


# --------------------------------------------------------------------------
# 意图分发：`wbsync do "<自然语言意图>"`（#18 · P0-1 入口层）
#
# 动机：命令 50+ 条，新对话/新用户的问题不是「命令多」，而是**决策难**——不知道该用哪条、
#       什么时候不该用。用错比不会用更伤（口碑崩在"用错"上）。
# 做法：**纯关键词 → 命令映射**。绝不调模型（否则每次都要花 token）、绝不执行、绝不写文件，
#       只给一条建议命令 + 要点。让 agent 只需记 1 条命令，而不是记住 52 条。
# --------------------------------------------------------------------------
DO_RULES = [
    {"id": "dispatch", "label": "派活给另一台机器 / 另一个对话",
     "kw": ["派活", "派给", "分派", "分配给", "交给", "安排给", "接力", "assign", "delegate",
            "dispatch"],
     "cmd": 'wbsync a2a send --to <后缀> --skill <技能名> --text "<≤2行摘要>" --file <相关文件>',
     "why": "要回执、能数欠条 → 走 A2A；正文留在共享文件里，只传指针（纪律 §14）",
     "alt": ["wbsync a2a check --full   # 看回执（默认截 60 字，务必 --full）",
             "wbsync a2a list           # 看全部任务与欠账"]},

    {"id": "receipt", "label": "回执：我这边做完了",
     "kw": ["回执", "完成了", "做完了", "已做完", "交付了", "报告完成", "收尾了", "completed"],
     "cmd": 'wbsync a2a update --id <任务ID> --state completed --text "<做了什么>" --file <产物>',
     "why": "回执要带产物路径；打回/返工也走同一条命令换状态",
     "alt": ["wbsync a2a check --full   # 先看全文再回执（可能漏掉关键指令）"]},

    {"id": "reject", "label": "打回 / 要求返工",
     "kw": ["打回", "返工", "重做", "退回", "不合格", "reject", "rework"],
     "cmd": 'wbsync a2a update --id <任务ID> --state rejected --reason "<≤100字>"',
     "why": "打回后对方要 `rework`（同 id 内 attempt+1，上限 3 轮），不接受文字申辩",
     "alt": ["wbsync a2a update --id <ID> --state canceled   # 直接作废"]},

    {"id": "claim", "label": "认领任务：我来做",
     "kw": ["认领", "我来做", "接了", "领任务", "开始做", "claim"],
     "cmd": "wbsync a2a claim --id <任务ID>",
     "why": "认领带租约 + fence 防脑裂；`requires=delete` 的任务要先有人签名放行票",
     "alt": ["wbsync a2a check --full   # 认领前先看全文"]},

    {"id": "status", "label": "看全局状态：谁在、有没有活、健康吗",
     "kw": ["全局", "状态", "怎么样", "啥情况", "进度", "健康", "谁在", "有活吗", "有没有待办",
            "status", "brief"],
     "cmd": "wbsync wake --full",
     "why": "现算、永远新鲜；只要一行摘要就用 `wbsync brief`",
     "alt": ["wbsync brief             # 更简（引擎/对端/待办/同步各一行）",
             "wbsync board             # 快照视图（别的机器在干什么）"]},

    {"id": "board", "label": "机群看板：各机正在做什么",
     "kw": ["看板", "谁在忙", "各机在干", "在线不", "在干什么", "board"],
     "cmd": "wbsync board",
     "why": "快照（>1h 未更新会变灰）；要「此刻」准确就用 wake --full",
     "alt": ["wbsync board render      # 重出 md/HTML（仅主维护机）"]},

    {"id": "edit", "label": "改共享文件（方案/记忆/skill/协作区）",
     "kw": ["改共享", "修改协作", "改文档", "改记忆", "写方案", "改skill", "改代码", "占锁",
            "加锁", "lock", "reserve"],
     "cmd": 'wbsync lock claim "<说明>"    # 同机多对话\nwbsync reserve claim <文件> --reason "<说明>"   # 跨机器',
     "why": "同机用 lock、跨机用 reserve；不占锁就改 = 静默覆盖 / 生 .sync-conflict",
     "alt": ["wbsync lock status / wbsync reserve status"]},

    {"id": "danger", "label": "危险动作：删除 / 卸载 / 踢掉某台机",
     "kw": ["删除", "删掉", "卸载", "退出协作", "离开协作", "踢掉", "移除机器", "退役", "清残留",
            "prune", "uninstall"],
     "cmd": "wbsync authorize <动作：prune|uninstall> --peer <后缀> --apply   # 人签，然后再跑真命令",
     "why": "高危三入口（prune/uninstall/forget）要一次性放行票（TTL 300s，用后即删）",
     "danger": "prune|uninstall|forget",
     "alt": ["wbsync uninstall        # 不加 --apply 只演练（先给人看会发生什么）"]},

    {"id": "stop", "label": "全组急停 / 解除急停",
     "kw": ["急停", "停下", "刹车", "全部暂停", "别动", "停手", "brake"],
     "cmd": "wbsync brake --on        # 解除：wbsync brake --off",
     "why": "STOP 文件经 Syncthing 秒级传全组；期间写操作全拒、只读照常",
     "alt": ["wbsync brake             # 看当前状态与未消费的票"]},

    {"id": "troubleshoot", "label": "出问题了：不同步 / 报错 / 没反应",
     "kw": ["不同步", "报错", "失败", "排查", "没反应", "连不上", "不生效", "坏了", "诊断",
            "doctor"],
     "cmd": "wbsync doctor",
     "why": "先归因再动手：三层（文件层/登记层/工作空间）哪层断，结论完全不同",
     "alt": ["wbsync verify            # 自检闭环", "wbsync network diag      # 跨网连不上时"]},

    {"id": "setup", "label": "新机器入组：安装 / 配对 / 建工作空间",
     "kw": ["安装", "新机器", "新电脑", "加一台", "同步到另一台", "配对", "配好", "install",
            "setup", "pair"],
     "cmd": "wbsync install --apply → wbsync setup --apply → wbsync id → wbsync pair <对端ID> --apply",
     "why": "逐条有先后；每步先不加 --apply 演练一次更稳",
     "alt": ["wbsync plan              # 看现状再决定跑哪步",
             "读 references/onboarding.md  # 新机引导全流程"]},

    {"id": "msg", "label": "留言 / 通知 / 广播（不需要回执）",
     "kw": ["留言", "通知", "告诉大家", "广播", "传话", "发个消息", "告知", "comms"],
     "cmd": 'wbsync comms send --to 全员 --title "<短标题>" --text "<内容>"',
     "why": "要回执/闭环就别用 comms，改用 a2a；组外窗口用 `wbsync tell`",
     "alt": ["wbsync comms check       # 有没有新留言（只看未读）"]},

    {"id": "mem", "label": "翻记忆 / 日志：上次说到哪了",
     "kw": ["记忆", "日志", "上次", "翻一下记录", "mem", "历史记录"],
     "cmd": "wbsync mem read",
     "why": "增量读（读过不重读，推进游标）——省 token，别整篇 Read 大日志",
     "alt": ["wbsync mem index         # 先看全貌索引再定点读"]},

    {"id": "handoff", "label": "交接 / 接手：换人接着做",
     "kw": ["交接", "接手", "换人", "我下班", "handoff", "pickup"],
     "cmd": 'wbsync handoff --to <后缀> --log-ref <日志段> --summary "<一句话>"',
     "why": "生成带指针的 A2A 任务 + 自动 reserve；接手方用 `wbsync pickup`",
     "alt": ["wbsync pickup"]},

    {"id": "revert", "label": "回滚 / 恢复被改坏的东西",
     "kw": ["回滚", "恢复", "改坏了", "还原", "revert"],
     "cmd": "⚠️ `revert` 命令尚未提供（待办 #19）",
     "todo": "备份已在同目录 `*.wbsync-bak-*`，Syncthing 回收站在 `.stversions/`；先手工拷回",
     "why": "不要用 `guard off` / 清 `.stversions` 去绕过，那会把可恢复变成不可恢复"},

    {"id": "archive", "label": "归档：协作区太乱 / 清已闭环的文档",
     "kw": ["归档", "整理文档", "协作区太乱", "清理已闭环", "archive"],
     "cmd": "wbsync archive-doc <文件> --apply",
     "why": "移入「历史协作/」并加标记 + 登记索引（不加 --apply 只演练）",
     "alt": ["wbsync registry-order --apply   # 重排资料库登记次序"]},

    {"id": "sessions", "label": "对话/会话：开了几个、谁在并行",
     "kw": ["几个对话", "会话", "谁在开", "并行", "dialogue", "sessions"],
     "cmd": "wbsync sessions          # 看并行；登记主题用 wbsync dialogue register --topic \"...\"",
     "why": "上下文不共享、记忆共享——改共享文件前先登记主题，别撞车"},

    {"id": "unattended", "label": "无人值守 / 自动干活 / 定时提醒",
     "kw": ["无人值守", "自动干活", "定时", "自己动", "门铃", "不在电脑前", "tick"],
     "cmd": "⚠️ `tick` 门铃档尚未提供（#17 L0 待做）",
     "todo": "计划：只读任务、有活发系统通知、token 严格 0、不写任何文件；L1 执行档默认关",
     "why": "临时替代：把 `wbsync wake` 挂在开机/定时器上手动跑（不惊动模型）"},
]

# 完全没命中时的兜底候选（最容易「其实想问这几个」的三个）
DO_FALLBACK = ("status", "board", "troubleshoot")


def _do_norm(s):
    return (s or "").strip().lower()


def _do_match(intent):
    """纯关键词打分：命中关键词越长越具体，分数越高。返回 [(score, rule)]，降序。"""
    low = _do_norm(intent)
    hits = []
    for idx, rule in enumerate(DO_RULES):
        best = 0
        for kw in rule["kw"]:
            k = _do_norm(kw)
            if k and k in low:
                best = max(best, len(k))
        if best:
            hits.append((best, idx, rule))
    hits.sort(key=lambda x: (-x[0], x[1]))
    return [(s, r) for s, _i, r in hits]


def _do_rule_by_id(rid):
    for rule in DO_RULES:
        if rule["id"] == rid:
            return rule
    return None


def _do_print_rule(rule, indent="    "):
    out("%s→ %s" % (indent, rule["label"]))
    cmd = rule["cmd"].replace("\n", "\n" + indent + "  ")
    out("%s建议：%s" % (indent, cmd))
    if rule.get("danger"):
        out("%s⚠️ 高危：票 TTL 300s、用后即删；**agent 不代签**（自己会签 = 闸门不存在）" % indent)
    if rule.get("todo"):
        out("%s⚠️ %s" % (indent, rule["todo"]))
    if rule.get("why"):
        out("%s要点：%s" % (indent, rule["why"]))
    for a in rule.get("alt") or []:
        out("%s相关：%s" % (indent, a))


def cmd_do(args):
    """把一句自然语言意图映射到该跑的命令（纯关键词；不调模型、不执行、不写盘）。"""
    parts = getattr(args, "intent", None) or []
    intent = " ".join(parts).strip() if isinstance(parts, (list, tuple)) else str(parts).strip()
    hits = _do_match(intent) if intent else []

    if getattr(args, "json", False):
        out(json.dumps({
            "intent": intent,
            "matched": [{"id": r["id"], "label": r["label"], "score": s,
                         "cmd": r["cmd"], "why": r.get("why", ""),
                         "todo": r.get("todo", "")} for s, r in hits[:3]],
            "rules": len(DO_RULES),
        }, ensure_ascii=False, indent=2))
        return 0

    head("wbsync do · 意图 → 命令（纯关键词，不调模型；只给建议，不执行）")

    if not intent:
        out("  用法：wbsync do \"帮我把这活派给 B\"")
        out("  共 %d 条规则（命中不了会退回 3 条候选，不做语义猜测）：" % len(DO_RULES))
        out("")
        for rule in DO_RULES:
            out("  %-12s %s" % (rule["id"], rule["label"]))
            out("               %s" % rule["cmd"].split("\n")[0])
        out("")
        info("全部命令：wbsync --help ；单条参数：wbsync <命令> -h")
        return 0

    out("  意图：%s" % intent)
    if not hits:
        warn("没认出明确意图（关键词匹配，不做语义猜测）——最接近的 3 条：")
        out("")
        for rid in DO_FALLBACK:
            rule = _do_rule_by_id(rid)
            if rule:
                _do_print_rule(rule, indent="    ")
                out("")
        info("全部命令：wbsync --help ；单条参数：wbsync <命令> -h")
        return 0

    top = hits[0][0]
    winners = [r for s, r in hits if s == top][:2]
    for rule in winners:
        _do_print_rule(rule)
        out("")
    others = [r for s, r in hits if s != top][:2]
    if others:
        out("  也可能想问：%s" % " / ".join(r["label"] for r in others))
    info("参数细节：wbsync <命令> -h ；不确定就先跑 `wbsync wake`（看全局）或 `wbsync doctor`（先归因）")
    return 0


def _drift_note():
    """#12 状态漂移：队列写 open、changelog 已提及该 #N → 一行提醒（正常**零输出**）。"""
    try:
        qpath = Path.cwd() / "A" / "memory" / "问题队列-skill优化.md"
        cpath = Path(__file__).resolve().parent.parent / "references" / "changelog.md"
        if not (qpath.exists() and cpath.exists()):
            return
        qtxt = qpath.read_text(encoding="utf-8", errors="replace")
        ct = cpath.read_text(encoding="utf-8", errors="replace")
        opens = set()
        for m in re.finditer(r"^### #(\d+)[^\n]*", qtxt, re.M):
            start = m.end()
            nxt = qtxt.find("### #", start)
            seg = qtxt[start: nxt if nxt > 0 else len(qtxt)]
            # 只认「- **状态**：xxx」这一行（避免抓到正文里含"状态"的行）
            st = re.search(r"^\s*-\s*\*{0,2}状态\*{0,2}\s*[：:]\s*(.+)$", seg, re.M)
            if st and re.match(r"\*{0,2}\s*open", st.group(1).strip(), re.I):
                opens.add(m.group(1))
        if not opens:
            return
        mentioned = set(re.findall(r"#(\d+)", ct))
        done = sorted(opens & mentioned, key=lambda x: int(x))
        if done:
            info("⚠️ 队列 open 但 changelog 已提及：%s —— 可能已完成未关（核实后 closed）"
                 % "、".join("#" + x for x in done))
    except Exception:
        pass


def _fat_log_note():
    """每日日志偏大提示（无超限日志则零行输出）。"""
    fat = _fat_daily_logs()
    if not fat:
        return
    txt = "、".join("%s %dKB" % (n, kb) for n, kb in fat[:3])
    more = ("（另有 %d 份）" % (len(fat) - 3)) if len(fat) > 3 else ""
    info("日志偏大可蒸馏：%s%s —— 要点并入长期记忆后归档该日志（先蒸馏再删）。" % (txt, more))


def cmd_sessions(args):
    """列出**本机、本工作空间**的所有对话（会话）。

    为什么需要：WorkBuddy 的会话之间**上下文不共享**，但**记忆文件是共享的** ——
    同机并行对话会写同一批文件（日报/长期记忆），「同机双写 = 覆盖（无 Syncthing 冲突）」。
    本命令把「本机还有哪些并行对话」摊开，供任何对话开场自检。
    纯本地文件读取（不依赖 Syncthing / 不联网）。
    """
    ws = (getattr(args, "ws", None) or "").strip() or None
    d = _ws_projects_dir(ws)
    head("本机 · 本工作空间的对话（会话）")
    if not d:
        info("找不到会话目录（~/.workbuddy/projects/ 下无匹配）。")
        info("工作空间：%s" % (Path(ws).resolve() if ws else Path.cwd()))
        return 0

    rows = []
    try:
        for p in sorted(d.glob("*.jsonl")):
            st = p.stat()
            rows.append({"id": p.stem, "bytes": st.st_size, "mtime": st.st_mtime})
    except Exception as e:
        die("读取会话目录失败：%s" % e, 1)
    # #5：本机开/关判定（dialogue-registry 主信号 + jsonl mtime 兜底），开着的排前
    reg_map = {r.get("dialogue_id"): r for r in _dialogue_registry_read()}
    for r in rows:
        r["lstatus"], r["lage"] = _session_local_status(r["id"], r["mtime"], reg_map)
    _lorder = {"active": 0, "idle": 1, "closed": 2}
    rows.sort(key=lambda r: (_lorder.get(r["lstatus"], 9), -r["mtime"]))

    limit = int(getattr(args, "limit", 8) or 8)
    for i, r in enumerate(rows):
        if i < limit:
            _label, _title, _first = _session_summary(d / (r["id"] + ".jsonl"))
            r["title"] = _title
            r["first"] = _first
    shown = rows[:limit]

    if getattr(args, "json", False):
        out(json.dumps({
            "workspace": str(Path(ws).resolve() if ws else Path.cwd()),
            "projects_dir": str(d),
            "count": len(rows),
            "open_count": sum(1 for r in rows if r["lstatus"] != "closed"),
            "closed_count": sum(1 for r in rows if r["lstatus"] == "closed"),
            "current_session": _my_session_id(),
            "sessions": [{
                "id": r["id"], "short": r["id"][:8], "bytes": r["bytes"],
                "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["mtime"])),
                "title": r.get("title", ""),
                "first_user_msg": r.get("first", ""),
                "local_status": r["lstatus"],
                "idle_min": r["lage"],
                "is_current": (r["id"] == _my_session_id()),
                "probably_current": (i == 0),
            } for i, r in enumerate(shown)],
        }, ensure_ascii=False))
        return 0

    _open_n = sum(1 for r in rows if r["lstatus"] != "closed")
    _closed_n = len(rows) - _open_n
    info("工作空间：%s" % (Path(ws).resolve() if ws else Path.cwd()))
    info("会话数：%d（本机：开着 %d / 已关闭 %d；显示 %d 个）" % (len(rows), _open_n, _closed_n, len(shown)))
    out()
    _me_sid = _my_session_id()
    _SESS_CN = {"active": "开着·活跃", "idle": "开着·空闲", "closed": "已关闭"}
    for i, r in enumerate(shown):
        if r["id"] == _me_sid:
            tag = " ← **本对话**"
        elif r["lstatus"] != "closed" and i == 0:
            tag = " ← 最近活跃"
        else:
            tag = ""
        _szflag = " ⚠️偏大" if r["bytes"] >= CONTEXT_HARD_MB * 1048576 else ""
        _st = _SESS_CN.get(r["lstatus"], "?")
        _when = ("  · %d 分钟前" % r["lage"]) if r["lstatus"] != "closed" else ""
        out("  %s  %s  %s%s%s  [%s]%s" % (r["id"][:8], time.strftime("%m-%d %H:%M", time.localtime(r["mtime"])),
                                          human(r["bytes"]), tag, _szflag, _st, _when))
        if r.get("title"):
            out("      主题：%s" % r["title"])
        elif r.get("first"):
            out("      首条：%s" % r["first"])
    _others_open = [r for r in rows if r["id"] != _me_sid and r["lstatus"] != "closed"]
    out()
    if _others_open:
        info("⚠️ 本机还有 %d 个**其它对话开着的**：改共享文件（记忆 / skill 代码 / coop 文档）前先想清楚——" % len(_others_open))
        info("   同机多对话改同一文件 **不产生冲突副本，而是静默覆盖**。改前可 `wbsync lock claim \"用途\"`。")
    # ---- 跨机 · 跨世代对话框（来自 coop/shared/.dialogue-registry，随同步可见）----
    my_suffix = _my_suffix_short()
    remote = [r for r in _dialogue_registry_read()
              if r.get("machine") != my_suffix and r.get("dialogue_id") != _me_sid]
    if remote:
        r_open = [r for r in remote if _dialogue_status(r) != "closed"]
        r_closed = [r for r in remote if _dialogue_status(r) == "closed"]
        info("跨机对话框（其它机器，随 Syncthing 同步可见）：开着 %d / 已关闭 %d"
             % (len(r_open), len(r_closed)))
        for r in r_open + r_closed:
            out(_dialogue_line(r, _me_sid))
    info("⚠️ 对话之间**上下文不共享**；但**记忆文件共享** → 日报**只追加**、长期记忆**只局部改**。")
    info("   跨机/跨世代协作信号：先 `wbsync dialogue register --topic \"<主题>\"` 宣告自己在做什么；")
    info("   对端机器是否在线，看 `wbsync wake`（本命令只管本机对话 + 跨机对话框列表）。")
    return 0


# ---- 跨对话框协作身份（对话框登记 · 2026-10-15）----
# 让「每台电脑 × 每个对话框 × 每个世代」互相识别，并区分「开着 / 已关闭」：
#   - 每个对话框在 coop/shared/.dialogue-registry/<dialogue_id>.json 写一条记录；
#   - 该目录随 Syncthing 同步 → 任意机器/世代可见；
#   - 开/关状态靠**显式 hook 事件**（不靠猜文件时间）：
#       SessionStart → state=open ；SessionEnd → state=closed ；UserPromptSubmit → 保活
#   - 崩溃兜底：仍为 open 但超时无活动 → 展示为「疑似已关闭（未收到关闭事件）」；
#   - 机器与世代取自工作空间登记表（machines[suffix].generation / retired[]）。

DIALOGUE_REGISTRY_STALE_MIN = 30      # 「活跃」判定的新鲜度窗口（分钟）
SESSION_ACTIVE_MIN = 5                # 本机会话「开着·活跃」窗口（分钟）；5–30 空闲，>30 无活动推定已关闭（#5）
DIALOGUE_OPEN_SUSPECT_HOURS = 12      # open 但超这么久没活动 → 疑似已关闭
DIALOGUE_CLOSED_RETAIN_DAYS = 3       # 「已关闭」记录保留天数（供回溯）
DIALOGUE_OPEN_ABANDON_DAYS = 7        # open 且超这么久 → 视为废弃，清理


def _dialogue_registry_dir():
    d = coop_shared() / ".dialogue-registry"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _dialogue_id():
    """稳定标识当前对话框 —— 与 lock / reserve 共用 `_session_id()` 唯一出口。

    三处口径必须一致：否则「只注入 WORKBUDDY_SESSION_ID、未注入 CLAUDE_SESSION_ID」的环境下，
    reserve 记的 holder_session 与写护栏认定的「自己」对不上 → 自己的预约反把自己拦下。
    """
    return _session_id()


def _dialogue_my_identity():
    """返回 (suffix, generation, device_id) —— 本机在登记表中的位置（世代已含）。"""
    my = _my_suffix_short()
    gen = _ws_my_generation() if my else 1
    dev = _my_device_id() or ""
    return my, gen, dev


def _dialogue_record_path(did):
    return _dialogue_registry_dir() / ("%s.json" % did)


def _dialogue_load(did):
    fp = _dialogue_record_path(did)
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8", errors="replace") or "{}")
    except Exception:
        pass
    return {}


def _dialogue_save(rec):
    did = rec.get("dialogue_id")
    if not did:
        return
    try:
        _dialogue_record_path(did).write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _dialogue_write(state=None, topic=None, locked=None, reason=None, sid=None,
                    refresh_identity=False):
    """写入/更新本对话框记录。

    state：open（开着）/ 传 "closed" 表示关闭；不传则沿用原值（默认 open）。
    sid：显式指定对话框 ID —— hook 会用它带进来的 session_id（权威来源）。
    refresh_identity：强制重算本机身份（后缀 / 世代 / 设备 ID）。默认 False ——
      高频心跳（UserPromptSubmit，每条消息都跑）**复用记录里已有的身份**，跳过
      Syncthing REST 调用与世代计算（实测每次消息约省 1.3s）；SessionStart /
      SessionEnd 传 True 强制刷新，避免换机 / 世代递增后记录长期滞留旧身份。
    """
    did = sid or _dialogue_id()
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")
    now_ts = int(time.time())
    rec = _dialogue_load(did)
    if (not refresh_identity and rec.get("machine") and rec.get("machine") != "?"
            and rec.get("device_id")):
        suffix = rec.get("machine")
        gen = rec.get("generation") or 1
        dev = rec.get("device_id") or ""
    else:
        suffix, gen, dev = _dialogue_my_identity()

    if not rec.get("opened_at"):
        rec["opened_at"] = now_iso
        rec["opened_ts"] = now_ts
    if topic is not None:
        rec["topic"] = topic
    elif not rec.get("topic"):
        rec["topic"] = "（未声明主题）"
    if locked is not None:
        rec["locked"] = list(locked)

    rec["state"] = state or rec.get("state") or "open"
    rec["last_seen"] = now_iso
    rec["last_seen_ts"] = now_ts
    rec["hb_ts"] = now_ts              # 兼容旧字段名
    rec["last_heartbeat"] = now_iso
    if rec["state"] == "closed":
        rec["closed_at"] = now_iso
        rec["closed_ts"] = now_ts
        if reason:
            rec["close_reason"] = reason

    rec.update({
        "dialogue_id": did,
        "machine": suffix or "?",
        "generation": gen,
        "device_id": dev,
        "host": socket.gethostname() if hasattr(socket, "gethostname") else "",
    })
    try:
        _cb = _transcript_bytes(did)          # 顺带记体量（一次 stat）：供 brief/sessions 提示
        if _cb:
            rec["context_bytes"] = _cb
    except Exception:
        pass
    _dialogue_save(rec)
    return rec


def _dialogue_heartbeat_write(topic=None, locked=None):
    """兼容入口（wake / brief / register 调用）：标为 open 并刷新时间。"""
    return _dialogue_write(state="open", topic=topic, locked=locked)


def _dialogue_status(rec):
    """归类：active（开着·活跃）/ idle（开着·空闲）/ suspect（疑似已关闭）/ closed（已关闭）。"""
    state = (rec.get("state") or "open").lower()
    if state == "closed":
        return "closed"
    ts = rec.get("last_seen_ts") or rec.get("hb_ts") or 0
    if not ts:
        return "idle"
    age = time.time() - float(ts)
    if age > DIALOGUE_OPEN_SUSPECT_HOURS * 3600:
        return "suspect"
    if age <= DIALOGUE_REGISTRY_STALE_MIN * 60:
        return "active"
    return "idle"


def _session_local_status(sid, mtime, reg_map):
    """本机会话「开着 / 已关闭」判定：dialogue-registry(主信号) + jsonl mtime(兜底)。

    为什么：`sessions` 本机列表原先只扫 `*.jsonl` 按 mtime 展示，不区分开/关 →
    把已关闭的历史会话也数进「还有 X 个其它对话」，并发风险基数高估（#5）。
    主信号 = dialogue-hook 三事件写入的 `.dialogue-registry` 记录（本机对话也在其中，
    复用而非另造 session-state，避免第四套原语）；老会话 / hook 未覆盖者用 mtime 兜底。
    三级：active（≤SESSION_ACTIVE_MIN 分钟）/ idle（≤DIALOGUE_REGISTRY_STALE_MIN）/ closed。
    注：SessionEnd 实测不总触发，故 registry 仍 open 但久无活动时按年龄兜底判 closed。
    返回 (status, age_min)。
    """
    now = time.time()
    rec = reg_map.get(sid)
    if rec:
        st = _dialogue_status(rec)
        ts = rec.get("last_seen_ts") or rec.get("hb_ts") or mtime
        age = max(0, int((now - float(ts)) // 60))
        if st == "closed" or st == "suspect":
            return "closed", age
        if age <= SESSION_ACTIVE_MIN:
            return "active", age
        if age <= DIALOGUE_REGISTRY_STALE_MIN:
            return "idle", age
        return "closed", age            # registry 仍 open 但久无活动 → 推定已关闭
    # 无 registry 记录（老会话 / hook 未覆盖）→ mtime 兜底
    age = max(0, int((now - float(mtime)) // 60))
    return ("idle" if age <= DIALOGUE_REGISTRY_STALE_MIN else "closed"), age


DIALOGUE_STATUS_CN = {
    "active": "开着 · 活跃",
    "idle": "开着 · 空闲",
    "suspect": "疑似已关闭（未收到关闭事件）",
    "closed": "已关闭",
}


def _dialogue_stale_for_drop(rec, now=None):
    """是否已过保留期、应清理。"""
    now = now if now is not None else time.time()
    st = _dialogue_status(rec)
    ts = rec.get("last_seen_ts") or rec.get("hb_ts") or 0
    age_days = (now - float(ts)) / 86400.0 if ts else 999.0
    if st == "closed":
        return age_days > DIALOGUE_CLOSED_RETAIN_DAYS
    return age_days > DIALOGUE_OPEN_ABANDON_DAYS


def _dialogue_registry_read(prune=True):
    """读出全部对话框记录（开着 + 已关闭），并按保留规则清理过老记录。"""
    d = _dialogue_registry_dir()
    out = []
    try:
        files = list(d.glob("*.json"))
    except Exception:
        return out
    for fp in files:
        try:
            rec = json.loads(fp.read_text(encoding="utf-8", errors="replace") or "{}")
        except Exception:
            continue
        if _dialogue_stale_for_drop(rec):
            if prune:
                try:
                    fp.unlink()
                except Exception:
                    pass
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("last_seen_ts") or r.get("hb_ts") or 0, reverse=True)
    return out


def _dialogue_registry_prune():
    """按保留规则清理过老记录（返回清理条数）。"""
    d = _dialogue_registry_dir()
    try:
        files = list(d.glob("*.json"))
    except Exception:
        return 0
    n = 0
    for fp in files:
        try:
            rec = json.loads(fp.read_text(encoding="utf-8", errors="replace") or "{}")
        except Exception:
            try:
                fp.unlink()
                n += 1
            except Exception:
                pass
            continue
        if _dialogue_stale_for_drop(rec):
            try:
                fp.unlink()
                n += 1
            except Exception:
                pass
    return n


def _heartbeat_age_min(hb_ts):
    if not hb_ts:
        return -1
    return max(0, int((time.time() - float(hb_ts)) // 60))


def _dialogue_announce(topic=None):
    """安全包装：在 wake/brief 里自动宣告本对话框（写心跳），失败静默。"""
    try:
        _dialogue_heartbeat_write(topic=topic)
    except Exception:
        pass


def _dialogue_age_ts(rec):
    """展示用的时间戳：已关闭看关闭时间，其它看最近活动时间。"""
    if _dialogue_status(rec) == "closed":
        return rec.get("closed_ts") or rec.get("last_seen_ts") or rec.get("hb_ts") or 0
    return rec.get("last_seen_ts") or rec.get("hb_ts") or 0


def _dialogue_line(rec, my_sid=None, with_machine=True):
    st = _dialogue_status(rec)
    age = _heartbeat_age_min(_dialogue_age_ts(rec))
    when = ("%d 分钟前" % age) if age >= 0 else "时间未知"
    cur = " ← **本对话**" if (my_sid and rec.get("dialogue_id") == my_sid) else ""
    prefix = ("[%s机 第%d代] " % (rec.get("machine"), rec.get("generation", 1))) if with_machine else ""
    _cb = rec.get("context_bytes")          # 体量由心跳记入记录，随同步可见（他机也能看到）
    sz = ("  %s" % human(_cb)) if isinstance(_cb, (int, float)) and _cb else ""
    return "  %s%s  %s%s  · %s %s%s" % (
        prefix, (rec.get("dialogue_id") or "")[:8],
        rec.get("topic") or "（无主题）", sz, DIALOGUE_STATUS_CN[st], when, cur)


def cmd_dialogue(args):
    """跨对话框协作身份：登记 / 关闭 / 列出（区分开着与已关闭）。

    - `register [--topic X] [--locked f1 f2]`：声明本对话框在做什么（协作信号，别人能看见）。
    - `hb`：hook 事件入口（由 wbsync-dialogue-lifecycle.sh 调用）——读 stdin JSON 的
      `hook_event_name`：SessionStart→open、SessionEnd→closed、UserPromptSubmit→保活。
      **完全静默、恒 0 退出**（否则会污染上下文 / 影响会话）。
    - `leave`：本对话框结束 → 标记「已关闭」（保留记录供回溯）。
    - `ls [--json]`：列出全部对话框，区分「开着（活跃/空闲）· 疑似已关闭 · 已关闭」。
    """
    action = (getattr(args, "action", None) or "ls").lower()

    if action == "hb":
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            d = json.loads(raw or "{}")
        except Exception:
            d = {}
        ev = (d.get("hook_event_name") or "").strip()
        sid = (d.get("session_id") or "").strip() or None
        reason = (d.get("reason") or "").strip() or None
        try:
            if ev == "SessionEnd":
                _dialogue_write(state="closed", reason=reason, sid=sid, refresh_identity=True)
                _reservation_release_session(sid)  # 关窗即释放本对话持有的跨机文件预约，防死锁
                _board_touch("session end", state="offline")   # #15：看板自述离线
            elif ev == "SessionStart":
                _dialogue_write(state="open", sid=sid, refresh_identity=True)
                _reservation_refresh_session(sid)  # 对话还在用 → 续期本对话的跨机文件预约
                _board_touch("session start", state="online")  # #15：看板自述上线
            elif ev == "UserPromptSubmit":
                # 每条消息都跑：复用记录里已有身份 —— 不触发 Syncthing REST / 世代计算（约省 1.3s）
                _dialogue_write(state="open", sid=sid)
                _reservation_refresh_session(sid)
            else:
                _dialogue_write(sid=sid)
        except Exception:
            pass
        return 0

    if action == "register":
        topic = (getattr(args, "topic", "") or "").strip() or None
        locked = list(getattr(args, "locked", []) or [])
        rec = _dialogue_heartbeat_write(topic=topic, locked=locked)
        ok("已宣告本对话框（%s 机 第%d代）：%s" % (rec.get("machine"), rec.get("generation", 1), rec.get("topic")))
        _board_touch("dialogue register")      # #15：换主题即刷看板「正在做」
        return 0

    if action == "leave":
        rec = _dialogue_write(state="closed", reason="leave")
        _board_touch("dialogue leave", state="offline")   # #15
        if rec.get("dialogue_id"):
            ok("已把本对话框标记为「已关闭」。")
        else:
            info("本对话框没有登记记录。")
        return 0

    # ls
    recs = _dialogue_registry_read()
    my_sid = _my_session_id()
    open_recs = [r for r in recs if _dialogue_status(r) != "closed"]
    closed_recs = [r for r in recs if _dialogue_status(r) == "closed"]

    if getattr(args, "json", False):
        out(json.dumps({
            "current_dialogue": my_sid,
            "count": len(recs),
            "open_count": len(open_recs),
            "closed_count": len(closed_recs),
            "dialogues": [{
                "dialogue_id": r.get("dialogue_id"), "short": (r.get("dialogue_id") or "")[:8],
                "machine": r.get("machine"), "generation": r.get("generation", 1),
                "topic": r.get("topic", ""), "state": _dialogue_status(r),
                "state_cn": DIALOGUE_STATUS_CN[_dialogue_status(r)],
                "age_min": _heartbeat_age_min(_dialogue_age_ts(r)),
                "is_current": (r.get("dialogue_id") == my_sid), "host": r.get("host", ""),
            } for r in recs],
        }, ensure_ascii=False))
        return 0

    head("跨机 · 跨世代对话框（开着 %d / 已关闭 %d）" % (len(open_recs), len(closed_recs)))
    if not recs:
        info("登记区为空（coop/shared/.dialogue-registry 无记录）。")
        return 0
    if open_recs:
        out("开着：")
        for r in open_recs:
            out(_dialogue_line(r, my_sid))
    if closed_recs:
        out("已关闭：")
        for r in closed_recs:
            out(_dialogue_line(r, my_sid))
    out()
    info("开/关状态来自显式 hook 事件（SessionStart→open、SessionEnd→closed、UserPromptSubmit→保活）；")
    info("未收到关闭事件的按时间兜底显示「疑似已关闭」。宣告自己在做什么：wbsync dialogue register --topic \"<主题>\"")
    return 0


# ---- 同机「编辑锁」：给"改共享文件"一个机制化的互斥，防同机多对话静默覆盖 ----
LOCK_FILE_NAME = ".wbsync-edit-locks.json"
LOCK_DEFAULT_TTL_MIN = 30


def _lock_file():
    """编辑锁文件：**机器本地**（不随同步走）——同机多对话是本地并发，跨机另有约定。"""
    return WB_ROOT / LOCK_FILE_NAME


def _locks_read():
    try:
        d = json.loads(_lock_file().read_text(encoding="utf-8"))
        return d.get("locks") or []
    except Exception:
        return []


def _locks_write(locks):
    try:
        atomic_write_json(str(_lock_file()), {"locks": locks,
                                              "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        warn("写锁文件失败：%s" % e)


def _locks_purge(locks):
    now = time.time()
    return [l for l in locks if (l.get("expires") or 0) > now]


def cmd_lock(args):
    """同机「编辑锁」：动**共享文件**（记忆 / skill 代码 / coop 文档）前先占锁，避免同机多对话静默覆盖。

    为什么需要：Syncthing 只在**跨设备**版本分歧时产生 `*.sync-conflict-*`；**同一台机**上两个对话
    改同一文件**不会**有冲突副本，而是**后写覆盖先写**（无声、最难发现）。锁是"进场前问一声"。
    锁文件在**机器本地**（`~/.workbuddy/.wbsync-edit-locks.json`），带 TTL 自动过期，不会卡死。
    """
    action = (getattr(args, "action", None) or "status").lower()
    me_sid = _my_session_id()
    me_suffix = _my_suffix_short() or "?"
    me = "%s/%s" % (me_suffix, me_sid[:8])

    locks = _locks_purge(_locks_read())
    if action == "status":
        head("同机编辑锁（%s）" % _lock_file())
        info("本对话：%s" % me)
        if not locks:
            ok("当前无锁。")
        for l in locks:
            who = "（本对话）" if l.get("sid") == me_sid else ""
            _sc = l.get("scope") or ""
            _sctag = ("  范围=%s" % _sc) if _sc else "  范围=全局"
            info("  %s  %s  用途=%s  剩余 %d 分钟%s%s"
                 % (l.get("owner"), l.get("at", "")[:16], l.get("purpose") or "-",
                    max(0, int(((l.get("expires") or 0) - time.time()) // 60)), who, _sctag))
        out()
        info("占锁：wbsync lock claim \"改 wbsync.py\" [--ttl 30] ；释放：wbsync lock release")
        return 0

    if action == "claim":
        purpose = (getattr(args, "purpose", "") or "").strip() or "改共享文件"
        ttl = int(getattr(args, "ttl", LOCK_DEFAULT_TTL_MIN) or LOCK_DEFAULT_TTL_MIN)
        others = [l for l in locks if l.get("sid") != me_sid]
        mine = [l for l in locks if l.get("sid") == me_sid]
        if others and not getattr(args, "force", False):
            warn("已有其它对话持锁 —— **先别改**（同机双写=静默覆盖）：")
            for l in others:
                info("  %s  用途=%s  剩余 %d 分钟" % (l.get("owner"), l.get("purpose") or "-",
                                                   max(0, int(((l.get("expires") or 0) - time.time()) // 60))))
            info("可等它过期/对方 `wbsync lock release`；确认无冲突再 `--force`。")
            return 2
        rec = {"sid": me_sid, "owner": me, "purpose": purpose, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "expires": time.time() + ttl * 60}
        locks = others + ([] if mine else []) + [rec]
        _locks_write(locks)
        ok("已占锁（%d 分钟）：%s — %s" % (ttl, me, purpose))
        return 0

    if action == "release":
        rest = [l for l in locks if l.get("sid") != me_sid]
        if len(rest) == len(locks):
            info("本对话原本没有占锁。")
            return 0
        _locks_write(rest)
        ok("已释放本对话的锁。")
        return 0

    die("未知动作：%s（应为 status / claim / release）" % action, 1)


# ---- 跨机器文件预约锁（reserve / release / reservations）----
# 与「同机编辑锁」(lock) 的区别：lock 是本机本地、防同机多对话；reserve 写进 coop/shared
# （随 Syncthing 同步到三机），防 A/B/C 不同机器**同时改同一份文件**。任何文件类型通用
# （媒体 / 文档 / 代码 / 工程），同一时刻只允许一个持有者，靠心跳 + TTL 防卡死。
# 2026-10-18 用户拍板 60 → 30（#19）：机器会随手关，长 TTL 会把文件锁死太久；
# 30 分钟 ≈ 6 个心跳周期，够容忍准实时同步延迟，关机后最多半小时自动回池。
RESERVE_DEFAULT_TTL_MIN = 30

RESERVE_FILE = ".file-reservations.json"


def _reserve_path():
    return coop_shared() / RESERVE_FILE


def _reservation_key(path):
    """预约表的主键：**跨机可比**的路径键（三机绝对路径各不相同，但相对结构一致）。

    取键顺序（必须与写护栏 `wbsync-write-guard.sh` 里的 python 侧**严格一致**，
    否则两边算出不同键 → 拦截静默失效）：
      1. 在**同步根**内 → 相对同步根的路径（保持既有键形式，向后兼容）；
      2. 否则在**用户主目录**内 → 相对主目录并加 `~/` 前缀
         （如 skill 代码 `~/.workbuddy/skills/workbuddy-sync-all/…`：三机同构，
         于是「A/B/C 同时改 skill 代码」这个最高危场景也有跨机互斥）；
      3. 都不在 → 绝对路径（仅本机可见，无法跨机比对）。
    """
    p = Path(path)
    if not p.is_absolute():
        # 相对路径的基准（2026-10-18 #19 修）：**先按同步根**，再退回 CWD。
        # 原因：纪律与 `--file` 都约定「相对路径 = 相对同步根」（`_to_syncroot_rel`），
        # 但这里原来按 CWD 解析——在 `workspaces/sync-all-A` 里跑
        # `reserve claim coop/shared/x.md` 会锁到一个**不存在的路径键**
        # （`~/WorkBuddySync/workspaces/sync-all-A/coop/shared/x.md`），
        # 而写护栏按同步根算出的是 `coop/shared/x.md` → 查不到 → 拦截静默失效。
        # 只在「同步根下确实存在该文件」时才改基准，原有 CWD 语义保留为退路。
        _sr_cand = cfg_sync_root() / p
        p = _sr_cand if _sr_cand.exists() else (Path(os.getcwd()) / p)
    try:
        p = p.resolve()
    except Exception:
        pass
    for root, prefix in ((cfg_sync_root(), ""), (HOME, "~/")):
        try:
            rel = p.relative_to(Path(root).resolve())
        except Exception:
            continue
        return (prefix + rel.as_posix()) if prefix else rel.as_posix()
    return str(p)


def _reserve_abs(key):
    """把预约键还原成「本机绝对路径」用于展示（`~/…` 键 → 主目录；绝对键原样返回）。"""
    k = str(key)
    if k.startswith("~/"):
        return str(HOME / k[2:])
    if os.path.isabs(k):
        return k
    return str(cfg_sync_root() / k)


def _reserve_read():
    try:
        return json.loads(_reserve_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _reserve_write(d):
    try:
        _reserve_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(_reserve_path()), d)
        return True
    except Exception as e:
        warn("写预约文件失败：%s" % e)
        return False


def _reserve_purge(d, now=None):
    now = now or time.time()
    return {k: v for k, v in d.items() if (v.get("expires") or 0) > now}


def _reserve_is_active(rec, now=None):
    if not rec:
        return False
    return ((rec.get("expires") or 0) > (now or time.time()))


def _reserve_identity():
    me_sid = _my_session_id()
    me_suffix = _my_suffix_short() or "?"
    return me_sid, me_suffix, "%s/%s" % (me_suffix, (me_sid or "?")[:8])


def _reservation_release_session(sid):
    """释放某对话持有的全部预约（SessionEnd 自动调用，fail-open 静默）。"""
    if not sid:
        return
    try:
        d = _reserve_purge(_reserve_read())
        left = {k: v for k, v in d.items() if v.get("holder_session") != sid}
        if len(left) != len(d):
            _reserve_write(left)
    except Exception:
        pass


def _reservation_refresh_session(sid):
    """续期本对话持有的全部预约（随对话心跳 UserPromptSubmit 触发）。

    为什么需要：预约只有 TTL 到期 + SessionEnd 释放，若一次长任务（如长剪辑）持续
    超过 TTL，预约会**静默失效** → 别的机器可趁虚而入。把这本对话的预约到期时间随
    每次用户交互往后推，即「只要对话还在用、预约就还活着」；真的长时间无交互（> TTL）
    才过期——这既是续期也是安全兜底。fail-open 静默。"""
    if not sid:
        return
    try:
        d = _reserve_purge(_reserve_read())
        now = time.time()
        changed = False
        for v in d.values():
            if v.get("holder_session") == sid:
                v["last_heartbeat"] = _a2a_now()
                v["expires"] = now + (v.get("ttl_min") or RESERVE_DEFAULT_TTL_MIN) * 60
                changed = True
        if changed:
            _reserve_write(d)
    except Exception:
        pass


def _reservation_claim(path, reason="", ttl=RESERVE_DEFAULT_TTL_MIN, force=False):
    """占预约。返回 (ok, msg)。fail-open：异常时放行。"""
    try:
        key = _reservation_key(path)
        d = _reserve_purge(_reserve_read())
        me_sid, me_suffix, me = _reserve_identity()
        now = time.time()
        existing = d.get(key)
        if existing and _reserve_is_active(existing, now) and existing.get("holder_session") != me_sid and not force:
            return False, ("已有其它机器占用 —— 先别改：%s 机（%s）剩余 %d 分钟"
                           % (existing.get("holder_machine"), (existing.get("holder_session") or "")[:8],
                              max(0, int(((existing.get("expires") or 0) - now) // 60))))
        try:
            did = _dialogue_id()
        except Exception:
            did = ""
        # fence（2026-10-18 #19）：单调递增序号，对标 a2a claim 的 fence。
        # 作用：持有者把「我拿到的是 fence=N」带到后续写入；续租/释放/校验都带上它，
        # 一旦中途被他人接管（fence 已 +1），旧持有者的 N 立即失效 → 不会在「假死复活」
        # 或「锁还没同步到对端」的窗口里继续双写，只能重新 claim 拿新序号。
        # 本人重复 claim / 续租 → 保留原 fence（不是接管）。
        same_holder = bool(existing) and existing.get("holder_session") == me_sid
        if same_holder:
            # 老记录（fence 字段是本次升级后才有的）补一个 1，否则永远停在 0，verify 没有可比对的值
            fence = int(existing.get("fence") or 0) or 1
            claimed_at = existing.get("claimed_at") or _a2a_now()
        else:
            fence = int((existing or {}).get("fence") or 0) + 1
            claimed_at = _a2a_now()
        rec = {"holder_machine": me_suffix, "holder_session": me_sid, "holder_dialogue": did,
               "key": key, "claimed_at": claimed_at, "last_heartbeat": _a2a_now(),
               "expires": now + ttl * 60, "ttl_min": ttl, "reason": reason, "fence": fence}
        d[key] = rec
        if not _reserve_write(d):
            return False, "写预约文件失败"
        return True, "已预约（%d 分钟，fence=%d）：%s — %s" % (ttl, fence, me, reason or "-")
    except Exception as e:
        return True, "预约异常已放行：%s" % e


def _reservation_release(path, force=False, session="", fence=None):
    """释放预约。session 给定时释放该对话持有的全部（忽略 path）。返回 (changed, msg)。

    fence 给定时先校验序号（#19）：序号已过期说明你早被接管了，此时「释放」会误删
    别人的锁 —— 直接拒绝，让调用方先看清现状。
    """
    try:
        d = _reserve_purge(_reserve_read())
        if session:
            left = {k: v for k, v in d.items() if v.get("holder_session") != session}
            changed = len(left) != len(d)
            if changed:
                _reserve_write(left)
            return changed, (("已释放 %s 持有的 %d 条预约" % (session[:8], len(d) - len(left))) if changed else "该对话无预约")
        key = _reservation_key(path)
        me_sid = _my_session_id()
        rec = d.get(key)
        if not rec:
            return False, "该文件当前无预约。"
        if rec.get("holder_session") != me_sid and not force:
            return False, "预约由 %s 机持有，非本对话；确认无冲突用 --force。" % rec.get("holder_machine")
        if fence is not None and not force:
            _fok, _fmsg = _reservation_fence_status(path, fence)
            if not _fok:
                return False, _fmsg
        d.pop(key, None)
        _reserve_write(d)
        return True, "已释放预约：%s" % _reserve_abs(key)
    except Exception as e:
        return False, "释放异常：%s" % e


def _reservation_takeover(path, reason="", ttl=RESERVE_DEFAULT_TTL_MIN):
    """接手：移除该文件任何现存预约（含他人），改为本对话持有（handoff 显式授权）。"""
    key = _reservation_key(path)
    d = _reserve_purge(_reserve_read())
    me_sid, me_suffix, me = _reserve_identity()
    now = time.time()
    prev = d.get(key)
    try:
        did = _dialogue_id()
    except Exception:
        did = ""
    fence = int((prev or {}).get("fence") or 0) + 1     # 接管即升序号（#19）
    d[key] = {"holder_machine": me_suffix, "holder_session": me_sid, "holder_dialogue": did,
              "key": key, "claimed_at": _a2a_now(), "last_heartbeat": _a2a_now(),
              "expires": now + ttl * 60, "ttl_min": ttl, "reason": reason, "fence": fence}
    _reserve_write(d)
    if prev:
        return True, "已从 %s 机接管预约（fence=%d）：%s" % (prev.get("holder_machine"), fence, reason or "-")
    return True, "已接管预约（fence=%d）：%s" % (fence, reason or "-")


def _reservation_fence_status(path, fence):
    """校验「我手上的 fence 是否仍然有效」。返回 (ok, msg)。

    为什么需要（2026-10-18 #19）：锁文件本身要走 Syncthing 传播（秒~分钟级），
    「我拿了锁、锁还没到对端」的窗口里对端也可能拿到 —— 加锁消除不了这个窗口。
    能消除的是**后续写入**：把 claim 拿到的序号带在身上，写之前 `reserve verify --fence N`
    复核一次。序号是单调递增的，别人一接管就 +1，旧序号立即失效 → 只能重新 claim。

    fail-open：校验本身异常时放行（宁可漏拦，不要把正常写入卡死）。
    """
    try:
        key = _reservation_key(path)
        d = _reserve_purge(_reserve_read())
        rec = d.get(key)
        me_sid = _my_session_id()
        try:
            want = int(fence)
        except Exception:
            return False, "fence 需要整数（就是 claim 时打印的序号）"
        if not rec:
            return False, "预约已过期或已释放（fence=%d 失效）—— 请重新 `wbsync reserve claim`" % want
        cur = int(rec.get("fence") or 0)
        if cur != want:
            return False, ("fence 已失效：你持 %d，当前 %d（%s 机/%s 持有）—— 你很可能已被接管，"
                           "别再写，重新 `reserve claim` 拿新序号"
                           % (want, cur, rec.get("holder_machine"), (rec.get("holder_session") or "")[:8]))
        if rec.get("holder_session") != me_sid:
            return False, "该预约现由 %s 机/%s 持有（fence=%d）—— 本对话不持有，不要写" % (
                rec.get("holder_machine"), (rec.get("holder_session") or "")[:8], cur)
        return True, "fence=%d 有效（%s 机，剩余 %d 分钟）" % (
            cur, rec.get("holder_machine"),
            max(0, int(((rec.get("expires") or 0) - time.time()) // 60)))
    except Exception as e:
        return True, "校验异常已放行：%s" % e


def _print_reservation(key, rec, me_sid):
    who = "（本对话）" if rec.get("holder_session") == me_sid else ""
    remain = max(0, int(((rec.get("expires") or 0) - time.time()) // 60))
    info("  %s\n    持有=%s 会话=%s 剩余 %d 分钟 fence=%s 用途=%s%s"
         % (_reserve_abs(key), rec.get("holder_machine"), (rec.get("holder_session") or "")[:8], remain,
            rec.get("fence") or "?", rec.get("reason") or "-", who))


def cmd_reserve(args):
    """跨机器文件预约锁：任意共享文件改前先占，防 A/B/C 同时改同一份。写 coop/shared，随同步可见。"""
    action = (getattr(args, "action", None) or "status").lower()
    session = (getattr(args, "session", "") or "").strip()
    if session:
        changed, msg = _reservation_release("", session=session)
        if changed:
            ok(msg)
        else:
            info(msg)
        return 0
    if action == "status":
        d = _reserve_purge(_reserve_read())
        me_sid = _my_session_id()
        path = (getattr(args, "path", "") or "").strip()
        head("跨机器文件预约（%s）" % _reserve_path())
        if path:
            rec = d.get(_reservation_key(path))
            if not rec:
                info("该文件当前无预约。")
            else:
                _print_reservation(_reservation_key(path), rec, me_sid)
            return 0
        if not d:
            ok("当前无文件预约。")
            return 0
        for k, rec in sorted(d.items()):
            _print_reservation(k, rec, me_sid)
        out()
        info("占：wbsync reserve claim <文件> [--ttl %d] [--reason …]；写前复核：wbsync reserve verify <文件> --fence N；释放：wbsync reserve release <文件>"
             % RESERVE_DEFAULT_TTL_MIN)
        return 0
    if action in ("verify", "check"):
        path = (getattr(args, "path", "") or "").strip()
        fence = getattr(args, "fence", None)
        if not path or fence is None:
            die("用法：wbsync reserve verify <文件> --fence <claim 时打印的序号>", 1)
        ok_flag, msg = _reservation_fence_status(path, fence)
        if ok_flag:
            ok(msg)
            return 0
        bad(msg)
        return 2
    if action == "claim":
        path = (getattr(args, "path", "") or "").strip()
        if not path:
            die("用法：wbsync reserve claim <文件> [--ttl %d] [--reason …] [--force]" % RESERVE_DEFAULT_TTL_MIN, 1)
        ttl = int(getattr(args, "ttl", RESERVE_DEFAULT_TTL_MIN) or RESERVE_DEFAULT_TTL_MIN)
        reason = (getattr(args, "reason", "") or "").strip()
        force = getattr(args, "force", False)
        ok_flag, msg = _reservation_claim(path, reason=reason, ttl=ttl, force=force)
        if ok_flag:
            ok(msg)
        else:
            bad(msg)
            return 2
        return 0
    if action == "release":
        path = (getattr(args, "path", "") or "").strip()
        if not path:
            die("用法：wbsync reserve release <文件> [--fence N] [--force]", 1)
        force = getattr(args, "force", False)
        changed, msg = _reservation_release(path, force=force, fence=getattr(args, "fence", None))
        if changed:
            ok(msg)
        else:
            info(msg)
        return 0
    die("未知动作：%s（应为 status / claim / verify / release）" % action, 1)


def _to_syncroot_rel(path):
    """把 log_ref 的路径部分规整为「相对同步根」的稳定形式，供三机解析。

    - 已是相对路径 → **原样保留**（约定即相对同步根，勿按 CWD 补全）。
    - 绝对路径 → 能相对化就相对化，否则原样返回。
    这样 handoff 存进 A2A 的 log_ref 在三机上都能被 _resolve_log_ref 解析回同一文件。"""
    p = Path(path)
    if not p.is_absolute():
        return str(p)
    try:
        p = p.resolve()
    except Exception:
        pass
    try:
        return str(p.relative_to(cfg_sync_root().resolve()))
    except Exception:
        return str(p)


def _resolve_log_ref(ref):
    """log_ref 形如 <相对同步根的路径>[::<标题>]。返回 (abs_path, heading)。"""
    if "::" in ref:
        path_part, heading = ref.split("::", 1)
    else:
        path_part, heading = ref, ""
    heading = heading.strip()
    p = Path(path_part)
    if not p.is_absolute():
        cand = cfg_sync_root() / path_part
        if cand.exists():
            p = cand
        else:
            p = Path(os.getcwd()) / path_part
    try:
        p = p.resolve()
    except Exception:
        pass
    return p, heading


def _extract_log_segment(ref, full=False, limit=400):
    """从日志指针读取详细段：无标题则给末 limit 行（或全文）；有标题则取该标题段落。"""
    p, heading = _resolve_log_ref(ref)
    if not p.exists():
        return "（日志指针解析不到文件：%s —— 可能尚未同步到本机，稍后重试）" % p
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return "（读取日志失败：%s）" % e
    lines = text.splitlines()
    if not heading:
        if full or len(lines) <= limit:
            return text
        return "\n".join(lines[-limit:]) + "\n\n…（末 %d 行；加 --full 看全文）" % limit
    start, hlevel = None, 0
    for i, ln in enumerate(lines):
        clean = re.sub(r"^#+\s*", "", ln.strip())
        if clean == heading or clean.startswith(heading):
            m = re.match(r"^(#{1,6})\s", ln)
            hlevel = len(m.group(1)) if m else 6
            start = i
            break
    if start is None:
        tail = "\n".join(lines[-limit:])
        return "（在 %s 找不到标题「%s」；以下是全文末 %d 行）\n%s" % (p, heading, limit, tail)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s", lines[j])
        if m and len(m.group(1)) <= hlevel:
            end = j
            break
    return "\n".join(lines[start:end])


def cmd_handoff(args):
    """指针式交接：生成一条 A2A 任务（skill=handoff），只带 TL;DR 摘要 + 日志指针，
    重内容留在你自己的日志里，让对端翻日志段接手 —— 避免把长流程全文塞进 A2A。
    若带 --file，自动 reserve 该共享文件，接手方 pickup 时接管。"""
    my = _my_suffix_short()
    to = (getattr(args, "to", "") or "").strip()
    summary = (getattr(args, "summary", "") or "").strip()
    log_ref = (getattr(args, "log_ref", "") or "").strip()
    file_arg = (getattr(args, "file", "") or "").strip()
    state = (getattr(args, "state", "") or "").strip()
    if not to or not summary or not log_ref:
        die("用法：wbsync handoff <log_ref> --summary \"TL;DR\" --to B [--file <共享文件>] [--state …]", 1)
    if file_arg:
        ok_flag, msg = _reservation_claim(file_arg, reason="handoff→%s" % to, ttl=RESERVE_DEFAULT_TTL_MIN)
        if ok_flag:
            ok("已自动预约文件：%s" % msg)
        else:
            bad("文件预约失败（可能他人占用）：%s —— 仍会继续创建交接任务，请确认无冲突。" % msg)
    # log_ref 形如 <相对同步根的路径>[::<标题>]；只把「路径部分」转相对、标题原样保留，
    # 否则 "::标题" 会被当成文件名的一部分，导致相对化失败。
    _lr_path, _lr_head = (log_ref.split("::", 1) + [""])[:2] if "::" in log_ref else (log_ref, "")
    rel = _to_syncroot_rel(_lr_path)
    params = {"log_ref": ("%s::%s" % (rel, _lr_head.strip())) if _lr_head.strip() else rel,
              "summary": summary,
              "reserve_file": (_reservation_key(file_arg) if file_arg else None),
              "from_state": state}
    # 早发现：日志指针本机就解析不到 → 对端 pickup 时多半也取不到，先提醒（不阻断）。
    _lp, _ = _resolve_log_ref(params["log_ref"])
    if not _lp.exists():
        warn("日志指针本机解析不到文件（%s）—— 对端 `pickup` 时可能也取不到；"
             "确认路径相对同步根、且日志已写入。" % _lp)
    tid = _a2a_next_task_id(my)
    _a2a_ensure_readme()
    _a2a_append_event(_a2a_journal_path(my or "?"), my, dict({
        "event": "task", "id": tid, "from": my, "to": to, "skill": (getattr(args, "skill", "") or "handoff") or "handoff",
        "params": params, "text": summary, "state": "submitted", "at": _a2a_now(),
    }, **_a2a_identity(my)))
    ok("已创建交接任务 %s（%s → %s）" % (tid, my, to))
    info("摘要已进 A2A（保底）；详细过程在对端 `wbsync pickup %s` 时从你的日志段读取。" % tid)
    if file_arg:
        info("已预约文件 %s，对端 pickup 时会自动接管 reserve。" % file_arg)
    return 0


def cmd_pickup(args):
    """B 接手：读 handoff 任务的摘要，跳转到 A 的日志段拿详细上下文，并接管 reserve。"""
    my = _my_suffix_short()
    tid = (getattr(args, "id", "") or "").strip()
    if not tid:
        die("用法：wbsync pickup <handoff任务ID>", 1)
    tasks = _a2a_all_tasks()
    t = next((x for x in tasks if x.get("id") == tid), None)
    if not t:
        die("找不到任务 %s（用 `wbsync a2a list` 确认 ID）" % tid, 1)
    if t.get("to") not in (my, "全员", "all"):
        bad("该任务不是发给本机（%s）的（发往 %s）。" % (my, t.get("to")))
    if t.get("state") in A2A_TERMINAL_STATES:
        info("该任务已是终态（%s），无需接手。" % t.get("state"))
    params = t.get("params") or {}
    summary = params.get("summary") or (t.get("messages") or [""])[0]
    head("接手任务 %s（来自 %s）" % (tid, _a2a_label(t.get("from"), t.get("fromGen"))))
    out("【摘要 / TL;DR】")
    out("  " + (summary or "（无摘要）"))
    if params.get("from_state"):
        out("【从哪接着做】 " + params["from_state"])
    rf = params.get("reserve_file")
    if rf:
        absf = _reserve_abs(rf)   # 键可能是「相对同步根」或「~/…」（skill 目录等），统一还原
        ok_flag, msg = _reservation_takeover(str(absf), reason="pickup from %s" % t.get("from"))
        if ok_flag:
            ok("已接管文件预约：%s" % msg)
        else:
            warn("接管预约失败：%s" % msg)
    lr = params.get("log_ref")
    if lr:
        out()
        out("【A 的日志段（详细上下文）】")
        out(_extract_log_segment(lr, full=getattr(args, "full", False)))
    else:
        info("（该交接未带日志指针，无详细段可读。）")
    ok("接手完成。处理后用 `wbsync a2a update --id %s --state completed` 回执；"
       "用 `wbsync reserve release <文件>` 释放预约。" % tid)
    return 0


# 协作文档的「历史协作」归档区（coop/shared/历史协作/）
HISTORY_DIRNAME = "历史协作"
HISTORY_INDEX = "README.md"
_ARCHIVE_MARK = "<!-- wbsync-archived -->"


def _history_dir():
    return coop_shared() / HISTORY_DIRNAME


def _history_index_header():
    return (
        "# 历史协作（归档区）\n\n"
        "> 存放**已消化 / 已闭环 / 已作废**的协作文档。规则见 `coop/shared/纪律-协作行为.md` §11。\n"
        "> 归档=移动 + 文件头加标记 + 在本表登记一行；**不删除**（判断错了也能翻回来）。\n"
        "> 例外（**不归档**）：仍在被代码读取的（如 `机器间沟通.md` 冻结兼容文件）、仍是活机制的产物\n"
        "> （如 `机器间沟通-归档.md`、`a2a/`、`工作空间登记.json`）。专线 `机器间沟通-<后缀>.md` 是活的，也不归档。\n\n"
        "| 归档日期 | 文件 | 原因 | 消化去向 | 归档机 |\n"
        "|---|---|---|---|---|\n"
    )


def _history_index_append(hist_dir, row, apply=True):
    """把一行登记写进 历史协作/README.md（不存在则连同表头一起建）。"""
    idx = Path(hist_dir) / HISTORY_INDEX
    if not apply:
        return idx
    Path(hist_dir).mkdir(parents=True, exist_ok=True)
    if not idx.exists():
        idx.write_text(_history_index_header(), encoding="utf-8")
    with open(str(idx), "a", encoding="utf-8") as f:
        f.write(row + "\n")
    return idx


def cmd_archive_doc(args):
    """把 coop/shared 里「已消化 / 已闭环 / 已作废」的协作文档移入 `历史协作/`，并做好标记。

    「消化」= 内容已进长期记忆 / 纪律文件 / SKILL.md / 代码实现；「不用了」= 任务已闭环或作废。
    动作：① 文件头插入归档标记（记原因、消化去向、归档机、原位置）② 移动到 `历史协作/`
          ③ 在 `历史协作/README.md` 索引表登记一行。**不删除**，判断错了能翻回来。
    默认演练；`--apply` 才落盘。`--list` 看已归档清单。
    """
    hist = _history_dir()
    if getattr(args, "list", False):
        idx = hist / HISTORY_INDEX
        if idx.exists():
            out(idx.read_text(encoding="utf-8"))
        else:
            info("尚无归档记录（%s 不存在）。" % idx)
        if hist.exists():
            files = sorted(p.name for p in hist.glob("*.md") if p.name != HISTORY_INDEX)
            info("已归档文件 %d 个：%s" % (len(files), "、".join(files) or "无"))
        return 0

    name = (getattr(args, "name", "") or "").strip()
    if not name:
        die("用法：wbsync archive-doc <文件名> [--reason …] [--into …] [--apply]；--list 看清单", 1)
    if "/" in name and not Path(name).is_absolute():
        src = coop_shared() / name
    else:
        src = Path(name) if Path(name).is_absolute() else (coop_shared() / name)
    src = Path(src)
    if not src.exists():
        # 幂等：已归档过的文件，源位置自然不存在 —— 报「已归档」而不是硬失败
        #（本环境会重试同一条命令，第二次必须给出可读结论，不能像出错）
        cand = hist / Path(name).name
        if cand.exists():
            ok("已在「%s」内（可能本次已归档）：%s" % (HISTORY_DIRNAME, cand.name))
            info("用 `wbsync archive-doc --list` 看归档清单。")
            return 0
        die("找不到文件：%s（相对路径按 coop/shared/ 解析）" % src, 1)
    if src.parent.resolve() == hist.resolve():
        info("该文件已在「%s」内，无需再归档。" % HISTORY_DIRNAME)
        return 0
    if src.suffix.lower() != ".md":
        info("提示：非 .md 文件（%s），仍可归档，但标记格式对非文本不适用。" % src.name)
    if src.name in ("机器间沟通.md",):
        warn("`机器间沟通.md` 是**冻结的兼容文件**，代码仍在读取它 —— 归档会破坏兼容读取，已跳过。")
        return 0

    reason = (getattr(args, "reason", "") or "").strip() or "已闭环 / 已消化"
    into = (getattr(args, "into", "") or "").strip() or "无需（已闭环 / 已实现 / 作废）"
    my = _my_suffix_short() or "?"
    today = time.strftime("%Y-%m-%d")
    dst = hist / src.name
    if dst.exists():
        dst = hist / ("%s-%s.md" % (src.stem, time.strftime("%Y%m%d-%H%M%S")))

    try:
        body = src.read_text(encoding="utf-8")
    except Exception as e:
        die("读取失败：%s" % e, 1)
    already = _ARCHIVE_MARK in body
    marker = ("" if already else
              "%s\n> **【已归档 · %s】** 原因：%s\n"
              "> 消化去向：%s\n"
              "> 归档机：%s 机 ｜ 原位置：`coop/shared/%s`\n\n---\n\n"
              % (_ARCHIVE_MARK, today, reason, into, my, src.name))

    head("归档协作文档 → %s/" % HISTORY_DIRNAME)
    info("文件：%s（%d B）" % (src.name, len(body.encode("utf-8"))))
    info("原因：%s" % reason)
    info("消化去向：%s" % into)
    info("标记：%s" % ("已含归档标记（不重复加）" if already else "将在文件头插入归档标记"))
    info("目标：%s" % dst)

    if not getattr(args, "apply", False):
        out()
        info("演练模式：未移动。加 --apply 真正归档（移动 + 加标记 + 登记索引）。")
        return 0

    try:
        hist.mkdir(parents=True, exist_ok=True)
        if not already:
            atomic_write_text(str(src), marker + body)
        shutil.move(str(src), str(dst))
    except Exception as e:
        die("归档失败：%s" % e, 1)
    _history_index_append(hist, "| %s | `%s` | %s | %s | %s |" % (today, src.name, reason, into, my))
    ok("已归档到 %s/ 并登记索引。" % HISTORY_DIRNAME)
    return 0


def cmd_prune(args):
    """对端侧收尾：清理已退出机器在本机的痕迹（协作区残留 / 登记表 / 设备表+授权）。
    默认演练，--apply 才执行；删除走系统回收站。"""
    peer = (getattr(args, "peer", "") or "").strip().upper()
    if not peer or not re.fullmatch(r"[A-Z]", peer):
        die("请用 --peer <后缀> 指定要清理的已退出机器（单个大写字母，如 --peer C）", 1)
    my = _my_suffix_short()
    if not my:
        die("无法确定本机后缀（Syncthing 未运行或未登记？先 wbsync id / workspace init）", 1)
    if peer == my:
        die("不能清理本机自己（%s）；本机退出请用 wbsync uninstall。" % peer, 1)

    plan = _prune_scan(peer)
    apply_flag = bool(getattr(args, "apply", False))

    # 前置校验（D 2026-10-16 复盘「建议2」）：对端**仍在役**且没有退出产物 → 拒绝 prune。
    # 事故形状：A 当年直接 prune D，跳过了"D 先自卸"，导致 D 后缀解析失败、自退死锁。
    # 幂等要点：对**已 retired** 的机器重复 prune 必须放行（收尾/重跑都靠它）。
    _ticketed = False
    try:
        _ticketed = bool(_authz_check("prune", peer, consume=False)[0])
    except Exception:
        _ticketed = False
    if apply_flag and plan.get("in_machines") and not _ticketed \
            and not getattr(args, "force_order", False):
        _has_prod = bool(list(coop_shared().glob("退出-%s-*.md" % peer)))
        if not _has_prod:
            die("拒绝 prune：%s 仍在现役 machines，且找不到它的退出产物（coop/shared/退出-%s-*.md）。\n"
                "  正确顺序：① 让 %s 自己跑 `wbsync uninstall --apply`（会写退出产物）→ ② 各对端 prune。\n"
                "  确认已人工核对、就是要跳过这一步：加 `--force-order`（会记录到输出里）。"
                % (peer, peer, peer), 1)
        info("前置校验通过：%s 仍在役，但已有退出产物（说明它确实退过）。" % peer)
    elif apply_flag:
        info("前置校验：%s 已不在现役 machines → 允许重复 prune（幂等）。" % peer)
    explicit_dev = (getattr(args, "device", "") or "").strip()
    dev = explicit_dev or plan["device_id"]

    head("清理已退出机器在本机的痕迹：%s 机" % peer)
    if plan["in_machines"]:
        info("登记表：%s 仍在现役 machines → 将移入 retired" % peer)
    elif plan["retired_gen"]:
        info("登记表：%s 已退役（第 %s 代）" % (peer, plan["retired_gen"]))
    else:
        info("登记表：未发现 %s 的条目" % peer)
    if dev:
        info("设备 ID：%s…%s" % (str(dev)[:7], "（来自 --device）" if explicit_dev else ""))
    else:
        warn("未能从登记表确定 %s 的设备 ID（已完全退役 / 从未登记）。" % peer)
        if plan["candidate_devices"]:
            warn("Syncthing 设备表里有 %d 个不在登记表的设备，可能是残留：" % len(plan["candidate_devices"]))
            for c in plan["candidate_devices"]:
                out("    - %s" % c)
            info("如确认是已退出机器，用 `wbsync prune --peer %s --device <ID> --apply` 移除。" % peer)
        else:
            info("Syncthing 设备表里没有多余的陌生设备。")
    out()
    items = list(plan["region"]) + list(plan["coop_own"]) + list(plan["a2a_own"])
    info("将清理的本机残留文件：")
    for p in items:
        out("    - %s" % p)
    if not items:
        out("    （无残留文件）")
    if dev:
        info("将移除 Syncthing：设备表条目 + %d 个夹的授权" % len(plan["folders"]))
    if not items and not dev and not plan["in_machines"] and not plan["candidate_devices"]:
        ok("未发现 %s 机的任何痕迹，无需清理。" % peer)
        return 0

    if not apply_flag:
        out()
        info("以上为演练（未执行任何改动）。加 --apply 真正清理（删除走回收站，可恢复）。")
        return 0

    # P1 闸门：真删会经 Syncthing 传播给全组 → 必须先停刹车检查 + 人签放行票
    _stop_guard("prune --peer %s（清理 %s 机痕迹）" % (peer, peer))
    _require_authz("prune", peer, "清理 %s 机在本机的痕迹" % peer)

    ts = time.strftime("%Y%m%d-%H%M%S")
    fallback = Path(os.path.expanduser("~")) / (".wbsync-prune-trash-%s" % ts)

    section("1/3 清协作区里 %s 机的残留（同步运行中 → 删除会传播给其他对端一并清）" % peer)
    if items:
        okl, faill = _trash_paths(items, fallback_dir=fallback)
        for p in okl:
            out("    ✓ 已移除 %s" % p)
        for p, e in faill:
            warn("移除失败 %s（%s）" % (p, e))
    else:
        out("    （无）")

    section("2/3 登记表：把 %s 移入 retired" % peer)
    try:
        reg = _read_ws_registry()
        if peer in (reg.get("machines") or {}):
            rec = _ws_retire(reg, peer)
            _write_ws_registry(reg)
            if rec:
                ok("已退役：%s 机第 %s 代（deviceID %s…）" %
                   (peer, rec.get("generation"), str(rec.get("deviceID"))[:7]))
        else:
            ok("%s 不在现役 machines，无需退役。" % peer)
    except Exception as e:
        warn("登记表更新失败：%s" % e)

    section("3/3 Syncthing：移除设备表条目与各夹授权")
    st = _st_client()
    if st and st.available and dev:
        nf = _st_unshare_device(st, dev)
        ok("已从 %d 个同步夹移除授权。" % nf)
        try:
            st.remove_device(dev)
            ok("已从设备表移除 %s…" % str(dev)[:7])
        except Exception as e:
            warn("移除设备表条目失败：%s" % e)
    elif st and st.available:
        warn("无设备 ID，**跳过 Syncthing 清理**——设备表里可能仍挂着 %s 机。" % peer)
        if plan["candidate_devices"]:
            info("候选设备（确认后用 `wbsync prune --peer %s --device <ID> --apply` 移除）：%s"
                 % (peer, "、".join(x[:7] + "…" for x in plan["candidate_devices"])))
    else:
        info("（Syncthing 不可用，跳过）")

    out()
    ok("清理完成：本机已无 %s 机的痕迹。" % peer)
    if fallback.exists():
        info("回收站不可用时降级暂存：%s（确认无误后可手动清）" % fallback)
    return 0


def cmd_uninstall(args):
    """退出机制：停同步 → 通知对端 → 脱离组 → 清本机自己的产物 → 资料库标记 → 归档/删记忆。
    默认只演练；--apply 才执行；删除走系统回收站。"""
    my = _my_suffix_short()
    apply_flag = bool(getattr(args, "apply", False))
    purge = bool(getattr(args, "purge", False))
    wait_s = int(getattr(args, "wait", 90) or 0)
    mem_action = getattr(args, "memory", None)
    if mem_action not in ("delete", "archive"):
        mem_action = None

    if not my:
        # 幂等（P1-1）：引擎已停 / 配置已清时取不到后缀。若本机已退出过，给明确提示而非硬失败。
        mk = _read_exit_mark()
        if mk:
            ok("本机已于 %s 退出协作组（后缀 %s）——无需重复退出。" % (mk.get("exitedAt"), mk.get("suffix")))
            info("如需重新加入：重装同步后再 wbsync setup / pair。")
            return 0
        die("无法确定本机后缀（Syncthing 未运行或未登记？先 wbsync id / workspace init）", 1)

    # 发起权限：只允许 A 机（主节点）或本机发起退出——B 不能远程踢 C。
    by = (getattr(args, "by", None) or my).strip().upper()
    if by != "A" and by != my:
        die("退出发起者非法：只允许 A 机或本机发起（--by 收到 %s；本机 %s）。" % (by, my), 1)

    plan = _uninstall_scan(my, purge)
    _uninstall_report(plan, apply_flag, mem_action)

    if not apply_flag:
        out()
        info("以上为演练（未执行任何改动）。确认无误后加 --apply 真正执行（删除走回收站，可恢复）。")
        info("记忆/日志：--memory archive（默认，留在其他机器上）/ --memory delete（一并删掉）——对话式流程里会先问用户。")
        return 0

    # P1 闸门：退出会清本机产物并传播给对端 → 必须先查刹车 + 人签放行票
    _stop_guard("uninstall --apply（退出协作组）")
    _require_authz("uninstall", my, "退出协作组（%s 机）" % my)

    ts = time.strftime("%Y%m%d-%H%M%S")
    fallback = Path(os.path.expanduser("~")) / (".wbsync-uninstall-trash-%s" % ts)

    # 记忆决策（运行时询问）
    if mem_action is None and plan.get("memory_dir"):
        mem_action = _uninstall_ask_memory(plan)
    if mem_action is None:
        mem_action = "archive"

    # ===== 阶段一 · 同步仍开启：只做「需要传播到对端」的动作，并确认送达 =====
    section("阶段一 · 1/4 通知对端（A2A + 记事本）")
    st = _st_client()
    n_a2a, comms_path = _uninstall_notify(my, plan["peers"])
    ok("已发 A2A 通知 %d 条；记事本公告：%s" % (n_a2a, comms_path or "（无）"))

    archive_dir = None
    if mem_action == "archive" and plan.get("memory_dir"):
        section("阶段一 · 2/4 归档长期记忆/工作日志到共享区（保留给其他机器）")
        archive_dir = _uninstall_archive_memory(my, plan, fallback)
        if archive_dir:
            ok("已归档到 %s（含 README 标记）" % archive_dir)
        else:
            warn("归档未完成，记忆保留原地。")

    # 关键：必须在「同步仍在运行」时删自己的东西——删除才会传播到对端，
    # 把其他机器上「本机做的文件」一并清掉（只删本机自己的，对端自写的不受影响）。
    section("阶段一 · 3/4 清协作区里【本机做的】文件（同步运行中 → 删除会传播到其他机器）")
    okl, faill, _rec = _uninstall_clear_region(my, plan, fallback, archive_dir)
    for p in okl:
        out("    ✓ 已移除 %s（删除将同步传播到对端）" % p)
    for p, e in faill:
        warn("移除失败 %s（%s）" % (p, e))
    if not okl and not faill:
        out("    （无）")
    if _rec:
        info("本机已登记退役：%s 机第 %d 代（deviceID %s…）→ 记入 retired" %
             (my, _rec.get("generation", 1), str(_rec.get("deviceID", ""))[:7]))
        info("继任机器若占用同序号，世代将递增（%s→g2/g3），据此可分辨新旧。" % my)
    if okl:
        info("只删本机自己的产物；对端自己写的文件不受影响。")

    section("阶段一 · 4/4 等送达（rescan → 本机 idle → 在线对端完成度 100%）")
    if _uninstall_wait_delivery(st, max(wait_s, 60)):
        ok("同步已 idle，删除/归档应已传播到对端。")
    else:
        warn("等待后仍非 idle（对端可能离线）。改动已落盘，将在下次同步时传播；")
        warn("若对端长期离线，请对端上线后自行跑 `wbsync prune --peer %s --apply` 兜底清理。" % my)

    # ===== 阶段二 · 本机收尾：脱离同步组后，做「不传播」的本地动作 =====
    section("阶段二 · 1/3 脱离同步组（关自启 / 注销 hook + 移走脚本 / 停引擎）")
    notes = _uninstall_detach(apply_flag, fallback) or {}
    ok("已关自启 / 关 hook / 停 Syncthing。")

    section("阶段二 · 2/3 资料库：把登记 doc 移入「已退出机器登记」归档")
    if plan["library_docs"]:
        n, moved = _uninstall_mark_library(my, plan["library_docs"])
        ok("已处理 %d/%d 个登记 doc（移入归档 %d 个）。" % (n, len(plan["library_docs"]), moved))
        if moved < len(plan["library_docs"]):
            out("    ⚠ 资料库 API 不支持建文件夹：请先在「多机同步登记（勿删）」下")
            out("       建一个「已退出机器登记」文件夹，之后退出会自动把登记 doc 移进去；")
            out("       本次已重命名标记，也可手动拖动这些 doc：")
            for _sid, _pid, title in plan["library_docs"]:
                out("        - %s" % title)
    else:
        out("    （未发现本机登记 doc）")

    section("阶段二 · 3/3 记忆/日志与本地基础设施")
    if mem_action == "delete" and plan.get("memory_dir"):
        dkl, dfl = _trash_paths([plan["memory_dir"]], fallback_dir=fallback)
        ok("记忆/日志已删除（%s）" % ("、".join(dkl) or "无"))
        for p, e in dfl:
            warn("删除失败 %s（%s）" % (p, e))
    elif mem_action == "archive" and archive_dir:
        out("    记忆/日志已归档（见第 2 步，会保留在对端）；本机原目录随私有区一并清理。")
    # 清理本地同步基础设施（Syncthing 配置 / 自启 plist / 各夹同步元数据）
    rm, fl = _uninstall_purge_infra(plan, fallback, include_bin=purge)
    if rm or fl:
        out("    本地同步基础设施：")
        for p in rm:
            out("      ✓ 已移除 %s" % p)
        for p, e in fl:
            warn("      移除失败 %s（%s）" % (p, e))

    out()
    ok("退出流程完成。本机已脱离协作组；skill 本体保留（如需移除请手动删 ~/.workbuddy/skills/workbuddy-sync-all）。")
    # 结束自检（P0-2）：hook 注册已无 + 脚本已移走 + 引擎未运行
    _left = []
    if notes.get("hook_script_left"):
        _left.append("hook 脚本仍在 %s" % _hook_script_path())
    if notes.get("hook_registered"):
        _left.append("settings.json 仍挂着 SessionStart hook")
    if notes.get("engine_up"):
        _left.append("Syncthing 仍在运行")
    if _left:
        warn("退出收尾自检未全部通过：%s" % "；".join(_left))
        warn("请在 WB 的 /hooks 面板确认本 skill 的 hook 已移除；必要时再跑一次 `wbsync stop --apply`。")
    else:
        ok("收尾自检通过：hook 已注销、脚本已移走、引擎已停。")
    _write_exit_mark(my)
    if fallback.exists():
        info("降级暂存位置（可恢复）：%s" % fallback)
        info("  恢复方法：把其中内容移回原路径即可；确认无误后再手动删该目录。")
    info("提示：其他机器收到通知后，各自跑 `wbsync prune --peer %s --apply` 清理本机侧痕迹。" % my)
    return 0


def main(argv=None):
    if IS_WIN:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        out()
        warn("已中断")
        return 130
    except Exception as e:
        if getattr(args, "json", False):
            out(json.dumps({"error": str(e)}, ensure_ascii=False))
            return 1
        die("出错：%s" % e, 1)


if __name__ == "__main__":
    sys.exit(main())
