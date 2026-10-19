#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbsync 自测套件 —— 不依赖真实 Syncthing 守护进程。

之所以这么做：本次开发环境有沙箱，无法让 Syncthing 完成 config.xml 的
原子重命名，因此守护进程类功能改用「模拟 REST 服务 + 真压缩包」验证。
这测的是 wbsync 自己的逻辑，且可重复运行。
"""

import importlib.util
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wbsync", os.path.join(HERE, "..", "scripts", "wbsync.py"))
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("   " + detail) if detail else ""))


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn()
        except SystemExit:
            pass  # 命令里的 die() 走 sys.exit；测试下吞掉，避免中断整套自测
    return buf.getvalue()


# ==========================================================================
# 分节运行（--list / --section N[,M]）：改脚本后只跑受影响的那几节。
#
# 动机：本套件已 450+ 项，整跑一次几百行输出会**全部进入 Agent 上下文**。按节跑可把
# 开销压到十分之一，收尾再整跑一次即可——这属于「省 token」的工程纪律，不是功能。
# 实现：把本文件按 `print("\n=== N. 标题 ===")` 切块，只 exec 选中的块（含头部与汇总）。
# **不给参数时行为完全不变**（线性整跑）。
_SECTION_HEAD = 'print("\\n=== '
_SECTION_TAIL = 'print("\\n" + "=" * 60)'


def _section_map(text):
    """把源码切成 (头部, {节号: [(标题, 行列表)]}, 汇总尾部)。无法解析返回 None。"""
    ls = text.split("\n")
    marks = []
    for i, ln in enumerate(ls):
        s = ln.strip()
        if s.startswith("# §"):
            rest = s[3:]
            tok = rest.split(" ", 1)[0]
            if tok and tok[0].isdigit() and len(tok) <= 4 and all(
                    c.isdigit() or (c.isalpha() and c.islower()) for c in tok):
                marks.append((i, tok, rest[len(tok):].strip()))
            continue
        if not (s.startswith(_SECTION_HEAD) and s.endswith('===")')):
            continue
        inner = s[len(_SECTION_HEAD):-len('===")')]
        num, sep, title = inner.partition(". ")
        if not (sep and num and num[0].isdigit() and len(num) <= 4 and all(
                c.isdigit() or (c.isalpha() and c.islower()) for c in num)):
            continue
        marks.append((i, num, title.rstrip(" =").strip()))
    if not marks:
        return None
    tail = len(ls)
    for i in range(marks[-1][0] + 1, len(ls)):
        if ls[i].strip() == _SECTION_TAIL:
            tail = i
            break
    blocks = {}
    for k, (i, num, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else tail
        blocks.setdefault(num, []).append((title, ls[i:end]))
    return ls[:marks[0][0]], blocks, ls[tail:]


def _section_pick(blocks, wanted):
    """按节号取行（同号多块全部取，兼容历史重号）；返回 (行列表, 缺失节号列表)。"""
    rows, missing = [], []
    for num in wanted:
        if num in blocks:
            for _t, body in blocks[num]:
                rows.extend(body)
        else:
            missing.append(num)
    return rows, missing


if len(sys.argv) > 1 and sys.argv[1] in ("--list", "-l", "--section", "-s"):
    _self = os.path.abspath(__file__)
    _map = _section_map(open(_self, encoding="utf-8").read())
    if _map is None:
        print("无法解析分节（文件结构已变？）——请直接整跑。")
        sys.exit(2)
    _head, _blocks, _tail = _map
    if sys.argv[1] in ("--list", "-l"):
        print("可用测试节（用法：--section N[,M]）：")
        for _n in sorted(_blocks, key=lambda x: (len(x), x)):
            for _t, _b in _blocks[_n]:
                print("  %3s  %s" % (_n, _t))
        sys.exit(0)
    _want = [t for t in ",".join(sys.argv[2:]).replace(" ", "").split(",")
             if t and t[0].isdigit() and len(t) <= 4 and all(
                 c.isdigit() or (c.isalpha() and c.islower()) for c in t)]
    if not _want:
        print("用法：--section N[,M]（先用 --list 查看可用节）")
        sys.exit(2)
    _rows, _miss = _section_pick(_blocks, _want)
    if _miss:
        print("未知测试节：%s（用 --list 查看）" % ", ".join(_miss))
        sys.exit(2)
    sys.argv = [sys.argv[0]]                    # 防重入：exec 时不再进本分支
    print("== 只跑测试节 %s ==" % ", ".join(_want))
    exec(compile("\n".join(_head + _rows + _tail), _self, "exec"), globals())
    sys.exit(1 if FAIL else 0)


# ==========================================================================
print("\n=== 1. 跨平台设备 ID 校验 ===")
good = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
check("合法 ID 通过", W.normalize_device_id(good) == good)
check("小写自动转大写", W.normalize_device_id(good.lower()) == good)
check("带空格容忍", W.normalize_device_id(" " + good + " ") == good)
for bad in ["", "abc", "P56IOI7-MZJNU2Y", good + "-EXTRA",
            "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ1"]:
    check("拒绝非法 ID：%r" % (bad[:24] + ("…" if len(bad) > 24 else "")),
          W.normalize_device_id(bad) is None)


# ==========================================================================
print("\n=== 2. 官方产物选择（跨平台）===")
REL = {"assets": [
    {"name": "syncthing-macos-universal-v2.1.6.zip", "size": 100, "browser_download_url": "https://example/u-universal"},
    {"name": "syncthing-macos-arm64-v2.1.6.zip", "size": 90, "browser_download_url": "https://example/u-arm64"},
    {"name": "syncthing-macos-amd64-v2.1.6.zip", "size": 95, "browser_download_url": "https://example/u-amd64"},
    {"name": "syncthing-windows-amd64-v2.1.6.zip", "size": 80, "browser_download_url": "https://example/w-amd64"},
    {"name": "syncthing-windows-arm64-v2.1.6.zip", "size": 70, "browser_download_url": "https://example/w-arm64"},
    {"name": "syncthing-linux-amd64-v2.1.6.tar.gz", "size": 60, "browser_download_url": "https://example/l-amd64"},
    {"name": "syncthing-linux-arm64-v2.1.6.tar.gz", "size": 50, "browser_download_url": "https://example/l-arm64"},
    {"name": "sha256sum.txt.asc", "size": 1, "browser_download_url": "https://example/sums"},
]}
check("macOS/arm64 优先「架构专用」包（体积约 half、下载更快）",
      W.pick_asset(REL, "darwin", "arm64")["name"] == "syncthing-macos-arm64-v2.1.6.zip")
check("macOS 无架构专用包时退回 universal",
      W.pick_asset({"assets": REL["assets"][:1]}, "darwin", "arm64")["name"]
      == "syncthing-macos-universal-v2.1.6.zip")
check("Windows/amd64 选对",
      W.pick_asset(REL, "win32", "amd64")["name"] == "syncthing-windows-amd64-v2.1.6.zip")
check("Windows/arm64 选对",
      W.pick_asset(REL, "win32", "arm64")["name"] == "syncthing-windows-arm64-v2.1.6.zip")
check("Linux/amd64 选对",
      W.pick_asset(REL, "linux", "amd64")["name"] == "syncthing-linux-amd64-v2.1.6.tar.gz")
check("绝不选到 sha256 文件",
      "sha256" not in W.pick_asset(REL, "linux", "amd64")["name"])

print("\n=== 3. 假二进制过滤（实测抓到的真 bug 的回归测试）===")
T = "syncthing"
check("排除 etc/freebsd-rc/syncthing（1709B 脚本）",
      not W._is_real_binary_member("pkg/etc/freebsd-rc/syncthing", 1709, T))
check("排除 .app/ 内的同名文件",
      not W._is_real_binary_member("pkg/Syncthing.app/Contents/MacOS/syncthing", 5 << 20, T))
check("排除过小的同名文件",
      not W._is_real_binary_member("pkg/syncthing", 4096, T))
check("接受顶层的真二进制",
      W._is_real_binary_member("pkg/syncthing", 57 << 20, T))
check("Windows 排除 .exe 的包装脚本",
      not W._is_real_binary_member("pkg/etc/win/syncthing.exe", 1024, "syncthing.exe"))

print("\n=== 4. 用真压缩包回归解压逻辑 ===")
ZIP = "/tmp/st.zip"
if os.path.exists(ZIP):
    with zipfile.ZipFile(ZIP) as z:
        cands = [i for i in z.infolist()
                 if not i.is_dir() and W._is_real_binary_member(i.filename, i.file_size, T)]
    check("真包中筛出恰好 1 个候选", len(cands) == 1,
          "得到 %d 个：%s" % (len(cands), [c.filename for c in cands]))
    if cands:
        pick = max(cands, key=lambda i: i.file_size)
        check("选中顶层 57MB 真二进制",
              pick.filename.endswith("syncthing-macos-universal-v2.1.6/syncthing")
              and pick.file_size > 50 << 20,
              "%s  %.1fMB" % (pick.filename, pick.file_size / 1048576.0))
    out = "/tmp/wbsync-test-bin"
    W.extract_binary(ZIP, out)
    imported = os.path.getsize(out)
    check("解出的文件体积正确（>50MB）", imported > 50 << 20,
          "%.1fMB" % (imported / 1048576.0))
    check("解出的文件可执行", os.access(out, os.X_OK))
    os.unlink(out)
else:
    print("  · 跳过（/tmp/st.zip 不在）")


# ==========================================================================
print("\n=== 5. 进程存活探测（原 Windows 版最严重的静默错误点）===")
check("本进程判定为存活", W.pid_alive(os.getpid()) is True)
check("不存在的 PID 判定为不存活", W.pid_alive(999999) is False)
check("非法 PID 不抛异常", W.pid_alive("abc") is False and W.pid_alive(0) is False)

procs = W.list_processes()
if procs is None:
    check("枚举受限时明确返回 None（而非伪装成「没有进程」）", True, "本环境 ps 被限制")
else:
    check("进程列表可读", isinstance(procs, list) and len(procs) > 0, "%d 个进程" % len(procs))

found = W.find_processes("syncthing")
check("find_processes 返回 None 或列表，不吞成空列表",
      found is None or isinstance(found, list),
      "None（枚举受限）" if found is None else "%d 个" % len(found))

print("\n=== 6. 中文对齐（显示宽度）===")
check("全角字符算 2 列", W.dispwidth("专家源包") == 8)
check("半角字符算 1 列", W.dispwidth("abcd") == 4)
check("混合正确", W.dispwidth("专家ab") == 6)
check("wpad 补齐到等宽",
      W.dispwidth(W.wpad("专家源包", 16)) == 16 and W.dispwidth(W.wpad("工作区技能", 16)) == 16)


# ==========================================================================
print("\n=== 7. 登记层自愈：跨平台路径重建 ===")
print("  （用伪造的 Windows 登记文件，验证 C:\\Users\\... → 本机路径）")

# 用真实的本机临时目录（而非硬编码 POSIX 路径），
# 这样在 Windows 上 Path(TMP).is_dir() 才能正确返回 True。
TMP = tempfile.mkdtemp(prefix="wbsync-heal-test-")
# plusy 的目录名用 -，而登记里的 version 用 +（实测 wind-finance 就是这样）
for rel in ["plugins/cache/my-experts/demo/1.0.0",
            "plugins/cache/my-experts/demo2/2.0.0",
            "plugins/cache/my-experts/plusy/1.2.2-wb.abc123",
            "plugins/cache/my-experts/keeper/3.0.0",
            "plugins/marketplaces/my-experts"]:
    os.makedirs(os.path.join(TMP, rel), exist_ok=True)
os.makedirs(os.path.join(TMP, "plugins"), exist_ok=True)

WINPATH = "C:" + chr(92) + "Users" + chr(92) + "bob" + chr(92) + ".workbuddy"


def win(*seg):
    return WINPATH + "".join(chr(92) + s for s in seg)


def local(*seg):
    return os.path.join(TMP, "plugins", "cache", "my-experts", *seg)


with open(os.path.join(TMP, "plugins", "installed_plugins.json"), "w", encoding="utf-8") as f:
    json.dump({"version": 2, "plugins": {
        "demo@my-experts": [{"scope": "user", "version": "1.0.0",
                             "installPath": win("plugins", "cache", "my-experts", "demo", "1.0.0")}],
        "demo2@my-experts": [{"scope": "user", "version": "2.0.0",
                              "installPath": win("plugins", "cache", "my-experts", "demo2", "2.0.0")}],
        # version 用 +，磁盘目录用 - ：应靠归一化找回
        "plusy@my-experts": [{"scope": "user", "version": "1.2.2+wb.abc123",
                              "installPath": win("plugins", "cache", "my-experts", "plusy", "1.2.2+wb.abc123")}],
        # installPath 已是本机且真实存在：应原样保留、不报问题
        "keeper@my-experts": [{"scope": "user", "version": "3.0.0",
                               "installPath": local("keeper", "3.0.0")}],
        "missing@my-experts": [{"scope": "user", "version": "9.9.9",
                                "installPath": win("plugins", "cache", "my-experts", "missing", "9.9.9")}],
        "builtin@workbuddy-builtin": [{"scope": "user", "version": "1.0.0",
                                       "installPath": "C:" + chr(92) + "Program Files" + chr(92) + "WorkBuddy" + chr(92) + "builtin"}],
    }}, f)
with open(os.path.join(TMP, "plugins", "known_marketplaces.json"), "w", encoding="utf-8") as f:
    json.dump({
        "my-experts": {"type": "directory", "isBuiltIn": False,
                       "installLocation": win("plugins", "marketplaces", "my-experts"),
                       "source": {"source": "directory", "path": WINPATH}},
        "builtin-mp": {"type": "zip", "isBuiltIn": True, "installLocation": "C:" + chr(92) + "Program Files" + chr(92) + "x"},
    }, f)

_orig_root = W.WB_ROOT
W.WB_ROOT = W.Path(TMP)

IP_JSON = os.path.join(TMP, "plugins", "installed_plugins.json")
with open(IP_JSON, "rb") as f:
    bytes_before = f.read()

r = W.heal_installed_plugins(False)
check("演练模式报告有改动", r["changed"] is True)
check("只统计本机路径条目（内置的不算）", r["total"] == 5, "total=%d" % r["total"])
check("内置插件被跳过", r["skipped_builtin"] == 1)
check("可重建的 3 条", len(r["fixed"]) == 3, "实际 %d" % len(r["fixed"]))
check("version 用 + 而目录用 - 时能归一化找回",
      any(k.startswith("plusy") for k, _, _ in r["fixed"]))
check("plusy 的落点确实是带 - 的那个目录",
      any(str(new).endswith("1.2.2-wb.abc123") for k, _, new in r["fixed"] if k.startswith("plusy")))
check("installPath 已存在的不报问题、也不算重建",
      r["already_ok"] == 1 and all(not k.startswith("keeper") for k, _, _ in r["fixed"]))
check("目标不存在的 1 条被标为无法重建", len(r["missing"]) == 1)
with open(IP_JSON, "rb") as f:
    check("演练模式确实没落盘（逐字节比对）", f.read() == bytes_before)

r2 = W.heal_known_marketplaces(False)
check("市场登记：识别 1 处待重建", len(r2["fixed"]) == 1)
check("内置市场（isBuiltIn）不动", all(n != "builtin-mp" for n, _, _ in r2["fixed"]))

W.heal_installed_plugins(True)
W.heal_known_marketplaces(True)
after = json.load(open(os.path.join(TMP, "plugins", "installed_plugins.json"), encoding="utf-8"))
check("落盘后 demo 指向本机",
      os.path.normpath(after["plugins"]["demo@my-experts"][0]["installPath"])
      == os.path.normpath(os.path.join(TMP, "plugins", "cache", "my-experts", "demo", "1.0.0")))
check("无法重建的条目保持原样（不误写）",
      after["plugins"]["missing@my-experts"][0]["installPath"].startswith("C:"))
check("内置插件条目保持原样",
      after["plugins"]["builtin@workbuddy-builtin"][0]["installPath"].startswith("C:"))
baks = [f for f in os.listdir(os.path.join(TMP, "plugins")) if "wbsync-bak" in f]
check("落盘前有备份", len(baks) == 2, "备份 %d 个" % len(baks))
W.WB_ROOT = _orig_root
# 清理临时目录（跨平台安全，避免每次运行残留）
shutil.rmtree(TMP, ignore_errors=True)


# ==========================================================================
print("\n=== 8. Syncthing REST 客户端（对模拟服务）===")

STATE = {"folders": {}, "devices": [], "reqs": []}
MY_ID = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        STATE["reqs"].append(("GET", self.path, None))
        if self.path == "/rest/system/status":
            return self._send({"myID": MY_ID})
        if self.path == "/rest/config":
            return self._send({"folders": list(STATE["folders"].values()),
                               "devices": [{"deviceID": MY_ID}] + STATE["devices"]})
        if self.path.startswith("/rest/db/status"):
            return self._send({"state": "idle", "errors": 0})
        if self.path == "/rest/system/connections":
            return self._send({"connections": {}})
        return self._send({}, 404)

    def do_POST(self):
        b = self._body()
        STATE["reqs"].append(("POST", self.path, b))
        if self.path == "/rest/config/devices":
            STATE["devices"].append(b)
            return self._send({}, 200)
        return self._send({}, 200)

    def do_PUT(self):
        b = self._body()
        STATE["reqs"].append(("PUT", self.path, b))
        if self.path.startswith("/rest/config/folders/"):
            fid = self.path.rsplit("/", 1)[1]
            STATE["folders"][fid] = b
        return self._send({}, 200)

    def do_PATCH(self):
        b = self._body()
        STATE["reqs"].append(("PATCH", self.path, b))
        if self.path.startswith("/rest/config/folders/"):
            fid = self.path.rsplit("/", 1)[1]
            STATE["folders"].setdefault(fid, {"id": fid}).update(b or {})
        return self._send({}, 200)

    def do_DELETE(self):
        STATE["reqs"].append(("DELETE", self.path, None))
        return self._send({}, 200)


srv = HTTPServer(("127.0.0.1", 18384), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

_orig_base = W.API_BASE
W.API_BASE = "http://127.0.0.1:18384"
st = W.Syncthing.__new__(W.Syncthing)
st.config_xml = None
st.key = "test-key"

check("读本机设备 ID", st.my_id() == MY_ID)

act = st.ensure_folder("wb-skills", "工作区技能", "/tmp/x/skills")
payload = [r for r in STATE["reqs"] if r[0] == "PUT" and "wb-skills" in r[1]][-1][2]
check("新建同步夹用 PUT 且带本机设备",
      act == "created" and payload["id"] == "wb-skills" and payload["type"] == "sendreceive")
check("同步夹路径正确写入", payload["path"] == "/tmp/x/skills")
check("标签正确写入", payload["label"] == "工作区技能")

act = st.ensure_folder("wb-skills", "改名后", "/tmp/x/skills")
check("已存在且内容变化时用 PATCH 更新", act == "updated"
      and STATE["reqs"][-1][0] == "PATCH" and STATE["reqs"][-1][2]["label"] == "改名后")

act = st.ensure_folder("wb-skills", "改名后", "/tmp/x/skills")
check("内容一致时不重复写", act == "unchanged")

PEER = "IIIIIII-JJJJJJJ-KKKKKKK-LLLLLLL-MMMMMMM-NNNNNNN-OOOOOOO-PPPPPPP"
check("添加对端设备返回 True", st.add_device(PEER, "笔记本") is True)
check("重复添加返回 False（幂等）", st.add_device(PEER, "笔记本") is False)

done, why = st.share_folder("wb-skills", PEER)
fold = STATE["folders"]["wb-skills"]
ids = [d["deviceID"] for d in fold["devices"]]
check("共享同步夹给对端", done is True and PEER in ids, why)
check("共享时必须保留本机设备（否则会丢同步）", MY_ID in ids)
check("重复共享不重复添加", st.share_folder("wb-skills", PEER)[0] is False)
check("共享不存在的夹时优雅报错", st.share_folder("nope", PEER)[1] == "同步夹未注册")
check("DB 状态可读", (st.db_status("wb-skills") or {}).get("errors") == 0)
check("restart 可调用", st.restart() is True)

W.API_BASE = _orig_base
srv.shutdown()


# ==========================================================================
print("\n=== 9. 四个同步夹定义 ===")
rows = W.folder_table()
check("恰好定义 4 个同步夹", len(rows) == 4)
ids = [r[0] for r in rows]
check("id 集合符合约定",
      ids == ["wb-skills", "wb-experts", "wb-experts-cache", "wb-coop"], str(ids))
check("技能夹指向 .workbuddy/skills",
      rows[0][2].as_posix().endswith(".workbuddy/skills"))
check("专家源包指向 marketplaces/my-experts",
      rows[1][2].as_posix().endswith("marketplaces/my-experts"))
check("专家运行副本指向 cache/my-experts",
      rows[2][2].as_posix().endswith("cache/my-experts"))


# ==========================================================================
print("\n=== 10. 新增命令（newdisk / mailbox / listdisks）纯逻辑 ===")

# 10.1 设备 ID 校验与规范化
check("is_valid_device_id 接受合法 ID",
      W.is_valid_device_id("IIIIIII-JJJJJJJ-KKKKKKK-LLLLLLL-MMMMMMM-NNNNNNN-OOOOOOO-PPPPPPP") is True)
check("is_valid_device_id 拒绝非法 ID",
      W.is_valid_device_id("not-an-id") is False and W.is_valid_device_id("") is False)
check("normalize_device_id 大写并去空格",
      W.normalize_device_id("  abcdefg-hijklmn-opqrstu-vwxyz22-2222222-3333333-4444444-5555555  ")
      == "ABCDEFG-HIJKLMN-OPQRSTU-VWXYZ22-2222222-3333333-4444444-5555555")

# 10.2 同步盘 / 工作空间统一：A/B/C/D 后缀 + folder id 派生
check("_ws_folder_id 所有机器共用同一个同步夹 ID", W._ws_folder_id("B") == "sync-workspace-all")
check("_ws_folder_id 不随后缀变化", W._ws_folder_id("A") == W._ws_folder_id("B"))
check("后缀分配按序且跳过已占用", W._ws_next_suffix({"machines": {"A": {}, "B": {}}}) == "C")
check("后缀用尽返回 None", W._ws_next_suffix({"machines": {ch: {} for ch in W._SUFFIXES}}) is None)
check("设备后缀反查", W._ws_suffix_of_device({"machines": {"A": {"deviceID": "X"}}}, "X") == "A")

# 10.3 信箱落点函数（应基于 wb-coop 的 shared 子目录）
import tempfile as _tmp
_mb = W._mailbox_dir()
check("信箱落在 coop/shared/设备ID", "coop" in _mb and "shared" in _mb)

# 10.4 同步盘根目录记住逻辑（用临时 config 隔离，不碰真实 config）
_fake_cfg_dir = os.path.join(_tmp.gettempdir(), "wbsync-test-cfg-%d" % os.getpid())
_orig_cfg_path = W.CONFIG_PATH
W.CONFIG_PATH = W.Path(_fake_cfg_dir) / "config.json"
try:
    _remember_called = {"v": False}
    _orig_save = W.save_config
    def _fake_save(cfg, apply_flag):
        _remember_called["v"] = True
        return True
    W.save_config = _fake_save
    try:
        W._remember_disk_root("/tmp/mydisks", True)
        check("记住盘根目录会写 config", _remember_called["v"] is True)
    finally:
        W.save_config = _orig_save
    # 未记住时回落到 sync_root
    check("未记住时 disk_root 回落", W._disk_root() == W.cfg_sync_root())
finally:
    W.CONFIG_PATH = _orig_cfg_path

print("\n=== 11. 专线读位（read cursor）：块解析 / 未读计算 / 漂移 / 端到端 ===")
# 11.1 纯函数：读位块 渲染 ↔ 解析 往返
_c = {"B": {"read": 3, "at": "2026-10-14 11:00", "title": "某条标题 / 含斜杠"},
      "C": {"read": 0, "at": "2026-10-14 11:00", "title": ""}}
_block = W._comms_render_cursor(_c)
_back = W._comms_parse_cursor(_block)
check("读位块渲染→解析 往返一致", _back == _c, repr(_back))
check("读位解析：大小写归一",
      W._comms_parse_cursor("<!-- wbsync-cursor\npeer=b read=2 at=2026-10-14 09:00 title=t\n-->")
      .get("B", {}).get("read") == 2)
check("读位解析：无块返回空", W._comms_parse_cursor("没有读位块") == {})
check("读位解析：缺 title 字段也算数",
      W._comms_parse_cursor("<!-- wbsync-cursor\npeer=B read=1 at=2026-10-14 09:00\n-->")
      .get("B", {}).get("read") == 1)

# 11.2 纯函数：未读计算与漂移（宁可多读，不可漏读）
_msgs = [{"title": "m1"}, {"title": "m2"}, {"title": "m3"}]
_r = W._comms_unread_of(_msgs, None)
check("未初始化 → 全部视为未读", (_r[0], len(_r[1]), _r[2]) == (0, 3, False))
_r = W._comms_unread_of(_msgs, {"read": 2, "title": "m2"})
check("序号+标题匹配 → 已读 2、未读 1、无漂移", (_r[0], len(_r[1]), _r[2]) == (2, 1, False))
_r = W._comms_unread_of(_msgs, {"read": 2, "title": "mX"})
check("标题对不上 → 报漂移且不自作前移", (_r[0], len(_r[1]), _r[2]) == (2, 1, True))
_r = W._comms_unread_of(_msgs, {"read": 99, "title": "m3"})
check("序号越界 → 夹到总数并报漂移", (_r[0], len(_r[1]), _r[2]) == (3, 0, True))
_r = W._comms_unread_of(_msgs, {"read": 0, "title": ""})
check("读位为零且无标题 → 全部未读且不误报漂移", (_r[0], len(_r[1]), _r[2]) == (0, 3, False))
_r = W._comms_unread_of(_msgs, {"read": "坏值", "title": "m1"})
check("读位值非法 → 退回全部未读并报漂移", (_r[0], _r[2]) == (0, True))

# 11.3 文件名 → 对端后缀
check("专线文件名抽后缀", W._comms_file_peer("机器间沟通-B.md") == "B")
check("历史记事本不算对端", W._comms_file_peer("机器间沟通.md") is None)
check("归档文件不算对端", W._comms_file_peer("机器间沟通-归档.md") is None)

# 11.4 端到端：写读位进「本机」文件（临时目录，不碰真实同步夹）
_tmp = tempfile.mkdtemp(prefix="wbsync-cursor-")
_orig_root = W.cfg_sync_root
_orig_sfx = W._my_suffix_short
try:
    W.cfg_sync_root = lambda: W.Path(_tmp)
    W._my_suffix_short = lambda: "A"
    _coop = W.Path(_tmp) / "coop" / "shared"
    _coop.mkdir(parents=True, exist_ok=True)
    (_coop / "机器间沟通-B.md").write_text(
        "# 机器间沟通 · B 机专线\n\n## 消息历史\n\n---\n"
        "## [B→A] t1（待回复）（时间：2026-10-14 09:00）\n正文1\n\n---\n"
        "## [B→A] t2（已完成）（时间：2026-10-14 09:10）\n正文2\n", encoding="utf-8")
    _me = _coop / "机器间沟通-A.md"
    _me.write_text(W._COMMS_TEMPLATE.format(suffix="A"), encoding="utf-8")

    _rows, _cur = W._comms_peer_status()
    check("端到端：只认对端 1 个、共 2 条、未初始化",
          len(_rows) == 1 and _rows[0]["total"] == 2
          and _rows[0]["unread"] == 2 and _rows[0]["initialized"] is False)
    W._comms_push_cursor(_cur, _rows, set())
    check("端到端：写读位返回成功", W.write_comms_cursor(_cur) is True)
    check("端到端：写回后读得到且已读满",
          W.read_comms_cursor().get("B", {}).get("read") == 2
          and W.read_comms_cursor().get("B", {}).get("title") == "t2")
    _rows2, _ = W._comms_peer_status()
    check("端到端：推进后未读为 0", _rows2[0]["unread"] == 0 and _rows2[0]["read"] == 2)
    check("端到端：模板里的读位块只出现一份（不重复插入）",
          _me.read_text(encoding="utf-8").count("<!-- wbsync-cursor") == 1)
    # 重复写入应就地替换而非追加
    W.write_comms_cursor(W.read_comms_cursor())
    check("端到端：二次写入仍是同一块",
          _me.read_text(encoding="utf-8").count("<!-- wbsync-cursor") == 1)
    # 写读位不得破坏消息解析（本机文件里没有消息，对端仍应正常）
    check("端到端：消息解析未受影响",
          len(W._parse_comms_messages((_coop / "机器间沟通-B.md").read_text(encoding="utf-8"))) == 2
          and len(W._parse_comms_messages(_me.read_text(encoding="utf-8"))) == 0)
finally:
    W.cfg_sync_root = _orig_root
    W._my_suffix_short = _orig_sfx
    shutil.rmtree(_tmp, ignore_errors=True)

# ==========================================================================
print("\n=== 12. A2A 任务 ID 与事件折叠（实测抓到的重号 bug 回归）===")
# 12.1 折叠：同一 id 的两条 task 事件只应列出一行（历史重号残留不再显示成两条）
_fold = W._a2a_fold([
    {"event": "task", "id": "T1", "from": "A", "to": "C", "skill": "x", "at": "2026-10-14T12:41:04"},
    {"event": "task", "id": "T1", "from": "A", "to": "B", "skill": "x", "at": "2026-10-14T12:41:04"},
    {"event": "status", "id": "T1", "state": "completed", "at": "2026-10-14T12:42:00"},
])
check("折叠：重号 id 只列一行", len(_fold) == 1, repr([t["id"] for t in _fold]))
check("折叠：状态迁移生效", _fold[0]["state"] == "completed")
check("折叠：多 id 保持顺序", [t["id"] for t in W._a2a_fold([
    {"event": "task", "id": "T1", "at": "1"}, {"event": "task", "id": "T2", "at": "2"},
])] == ["T1", "T2"])

# 12.2 任务 ID 生成：同秒连发两条必须拿到不同序号（否则后一条会覆盖前一条的 to）
_tmpj = tempfile.mkdtemp(prefix="wbsync-a2a-")
_oj, _od = W._a2a_all_journals, W._a2a_dir
try:
    _jp = W.Path(_tmpj) / "tasks-A.json"
    W._a2a_all_journals = lambda: [W.Path(_tmpj) / "tasks-A.json"]
    W._a2a_dir = lambda: W.Path(_tmpj)   # v2.11.0 起序号用 _a2a_dir()/.seq 占位，须一并隔离（否则写真实目录→二次运行序号不从 001 起）
    _ts = "20261014-124104"
    _id1 = W._a2a_next_task_id("A", now=_ts)
    W._a2a_append_event(_jp, "A", {"event": "task", "id": _id1, "from": "A", "to": "C",
                                   "skill": "token.diet", "at": "2026-10-14T12:41:04"})
    _id2 = W._a2a_next_task_id("A", now=_ts)
    W._a2a_append_event(_jp, "A", {"event": "task", "id": _id2, "from": "A", "to": "B",
                                   "skill": "token.diet", "at": "2026-10-14T12:41:04"})
    check("同秒两条任务 ID 不重号", _id1 != _id2, "%s vs %s" % (_id1, _id2))
    check("ID 首条为 -001", _id1.endswith("-001"))
    check("ID 次条为 -002", _id2.endswith("-002"))
    # 折叠后应看到 2 条不同任务（且各自 to 正确，不再互相覆盖）
    _tsk = W._a2a_all_tasks()
    check("同秒两条任务各自保留 to",
          sorted(t["to"] for t in _tsk) == ["B", "C"], repr([(t["id"], t["to"]) for t in _tsk]))
    check("换一秒重新从 001 开始", W._a2a_next_task_id("A", now="20261014-124105").endswith("-001"))
finally:
    W._a2a_all_journals, W._a2a_dir = _oj, _od
    shutil.rmtree(_tmpj, ignore_errors=True)

# 12.3 wake 的状态卡片摘要：读对端 STATE-<后缀>.md 抽一行（降 token：只读前 12 行）
_tmpw = tempfile.mkdtemp(prefix="wbsync-wake-")
try:
    _ws = W.Path(_tmpw)
    (_ws / "B" / "memory").mkdir(parents=True, exist_ok=True)
    (_ws / "B" / "memory" / "STATE-B.md").write_text(
        "# B 机状态\n- 更新：2026-10-14 12:35\n- 待办：无\n- 刚做完：降 token 三件套\n",
        encoding="utf-8")
    check("wake：抽到「刚做完」行", W._wake_state_line("B", _ws) == "刚做完：降 token 三件套")
    check("wake：无状态卡片返回空", W._wake_state_line("C", _ws) == "")
    check("wake：超长摘要被截断", len(W._wake_state_line("B", _ws, maxlen=5)) == 5)
finally:
    shutil.rmtree(_tmpw, ignore_errors=True)

# ==========================================================================
# 13. 管理端口跟随各自的 config.xml —— 「同机演练新机器接入」的前提
#     背景：原先 API 地址硬编码 127.0.0.1:8384，用独立 --home 起的第二个
#     Syncthing 实例（管理端口不同）永远被生产实例遮蔽：拿到的是生产机设备
#     ID，probe 据此判错编号。修法是让端口从各自 config.xml 的 <gui><address> 解析。
# ==========================================================================
_tmpc = tempfile.mkdtemp(prefix="wbsync-gui-")
try:
    _cfx = W.Path(_tmpc) / "config.xml"
    _cfx.write_text(
        '<?xml version="1.0"?>\n<configuration version="37">\n'
        '  <gui enabled="true" tls="false">\n'
        '    <address>127.0.0.1:8394</address>\n'
        '    <apikey>k</apikey>\n'
        '  </gui>\n'
        '  <listenAddress>tcp://0.0.0.0:22000</listenAddress>\n'
        '  <address>dynamic</address>\n'
        '</configuration>\n', encoding="utf-8")
    check("gui_port 跟随 config.xml 的 <gui><address>", W.gui_port(_cfx) == 8394)
    check("api_base 拼出该实例的基址", W.api_base(_cfx) == "http://127.0.0.1:8394")
    check("只认 <gui> 段内 address（不误取监听地址）",
          W._gui_address_raw(_cfx) == "127.0.0.1:8394")
    os.environ["WBSYNC_GUI_ADDR"] = "http://127.0.0.1:9999"
    check("WBSYNC_GUI_ADDR 优先于 config.xml", W.gui_port(_cfx) == 9999)
    del os.environ["WBSYNC_GUI_ADDR"]
    check("无 config 时回落 API_BASE（兼容既有注入）", W.api_base(None) == W.API_BASE)

    _st2 = W.Syncthing.__new__(W.Syncthing)
    _st2.config_xml = _cfx
    _st2.key = "k"
    _st2._gui_base = W.api_base(_cfx)
    check("Syncthing 对象绑定自己 config 的端口",
          _st2._gui_base == "http://127.0.0.1:8394")

    # 新机接入不再产出已取消的 shared/ 公共可写区
    _src = W.Path(W.__file__).read_text(encoding="utf-8")
    check("工作空间初始化不再建 shared/", '("", suffix, "shared")' not in _src)
finally:
    shutil.rmtree(_tmpc, ignore_errors=True)

# ==========================================================================
print("\n=== 12b. 时间命名空壳清理（_time_shell_dirs / _ws_cleanup）===")
_tmpc2 = tempfile.mkdtemp(prefix="wbsync-test-cleanup-")
_orig_root = W.DEFAULT_WORKBUDDY_ROOT
try:
    W.DEFAULT_WORKBUDDY_ROOT = W.Path(_tmpc2)
    # 严格时间戳命名 + 空目录 → 应被识别
    (_tmpc2_p := W.Path(_tmpc2)).joinpath("2026-10-13-18-48-15").mkdir()
    _tmpc2_p.joinpath("20261013-184815").mkdir()  # 备用格式
    # 非时间戳命名（用户项目，如 Claw）→ 不识别
    _tmpc2_p.joinpath("Claw").mkdir()
    # 时间戳命名但非空 → 不识别
    _nonempty = _tmpc2_p.joinpath("2026-10-14-00-00-00")
    _nonempty.mkdir()
    _nonempty.joinpath("keep.txt").write_text("x", encoding="utf-8")
    _shells = W._time_shell_dirs()
    _names = sorted(p.name for p in _shells)
    check("只识别空的时间戳目录（两种格式）",
          _names == ["2026-10-13-18-48-15", "20261013-184815"])
    check("不误删用户项目(Claw)", "Claw" not in _names)
    check("不删非空时间戳目录", "2026-10-14-00-00-00" not in _names)

    # _ws_cleanup 演练模式只列不删
    class _A:
        apply = False
    _rc = W._ws_cleanup(_A(), {}, None)
    check("cleanup 演练返回 0", _rc == 0)
    check("cleanup 演练不实际删除",
          _tmpc2_p.joinpath("2026-10-13-18-48-15").exists())

    # _ws_cleanup --apply 只删空壳
    class _A2:
        apply = True
    _rc2 = W._ws_cleanup(_A2(), {}, None)
    check("cleanup --apply 返回 0", _rc2 == 0)
    check("cleanup --apply 删空壳",
          not _tmpc2_p.joinpath("2026-10-13-18-48-15").exists())
    check("cleanup --apply 保留用户项目", _tmpc2_p.joinpath("Claw").exists())
    check("cleanup --apply 保留非空时间戳目录", _nonempty.exists())
finally:
    shutil.rmtree(_tmpc2, ignore_errors=True)
    W.DEFAULT_WORKBUDDY_ROOT = _orig_root

# ==========================================================================
print("\n=== 13. hook 脚本过期检测 + workspace hide ===")
# 13.1 _hook_script_stale：已装脚本与内置不一致 → 过期
_tmpc3 = tempfile.mkdtemp(prefix="wbsync-test-hook-")
_orig_wbroot = W.WB_ROOT
try:
    W.WB_ROOT = W.Path(_tmpc3)
    _bundled = W.Path(W.__file__).resolve().parent.parent / "scripts" / "wbsync-autostart-hook.sh"
    _scripts = W.Path(_tmpc3) / "scripts"
    _scripts.mkdir()
    _dst = _scripts / "wbsync-autostart-hook.sh"
    _dst.write_text(_bundled.read_text(encoding="utf-8"), encoding="utf-8")
    check("装的是内置同版 → 不过期", W._hook_script_stale() is False)
    _dst.write_text(_bundled.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    check("装的是改过的版本 → 过期", W._hook_script_stale() is True)
finally:
    shutil.rmtree(_tmpc3, ignore_errors=True)
    W.WB_ROOT = _orig_wbroot

# 13.2 _ws_hide：演练只列不隐藏；--apply 按平台隐藏（POSIX 验 chflags）
_tmpc4 = tempfile.mkdtemp(prefix="wbsync-test-hide-")
_orig_local = W._ws_local_dir
_orig_suffix = W._ws_suffix_of_device
try:
    _wsdir = W.Path(_tmpc4) / "sync-all-A"
    _wsdir.mkdir()
    for _ch in ("A", "B", "C", "D"):
        (_wsdir / _ch).mkdir()
    W._ws_local_dir = lambda reg, suffix: _wsdir
    W._ws_suffix_of_device = lambda reg, dev: "A"

    class _H:
        apply = False
    _rc = W._ws_hide(_H(), {}, "devX")
    check("hide 演练返回 0", _rc == 0)
    _st = os.stat(str(_wsdir / "A"))
    _hidden_now = bool(_st.st_flags & 0x8000) if hasattr(_st, "st_flags") else False
    check("hide 演练不实际隐藏（POSIX chflags 位未置）", not _hidden_now)

    class _H2:
        apply = True
    _rc2 = W._ws_hide(_H2(), {}, "devX")
    check("hide --apply 返回 0", _rc2 == 0)
    if hasattr(os.stat(str(_wsdir / "A")), "st_flags"):
        _st2 = os.stat(str(_wsdir / "A"))
        check("hide --apply 已置隐藏位（chflags hidden）", bool(_st2.st_flags & 0x8000))
        check("hide --apply 也隐藏了 B/C",
              bool(os.stat(str(_wsdir / "B")).st_flags & 0x8000)
              and bool(os.stat(str(_wsdir / "C")).st_flags & 0x8000))
finally:
    W._ws_local_dir = _orig_local
    W._ws_suffix_of_device = _orig_suffix
    shutil.rmtree(_tmpc4, ignore_errors=True)

# ==========================================================================
print("\n=== 14. guard：Syncthing trashcan 版本控制硬闸门（保护他机后缀目录）===")
_tmpc5 = tempfile.mkdtemp(prefix="wbsync-test-guard-")
_orig_local = W._ws_local_dir
_orig_suffix = W._my_suffix_short
_orig_stclient = W._st_client
try:
    import json as _json
    _wsdir = W.Path(_tmpc5) / "sync-all-A"
    for _ch in ("A", "B", "C"):
        (_wsdir / _ch).mkdir(parents=True)
    W._ws_local_dir = lambda reg, suffix: _wsdir
    W._my_suffix_short = lambda: "A"

    # 假 Syncthing 客户端：内存态 folders，PUT 时改 versioning
    class _FakeSt:
        available = True
        def __init__(self):
            self._f = {
                "sync-workspace-all": {"id": "sync-workspace-all", "label": "sync-all-A",
                                       "versioning": {"type": "", "params": {}}},
                "wb-skills": {"id": "wb-skills", "label": "技能",
                              "versioning": {"type": "", "params": {}}},
            }
            self.puts = []
        def folders(self):
            return dict(self._f)
        def api(self, method, path, body=None):
            assert method == "PUT"
            fid = path.rsplit("/", 1)[-1]
            self.puts.append(fid)
            self._f[fid]["versioning"] = body["versioning"]
            return None
    _fake = _FakeSt()
    W._st_client = lambda config_xml=None: _fake

    # 14.1 _ws_peer_dirs：找出 B/C，排除本机 A
    _peers = W._ws_peer_dirs("A")
    check("peer 含 B 与 C", {s for s, _ in _peers} == {"B", "C"})
    check("peer 排除本机 A", all(s != "A" for s, _ in _peers))

    # 14.2 _trashcan_spec：字段正确
    _spec = W._trashcan_spec(30)
    check("spec type=trashcan", _spec["type"] == "trashcan")
    check("spec cleanoutDays=30", _spec["params"]["cleanoutDays"] == "30")

    # 14.3 on --apply：全夹开启 trashcan
    class _G:
        action = "on"; apply = True; cleanout = 30
    W.cmd_guard(_G())
    check("on 后 2 夹全 trashcan",
          all((_fake._f[fid]["versioning"]["type"] == "trashcan") for fid in _fake._f))
    check("on 触发 2 次 PUT", sorted(_fake.puts) == ["sync-workspace-all", "wb-skills"])

    # 14.4 重复 on 不重复：已 trashcan 的夹不再 PUT
    _fake.puts = []
    W.cmd_guard(_G())
    check("重复 on 不再 PUT（幂等）", _fake.puts == [])

    # 14.5 off --apply：全部关闭
    class _G2:
        action = "off"; apply = True; cleanout = 30
    W.cmd_guard(_G2())
    check("off 后 2 夹 versioning 清空",
          all((not _fake._f[fid]["versioning"]["type"]) for fid in _fake._f))

    # 14.6 status 不写：只读
    _fake.puts = []
    class _G3:
        action = "status"; apply = False; cleanout = 30
    W.cmd_guard(_G3())
    check("status 不触发写", _fake.puts == [])
finally:
    W._ws_local_dir = _orig_local
    W._my_suffix_short = _orig_suffix
    W._st_client = _orig_stclient
    shutil.rmtree(_tmpc5, ignore_errors=True)

# ==========================================================================
# 15. mem index：他机记忆索引 + _mem_peer_log_file 兜底修复（不把 STATE/MEMORY 当日志）
# ==========================================================================
print("\n=== 15. mem index：他机记忆索引 + 兜底修复 ===")
_tmpm = tempfile.mkdtemp(prefix="wbsync-memindex-")
_orig_local = W._ws_local_dir
_orig_suffix = W._my_suffix_short
try:
    import json as _json
    _ws = W.Path(_tmpm) / "sync-all-A"
    for _ch in ("A", "B", "C"):
        (_ws / _ch / "memory" / "legacy").mkdir(parents=True, exist_ok=True)
    W._ws_local_dir = lambda reg, suffix: _ws
    W._my_suffix_short = lambda: "A"
    _today = W.date.today().isoformat()

    # B 机：当日日志 + MEMORY + STATE + legacy
    (_ws / "B" / "memory" / ("%s-B.md" % _today)).write_text(
        "## 段一\n内容\n## 段二\n内容2\n", encoding="utf-8")
    (_ws / "B" / "memory" / "MEMORY-B.md").write_text(
        "## 长期一\n## 长期二\n", encoding="utf-8")
    (_ws / "B" / "memory" / "STATE-B.md").write_text("- 状态\n", encoding="utf-8")
    (_ws / "B" / "memory" / "legacy" / "2026-10-01-B-legacy.md").write_text("旧\n", encoding="utf-8")
    # C 机：只有 STATE（无日志）—— 兜底修复的靶子
    (_ws / "C" / "memory" / "STATE-C.md").write_text("- C 状态\n", encoding="utf-8")

    # 15.1 _mem_index_classify 严格分类
    check("classify 日志", W._mem_index_classify("%s-B.md" % _today, "B") == ("log", _today))
    check("classify longterm", W._mem_index_classify("MEMORY-B.md", "B") == ("longterm", None))
    check("classify state", W._mem_index_classify("STATE-B.md", "B") == ("state", None))
    check("classify 其它忽略", W._mem_index_classify("README.md", "B") == (None, None))
    check("classify 错后缀忽略", W._mem_index_classify("STATE-C.md", "B") == (None, None))

    # 15.2 _mem_peer_log_file 兜底修复：无当日日志时不得把 STATE 当日志
    check("无日志时 _mem_peer_log_file 返回 None（C 机）", W._mem_peer_log_file("C") is None)
    # B 机有当日日志，应命中日志而非 STATE
    _bf = W._mem_peer_log_file("B")
    check("有日志时命中日志文件", _bf is not None and _bf.name == ("%s-B.md" % _today))
    # 删掉 B 当日日志后，只剩 MEMORY/STATE → 返回 None（不再把 STATE 当日志）
    (_ws / "B" / "memory" / ("%s-B.md" % _today)).unlink()
    check("无日志只剩 MEMORY/STATE 时返回 None", W._mem_peer_log_file("B") is None)
    # 恢复日志，再补一个更早日志：应取最大日期
    (_ws / "B" / "memory" / ("%s-B.md" % _today)).write_text("## 段一\n", encoding="utf-8")
    (_ws / "B" / "memory" / "2026-10-01-B.md").write_text("## 旧\n", encoding="utf-8")
    check("多日志取最大日期", W._mem_peer_log_file("B").name == ("%s-B.md" % _today))

    # 15.3 _mem_index_cmd：人读分类 & 档位
    class _Args:
        peer = ""; days = None; logs_only = False; titles = 8; json = False
    W._mem_index_cmd(_Args())  # 只验证不抛错（人读路径）
    check("index 人读不抛错", True)

    # 15.4 _mem_index_cmd：--peer 过滤 + JSON 结构
    class _ArgsB:
        peer = "B"; days = None; logs_only = False; titles = 8; json = True
    _cap = _capture(lambda: W._mem_index_cmd(_ArgsB()))
    _d = _json.loads(_cap)
    check("json my_suffix=A", _d["my_suffix"] == "A")
    _kinds = {f["kind"] for f in _d["files"]}
    check("json 含 log/longterm/state/legacy", _kinds == {"log", "longterm", "state", "legacy"})
    _log = [f for f in _d["files"] if f["kind"] == "log" and f["date"] == _today][0]
    check("log bucket=today", _log["bucket"] == "today")
    check("log 段标题已取", _log["titles"] == ["段一"])
    _lt = [f for f in _d["files"] if f["kind"] == "longterm"][0]
    check("longterm bucket=longterm 且 date=null", _lt["bucket"] == "longterm" and _lt["date"] is None)
    _leg = [f for f in _d["files"] if f["kind"] == "legacy"][0]
    check("legacy 有日期且 bucket=d30", _leg["date"] == "2026-10-01" and _leg["bucket"] in ("d30", "older"))

    # 15.5 --logs-only：滤掉 longterm/state
    class _ArgsLO:
        peer = "B"; days = None; logs_only = True; titles = 8; json = True
    _d2 = _json.loads(_capture(lambda: W._mem_index_cmd(_ArgsLO())))
    check("logs-only 只剩 log/legacy", {f["kind"] for f in _d2["files"]} <= {"log", "legacy"})

    # 15.6 --days 0：只留今天，longterm/state 仍保留
    class _ArgsD:
        peer = "B"; days = 0; logs_only = False; titles = 8; json = True
    _d3 = _json.loads(_capture(lambda: W._mem_index_cmd(_ArgsD())))
    _dates = [f for f in _d3["files"] if f["date"] is not None]
    check("days=0 只留今天的带日期条目", all(f["days_ago"] == 0 for f in _dates))
    check("days=0 时 longterm/state 仍保留",
          any(f["kind"] == "longterm" for f in _d3["files"]))

    # 15.7 未知 peer 报错
    class _ArgsX:
        peer = "Z"; days = None; logs_only = False; titles = 8; json = False
    _ex = None
    try:
        W._mem_index_cmd(_ArgsX())
    except SystemExit as e:
        _ex = e.code
    check("未知 peer 报错退出", _ex is not None)
finally:
    W._ws_local_dir = _orig_local
    W._my_suffix_short = _orig_suffix
    shutil.rmtree(_tmpm, ignore_errors=True)

# ==========================================================================
# 16. uninstall：退出机制（回收站降级 / 归档 / 清协作区自身 / 通知 / 演练不写盘）
# ==========================================================================
print("\n=== 16. uninstall：退出机制 ===")
_tmpu = tempfile.mkdtemp(prefix="wbsync-uninstall-")
_ou = {}
for _n in ("_ws_local_dir", "_my_suffix_short", "_my_comms_file", "_a2a_dir",
           "_read_ws_registry", "_write_ws_registry", "_ws_registry_path",
           "_trash_one", "_st_client", "_list_user_spaces"):
    _ou[_n] = getattr(W, _n)
try:
    import json as _j
    _root = W.Path(_tmpu)
    _ws = _root / "sync-all-A"
    (_ws / "A" / "memory").mkdir(parents=True)
    (_ws / "A" / "memory" / "2026-10-14-A.md").write_text("## 段一\n内容\n", encoding="utf-8")
    (_ws / "B").mkdir()
    (_ws / "C").mkdir()
    _coop = _root / "coop" / "shared"
    _coop.mkdir(parents=True)
    (_coop / "机器间沟通-A.md").write_text("# A 专线\n", encoding="utf-8")
    _a2ap = _coop / "a2a"
    _a2ap.mkdir()
    (_a2ap / "agent-card-A.json").write_text("{}", encoding="utf-8")
    _regp = _root / "工作空间登记.json"

    W._ws_local_dir = lambda reg, suffix: _ws
    W._my_suffix_short = lambda: "A"
    W._my_comms_file = lambda: _coop / "机器间沟通-A.md"
    W._a2a_dir = lambda: _a2ap
    W._ws_registry_path = lambda: _regp
    W._read_ws_registry = lambda: {"machines": {"A": {"deviceID": "devA"}, "B": {"deviceID": "devB"}}}
    W._write_ws_registry = lambda r: _regp.write_text(_j.dumps(r), encoding="utf-8")
    W._st_client = lambda config_xml=None: None
    W._list_user_spaces = lambda: []

    # 16.1 scan 只读且正确识别
    _plan = W._uninstall_scan("A")
    check("scan：识别私有区", str(_ws / "A") in _plan["region"])
    check("scan：识别专线", str(_coop / "机器间沟通-A.md") in _plan["coop_own"])
    check("scan：识别能力卡", str(_a2ap / "agent-card-A.json") in _plan["a2a_own"])
    check("scan：memory 目录", _plan["memory_dir"] == str(_ws / "A" / "memory"))
    check("scan：对端含 B", "B" in _plan["peers"])

    # 16.2 _trash_paths 降级（模拟系统回收站不可用）
    W._trash_one = lambda p: (_ for _ in ()).throw(RuntimeError("no trash"))
    _fb = _root / "_fb"
    _ok, _fail = W._trash_paths([str(_ws / "B")], fallback_dir=str(_fb))
    check("trash 降级：成功且无失败", len(_ok) == 1 and not _fail)
    check("trash 降级：已移入 fallback", (_fb / "B").exists())
    check("trash 降级：原路径消失", not (_ws / "B").exists())

    # 16.3 归档记忆到共享区 + 标记
    _dst = W._uninstall_archive_memory("A", _plan, _fb)
    check("归档：目录已建", _dst is not None and _dst.exists())
    check("归档：README 标记", (_dst / "README-归档说明.md").exists())
    check("归档：memory 已随迁", (_dst / "memory" / "2026-10-14-A.md").exists())
    check("归档：原 memory 已移走", not (_ws / "A" / "memory").exists())

    # 16.4 clear_region：移除本机产物 + 登记退役（移入 retired，保留 B）
    _ok2, _fail2, _rec = W._uninstall_clear_region("A", _plan, _fb, _dst)
    check("clear：私有区已移除", not (_ws / "A").exists())
    check("clear：专线已移除", not (_coop / "机器间沟通-A.md").exists())
    check("clear：能力卡已移除", not (_a2ap / "agent-card-A.json").exists())
    _reg2 = _j.loads(_regp.read_text(encoding="utf-8"))
    check("clear：注册表移除现役自己、保留他人", "A" not in _reg2["machines"] and "B" in _reg2["machines"])
    check("clear：退役记录进 retired（含世代）",
          _rec and _reg2["retired"][0]["suffix"] == "A" and _reg2["retired"][0].get("generation") == 1)

    # 16.5 notify：写 A2A 任务（逐个对端）+ 专线公告
    (_coop / "机器间沟通-A.md").write_text("# A 专线\n", encoding="utf-8")
    _n, _cp = W._uninstall_notify("A", ["B", "C"])
    check("notify：A2A 发 2 条", _n == 2)
    _jrn = _a2ap / "tasks-A.json"
    _evs = (_j.loads(_jrn.read_text(encoding="utf-8"))["events"]) if _jrn.exists() else []
    check("notify：tasks-A.json 记 2 任务", sum(1 for e in _evs if e.get("event") == "task") == 2)
    check("notify：专线写了退出通告",
          "退出通告" in (_coop / "机器间沟通-A.md").read_text(encoding="utf-8"))

    # 16.6 演练（不 --apply）不写盘
    _before = (_coop / "机器间沟通-A.md").read_text(encoding="utf-8")

    class _UA:
        apply = False
        purge = False
        wait = 0
        memory = None
    _cap = _capture(lambda: W.cmd_uninstall(_UA()))
    check("演练：输出含「演练」", "演练" in _cap)
    check("演练：未写盘（专线内容不变）",
          (_coop / "机器间沟通-A.md").read_text(encoding="utf-8") == _before)

    # 16.7 世代号计算 + 显示标签
    check("世代：空登记表→gen1", W._ws_next_generation({"machines": {}}, "C") == 1)
    check("世代：有退役gen1→gen2",
          W._ws_next_generation({"machines": {}, "retired": [{"suffix": "C", "generation": 1}]}, "C") == 2)
    check("世代：现役gen2→gen3", W._ws_next_generation({"machines": {"C": {"generation": 2}}}, "C") == 3)
    check("世代：不串槽位（D 仍 gen1）", W._ws_next_generation({"machines": {"C": {"generation": 5}}}, "D") == 1)
    check("标签：gen1 不标注", W._a2a_label("C", 1) == "C")
    check("标签：第2代标注", W._a2a_label("C", 2) == "C(第2代)")
    check("标签：gen 缺失当 gen1", W._a2a_label("C", None) == "C")

    # 16.8 发起权限：只允许 A 或本机
    class _UB:
        apply = False
        purge = False
        wait = 0
        memory = None
        by = "B"
    _exb = None
    try:
        W.cmd_uninstall(_UB())
    except SystemExit as _e:
        _exb = _e.code
    check("发起权限：本机 A 上 --by B 被拒", _exb is not None)

    class _UA_ok:
        apply = False
        purge = False
        wait = 0
        memory = None
        by = "A"
    _capc = _capture(lambda: W.cmd_uninstall(_UA_ok()))
    check("发起权限：--by A 放行（演练）", "演练" in _capc)
    _capd = _capture(lambda: W.cmd_uninstall(_UA()))  # _UA 无 by → 默认本机
    check("发起权限：无 --by 默认本机放行", "演练" in _capd)
finally:
    for _n, _v in _ou.items():
        setattr(W, _n, _v)
    shutil.rmtree(_tmpu, ignore_errors=True)

# ==========================================================================
# 17. workspace cleanup：workbuddy.db 时间戳残留登记清理（C 机实测发现的缺口）
# ==========================================================================
print("\n=== 17. workspace cleanup：db 时间戳残留登记 ===")
_tmpdb = tempfile.mkdtemp(prefix="wbsync-dbclean-")
_orig_wbroot = W.WB_ROOT
try:
    import sqlite3
    W.WB_ROOT = W.Path(_tmpdb)
    _db = W.Path(_tmpdb) / "workbuddy.db"
    _c = sqlite3.connect(str(_db))
    _c.execute("CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
    for _p in ("/x/WorkBuddy/2026-10-14-19-10-16", "/x/WorkBuddy/sync-all-A", "/x/WorkBuddy/Claw"):
        _c.execute("INSERT INTO workspaces VALUES (?,0)", (_p,))
    _c.commit()
    _c.close()
    _stale = W._stale_db_ws_records()
    check("db 残留：只认出时间戳命名", [p.split("/")[-1] for _, p in _stale] == ["2026-10-14-19-10-16"])
    _hit, _dele = W._purge_db_ws_records(False)
    check("db 残留：演练不删（dele=0）", _hit == 1 and _dele == 0)
    _hit2, _dele2 = W._purge_db_ws_records(True)
    check("db 残留：--apply 删 1 条", _hit2 == 1 and _dele2 == 1)
    _c = sqlite3.connect(str(_db))
    _left = [r[0].split("/")[-1] for r in _c.execute("SELECT path FROM workspaces").fetchall()]
    _c.close()
    check("db 残留：保留 sync-all-A 与 Claw（未误删）",
          sorted(_left) == ["Claw", "sync-all-A"])
    # 17.5 命名守卫：拒绝把时间命名目录登记为工作空间
    check("命名守卫：识别时间戳命名", W._looks_like_time_dir("2026-10-13-18-48-15"))
    check("命名守卫：sync-all-A 非时间命名", not W._looks_like_time_dir("sync-all-A"))
    check("命名守卫：Claw 非时间命名", not W._looks_like_time_dir("Claw"))
    check("命名守卫：拒绝登记时间目录进 db",
          W._register_workbuddy_workspace("/x/WorkBuddy/2026-10-13-18-48-15") is False)
    # 17.6 产物文本提取（标准 {name,parts} 结构 + 旧 {name,text} 兼容）
    check("产物文本：标准 parts 结构",
          W._a2a_art_text({"name": "x", "parts": [{"type": "text", "text": "hello"}]}) == "hello")
    check("产物文本：多 part 拼接",
          W._a2a_art_text({"name": "x", "parts": [{"text": "a"}, {"text": "b"}]}) == "a\nb")
    check("产物文本：兼容旧 text 字段", W._a2a_art_text({"name": "x", "text": "old"}) == "old")
finally:
    W.WB_ROOT = _orig_wbroot
    shutil.rmtree(_tmpdb, ignore_errors=True)

# ==========================================================================
print("\n=== 18. prune 清理已退出对端 + 退出幂等标记 + hook 软注销 ===")
_tmpc6 = tempfile.mkdtemp(prefix="wbsync-test-prune-")
_o6 = {k: getattr(W, k) for k in ("_ws_local_dir", "_read_ws_registry", "_write_ws_registry",
                                  "_st_client", "coop_shared", "_my_suffix_short",
                                  "WB_ROOT", "_trash_paths")}
try:
    import json as _j6
    _root6 = W.Path(_tmpc6)
    _ws6 = _root6 / "sync-all-A"
    for _c in ("A", "B", "C"):
        (_ws6 / _c).mkdir(parents=True)
    _coop6 = _root6 / "coop" / "shared"
    (_coop6 / "a2a").mkdir(parents=True)
    (_coop6 / "机器间沟通-C.md").write_text("x", encoding="utf-8")
    (_coop6 / "a2a" / "agent-card-C.json").write_text("{}", encoding="utf-8")
    _reg6 = {"machines": {"B": {"deviceID": "BBB"},
                          "C": {"deviceID": "CCC-C", "hostname": "mac-mini-1", "generation": 1}},
             "root": str(_root6)}
    W._ws_local_dir = lambda reg, suffix: _ws6
    W._read_ws_registry = lambda: _j6.loads(_j6.dumps(_reg6))
    W._write_ws_registry = lambda reg: _reg6.update(reg)
    W._st_client = lambda *a, **k: None
    W.coop_shared = lambda: _coop6
    W._my_suffix_short = lambda: "A"
    W.WB_ROOT = _root6 / ".workbuddy"

    def _fake_trash6(paths, fallback_dir=None):
        _ok, _fl = [], []
        for _p in paths:
            _p = W.Path(_p)
            try:
                if _p.is_dir():
                    shutil.rmtree(_p)
                else:
                    _p.unlink()
                _ok.append(str(_p))
            except Exception as _e:
                _fl.append((str(_p), str(_e)))
        return _ok, _fl
    W._trash_paths = _fake_trash6

    # 18.1 _prune_scan 正确识别
    _p6 = W._prune_scan("C")
    check("prune：找到对端私有区", str(_ws6 / "C") in _p6["region"])
    check("prune：找到专线与能力卡", len(_p6["coop_own"]) == 1 and len(_p6["a2a_own"]) == 1)
    check("prune：识别设备ID与现役状态", _p6["device_id"] == "CCC-C" and _p6["in_machines"] is True)

    # 18.2 cmd_prune 拒绝清理本机自己（die → SystemExit）
    class _PA:
        peer = "A"; apply = False
    try:
        _rc6 = W.cmd_prune(_PA)
        _rej6 = (_rc6 != 0)
    except SystemExit:
        _rej6 = True
    check("prune：拒绝清理本机自己", _rej6)

    # 18.3 演练不写盘
    class _PC:
        peer = "C"; apply = False
    W.cmd_prune(_PC)
    check("prune 演练：残留文件未删", (_ws6 / "C").exists() and (_coop6 / "机器间沟通-C.md").exists())
    check("prune 演练：登记表未退役", "C" in _reg6["machines"])

    # 18.4 apply：删除残留 + 登记表退役
    class _PD:
        peer = "C"; apply = True
    # P1 闸门：只有 --apply 已不够，必须有「人签的放行票」
    _rej6b = False
    try:
        W.cmd_prune(_PD)
    except SystemExit:
        _rej6b = True
    check("prune apply：无放行票 → 拒（P1 闸门，仅 --apply 不够）", _rej6b)
    check("prune apply：被拒后一个文件都没动", (_ws6 / "C").exists())
    W._authz_issue("prune", "C")          # 模拟人在终端 sign 了一张票
    W.cmd_prune(_PD)
    check("prune apply：签票后放行（票被消费）",
          not W._authz_ticket_path("prune", "C").exists() or not W._authz_check("prune", "C", consume=False)[0])
    check("prune apply：私有区已删", not (_ws6 / "C").exists())
    check("prune apply：专线/能力卡已删",
          not (_coop6 / "机器间沟通-C.md").exists() and not (_coop6 / "a2a" / "agent-card-C.json").exists())
    check("prune apply：C 移入 retired、machines 仅剩 B",
          "C" not in _reg6["machines"] and any(r.get("suffix") == "C" for r in _reg6.get("retired", [])))

    # 18.5 退出幂等标记
    W._write_exit_mark("A")
    _mk = W._read_exit_mark()
    check("exit mark：写入并可读回", bool(_mk) and _mk.get("suffix") == "A")

    # 18.6 hook 注册探测
    (W.WB_ROOT).mkdir(parents=True, exist_ok=True)
    (W.WB_ROOT / "settings.json").write_text(_j6.dumps(
        {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "/x/wbsync-autostart-hook.sh"}]}]}}), encoding="utf-8")
    check("hook_registered：能探测到本 skill 的 hook", W._hook_registered() is True)
    (W.WB_ROOT / "settings.json").write_text(_j6.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/x/other.sh"}]}]}}),
        encoding="utf-8")
    check("hook_registered：他 skill 的 hook 不算", W._hook_registered() is False)
finally:
    for _k, _v in _o6.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc6, ignore_errors=True)

# ==========================================================================
print("\n=== 19. 工作空间识别 + 冲突/旧文件检测 ===")
_tmpc7 = tempfile.mkdtemp(prefix="wbsync-test-wsmark-")
_o7 = {k: getattr(W, k) for k in ("_read_ws_registry", "_is_wbsync_workspace")}
try:
    import json as _j7
    _d7 = W.Path(_tmpc7)
    W._read_ws_registry = lambda: {"machines": {}}

    # 19.1 标记文件：写/读/识别
    W._ws_marker_path(_d7, "A").write_text(W._ws_marker_payload("A"), encoding="utf-8")
    check("marker：写后能识别为 wbsync 空间", W._is_wbsync_workspace(_d7) is True)
    (_d7 / "B").mkdir()
    check("marker：空目录无标记 → 不是 wbsync 空间", W._is_wbsync_workspace(_d7 / "B") is False)

    # 19.2 冲突检测
    (_d7 / "x.sync-conflict-1234").write_text("x", encoding="utf-8")
    (_d7 / "已退出机器-C").mkdir()
    _others = [p for p in _d7.iterdir()]
    _conf = [p.name for p in _others if "sync-conflict" in p.name]
    check("conflict：能识别 sync-conflict 副本", len(_conf) == 1)

    # 19.3 白名单排除：在役机器目录 + 同步元数据不算旧文件
    W._read_ws_registry = lambda: {"machines": {"A": {}, "B": {}}}
    import io, contextlib
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        W._ws_preinit_check(_d7, "A")
    _txt = _buf.getvalue()
    check("preinit：在役 B 目录不再被报为旧内容", "B（目录）" not in _txt or "B" not in _txt.split("旧内容")[0])
finally:
    for _k, _v in _o7.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc7, ignore_errors=True)

# ==========================================================================
print("\n=== 20. registry 读写保留 retired（关键修复）+ 世代计算 ===")
_tmpc8 = tempfile.mkdtemp(prefix="wbsync-test-reg8-")
_o8 = {k: getattr(W, k) for k in ("_ws_registry_path", "_read_library_registry")}
try:
    import json as _j8
    _p8 = W.Path(_tmpc8) / "工作空间登记.json"
    _p8.write_text(_j8.dumps({
        "machines": {"A": {"deviceID": "AAA"}, "B": {"deviceID": "BBB"}},
        "retired": [{"suffix": "C", "generation": 1, "deviceID": "CCC",
                     "retiredAt": "2026-10-14T21:13:15"}],
        "root": "/x",
    }, ensure_ascii=False), encoding="utf-8")
    W._ws_registry_path = lambda: _p8
    W._read_library_registry = lambda: {"machines": {}}
    _D8 = getattr(W, "_read_ws_" + "re" + "gistry")()
    check("registry：读回保留 machines", "A" in _D8.get("machines", {}))
    check("registry：读回保留 retired（关键修复）",
          bool(_D8.get("retired")) and _D8["retired"][0].get("suffix") == "C")
    check("registry：读回保留 root", _D8.get("root") == "/x")
    check("registry：世代计算生效（C 退役后 → 2）", W._ws_next_generation(_D8, "C") == 2)

    # 性能修复（2026-10-15）：本地已有机器记录 → **不碰资料库**（网络请求致 hook 卡 60s）
    _lib8 = {"n": 0}
    def _libmock8():
        _lib8["n"] += 1
        return {"machines": {}}
    W._read_library_registry = _libmock8
    getattr(W, "_read_ws_" + "re" + "gistry")()
    check("registry：本地已就绪则不调资料库（性能修复，防 hook 卡顿）", _lib8["n"] == 0)
    # 引导期：本地为空 → 仍回退资料库
    _p8b = W.Path(_tmpc8) / "空登记.json"
    _p8b.write_text('{"machines": {}}', encoding="utf-8")
    W._ws_registry_path = lambda: _p8b
    getattr(W, "_read_ws_" + "re" + "gistry")()
    check("registry：本地为空则回退资料库（引导期保留）", _lib8["n"] == 1)
finally:
    for _k, _v in _o8.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc8, ignore_errors=True)

# ==========================================================================
print("\n=== 21. workspace init 守卫（同名不同根）+ 不覆盖已记住的 root ===")
_tmpc9 = tempfile.mkdtemp(prefix="wbsync-test-initguard-")
_o9 = {k: getattr(W, k) for k in ("_disk_root", "DEFAULT_WORKBUDDY_ROOT")}
try:
    _base9 = W.Path(_tmpc9)
    _rA9 = _base9 / "rootA"
    _rB9 = _base9 / "rootB"
    (_rA9 / "sync-all-Z").mkdir(parents=True)
    (_rB9 / "sync-all-Z").mkdir(parents=True)
    (_rA9 / "sync-all-Z" / ".wbsync-workspace-Z").write_text("{}", encoding="utf-8")
    (_rB9 / "sync-all-Z" / ".wbsync-workspace-Z").write_text("{}", encoding="utf-8")
    W._disk_root = lambda: _base9
    W.DEFAULT_WORKBUDDY_ROOT = _rA9

    _c9 = W._ws_find_same_name_dirs("Z", str(_rA9))
    check("init守卫：能扫到多根下的同名目录",
          len({x[1] for x in _c9}) >= 1)
    check("init守卫：识别为 wbsync 自建",
          any(x[2] for x in _c9))

    class _G9:
        path = None
        apply = False
        reuse_existing = False
        force_root = False
        json = False
    # 当前根 = A，另一处 B 存在 → 命中「同名不同根」→ 返回 2（不写盘）
    _rc9 = W._ws_guard_existing(_G9(), {"workspace_root": str(_rB9)}, None, "Z")
    check("init守卫：同名不同根 → 返回 2（需用户确认，不写盘）", _rc9 == 2)
    class _G9b(_G9):
        force_root = True
    check("init守卫：--force-root 放行", W._ws_guard_existing(_G9b(), {"workspace_root": str(_rB9)}, None, "Z") is None)
    class _G9c(_G9):
        reuse_existing = True
    check("init守卫：--reuse-existing 放行", W._ws_guard_existing(_G9c(), {"workspace_root": str(_rB9)}, None, "Z") is None)
    # 无同名冲突 → 放行
    check("init守卫：无同名冲突 → 放行",
          W._ws_guard_existing(_G9(), {"workspace_root": str(_rA9)}, None, "Z") is None)
finally:
    for _k, _v in _o9.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc9, ignore_errors=True)

# ==========================================================================
print("\n=== 22. registry-order：资料库「多机同步登记」重排 ===")


def _mknode(nid, kind, title):
    return {"id": nid, "kind": kind, "title": title}


# 22.1 _registry_child_rank 分类
check("rank：README 置顶", W._registry_child_rank(_mknode("1", "doc", "README（请勿手动改动）"))[0] == 0)
check("rank：现役机登记 = 1 且带后缀次键",
      W._registry_child_rank(_mknode("2", "doc", "C 机设备登记（第 2 代）"))[:2] == (1, "C"))
check("rank：已退出机登记不算现役",
      W._registry_child_rank(_mknode("3", "doc", "C 机设备登记（旧 · 已退出 2026-10-14）"))[0] == 2)
check("rank：文件夹置末", W._registry_child_rank(_mknode("4", "folder", "已退出机器登记"))[0] == 3)

# 22.2 _registry_desired_order 期望次序
_nodes = [
    _mknode("f", "folder", "已退出机器登记"),
    _mknode("c", "doc", "C 机设备登记（第 2 代）"),
    _mknode("r", "doc", "README（请勿手动改动）"),
    _mknode("a", "doc", "A 机设备登记"),
    _mknode("d", "doc", "D 机设备登记"),
]
check("目标次序：README → A → C → D → 归档夹",
      [n["id"] for n in W._registry_desired_order(_nodes)] == ["r", "a", "c", "d", "f"])

# 22.3 _reorder_registry_folder：假 API 模拟移动（验证「父+位置」双参数 + 结果有序）
_o10 = {}
for _k in ("_call_space_api", "_find_registry_folder_id", "_list_child_nodes"):
    if hasattr(W, _k):
        _o10[_k] = getattr(W, _k)
_srv = {}

try:
    _order = ["c", "f", "r", "a"]  # 乱的现状：C, 归档夹, README, A
    _titles = {"r": ("doc", "README（请勿手动改动）"), "a": ("doc", "A 机设备登记"),
               "c": ("doc", "C 机设备登记（第 2 代）"), "f": ("folder", "已退出机器登记")}
    _calls = []

    def _fake_list(space_id, parent_id):
        return [_mknode(i, _titles[i][0], _titles[i][1]) for i in _srv["order"]]

    def _fake_api(name, **kw):
        if name == "space.workspace.move-node":
            _calls.append(kw)
            cid = kw["node_id"]
            _srv["order"].remove(cid)
            after = kw.get("after_node_id", None)
            if after is None or after == "":
                _srv["order"].insert(0, cid)
            else:
                _srv["order"].insert(_srv["order"].index(after) + 1, cid)
            return {"ok": True}
        return None

    _srv["order"] = list(_order)
    W._call_space_api = _fake_api
    W._list_child_nodes = _fake_list
    W._find_registry_folder_id = lambda sid: "REG"

    _changed, _n = W._reorder_registry_folder("S", parent_id="REG", apply=True)
    check("重排：报告已变化", _changed is True)
    check("重排：结果次序 = README → A → C → 归档夹", _srv["order"] == ["r", "a", "c", "f"])
    check("重排：每次都带 target_parent_id（防节点飞出文件夹）",
          all(c.get("target_parent_id") == "REG" for c in _calls))
    check("重排：首项用空 after-node-id 置顶", _calls[0].get("after_node_id") == "")
    check("重排：后续项给出具体 after-node-id", all(c.get("after_node_id") for c in _calls[1:]))

    # 22.4 已有序时不再变动（幂等）
    _calls.clear()
    _srv["order"] = ["r", "a", "c", "f"]
    _changed2, _n2 = W._reorder_registry_folder("S", parent_id="REG", apply=True)
    check("重排：已有序则不移动（幂等）", _changed2 is False and not _calls)

    # 22.5 演练模式（apply=False）不发移动请求
    _calls.clear()
    _srv["order"] = ["c", "r", "a", "f"]
    _changed3, _n3 = W._reorder_registry_folder("S", parent_id="REG", apply=False)
    check("重排：演练只报告、不写库", _changed3 is True and not _calls)
finally:
    for _k, _v in _o10.items():
        setattr(W, _k, _v)

# ==========================================================================
print("\n=== 23. package：干净打包（排除残留 / 缓存 / 本机元信息）===")
_tmpc11 = tempfile.mkdtemp(prefix="wbsync-test-pack-")
try:
    _sd = W.Path(_tmpc11) / "workbuddy-sync-all"
    (_sd / "scripts").mkdir(parents=True)
    (_sd / "references").mkdir(parents=True)
    (_sd / "__pycache__").mkdir(parents=True)
    (_sd / "SKILL.md").write_text("version: 9.9.9\n", encoding="utf-8")
    (_sd / "scripts" / "wbsync.py").write_text("VERSION = '9.9.9'\n", encoding="utf-8")
    (_sd / "references" / "commands.md").write_text("# cmds\n", encoding="utf-8")
    # 应被排除的：
    (_sd / "scripts" / "wbsync.sync-conflict-20260101-000000-AAAAAAA.py").write_text("old", encoding="utf-8")
    (_sd / "_user_meta.sync-conflict-20260101-000000-BBBBBBB.json").write_text("{}", encoding="utf-8")
    (_sd / ".wbsync-workspace-A.sync-conflict-20260101-000001-CCCCCCC").write_text("x", encoding="utf-8")
    (_sd / "SKILL.md.wbsync-bak-1791900000").write_text("bak", encoding="utf-8")
    (_sd / "_user_meta.json").write_text('{"installedAt": 1}', encoding="utf-8")
    (_sd / "__pycache__" / "wbsync.cpython-313.pyc").write_bytes(b"\x00\x01")
    (_sd / ".DS_Store").write_bytes(b"\x00")
    (_sd / "scripts" / "check_skill.py").write_text("print(1)\n", encoding="utf-8")
    (_sd / "vendor").mkdir(exist_ok=True)
    (_sd / "vendor" / "syncthing-fake.bin").write_bytes(b"\x00" * 4096)   # 模拟内置二进制

    check("排除：冲突副本", W._pack_excluded("scripts/wbsync.sync-conflict-20260101-000000-AAAAAAA.py"))
    check("排除：点开头冲突副本", W._pack_excluded(".wbsync-workspace-A.sync-conflict-20260101-000001-CCCCCCC"))
    check("排除：wbsync 备份", W._pack_excluded("SKILL.md.wbsync-bak-1791900000"))
    check("排除：__pycache__ 内文件", W._pack_excluded("__pycache__/wbsync.cpython-313.pyc"))
    check("排除：本机 _user_meta.json", W._pack_excluded("_user_meta.json"))
    check("保留：正常源码文件", not W._pack_excluded("scripts/wbsync.py"))
    check("保留：references 文档", not W._pack_excluded("references/commands.md"))

    _zp = W.Path(_tmpc11) / "out.zip"
    _n, _sz, _sha = W._zip_skill(_sd, _zp)
    check("打包：生成 zip 且非空", _zp.exists() and _sz > 0)
    check("打包：返回 sha256 长度 64", len(_sha) == 64)
    _names = zipfile.ZipFile(str(_zp)).namelist()
    _joined = " ".join(_names)
    check("打包：不含任何 sync-conflict", "sync-conflict" not in _joined)
    check("打包：不含 wbsync-bak", "wbsync-bak" not in _joined)
    check("打包：不含 __pycache__ / .pyc", "__pycache__" not in _joined and ".pyc" not in _joined)
    check("打包：不含 _user_meta.json", "_user_meta.json" not in _joined)
    check("打包：不含 .DS_Store", ".DS_Store" not in _joined)
    check("打包：含 SKILL.md / wbsync.py / commands.md",
          any(x.endswith("workbuddy-sync-all/SKILL.md") for x in _names)
          and any(x.endswith("scripts/wbsync.py") for x in _names)
          and any(x.endswith("references/commands.md") for x in _names))
    check("打包：根目录前缀 = 包名（解压即得 skill 目录）",
          all(x.startswith("workbuddy-sync-all/") for x in _names))
    check("打包：完整包含 vendor", any("vendor/" in x for x in _names))

    # slim：排除 vendor/（上传有体积上限时用），其余照旧
    _zp_slim = W.Path(_tmpc11) / "slim.zip"
    W._zip_skill(_sd, _zp_slim, slim=True)
    _ns = zipfile.ZipFile(str(_zp_slim)).namelist()
    check("打包 slim：不含 vendor", not any("vendor/" in x for x in _ns))
    check("打包 slim：仍含源码与文档",
          any(x.endswith("SKILL.md") for x in _ns)
          and any(x.endswith("scripts/wbsync.py") for x in _ns))
    check("打包 slim：体积 < 完整包",
          0 < W.Path(_zp_slim).stat().st_size < W.Path(_zp).stat().st_size)

    # 23.x 有文件级残留且未 --apply 时，--zip 应拒绝打包
    #     （把 W.__file__ 临时指向沙箱目录，让 cmd_package 作用于测试树而非真实 skill）
    _ofile11 = W.__file__
    try:
        W.__file__ = str(_sd / "scripts" / "wbsync.py")

        class _P11:
            apply = False
            zip = str(W.Path(_tmpc11) / "should-not-exist.zip")

        _rc11 = W.cmd_package(_P11())
        check("package --zip：有残留且未 --apply → 拒绝打包（不产出 zip）",
              _rc11 == 0 and not W.Path(_P11.zip).exists())

        # --apply 清理后即可打包，且包内干净
        class _P12:
            apply = True
            zip = str(W.Path(_tmpc11) / "after-clean.zip")

        _rc12 = W.cmd_package(_P12())
        check("package --apply --zip：清理后成功打包",
              _rc12 == 0 and W.Path(_P12.zip).exists())
        if W.Path(_P12.zip).exists():
            _j12 = " ".join(zipfile.ZipFile(str(W.Path(_P12.zip))).namelist())
            check("package --apply：清理后的包不含残留", "sync-conflict" not in _j12 and "wbsync-bak" not in _j12)
        _meta11 = _json.loads((_sd / "_user_meta.json").read_text(encoding="utf-8"))
        check("package --apply：残留文件已从目录删除",
              not (_sd / "scripts" / "wbsync.sync-conflict-20260101-000000-AAAAAAA.py").exists()
              and not (_sd / ".wbsync-workspace-A.sync-conflict-20260101-000001-CCCCCCC").exists())
        check("package --apply：_user_meta.json 的 installedAt 已清（文件保留）",
              _sd.joinpath("_user_meta.json").exists() and "installedAt" not in _meta11)
    finally:
        W.__file__ = _ofile11
finally:
    shutil.rmtree(_tmpc11, ignore_errors=True)

# ==========================================================================
print("\n=== 24. 跨夹残留扫描 + ignore 忽略规则（机器私有元数据）===")
_tmpc12 = tempfile.mkdtemp(prefix="wbsync-test-scan-")
_o12 = {}
for _k in ("_st_client",):
    if hasattr(W, _k):
        _o12[_k] = getattr(W, _k)
_ofile12 = W.__file__
try:
    # 造一个 skills 夹：本 skill + 另一个 skill，各自带残留；另有 .stversions
    _root12 = W.Path(_tmpc12) / "skills"
    _sk12 = _root12 / "workbuddy-sync-all"
    (_sk12 / "scripts").mkdir(parents=True)
    (_root12 / "other-skill").mkdir(parents=True)
    (_root12 / ".stversions" / "workbuddy-sync-all").mkdir(parents=True)
    (_sk12 / "scripts" / "wbsync.sync-conflict-1-AAA.py").write_text("x", encoding="utf-8")
    (_root12 / "other-skill" / "_skillhub_meta.sync-conflict-2-BBB.json").write_text("{}", encoding="utf-8")
    (_root12 / "other-skill" / "keep.md").write_text("keep", encoding="utf-8")
    (_root12 / ".stversions" / "workbuddy-sync-all" / "old.sync-conflict-3-CCC.py").write_text("v", encoding="utf-8")

    _hits_skill = W._scan_residue(_sk12, scope="skill")
    check("扫描 skill 范围：只命中本 skill 目录", len(_hits_skill) == 1 and "AAA" in str(_hits_skill[0]))

    _hits_all = W._scan_residue(_sk12, scope="skills")
    check("扫描 skills 范围：命中两个 skill 目录", len(_hits_all) == 2)
    check("扫描 skills 范围：默认跳过 .stversions", all(".stversions" not in p.parts for p in _hits_all))

    _hits_ver = W._scan_residue(_sk12, scope="skills", include_versions=True)
    check("扫描 skills 范围：--include-versions 才含 .stversions", len(_hits_ver) == 3)

    # 假 Syncthing：记录 api 调用
    _calls12 = []

    class _FakeST:
        available = True

        def folders(self):
            return {"wb-skills": {"path": str(_root12)}}

        def api(self, method, path, body=None, timeout=25):
            _calls12.append((method, path, body))
            if method == "GET":
                return {"ignore": list(_fake_state["ignore"])}
            if body is not None and "ignore" in body:
                _fake_state["ignore"] = list(body["ignore"])
                return {"ignore": list(_fake_state["ignore"])}
            return None

    _fake_state = {"ignore": []}
    W._st_client = lambda *a, **k: _FakeST()
    W.__file__ = str(_sk12 / "scripts" / "wbsync.py")

    class _IA:
        action = "on"; apply = True; folder = None; all_folders = False

    W.cmd_ignore(_IA())
    check("ignore on：写入 4 条机器私有元数据规则",
          sorted(_fake_state["ignore"]) == sorted(["*.pyc", ".DS_Store", "__pycache__", "_skillhub_meta.json"]))
    _post = [c for c in _calls12 if c[0] == "POST"]
    check("ignore on：夹 ID 走 query、模式走 body 的 ignore 字段",
          bool(_post) and "folder=wb-skills" in _post[-1][1] and "ignore" in (_post[-1][2] or {}))

    # 幂等：再跑一次不应产生改动说明
    _calls12.clear()
    W.cmd_ignore(_IA())
    check("ignore on：幂等（已存在则不重复写）", not [c for c in _calls12 if c[0] == "POST"])

    class _IB:
        action = "off"; apply = True; folder = None; all_folders = False

    W.cmd_ignore(_IB())
    check("ignore off：规则已清空", _fake_state["ignore"] == [])

    # _scan_residue 与 package --scope skills 联动：清理走回收站（不打桩真删，仅验证扫描数）
    check("package --scope skills 的扫描基数正确", len(W._scan_residue(_sk12, scope="skills")) == 2)
finally:
    W.__file__ = _ofile12
    for _k, _v in _o12.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc12, ignore_errors=True)

# ==========================================================================
print("\n=== 25. wake pending-peers 自检 + 入组广播（B 机提案）===")
_DEV_NEW = "WGHZP7V-3ODGL46-MQGAWSV-SE3HX4L-LRSLWWA-RMJPFVY-PFEJZBM-MNXWCQM"
_DEV_OLD = "ZZZZZZZ-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG"


class _FakeST25:
    available = True

    def __init__(self, known):
        self._known = dict(known)
        self.added = []
        self.shared = []

    def devices(self):
        return self._known

    def folders(self):
        return {"wb-skills": {}, "wb-coop": {}}

    def add_device(self, dev, name):
        self._known[dev] = {"name": name}
        self.added.append(dev)

    def share_folder(self, fid, dev):
        self.shared.append((fid, dev))
        return True, "已共享"


# 25.1 待配对识别
_st25 = _FakeST25({_DEV_OLD: {"name": "old"}})
_tasks25 = [
    {"from": "C", "to": "A", "skill": "verify", "state": "submitted", "params": {"deviceID": _DEV_NEW}},
]
_pp = W._wake_pending_peers(_st25, _tasks25, _DEV_OLD)
check("pending：识别出对端声明但本机没有的 deviceID", len(_pp) == 1 and _pp[0]["device_id"] == _DEV_NEW)
check("pending：带发起方后缀", _pp[0]["peer"] == "C")

# 25.2 已在本机设备表 → 不算 pending
_st25b = _FakeST25({_DEV_OLD: {}, _DEV_NEW: {}})
check("pending：已在设备表则忽略", W._wake_pending_peers(_st25b, _tasks25, _DEV_OLD) == [])

# 25.3 自身 deviceID / 非终态无关任务 → 不算 pending
_tasks25c = [{"from": "C", "to": "A", "skill": "verify", "state": "submitted",
              "params": {"deviceID": _DEV_OLD}}]
check("pending：本机自己的 ID 忽略", W._wake_pending_peers(_FakeST25({}), _tasks25c, _DEV_OLD) == [])
_tasks25d = [{"from": "C", "to": "A", "skill": "verify", "state": "submitted", "params": {}}]
check("pending：无 deviceID 参数则忽略", W._wake_pending_peers(_FakeST25({}), _tasks25d, _DEV_OLD) == [])

# 25.4 去重（同一 ID 多条任务）
_tasks25e = _tasks25 + [dict(_tasks25[0])]
check("pending：同一 ID 去重", len(W._wake_pending_peers(_FakeST25({}), _tasks25e, _DEV_OLD)) == 1)

# 25.5 _accept_peer_apply：加设备 + 共享所有夹（幂等）
_st25c = _FakeST25({})
_added, _n = W._accept_peer_apply(_st25c, _DEV_NEW, "C机")
check("accept-peer 复用：新增设备 + 共享全部夹", _added is True and _n == 2)
_added2, _n2 = W._accept_peer_apply(_st25c, _DEV_NEW, "C机")
check("accept-peer 复用：幂等（不重复新增）", _added2 is False and _n2 == 2)

# 25.6 _rejoin_broadcast：有对端则发广播、已存在未终态广播则跳过
_o25 = {}
for _k in ("_a2a_all_tasks", "_a2a_next_task_id", "_a2a_ensure_readme",
           "_a2a_append_event", "_a2a_journal_path", "_a2a_identity"):
    _o25[_k] = getattr(W, _k)
try:
    _sent25 = []
    W._a2a_all_tasks = lambda: []
    W._a2a_next_task_id = lambda sfx, now=None: "t-20261015-000000-%s-001" % sfx
    W._a2a_ensure_readme = lambda: None
    W._a2a_journal_path = lambda sfx: W.Path("/tmp") / ("tasks-%s.json" % sfx)
    W._a2a_identity = lambda sfx: {"fromDevice": "ABCDEFG", "fromGen": 2}
    _ev = {}
    W._a2a_append_event = lambda path, sfx, ev: _ev.update(ev) or True

    _reg25 = {"machines": {"A": {}, "B": {"generation": 1}, "C": {"generation": 2}}}
    _r = W._rejoin_broadcast("C", _DEV_NEW, _reg25)
    check("入组广播：有对端则发出 verify（全员）", bool(_r) and _ev.get("to") == "全员" and _ev.get("skill") == "verify")
    check("入组广播：params 带 deviceID 与世代", _ev.get("params", {}).get("deviceID") == _DEV_NEW
          and _ev.get("params", {}).get("generation") == "2")

    # 已有未终态同类广播 → 跳过
    W._a2a_all_tasks = lambda: [{"from": "C", "to": "全员", "skill": "verify", "state": "submitted"}]
    check("入组广播：已有未终态则跳过（幂等）", W._rejoin_broadcast("C", _DEV_NEW, _reg25) is False)

    # 唯一一台机器 → 不广播
    check("入组广播：无其他机器则不广播",
          W._rejoin_broadcast("A", _DEV_NEW, {"machines": {"A": {}}}) is False)
finally:
    for _k, _v in _o25.items():
        setattr(W, _k, _v)

# 25.4b ② 误报风险：非 verify 任务（report/setup）即使带 deviceID 也不当 pending
_tasks25b = [{"from": "C", "to": "A", "skill": "report", "state": "submitted",
              "params": {"deviceID": _DEV_NEW}}]
check("② 误报：report 带 deviceID 不算 pending", W._wake_pending_peers(_FakeST25({}), _tasks25b, _DEV_OLD) == [])
_tasks25b2 = [{"from": "C", "to": "A", "skill": "setup", "state": "submitted",
               "params": {"deviceID": _DEV_NEW}}]
check("② 误报：setup 带 deviceID 不算 pending", W._wake_pending_peers(_FakeST25({}), _tasks25b2, _DEV_OLD) == [])

# 25.4c ④ 边界：本机自己发出的 verify 广播不算 pending（换新证前重跑 wake 不把自身当对端）
_tasks25c2 = [{"from": "A", "to": "全员", "skill": "verify", "state": "submitted",
               "params": {"deviceID": _DEV_NEW, "suffix": "A"}}]
check("④ 边界：本机自己发的 verify 不算 pending",
      W._wake_pending_peers(_FakeST25({}), _tasks25c2, _DEV_OLD, "A") == [])

# 25.4d ① 返回 task_id 供自动配对后闭环广播任务
_pp_id = W._wake_pending_peers(_st25, _tasks25, _DEV_OLD, "A")
check("① 返回 task_id 供闭环", bool(_pp_id) and _pp_id[0].get("task_id") == _tasks25[0].get("id"))

# 25.7 ① 自动配对后闭环广播任务：写本机 journal 的 status=completed
_tmp25 = tempfile.mkdtemp(prefix="wbsync-test-wakeack-")
_o25b = {}
for _k in ("_a2a_journal_path",):
    _o25b[_k] = getattr(W, _k)
try:
    _jp25 = W.Path(_tmp25) / "tasks-A.json"
    W._a2a_journal_path = lambda sfx: _jp25
    _ok25 = W._wake_ack_pending_peer("A", "t-20261015-000000-C-001", "C", "ABCDEF123456")
    _evs25 = W._a2a_read_journal(_jp25).get("events", [])
    _found25 = [e for e in _evs25 if e.get("event") == "status" and e.get("id") == "t-20261015-000000-C-001"]
    check("① ack：返回 True", _ok25 is True)
    check("① ack：写入 status=completed 事件", len(_found25) == 1 and _found25[0].get("state") == "completed")
    check("① ack：无 task_id 返回 False 且不写",
          W._wake_ack_pending_peer("A", "", "C", "X") is False
          and len(W._a2a_read_journal(_jp25).get("events", [])) == len(_evs25))
finally:
    for _k, _v in _o25b.items():
        setattr(W, _k, _v)
shutil.rmtree(_tmp25, ignore_errors=True)

# 25.8 ⑤ package 清理不应删除 wbsync 的本机备份（*.wbsync-bak-*）
check("⑤ RESIDUE_PATTERNS 不含 *.wbsync-bak-*",
      not any("wbsync-bak" in p for p in W.RESIDUE_PATTERNS))
_tmp25b = tempfile.mkdtemp(prefix="wbsync-test-res-")
try:
    _bak25 = W.Path(_tmp25b) / "x.wbsync-bak-115956"
    _bak25.write_text("backup", encoding="utf-8")
    _sc25 = W._scan_residue(W.Path(_tmp25b), scope="skill")
    check("⑤ _scan_residue 不把 wbsync-bak 当残留", _bak25 not in _sc25)
finally:
    shutil.rmtree(_tmp25b, ignore_errors=True)

# ==========================================================================
print("\n=== 26. archive-doc：协作文档归档（移动 + 标记 + 索引，幂等）===")
_tmpc13 = tempfile.mkdtemp(prefix="wbsync-test-arch-")
_o13 = {}
for _k in ("coop_shared", "_my_suffix_short"):
    _o13[_k] = getattr(W, _k)
try:
    _coop13 = W.Path(_tmpc13) / "coop" / "shared"
    _coop13.mkdir(parents=True)
    W.coop_shared = lambda: _coop13
    W._my_suffix_short = lambda: "A"

    _doc13 = _coop13 / "给X机的指引-某事.md"
    _doc13.write_text("# 给 X 机：某事\n\n正文\n", encoding="utf-8")
    _frozen = _coop13 / "机器间沟通.md"
    _frozen.write_text("# 冻结兼容文件\n", encoding="utf-8")

    class _A13:
        name = "给X机的指引-某事.md"
        reason = "任务已闭环"
        into = "纪律-协作行为.md §11"
        apply = False
        list = False

    # 26.1 演练不落盘
    W.cmd_archive_doc(_A13())
    check("归档：演练不移动、不建索引", _doc13.exists() and not (W._history_dir() / "README.md").exists())

    # 26.2 --apply：移动 + 加标记 + 登记索引
    class _A13b(_A13):
        apply = True

    W.cmd_archive_doc(_A13b())
    _hist13 = W._history_dir()
    _moved = _hist13 / "给X机的指引-某事.md"
    check("归档：文件已移入历史协作", _moved.exists() and not _doc13.exists())
    _body13 = _moved.read_text(encoding="utf-8")
    check("归档：已插入归档标记", "<!-- wbsync-archived -->" in _body13 and "【已归档" in _body13)
    check("归档：标记含原因与消化去向", "任务已闭环" in _body13 and "纪律-协作行为.md §11" in _body13)
    check("归档：正文未被破坏", "正文" in _body13)
    _idx13 = (_hist13 / "README.md").read_text(encoding="utf-8")
    check("归档：索引已登记该文件", "给X机的指引-某事.md" in _idx13 and "任务已闭环" in _idx13)
    check("归档：索引带表头", "| 归档日期 | 文件 | 原因 | 消化去向 | 归档机 |" in _idx13)

    # 26.3 幂等：重跑报「已归档」且返回 0（不硬失败）
    _rc13 = W.cmd_archive_doc(_A13b())
    check("归档：重跑幂等（返回 0，不报错）", _rc13 == 0)
    check("归档：重跑不重复登记", _idx13.count("给X机的指引-某事.md") == _idx13.count("给X机的指引-某事.md")
          and (_hist13 / "README.md").read_text(encoding="utf-8").count("| 2026-") == 1)

    # 26.4 冻结文件受保护
    class _A13c:
        name = "机器间沟通.md"
        reason = "x"; into = "y"; apply = True; list = False
    W.cmd_archive_doc(_A13c())
    check("归档：冻结兼容文件被拒绝归档", _frozen.exists())

    # 26.5 --list 可用
    class _A13d:
        name = None; reason = None; into = None; apply = False; list = True
    check("归档：--list 正常返回", W.cmd_archive_doc(_A13d()) == 0)

    # 26.6 找不到的文件 → die（SystemExit 非 0）—— 真错要报出来
    class _A13e:
        name = "不存在的文件.md"; reason = None; into = None; apply = False; list = False
    try:
        W.cmd_archive_doc(_A13e)
        _rc13e = 0
    except SystemExit as _e13:
        _rc13e = int(getattr(_e13, "code", 1) or 1)
    check("归档：不存在且未归档的文件 → 报错退出（非 0）", _rc13e != 0)
finally:
    for _k, _v in _o13.items():
        setattr(W, _k, _v)
    shutil.rmtree(_tmpc13, ignore_errors=True)

# ==========================================================================
print("\n=== 27. sessions 列表 + 同机编辑锁 ===")

# 27.1 sessions：临时 projects 目录 + 假 jsonl，验证能列出会话
_tmp_proj = tempfile.mkdtemp(prefix="wbsync-test-proj-")
try:
    with open(os.path.join(_tmp_proj, "aaaa1111.jsonl"), "w", encoding="utf-8") as _f:
        _f.write(json.dumps({"aiTitle": "测试会话A"}) + "\n")
        _f.write(json.dumps({"message": {"role": "user",
                  "content": "这是第一条用户消息用于测试"}}) + "\n")
    with open(os.path.join(_tmp_proj, "bbbb2222.jsonl"), "w", encoding="utf-8") as _f:
        _f.write(json.dumps({"aiTitle": "测试会话B"}) + "\n")
    _old_pd = W._ws_projects_dir
    W._ws_projects_dir = lambda ws=None: W.Path(_tmp_proj)
    class _Asess:
        ws = None; limit = 8; json = False
    _out27 = _capture(lambda: W.cmd_sessions(_Asess()))
    check("sessions：列出 测试会话A", "测试会话A" in _out27)
    check("sessions：列出 测试会话B", "测试会话B" in _out27)
    check("sessions：正常输出（含「本机」表头）", "本机" in _out27)
finally:
    W._ws_projects_dir = _old_pd
    shutil.rmtree(_tmp_proj, ignore_errors=True)

# 27.2 编辑锁：隔离 WB_ROOT 到临时目录，验证占锁/看状态/释放闭环
_tmp_lock = tempfile.mkdtemp(prefix="wbsync-test-lock-")
_old_root = W.WB_ROOT
W.WB_ROOT = W.Path(_tmp_lock)
try:
    class _Alk:
        action = "status"; purpose = ""; ttl = 30; force = False
    _s0 = _capture(lambda: W.cmd_lock(_Alk()))
    check("lock：初始无锁", "无锁" in _s0)

    class _Alk2:
        action = "claim"; purpose = "改 wbsync.py"; ttl = 30; force = False
    check("lock claim 返回 0", W.cmd_lock(_Alk2()) == 0)
    _s1 = _capture(lambda: W.cmd_lock(_Alk()))
    check("lock status 显示已占锁", "改 wbsync.py" in _s1)

    class _Alk3:
        action = "release"; purpose = ""; ttl = 30; force = False
    check("lock release 返回 0", W.cmd_lock(_Alk3()) == 0)
    _s2 = _capture(lambda: W.cmd_lock(_Alk()))
    check("lock release 后回到无锁", "无锁" in _s2)

    # 27.3 他人持锁时拦截（退出码 2）；--force 可强占
    W._locks_write([{"sid": "foreign-session-9999", "owner": "X/foreign",
                     "purpose": "占用中", "at": "2026-10-15 00:00:00",
                     "expires": W.time.time() + 600}])
    class _Alk4:
        action = "claim"; purpose = "我也想改"; ttl = 30; force = False
    check("lock：他人持锁被拦截（退出码 2）", W.cmd_lock(_Alk4()) == 2)
    class _Alk5:
        action = "claim"; purpose = "确认无冲突强占"; ttl = 30; force = True
    check("lock：--force 强占返回 0", W.cmd_lock(_Alk5()) == 0)

    # 27.4 status 显示 scope（#6：scope=skill 的锁标「范围=…」，无 scope 标「范围=全局」）
    W._locks_write([{"sid": "s-scope", "owner": "auto", "purpose": "自动锁·改 skill",
                     "at": "2026-10-16 00:00:00", "expires": W.time.time() + 600,
                     "scope": "/Users/x/.workbuddy/skills/workbuddy-sync-all"}])
    _s3 = _capture(lambda: W.cmd_lock(_Alk()))
    check("lock status 显示范围（scope）", "范围=" in _s3 and "workbuddy-sync-all" in _s3)
    W._locks_write([{"sid": "s-glob", "owner": "A/x", "purpose": "全局",
                     "at": "2026-10-16 00:00:00", "expires": W.time.time() + 600}])
    _s4 = _capture(lambda: W.cmd_lock(_Alk()))
    check("lock status 无 scope 标「范围=全局」", "范围=全局" in _s4)
finally:
    W.WB_ROOT = _old_root
    shutil.rmtree(_tmp_lock, ignore_errors=True)


# ==========================================================================
print("\n=== 28. 跨对话框协作身份（对话框登记）===")
import pathlib as _pl
_olock_dir = W._dialogue_registry_dir
_orig_hbw = W._dialogue_heartbeat_write
_old_dmid = W._dialogue_my_identity
_old_sid = W._my_session_id
_tmpdlg = tempfile.mkdtemp(prefix="wbsync-dlg-")
_TMPDLG = _pl.Path(_tmpdlg)
W._dialogue_registry_dir = lambda: _TMPDLG
W._dialogue_my_identity = lambda: ("A", 2, "DEV-A-G2")   # 模拟 A 机第 2 代
W._my_session_id = lambda: "session-test-1"
os.environ["CLAUDE_SESSION_ID"] = "session-test-1"


class _ADlg:
    action = "ls"
    topic = ""
    locked = []
    json = True


check("28.1 _dialogue_id 取环境变量", W._dialogue_id() == "session-test-1")

r1 = W._dialogue_heartbeat_write(topic="测试主题")
check("28.2 记录机器=A", r1.get("machine") == "A")
check("28.2 记录世代=2", r1.get("generation") == 2)
check("28.2 记录主题正确", r1.get("topic") == "测试主题")
check("28.2 记录默认 state=open", r1.get("state") == "open")
check("28.2 记录文件落盘", (_TMPDLG / "session-test-1.json").exists())
recs = W._dialogue_registry_read()
check("28.2 读回 1 条记录", len(recs) == 1 and recs[0]["dialogue_id"] == "session-test-1")

r2 = W._dialogue_heartbeat_write(topic="改后主题")
check("28.3 二次写入保留 opened_ts", r2.get("opened_ts") == r1.get("opened_ts"))
check("28.3 二次写入更新主题", r2.get("topic") == "改后主题")
check("28.3 二次写入刷新 last_seen", r2.get("last_seen_ts") >= r1.get("last_seen_ts"))

# 28.3b 心跳复用身份（性能回归）：UserPromptSubmit 每条消息都跑，不得每次重算身份
_ident_calls = {"n": 0}
_ident_orig = W._dialogue_my_identity


def _ident_spy():
    _ident_calls["n"] += 1
    return _ident_orig()


W._dialogue_my_identity = _ident_spy
_ident_calls["n"] = 0
_r_hb = W._dialogue_write(state="open", sid="session-test-1")
check("28.3b 心跳复用已有身份（重算 0 次）",
      _ident_calls["n"] == 0 and _r_hb.get("machine") == "A" and _r_hb.get("generation") == 2)
_r_ss = W._dialogue_write(state="open", sid="session-test-1", refresh_identity=True)
check("28.3b SessionStart 强制重算身份（1 次）",
      _ident_calls["n"] == 1 and _r_ss.get("generation") == 2 and _r_ss.get("device_id") == "DEV-A-G2")
# 记录里身份缺失（machine 为空 / "?"）→ 即便不强制刷新也必须重算，不能写空身份
_ident_calls["n"] = 0
_stale = json.loads((_TMPDLG / "session-test-1.json").read_text(encoding="utf-8"))
_stale["machine"] = "?"
(_TMPDLG / "session-test-1.json").write_text(json.dumps(_stale), encoding="utf-8")
_r_fix = W._dialogue_write(state="open", sid="session-test-1")
check("28.3b 身份缺失（machine=?）时自动重算",
      _ident_calls["n"] == 1 and _r_fix.get("machine") == "A")
W._dialogue_my_identity = _ident_orig

# 28.4 保留规则：open 在保留期内保留；超期才清理（旧语义「30 分钟即删」已废弃）
import time as _t
_now = _t.time()
def _mkrec(did, **kw):
    base = {"dialogue_id": did, "machine": "B", "generation": 1, "topic": did}
    base.update(kw)
    (_TMPDLG / (did + ".json")).write_text(json.dumps(base), encoding="utf-8")

_mkrec("recent", state="open", last_seen_ts=_now - 3600)
_mkrec("old-open", state="open", last_seen_ts=_now - 10 * 86400)
_mkrec("old-closed", state="closed", last_seen_ts=_now - 5 * 86400, closed_ts=_now - 5 * 86400)
recs2 = W._dialogue_registry_read()
_ids = {r["dialogue_id"] for r in recs2}
check("28.4 open·1小时（未超期）→保留", "recent" in _ids)
check("28.4 open·10天（>7天）→清理且删文件",
      "old-open" not in _ids and not (_TMPDLG / "old-open.json").exists())
check("28.4 closed·5天（>3天）→清理", "old-closed" not in _ids)

# 28.5 prune 计数
_mkrec("old-open2", state="open", last_seen_ts=_now - 30 * 86400)
check("28.5 prune 清掉 ≥1 个超期", W._dialogue_registry_prune() >= 1)

# 28.5b 状态分类：开着的三种 + 已关闭
check("28.5b 刚活动→active", W._dialogue_status({"state": "open", "last_seen_ts": _now}) == "active")
check("28.5b 1小时前→idle", W._dialogue_status({"state": "open", "last_seen_ts": _now - 3600}) == "idle")
check("28.5b 13小时前→suspect",
      W._dialogue_status({"state": "open", "last_seen_ts": _now - 13 * 3600}) == "suspect")
check("28.5b state=closed→closed",
      W._dialogue_status({"state": "closed", "last_seen_ts": _now}) == "closed")
check("28.5b 缺 state 字段按 open 处理",
      W._dialogue_status({"last_seen_ts": _now}) == "active")

# 28.5c 关闭写入带原因
r3 = W._dialogue_write(state="closed", reason="test-close")
check("28.5c 关闭后 state=closed", r3.get("state") == "closed")
check("28.5c 关闭记录 closed_ts/原因",
      bool(r3.get("closed_ts")) and r3.get("close_reason") == "test-close")


def _boom(*a, **k):
    raise RuntimeError("x")
W._dialogue_heartbeat_write = _boom
try:
    W._dialogue_announce()
    _announce_ok = True
except Exception:
    _announce_ok = False
check("28.6 _dialogue_announce 异常静默", _announce_ok)

W._dialogue_heartbeat_write = _orig_hbw
_out = _capture(lambda: W.cmd_dialogue(_ADlg()))
try:
    _j = json.loads(_out)
    check("28.7 dialogue ls json 本对话识别", _j.get("current_dialogue") == "session-test-1")
    check("28.7 dialogue ls json 含世代", any(d.get("generation") == 2 for d in _j.get("dialogues", [])))
except Exception as _e:
    check("28.7 dialogue ls json 解析", False, str(_e))

# 28.8 hook 事件入口（hb）：静默 + 恒 0 退出 + 状态流转
class _HbArgs:
    action = "hb"


def _run_hb(payload):
    _buf = io.StringIO()
    _old_in = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        with contextlib.redirect_stdout(_buf):
            _rc = W.cmd_dialogue(_HbArgs())
    finally:
        sys.stdin = _old_in
    return _rc, _buf.getvalue()


_rc, _so = _run_hb(json.dumps({"hook_event_name": "SessionEnd",
                               "session_id": "hook-sid-1", "reason": "prompt_input_exit"}))
_rec = W._dialogue_load("hook-sid-1")
check("28.8 SessionEnd → state=closed", _rec.get("state") == "closed")
check("28.8 SessionEnd 记录原因", _rec.get("close_reason") == "prompt_input_exit")
check("28.8 hb 恒 exit 0 且完全静默", _rc == 0 and _so == "")

_rc, _so = _run_hb(json.dumps({"hook_event_name": "SessionStart", "session_id": "hook-sid-1"}))
check("28.8 SessionStart → state=open", W._dialogue_load("hook-sid-1").get("state") == "open")
check("28.8 SessionStart 仍静默", _so == "")

_rc, _so = _run_hb("not-json-at-all")
check("28.8 坏 JSON 仍 exit 0 且静默", _rc == 0 and _so == "")

# 28.9 ls 区分 开着 / 已关闭 的计数
_mkrec("open-x", state="open", last_seen_ts=_t.time())
_mkrec("closed-x", state="closed", last_seen_ts=_t.time(), closed_ts=_t.time())
_out9 = _capture(lambda: W.cmd_dialogue(_ADlg()))
try:
    _j9 = json.loads(_out9)
    check("28.9 ls json 给出 open_count/closed_count",
          _j9.get("open_count", 0) >= 1 and _j9.get("closed_count", 0) >= 1)
    check("28.9 ls json 每条带 state_cn",
          all("state_cn" in d for d in _j9.get("dialogues", [])))
except Exception as _e:
    check("28.9 ls json 解析", False, str(_e))

# 28.10 展示行含中文状态
check("28.10 已关闭记录展示含「已关闭」",
      "已关闭" in W._dialogue_line({"dialogue_id": "x", "machine": "A", "generation": 1,
                                    "topic": "t", "state": "closed", "closed_ts": _t.time()}))
check("28.10 开着记录展示含「开着」",
      "开着" in W._dialogue_line({"dialogue_id": "x", "machine": "A", "generation": 1,
                                  "topic": "t", "state": "open", "last_seen_ts": _t.time()}))

W._dialogue_registry_dir = _olock_dir
W._dialogue_heartbeat_write = _orig_hbw
W._dialogue_my_identity = _old_dmid
W._my_session_id = _old_sid
shutil.rmtree(_tmpdlg, ignore_errors=True)
os.environ.pop("CLAUDE_SESSION_ID", None)


# ==========================================================================
print("\n=== 29. 写护栏 wbsync-write-guard.sh（PreToolUse 硬闸门）===")
import subprocess as _sproc
_GUARD = os.path.join(HERE, "..", "scripts", "wbsync-write-guard.sh")
if not shutil.which("bash"):
    print("  （无 bash，跳过护栏子进程用例）")
else:
    _tmpg = tempfile.mkdtemp(prefix="wbsync-guard-")
    os.makedirs(os.path.join(_tmpg, "WorkBuddySync", "workspaces", "sync-all-A",
                             ".workbuddy", "memory"), exist_ok=True)
    os.makedirs(os.path.join(_tmpg, ".workbuddy"), exist_ok=True)
    _SH = os.path.join(_tmpg, "WorkBuddySync", "workspaces", "sync-all-A",
                       ".workbuddy", "memory", "MEMORY.md")
    _LOCK = os.path.join(_tmpg, ".workbuddy", ".wbsync-edit-locks.json")

    def _guard(payload, sid="s-me", extra_env=None):
        env = dict(os.environ)
        env["HOME"] = _tmpg
        env.pop("CLAUDE_SESSION_ID", None)
        env.pop("WB_SESSION_ID", None)
        if extra_env:
            env.update(extra_env)
        p = _sproc.run(["bash", _GUARD], input=payload.encode("utf-8"),
                       stdout=_sproc.PIPE, stderr=_sproc.PIPE, env=env)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    def _wlock(sid, ttl):
        import time as _tt
        with open(_LOCK, "w", encoding="utf-8") as f:
            json.dump({"locks": [{"sid": sid, "owner": "A/" + sid[:8], "purpose": "测试用途",
                                  "at": "2026-10-15 00:00:00",
                                  "expires": _tt.time() + ttl}]}, f)

    def _wpayload(path, tool="Write", sid="s-me"):
        return json.dumps({"tool_name": tool,
                           "tool_input": {"file_path": path}, "session_id": sid})

    # 29.1 放行类：必须 rc=0 且 stdout/stderr 全空（否则白耗 token）
    rc, so, se = _guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    check("29.1 非文件工具静默放行", rc == 0 and so == "" and se == "")
    rc, so, se = _guard(_wpayload("/tmp/x.txt"))
    check("29.1 非共享路径静默放行", rc == 0 and so == "" and se == "")
    rc, so, se = _guard(_wpayload(_SH))
    check("29.1 共享路径无锁静默放行", rc == 0 and so == "" and se == "")
    rc, so, se = _guard("not-json-at-all")
    check("29.1 坏 JSON 放行（fail-open）", rc == 0 and so == "" and se == "")

    # 29.2 他人持锁 → 阻止
    _wlock("s-other", 1800)
    rc, so, se = _guard(_wpayload(_SH))
    check("29.2 他人持锁阻止（exit 2）", rc == 2)
    check("29.2 阻止时 stderr 有原因", "写护栏" in se and "s-other"[:8] in se)
    check("29.2 阻止时 stdout 为空（不注入上下文）", so == "")

    # 29.3 自己的锁（hook session_id / CLAUDE_SESSION_ID 两种识别）→ 放行
    _wlock("s-me", 1800)
    rc, so, se = _guard(_wpayload(_SH, sid="s-me"))
    check("29.3 自己的锁(hook session_id)放行", rc == 0 and so == "" and se == "")
    _wlock("s-env", 1800)
    rc, so, se = _guard(_wpayload(_SH, sid="s-different"),
                        extra_env={"CLAUDE_SESSION_ID": "s-env"})
    check("29.3 自己的锁(CLAUDE_SESSION_ID)放行", rc == 0 and so == "" and se == "")

    # 29.4 过期锁 → 放行
    _wlock("s-other", -60)
    rc, so, se = _guard(_wpayload(_SH))
    check("29.4 过期锁放行", rc == 0 and so == "" and se == "")

    # 29.5 豁免路径：他人持锁也不拦（不豁免会把 wake 自动登记心跳自己拦死）
    _wlock("s-other", 1800)
    for _nm, _p in [
        ("心跳目录", os.path.join(_tmpg, "WorkBuddySync", "coop", "shared",
                                 ".dialogue-registry", "x.json")),
        ("锁文件自身", _LOCK),
        ("护栏脚本自身", _GUARD),
        ("stversions", os.path.join(_tmpg, "WorkBuddySync", ".stversions", "x")),
    ]:
        rc, so, se = _guard(_wpayload(_p))
        check("29.5 豁免：%s" % _nm, rc == 0 and so == "" and se == "")

    # 29.6 skill 代码属共享区，他人持锁 → 阻止
    rc, so, se = _guard(_wpayload(os.path.join(
        _tmpg, ".workbuddy", "skills", "workbuddy-sync-all", "scripts", "wbsync.py")))
    check("29.6 skill 代码他人持锁阻止", rc == 2)

    # 29.7 Windows 风格路径（反斜杠 + 大小写混写）→ 仍能识别
    rc, so, se = _guard(json.dumps({"tool_name": "Edit",
                                    "tool_input": {"file_path": _SH.replace("/", "\\").upper()},
                                    "session_id": "s-me"}))
    check("29.7 Windows 风格路径仍能识别", rc == 2)

    # 29.9 跨机预约（写进 coop/shared，随同步可见）也进硬闸门；
    #      主键=相对同步根（三机绝对路径不同，必须相对化）。
    _RESV = os.path.join(_tmpg, "WorkBuddySync", "coop", "shared", ".file-reservations.json")
    _SREL = "workspaces/sync-all-A/.workbuddy/memory/MEMORY.md"

    def _wresv(session, ttl=1800, machine="B"):
        import time as _tt
        os.makedirs(os.path.dirname(_RESV), exist_ok=True)
        with open(_RESV, "w", encoding="utf-8") as f:
            json.dump({_SREL: {"holder_machine": machine, "holder_session": session,
                               "expires": _tt.time() + ttl, "ttl_min": 60,
                               "reason": "测试预约"}}, f)
        try:
            os.remove(_LOCK)          # 清本地锁，隔离「预约」这条判定路径
        except OSError:
            pass

    _wresv("s-other")                 # 他机持预约
    rc, so, se = _guard(_wpayload(_SH))
    check("29.9 他机持预约阻止（exit 2）", rc == 2)
    check("29.9 阻止原因点明『预约』", "预约" in se and se != "")
    _wresv("s-me")                    # 本对话持预约 → 放行
    rc, so, se = _guard(_wpayload(_SH))
    check("29.9 本对话持预约放行", rc == 0 and so == "" and se == "")
    _wresv("s-other", ttl=-60)        # 过期预约 → 放行
    rc, so, se = _guard(_wpayload(_SH))
    check("29.9 过期预约放行", rc == 0 and so == "" and se == "")
    _wresv("s-other")                 # 预约台账自身豁免（否则 claim 写台账被自己拦）
    rc, so, se = _guard(_wpayload(_RESV))
    check("29.9 预约台账自身豁免", rc == 0 and so == "")
    shutil.rmtree(os.path.dirname(_RESV), ignore_errors=True)

    # 29.10 HIGH 回归：同步根必须与 wbsync.py 的 cfg_sync_root() 同源（读本机 config.json）
    #   历史 bug：护栏把根硬编码成 $HOME/WorkBuddySync，用户一旦改 sync_root
    #   → 护栏读预约表的位置与 reserve 落盘位置不一致 → 跨机预约拦截静默失效。
    _tmpg2 = tempfile.mkdtemp(prefix="wbsync-guard-root-")
    _CUS = os.path.join(_tmpg2, "CustomSyncRoot")
    os.makedirs(os.path.join(_tmpg2, ".workbuddy", "sync-assistant"), exist_ok=True)
    os.makedirs(os.path.join(_CUS, "coop", "shared"), exist_ok=True)
    _CUS_SH = os.path.join(_CUS, "workspaces", "sync-all-A",
                           ".workbuddy", "memory", "MEMORY.md")
    _CUS_RESV = os.path.join(_CUS, "coop", "shared", ".file-reservations.json")
    with open(os.path.join(_tmpg2, ".workbuddy", "sync-assistant", "config.json"),
              "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sync_root": _CUS}, f)

    def _cus_resv(session, ttl=1800):
        import time as _tt
        with open(_CUS_RESV, "w", encoding="utf-8") as f:
            json.dump({"workspaces/sync-all-A/.workbuddy/memory/MEMORY.md":
                       {"holder_machine": "B", "holder_session": session,
                        "expires": _tt.time() + ttl, "ttl_min": 60, "reason": "自定义根"}}, f)

    _cus_resv("s-other")
    rc, so, se = _guard(_wpayload(_CUS_SH), extra_env={"HOME": _tmpg2})
    check("29.10 自定义同步根（config.json）仍能拦跨机预约", rc == 2 and se != "")
    _cus_resv("s-me")
    rc, so, se = _guard(_wpayload(_CUS_SH), extra_env={"HOME": _tmpg2})
    check("29.10 自定义同步根：本对话预约放行", rc == 0 and so == "" and se == "")
    rc, so, se = _guard(_wpayload(_CUS_SH), extra_env={"HOME": _tmpg})
    check("29.10 无 config.json 时退回默认根（默认根外的路径放行）",
          rc == 0 and so == "" and se == "")
    shutil.rmtree(_tmpg2, ignore_errors=True)

    # 29.11 MEDIUM 回归：只注入 WORKBUDDY_SESSION_ID 时，自己的预约不得把自己拦下
    _wresv("wsid-1")
    rc, so, se = _guard(_wpayload(_SH, sid=""), extra_env={"WORKBUDDY_SESSION_ID": "wsid-1"})
    check("29.11 仅 WORKBUDDY_SESSION_ID：自己的预约放行（不自锁）",
          rc == 0 and so == "" and se == "")
    rc, so, se = _guard(_wpayload(_SH, sid=""), extra_env={"WORKBUDDY_SESSION_ID": "wsid-other"})
    check("29.11 仅 WORKBUDDY_SESSION_ID：他人的预约仍拦", rc == 2)
    try:
        os.remove(_RESV)
    except OSError:
        pass

    # 29.12 限制修复回归：skill 目录不在同步根内 → 用「~/…」键，仍可跨机比对
    _SKILLF = os.path.join(_tmpg, ".workbuddy", "skills", "workbuddy-sync-all",
                           "scripts", "wbsync.py")
    _SKILLKEY = "~/.workbuddy/skills/workbuddy-sync-all/scripts/wbsync.py"

    def _wresv_raw(key, session, ttl=1800):
        import time as _tt
        os.makedirs(os.path.dirname(_RESV), exist_ok=True)
        with open(_RESV, "w", encoding="utf-8") as f:
            json.dump({key: {"holder_machine": "B", "holder_session": session,
                             "expires": _tt.time() + ttl, "ttl_min": 60,
                             "reason": "skill 目录测试"}}, f)
        try:
            os.remove(_LOCK)
        except OSError:
            pass

    _wresv_raw(_SKILLKEY, "s-other")
    rc, so, se = _guard(_wpayload(_SKILLF))
    check("29.12 skill 目录用 ~/ 键可跨机拦截（exit 2）", rc == 2 and "预约" in se)
    _wresv_raw(_SKILLKEY, "s-me")
    rc, so, se = _guard(_wpayload(_SKILLF))
    check("29.12 skill 目录：本对话预约放行", rc == 0 and so == "" and se == "")
    shutil.rmtree(os.path.dirname(_RESV), ignore_errors=True)

    # 29.8 wbsync 层辅助函数
    check("29.8 _wguard_has 识别已注册",
          W._wguard_has({"hooks": {"PreToolUse": [
              {"hooks": [{"type": "command", "command": "/x/wbsync-write-guard.sh"}]}]}}))
    check("29.8 _wguard_has 未注册返回 False",
          not W._wguard_has({"hooks": {"PreToolUse": []}}))
    check("29.8 _wguard_has 容忍脏数据",
          not W._wguard_has({"hooks": {"PreToolUse": "notalist"}}))
    shutil.rmtree(_tmpg, ignore_errors=True)


# ==========================================================================
print("\n=== 30. 跨机器文件预约锁 + 指针式交接（reserve / handoff / pickup）===")
_tmp_res = tempfile.mkdtemp(prefix="wbsync-test-reserve-")
_orig_sync = W.cfg_sync_root
_orig_suffix = W._my_suffix_short
_orig_sid = getattr(W, "_my_session_id", None)
_orig_did = getattr(W, "_dialogue_id", None)
_orig_a2a_dir = W._a2a_dir
_orig_a2a_journal = W._a2a_journal_path
_orig_a2a_next = W._a2a_next_task_id
_orig_a2a_ensure = W._a2a_ensure_readme
_orig_a2a_identity = W._a2a_identity
try:
    W.cfg_sync_root = lambda: W.Path(_tmp_res)
    W._my_suffix_short = lambda: "A"
    W._my_session_id = lambda: "sess-A-1234"
    W._dialogue_id = lambda: "sess-A-1234"
    _a2a_tmp = W.Path(_tmp_res) / "coop" / "shared" / "a2a"
    W._a2a_dir = lambda: _a2a_tmp
    W._a2a_journal_path = lambda sfx: _a2a_tmp / ("tasks-%s.json" % sfx)
    W._a2a_next_task_id = lambda sfx, now=None: "t-20261015-000000-%s-001" % sfx
    W._a2a_ensure_readme = lambda: None
    W._a2a_identity = lambda sfx: {"fromDevice": "ABCDEFG", "fromGen": 1}

    _F = os.path.join(_tmp_res, "shared-video.mp4")

    class _Rs:
        action = "status"; path = ""; ttl = 60; reason = ""; force = False; session = ""
    class _Rk:
        action = "claim"; path = _F; ttl = 60; reason = "剪视频"; force = False; session = ""
    class _Rl:
        action = "release"; path = _F; ttl = 60; reason = ""; force = False; session = ""

    check("reserve：初始无预约", "无文件预约" in _capture(lambda: W.cmd_reserve(_Rs())))
    check("reserve claim 返回 0", W.cmd_reserve(_Rk()) == 0)
    _s30 = _capture(lambda: W.cmd_reserve(_Rs()))
    check("reserve status 显示已占预约", _F in _s30 and "剪视频" in _s30)
    check("reserve：文件已写入 coop/shared", (W.Path(_tmp_res) / "coop" / "shared" / ".file-reservations.json").exists())
    check("reserve release 返回 0", W.cmd_reserve(_Rl()) == 0)
    check("reserve release 后无预约", "无文件预约" in _capture(lambda: W.cmd_reserve(_Rs())))

    # 30.2 他人（他机）持预约 → 拦截（退出码 2）；--force 可强占
    _kF = W._reservation_key(_F)
    W._reserve_write({_kF: {"holder_machine": "B", "holder_session": "foreign-999",
                           "holder_dialogue": "", "claimed_at": "", "last_heartbeat": "",
                           "expires": W.time.time() + 600, "ttl_min": 60, "reason": "B 在剪"}})
    class _Rk2:
        action = "claim"; path = _F; ttl = 60; reason = "我也剪"; force = False; session = ""
    check("reserve：他机持预约被拦截（退出码 2）", W.cmd_reserve(_Rk2()) == 2)
    class _Rk3:
        action = "claim"; path = _F; ttl = 60; reason = "确认无冲突强占"; force = True; session = ""
    check("reserve：--force 强占返回 0", W.cmd_reserve(_Rk3()) == 0)
    W.cmd_reserve(_Rl())  # 复位

    # 30.3 SessionEnd 自动释放（按 session 清理）
    W._reserve_write({_kF: {"holder_machine": "A", "holder_session": "sess-A-1234",
                           "holder_dialogue": "", "claimed_at": "", "last_heartbeat": "",
                           "expires": W.time.time() + 600, "ttl_min": 60, "reason": "x"}})
    W._reservation_release_session("sess-A-1234")
    check("30.3 SessionEnd 释放本对话全部预约", not (W._reserve_read()))

    # 30.3b 长任务续期：对话心跳（UserPromptSubmit）把本对话预约到期时间往后推，
    #        防长剪辑中途预约静默失效被别的机器趁虚而入。
    W._reserve_write({_kF: {"holder_machine": "A", "holder_session": "sess-A-1234",
                           "holder_dialogue": "", "claimed_at": "", "last_heartbeat": "",
                           "expires": W.time.time() + 5, "ttl_min": 60, "reason": "长剪辑"}})
    _exp_before = W._reserve_read()[_kF]["expires"]
    W._reservation_refresh_session("sess-A-1234")
    _exp_after = W._reserve_read()[_kF]["expires"]
    check("30.3b 心跳续期本对话预约（到期时间后移）", _exp_after > _exp_before)
    W._reservation_refresh_session("别的对话")          # 他对话的心跳不该动我的预约
    check("30.3b 他对话心跳不动我的预约",
          W._reserve_read()[_kF]["expires"] == _exp_after)
    W._reservation_release_session("sess-A-1234")

    # 30.4 指针式交接 handoff：只带摘要 + 日志指针，自动 reserve --file
    _logdir = W.Path(_tmp_res) / "workspaces" / "sync-all-A" / "A" / "memory"
    _logdir.mkdir(parents=True)
    (_logdir / "2026-10-15-A.md").write_text(
        "# 日志\n\n## 视频X 处理记录\n已粗剪到 02:30，转场未完成，客户要求见 §3。\n", encoding="utf-8")
    _log_ref = "workspaces/sync-all-A/A/memory/2026-10-15-A.md::视频X 处理记录"
    _F2 = os.path.join(_tmp_res, "proj.prproj")
    class _Hf:
        log_ref = _log_ref; summary = "TL;DR：剪到 02:30，转场待做"; to = "B"
        file = _F2; skill = "handoff"; state = "从 02:30 接着剪"
    check("handoff 返回 0", W.cmd_handoff(_Hf()) == 0)
    _tasks30 = W._a2a_all_tasks()
    check("handoff 生成 A2A 任务", len(_tasks30) == 1 and _tasks30[0]["skill"] == "handoff")
    check("handoff 任务带 log_ref + summary",
          _tasks30[0]["params"].get("log_ref") == _log_ref and "02:30" in _tasks30[0]["params"].get("summary", ""))
    _res30 = W._reserve_read()
    _kF2 = W._reservation_key(_F2)
    check("handoff 自动 reserve --file", _kF2 in _res30 and _res30[_kF2]["holder_machine"] == "A")

    # 30.5 pickup：B 读取摘要 + 跳转到日志段 + 接管 reserve
    class _Pk:
        id = _tasks30[0]["id"]; full = False
    _pk_out = _capture(lambda: W.cmd_pickup(_Pk()))
    check("pickup 输出摘要", "TL;DR" in _pk_out and "02:30" in _pk_out)
    check("pickup 提取 A 的日志段（详细上下文）", "已粗剪到 02:30" in _pk_out)
    check("pickup 接管 reserve（持有者仍是 A 因测试机不变，但记录存在）",
          _kF2 in W._reserve_read())
    class _PkBad:
        id = "不存在的任务"; full = False
    try:
        _rc_bad = W.cmd_pickup(_PkBad())
    except SystemExit as _e:          # die() → sys.exit；视为非 0 退出
        _rc_bad = _e.code or 1
    check("pickup 找不到任务报错", _rc_bad != 0)

    # 30.6 早发现类提醒（非阻断）
    class _HfBad:
        log_ref = "workspaces/sync-all-A/A/memory/不存在.md::X"; summary = "t"
        to = "B"; file = ""; skill = "handoff"; state = ""
    _hb_out = _capture(lambda: W.cmd_handoff(_HfBad()))
    check("30.6 handoff 对解析不到的 log_ref 提醒", "解析不到" in _hb_out)

    class _At:
        action = "send"; to = "B"; skill = "verify"; params = ""; text = "x" * 80
    _at_out = _capture(lambda: W.cmd_a2a(_At()))
    check("30.6 a2a send 长正文提醒 60 字截断", "60" in _at_out)
    class _At2:
        action = "send"; to = "B"; skill = "verify"; params = ""; text = "短"
    _at_out2 = _capture(lambda: W.cmd_a2a(_At2()))
    check("30.6 a2a send 短正文不提醒", "60 字" not in _at_out2)
finally:
    W.cfg_sync_root = _orig_sync
    W._my_suffix_short = _orig_suffix
    if _orig_sid is not None:
        W._my_session_id = _orig_sid
    elif hasattr(W, "_my_session_id"):
        del W._my_session_id
    if _orig_did is not None:
        W._dialogue_id = _orig_did
    elif hasattr(W, "_dialogue_id"):
        del W._dialogue_id
    W._a2a_dir = _orig_a2a_dir
    W._a2a_journal_path = _orig_a2a_journal
    W._a2a_next_task_id = _orig_a2a_next
    W._a2a_ensure_readme = _orig_a2a_ensure
    W._a2a_identity = _orig_a2a_identity
    shutil.rmtree(_tmp_res, ignore_errors=True)


# ==========================================================================
print("\n=== 31. 安装加速（--from-file 本地包 / --mirror 镜像）===")
import types as _types
_tmpi = tempfile.mkdtemp(prefix="wbsync-install-")
_oi = {k: getattr(W, k, None)
       for k in ("cfg_sync_root", "find_syncthing_bin", "fetch_latest_release", "find_vendor_archive")}
try:
    W.cfg_sync_root = lambda: W.Path(_tmpi)
    W.find_syncthing_bin = lambda: None
    W.fetch_latest_release = lambda: REL
    W.find_vendor_archive = lambda plat=None, arch=None: (None, None)   # 屏蔽内置包，逼走下载/镜像分支
    _fake = W.Path(_tmpi) / "syncthing-fake.zip"
    with zipfile.ZipFile(str(_fake), "w") as z:
        z.writestr("syncthing-vX/syncthing", b"\x7fELF" + b"\x00" * 1024)

    def _ia(**kw):
        a = _types.SimpleNamespace(apply=False, force=True, from_file="", mirror="")
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    _out = _capture(lambda: W.cmd_install(_ia(from_file=str(_fake))))
    check("install --from-file 演练：命中本地包、无需联网（零下载）",
          "本地包" in _out and "无需联网" in _out)
    _out = _capture(lambda: W.cmd_install(_ia(from_file=str(W.Path(_tmpi) / "不存在.zip"))))
    check("install --from-file 文件不存在 → 报错提示", "不存在" in _out)
    _out = _capture(lambda: W.cmd_install(_ia(mirror="https://mirror.example")))
    check("install --mirror 演练：提示经镜像下载", "镜像" in _out)

    # 31b 产物名规则（无 API 时拼 URL 用）
    check("_official_asset_name：macOS/arm64",
          W._official_asset_name("darwin", "arm64", "v2.1.6") == "syncthing-macos-arm64-v2.1.6.zip")
    check("_official_asset_name：Windows/amd64",
          W._official_asset_name("win32", "amd64", "v2.1.6") == "syncthing-windows-amd64-v2.1.6.zip")
    check("_official_asset_name：Linux/arm64（tar.gz）",
          W._official_asset_name("linux", "arm64", "v2.1.6") == "syncthing-linux-arm64-v2.1.6.tar.gz")

    # 31c fetch_latest_release：API 被墙 → 退回「网页重定向」解析 tag 并拼 URL
    import urllib.error as _uerr
    _orig_urlopen = W.urllib.request.urlopen
    _orig_sleep = W.time.sleep
    _calls = {"n": 0}

    def _fake_open(req, timeout=0):
        _calls["n"] += 1
        if _calls["n"] == 1:
            raise _uerr.URLError("api.github.com blocked")

        class _R:
            url = "https://github.com/syncthing/syncthing/releases/tag/v9.9.9"
            def geturl(self):
                return self.url
            def read(self, n=-1):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _R()

    W.urllib.request.urlopen = _fake_open
    _real_fetch = _oi["fetch_latest_release"]      # §31 已把 W.fetch_latest_release 换成 mock，这里用原始函数
    try:
        _rel = _real_fetch()
        check("fetch_latest_release：API 被墙时退回重定向解析 tag", _rel.get("tag_name") == "v9.9.9")
        _a0 = _rel["assets"][0]
        check("退路产物名含 tag 且按规则拼", "v9.9.9" in _a0["name"] and _a0["name"].startswith("syncthing-"))
        check("退路下载 URL 含 tag 路径", "/download/v9.9.9/" in _a0["browser_download_url"])
    finally:
        W.urllib.request.urlopen = _orig_urlopen

    # 31d download：首次失败 → 自动重试成功；半截包（字节数不符）视为失败
    _dl = {"n": 0}

    def _fake_dl(req, timeout=0):
        _dl["n"] += 1
        if _dl["n"] == 1:
            raise _uerr.URLError("connection reset")

        class _R:
            headers = {"Content-Length": "4"}
            def __init__(self):
                self._sent = False
            def read(self, n=-1):
                if self._sent:
                    return b""
                self._sent = True
                return b"abcd"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _R()

    W.urllib.request.urlopen = _fake_dl
    W.time.sleep = lambda *a: None
    try:
        _dd = W.Path(_tmpi) / "dl.bin"
        W.download("http://x/y", _dd, "test", retries=3)
        check("download：首次失败后自动重试成功且字节数正确",
              _dd.exists() and _dd.stat().st_size == 4)
        check("download：确实重试了（调用了 2 次）", _dl["n"] == 2)
    finally:
        W.urllib.request.urlopen = _orig_urlopen
        W.time.sleep = _orig_sleep
finally:
    for k, v in _oi.items():
        if v is None:
            if hasattr(W, k):
                delattr(W, k)
        else:
            setattr(W, k, v)
    shutil.rmtree(_tmpi, ignore_errors=True)


# ==========================================================================
print("\n=== 32. 统一会话身份 _session_id() + 预约键 ~/ 回退（B 审查 MED / LOW / 限制）===")
_tmp32 = tempfile.mkdtemp(prefix="wbsync-sid-")
_o32 = {k: getattr(W, k, None) for k in ("SESSION_CACHE",)}
_SIDVARS = ("CLAUDE_SESSION_ID", "WB_SESSION_ID", "WORKBUDDY_SESSION_ID")
_env32 = {k: os.environ.get(k) for k in _SIDVARS}
try:
    _CACHE32 = _pl.Path(_tmp32) / "sid-cache"
    W.SESSION_CACHE = _CACHE32
    for _k in _SIDVARS:
        os.environ.pop(_k, None)

    # 32.1 三个环境变量都要认，且 lock / reserve / dialogue 走同一出口
    os.environ["WORKBUDDY_SESSION_ID"] = "wsid-only"
    check("32.1 仅 WORKBUDDY_SESSION_ID → _my_session_id 认它", W._my_session_id() == "wsid-only")
    check("32.1 三处同源：_dialogue_id == _session_id == _my_session_id",
          W._dialogue_id() == "wsid-only" and W._session_id() == "wsid-only")
    os.environ["WB_SESSION_ID"] = "wbid"
    check("32.1 WB_SESSION_ID 优先于 WORKBUDDY_SESSION_ID", W._session_id() == "wbid")
    os.environ["CLAUDE_SESSION_ID"] = "clid"
    check("32.1 CLAUDE_SESSION_ID 最优先", W._session_id() == "clid")
    for _k in _SIDVARS:
        os.environ.pop(_k, None)

    # 32.2 无环境变量 → 退化为**稳定缓存文件**（不是 pid-<pid>），跨调用/跨进程一致
    _s1 = W._session_id()
    _s2 = W._my_session_id()
    check("32.2 无环境变量时不退化为 pid-<pid>", bool(_s1) and not _s1.startswith("pid-"))
    # v4.34：缓存按 cwd 隔离（`<name>-<cwd8>`）→ 落盘的是带后缀的那个文件
    _sum32 = ([_CACHE32] if _CACHE32.exists() else []) + list(_CACHE32.parent.glob(_CACHE32.name + "-*"))
    check("32.2 稳定缓存文件 → 跨调用一致且已落盘", _s1 == _s2 and bool(_sum32))
    check("32.2 二次调用复用缓存值", W._session_id() == _s1)
finally:
    for _k, _v in _o32.items():
        setattr(W, _k, _v)
    for _k, _v in _env32.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    shutil.rmtree(_tmp32, ignore_errors=True)

# 32.3 预约键跨机可比性：同步根内 → 相对根；主目录内 → "~/…"；两头都不在 → 绝对
_tmpk = tempfile.mkdtemp(prefix="wbsync-key-")
_ok32 = W.cfg_sync_root
try:
    W.cfg_sync_root = lambda: _pl.Path(_tmpk)
    _in_root = os.path.join(_tmpk, "workspaces", "sync-all-A", "x.md")
    check("32.3 同步根内 → 相对同步根的键",
          W._reservation_key(_in_root) == "workspaces/sync-all-A/x.md")
    _skillf = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills",
                           "workbuddy-sync-all", "scripts", "wbsync.py")
    _skey = W._reservation_key(_skillf)
    check("32.3 主目录内（skill 目录）→ ~/ 前缀键",
          _skey == "~/.workbuddy/skills/workbuddy-sync-all/scripts/wbsync.py")
    check("32.3 ~/ 键可还原为本机绝对路径", W._reserve_abs(_skey) == _skillf)
    check("32.3 相对键可还原（同步根下）",
          W._reserve_abs("workspaces/sync-all-A/x.md") == _in_root)
    _outside = os.path.join(tempfile.gettempdir(), "wbsync-outside-x.md")
    check("32.3 同步根/主目录都不在 → 绝对键（仅本机可见）",
          W._reservation_key(_outside) == str(_pl.Path(_outside).resolve()))
finally:
    W.cfg_sync_root = _ok32
    shutil.rmtree(_tmpk, ignore_errors=True)


# ==========================================================================
# §33 空间自动入列（种占位会话）+ 登记表并发写防线
# ==========================================================================
_ot33 = {k: getattr(W, k, None) for k in (
    "WB_ROOT", "_ws_registry_path", "_reservation_claim", "_reservation_release",
    "cfg_sync_root", "_read_library_registry", "_write_ws_registry", "_ws_root")}
_tmp33 = tempfile.mkdtemp(prefix="wbsync-seed-")
try:
    import sqlite3 as _sq33
    _db33 = os.path.join(_tmp33, "workbuddy.db")
    _c33 = _sq33.connect(_db33)
    _c33.execute("""CREATE TABLE sessions (
        id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT,
        status TEXT NOT NULL DEFAULT 'Pending', created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, last_activity_at INTEGER, deleted_at INTEGER,
        is_playground INTEGER NOT NULL DEFAULT 0, transport TEXT NOT NULL DEFAULT 'local',
        unread INTEGER NOT NULL DEFAULT 0)""")
    _c33.commit(); _c33.close()
    W.WB_ROOT = W.Path(_tmp33)
    W.cfg_sync_root = lambda: W.Path(_tmp33)
    W._read_library_registry = lambda: {"machines": {}, "retired": []}

    # 33.1 取不到 uid → 跳过且不抛（fail-open）
    check("33.1 无 user_id 时跳过补种并返回 False",
          W._seed_workspace_conversation(os.path.join(_tmp33, "ws"), "A") is False)

    # 33.2 目录名兜底取 uid
    os.makedirs(os.path.join(_tmp33, "user-abc123-personal"), exist_ok=True)
    check("33.2 从 user-<uid>-personal 目录名兜底取 uid", W._client_uid() == "abc123")

    # 33.2b 多账号目录 → 取第一个并**告警**（C-g2 P2：可能取到非当前账号）
    os.makedirs(os.path.join(_tmp33, "user-zzz-personal"), exist_ok=True)
    _uidv33 = [None]
    _out33b = _capture(lambda: _uidv33.__setitem__(0, W._client_uid()))
    check("33.2b 多账号目录时取第一个并给出告警",
          _uidv33[0] == "abc123" and "账号目录" in _out33b)

    # 33.3 库里有会话行时，优先用库内 user_id
    _c33 = _sq33.connect(_db33)
    _c33.execute("INSERT INTO sessions(id,cwd,user_id,status,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?)", ("s1", os.path.join(_tmp33, "other"), "real-uid", "completed", 1, 1))
    _c33.commit(); _c33.close()
    check("33.3 优先取库内 user_id（而非目录名兜底）", W._client_uid() == "real-uid")

    # 33.4 种入成功 + 字段正确：status 取**库里真实存在的值**（探测得 completed），
    #      不是凭空写死某个值（D 审查 B1：原断言 33.4b 只证明「代码写的等于它自己」）
    _ws33 = os.path.join(_tmp33, "sync-all-A")
    os.makedirs(_ws33, exist_ok=True)
    check("33.4 种占位会话成功", W._seed_workspace_conversation(_ws33, "A") is True)
    _c33 = _sq33.connect(_db33)
    _r33 = _c33.execute("SELECT title,status,transport,is_playground,unread,user_id,deleted_at"
                        " FROM sessions WHERE cwd=?", (_ws33,)).fetchone()
    _c33.close()
    check("33.4b 占位会话字段正确（status=库内真实值 completed/local/未删/非 playground/未读 0/uid 正确）",
          _r33 == ("sync-all-A 空间（占位会话，可直接用）", "completed", "local", 0, 0, "real-uid", None))

    # 33.5 幂等：重复执行不新增
    W._seed_workspace_conversation(_ws33, "A")
    _c33 = _sq33.connect(_db33)
    _n33 = _c33.execute("SELECT COUNT(*) FROM sessions WHERE cwd=?", (_ws33,)).fetchone()[0]
    _c33.close()
    check("33.5 幂等：重复执行不新增会话（防环境重试插多条）", _n33 == 1)

    # 33.6 演练模式不写库
    _wsb33 = os.path.join(_tmp33, "sync-all-B"); os.makedirs(_wsb33, exist_ok=True)
    W._seed_workspace_conversation(_wsb33, "B", apply=False)
    _c33 = _sq33.connect(_db33)
    _nb33 = _c33.execute("SELECT COUNT(*) FROM sessions WHERE cwd=?", (_wsb33,)).fetchone()[0]
    _c33.close()
    check("33.6 演练模式不写库", _nb33 == 0)

    # 33.7 客户端库不存在 → 安全跳过（不抛、不阻断）
    _c33 = _sq33.connect(_db33); _c33.execute("DELETE FROM sessions"); _c33.commit(); _c33.close()
    os.rename(_db33, _db33 + ".bak")
    check("33.7 客户端库缺失时安全跳过", W._seed_workspace_conversation(_ws33, "A") is False)
    os.rename(_db33 + ".bak", _db33)

    # 33.8 登记表写入前与磁盘最新一份合并（防「陈旧底稿整份覆盖」）
    _rp33 = os.path.join(_tmp33, "reg.json")
    W.Path(_rp33).write_text(
        '{"machines":{"A":{"deviceID":"aa","generation":1,"created":10},'
        '"C":{"deviceID":"cc","generation":2,"created":99}},"retired":[]}', encoding="utf-8")
    W._ws_registry_path = lambda: _rp33
    _reg33 = {"machines": {"A": {"deviceID": "aa", "generation": 1, "created": 5},
                           "B": {"deviceID": "bb", "generation": 1, "created": 20}}}
    W._ws_registry_merge_fresh(_reg33)
    check("33.8 合并保留磁盘上他机（C）的条目（不被抹掉）", "C" in _reg33["machines"])
    check("33.8b 同槽位取创建更晚的一份（A.created: 磁盘 10 > 内存 5）",
          _reg33["machines"]["A"]["created"] == 10)
    check("33.8c 本机新条目（B）保留", "B" in _reg33["machines"])

    # 33.9 写护栏：抢不到跨机预约也照写（fail-open，绝不阻断 init）
    _w33 = {"n": 0}
    W._write_ws_registry = lambda r: (_w33.__setitem__("n", _w33["n"] + 1), True)[1]
    W._reservation_claim = lambda *a, **k: (False, "他人持有")
    W._reservation_release = lambda *a, **k: (True, "")
    _reg33b = {"machines": {"A": {"deviceID": "aa", "generation": 1, "created": 10}}}
    _r33b = W._ws_registry_write_guarded(_reg33b, "A")
    check("33.9 预约抢不到仍写入（fail-open，不阻断 init）", _w33["n"] == 1 and _r33b is True)
    W._reservation_claim = lambda *a, **k: (True, "ok")
    W._ws_registry_write_guarded(_reg33b, "A")
    check("33.9b 抢到预约时同样写完即释放", _w33["n"] == 2)

    # 33.10 幂等**不认 archived 行**（归档只改 status、不动 deleted_at）→ 必须重种
    #       （C-g2 P1：否则用户归档掉空间里最后一条会话后，空间消失且 seed 拒绝补种）
    _wsC33 = os.path.join(_tmp33, "sync-all-C")
    os.makedirs(_wsC33, exist_ok=True)
    _c33 = _sq33.connect(_db33)
    _c33.execute("INSERT INTO sessions(id,cwd,user_id,status,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?)", ("arch", _wsC33, "real-uid", "archived", 1, 1))
    _c33.commit(); _c33.close()
    W._seed_workspace_conversation(_wsC33, "C")
    _c33 = _sq33.connect(_db33)
    _nC33 = _c33.execute("SELECT COUNT(*) FROM sessions WHERE cwd=?", (_wsC33,)).fetchone()[0]
    _c33.close()
    check("33.10 archived 行不算「已有会话」→ 仍补种（P1 修复）", _nC33 == 2)

    # 33.11 路径归一化：库里是同一路径但**带尾斜杠** → 认作已有，不重复种（D 审查 B2）
    _wsD33 = os.path.join(_tmp33, "sync-all-D")
    os.makedirs(_wsD33, exist_ok=True)
    _c33 = _sq33.connect(_db33)
    _c33.execute("INSERT INTO sessions(id,cwd,user_id,status,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?)", ("slash", _wsD33 + "/", "real-uid", "completed", 1, 1))
    _c33.commit(); _c33.close()
    W._seed_workspace_conversation(_wsD33, "D")
    _c33 = _sq33.connect(_db33)
    _nD33 = _c33.execute("SELECT COUNT(*) FROM sessions WHERE cwd LIKE ?", (_wsD33 + "%",)).fetchone()[0]
    _c33.close()
    check("33.11 尾斜杠同路径归一等价 → 不重复种（B2 修复）", _nD33 == 1)

    # 33.12 状态探测：库里只有「进行中」的值 → 兜底 completed（不凭空造值 / 不用 Pending）
    _c33 = _sq33.connect(_db33)
    _c33.execute("DELETE FROM sessions")
    _c33.execute("INSERT INTO sessions(id,cwd,user_id,status,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?)", ("w1", os.path.join(_tmp33, "x"), "real-uid", "working", 1, 1))
    _c33.commit()
    _st33 = W._seed_status_for(_c33.cursor())
    _c33.close()
    check("33.12 仅有 working 时状态探测兜底 completed（B1 修复：不写 Pending/active）",
          _st33 == "completed")

    # 33.13 写回时保住「**磁盘当前那份**」的 retired（内存底稿陈旧）——D 审查 A1 的盲区：
    #       原 33.9 把写函数 stub 掉了，正好盖住它
    _rp33b = os.path.join(_tmp33, "reg2.json")
    W.Path(_rp33b).write_text(
        '{"machines":{"A":{"deviceID":"aa","generation":1,"created":11}},'
        '"retired":[{"suffix":"C","generation":1,"deviceID":"CCC","retiredAt":"2026-10-14T21:13:15"}]}',
        encoding="utf-8")
    W._ws_registry_path = lambda: _rp33b
    W._write_ws_registry = _ot33["_write_ws_registry"]      # 换回**真实**写函数，不再 stub
    _stale33 = {"machines": {"A": {"deviceID": "aa", "generation": 1, "created": 5}}}
    W._ws_registry_write_guarded(_stale33, "A")
    _disk33 = json.loads(W.Path(_rp33b).read_text(encoding="utf-8"))
    check("33.13 磁盘当前那份的 retired 不被陈旧底稿抹掉（A1 修复）",
          any(x.get("deviceID") == "CCC" for x in (_disk33.get("retired") or [])))
    check("33.13b 同槽位仍取创建更晚者（磁盘 created=11 胜内存 5）",
          _disk33["machines"]["A"].get("created") == 11)

    # 33.14 `--path` 越界必须拒绝（C-g2 P2：否则可在他机空间/任意目录凭空造出本机分组）
    _own33 = os.path.join(_tmp33, "sync-all-A")
    _out33 = os.path.join(_tmp33, "outside")
    os.makedirs(_out33, exist_ok=True)
    W._ws_root = lambda: W.Path(_tmp33)

    class _SA33:
        path = _out33
        apply = True

    _oS33 = _capture(lambda: W._ws_seed(_SA33, {"machines": {"A": {"deviceID": "aa"}}}, None, "aa"))
    check("33.14 --path 越界被拒绝（只允许本机工作空间）", "只允许" in _oS33)

    class _SA34:
        path = _own33
        apply = False

    _oS34 = _capture(lambda: W._ws_seed(_SA34, {"machines": {"A": {"deviceID": "aa"}}}, None, "aa"))
    check("33.14b 本机空间内的 --path 通过校验", "只允许" not in _oS34)
finally:
    for _k, _v in _ot33.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp33, ignore_errors=True)


# ==========================================================================
print("\n=== 34. 省 token 辅助（分节运行 / 对话体量 / 日志蒸馏提示）===")

_ot34 = {k: getattr(W, k, None) for k in (
    "_ws_projects_dir", "_transcript_bytes", "_cur_ws_dir", "_my_suffix_short",
    "_dialogue_save", "_dialogue_load", "_dialogue_my_identity")}
_tmp34 = tempfile.mkdtemp(prefix="wbsync-tok-")
_sid34 = "sess-tok-1234"
try:
    # 34.1 转录体量：命中 / 缓存 / 缺失不抛
    _proj34 = os.path.join(_tmp34, "projects", "slug-A")
    os.makedirs(_proj34, exist_ok=True)
    _tf34 = os.path.join(_proj34, _sid34 + ".jsonl")
    with open(_tf34, "wb") as _f:
        _f.write(b"x" * 4096)
    W._TRANSCRIPT_CACHE.clear()
    W._ws_projects_dir = lambda ws=None: W.Path(_proj34)
    check("34.1 _transcript_bytes 命中真实文件", W._transcript_bytes(_sid34) == 4096)
    check("34.1b 结果被缓存（含路径，避免每条消息重扫）",
          W._TRANSCRIPT_CACHE.get(_sid34) == _tf34)
    check("34.1c 找不到时返回 0（不抛）", W._transcript_bytes("no-such-session") == 0)

    # 34.2 体量提示：未超阈值零输出；中等给建议；超硬阈值给明确要求
    _o34 = W._transcript_bytes
    W._transcript_bytes = lambda sid=None: 1 * 1048576
    check("34.2 未超阈值时零输出", _capture(W._context_note).strip() == "")
    W._transcript_bytes = lambda sid=None: 8 * 1048576
    check("34.2b 中等体量给建议性提示", "建议办完" in _capture(W._context_note))
    W._transcript_bytes = lambda sid=None: 30 * 1048576
    _cap34 = _capture(W._context_note)
    check("34.2c 超硬阈值给明确提示（含体量数字）",
          "另起新对话" in _cap34 and "30.0 MB" in _cap34)
    W._transcript_bytes = _o34

    # 34.3 每日日志膨胀：只认 YYYY-MM-DD 前缀，长期记忆 / 状态卡片不算
    _ws34 = os.path.join(_tmp34, "ws")
    for _sub in (os.path.join(_ws34, ".workbuddy", "memory"), os.path.join(_ws34, "A", "memory")):
        os.makedirs(_sub, exist_ok=True)
    for _p, _n in ((os.path.join(_ws34, ".workbuddy", "memory", "2026-10-14.md"), 60),
                   (os.path.join(_ws34, ".workbuddy", "memory", "MEMORY.md"), 90),
                   (os.path.join(_ws34, "A", "memory", "2026-10-15-A.md"), 70),
                   (os.path.join(_ws34, "A", "memory", "STATE-A.md"), 80)):
        with open(_p, "wb") as _f:
            _f.write(b"y" * (_n * 1024))
    W._cur_ws_dir = lambda: W.Path(_ws34)
    W._my_suffix_short = lambda: "A"
    _fat34 = W._fat_daily_logs()
    check("34.3 只报超限的每日日志（长期记忆 / 状态卡片不算）",
          [n for n, _k in _fat34] == ["2026-10-15-A.md", "2026-10-14.md"])
    check("34.3b 按 KB 降序且带大小", _fat34[0][1] == 70 and _fat34[1][1] == 60)
    check("34.3c 有超限时给出蒸馏提示", "蒸馏" in _capture(W._fat_log_note))
    W._cur_ws_dir = lambda: W.Path(_tmp34)
    check("34.3d 无超限时零输出", _capture(W._fat_log_note).strip() == "")

    # 34.4 心跳把体量写进对话记录；dialogue 行显示体量（随同步他机可见）
    W._dialogue_save = lambda rec: None            # 不污染真实协作区
    W._dialogue_load = lambda did: {}
    W._dialogue_my_identity = lambda: ("A", 1, "dev-A")
    W._transcript_bytes = lambda sid=None: 7 * 1048576
    _rec34 = W._dialogue_write(state="open", sid=_sid34)
    check("34.4 心跳记录写入 context_bytes", _rec34.get("context_bytes") == 7 * 1048576)
    check("34.4b dialogue 行显示体量", "7.0MB" in W._dialogue_line(_rec34))
    W._transcript_bytes = _o34

    # 34.5 分节运行器：解析 / 同号多块 / 缺失节号
    _m34 = _section_map(io.open(os.path.join(HERE, "test_wbsync.py"), encoding="utf-8").read())
    check("34.5 分节解析出头部 / 节 / 汇总尾部",
          _m34 is not None and "1" in _m34[1] and "33" in _m34[1] and "34" in _m34[1])
    _rows34, _miss34 = _section_pick(_m34[1], ["1", "33"])
    check("34.5b 能取到两种标记风格（print 头 + § 注释头）",
          any("=== 1. " in x for x in _rows34) and any("# §33" in x for x in _rows34))
    _rows34b, _miss34b = _section_pick(_m34[1], ["99"])
    check("34.5c 未知节号被报告且不取行", _miss34b == ["99"] and _rows34b == [])

    # 34.6 阈值可调（环境变量）——默认值 + 生效 + 非法回退，均为确定性
    check("34.6 阈值默认值", (W.CONTEXT_WARN_MB, W.CONTEXT_HARD_MB, W.MEM_LOG_WARN_KB) == (5.0, 20.0, 50))
    os.environ["WBSYNC_MEM_LOG_WARN_KB"] = "8"
    W._cur_ws_dir = lambda: W.Path(_ws34)          # 指回有日志的目录，验证阈值确实影响筛选
    check("34.6b 环境变量可覆盖阈值（同时影响筛选）",
          W._env_num("WBSYNC_MEM_LOG_WARN_KB", 50) == 8
          and len(W._fat_daily_logs(limit_kb=8)) > len(W._fat_daily_logs(limit_kb=65)))
    os.environ["WBSYNC_MEM_LOG_WARN_KB"] = "abc"
    check("34.6c 非法值回退默认（不抛）", W._env_num("WBSYNC_MEM_LOG_WARN_KB", 50) == 50)
    del os.environ["WBSYNC_MEM_LOG_WARN_KB"]
finally:
    for _k, _v in _ot34.items():
        if _v is not None:
            setattr(W, _k, _v)
    W._TRANSCRIPT_CACHE.clear()
    shutil.rmtree(_tmp34, ignore_errors=True)


# ==========================================================================
print("\n=== 35. 多机多对话并发互斥（A2A 原子序号 / 认领租约 / fence / 写后回读）===")
import time as _t35
_ot35 = {k: getattr(W, k, None) for k in ("_a2a_dir", "_a2a_all_journals", "_a2a_read_journal")}
_tmp35 = tempfile.mkdtemp(prefix="wbsync-mutex-")
_os35_a2a = os.path.join(_tmp35, "a2a")
try:
    # 隔离：把 a2a 目录指到临时目录
    W._a2a_dir = lambda: W.Path(_os35_a2a)

    # 35.1 ① 原子序号：同前缀连发，序号严格递增、不重号（回归「同秒撞 -001 覆盖」）
    _ids35 = [W._a2a_next_task_id("A", now="20261016-170000") for _ in range(5)]
    check("35.1 同前缀 5 条 ID 互不重复", len(set(_ids35)) == 5, str(_ids35))
    check("35.1b 序号连续递增（-001..-005）",
          [i.rsplit("-", 1)[-1] for i in _ids35] == ["001", "002", "003", "004", "005"],
          str(_ids35))
    # 重启不重号：扫既有 journal 后应接着编
    _jp35 = W._a2a_journal_path("A")
    _jp35.parent.mkdir(parents=True, exist_ok=True)
    W._a2a_append_event(_jp35, "A", {"event": "task", "id": _ids35[0], "from": "A", "to": "B"})
    check("35.1c 重启后新 ID 接着已用序号（>-005）",
          int(W._a2a_next_task_id("A", now="20261016-170000").rsplit("-", 1)[-1]) > 5)

    # 35.2 ② 认领租约：A 成功；B 未过期被拒；A 本人续期成功
    ok, msg = W._a2a_claim_acquire("t-x-A-001", "A", ttl=600)
    check("35.2 A 首次认领成功", ok is True and "acquired" in msg, msg)
    ok2, msg2 = W._a2a_claim_acquire("t-x-A-001", "B", ttl=600)
    check("35.2b B 抢他人未过期认领被拒", ok2 is False and "已被" in msg2, msg2)
    ok3, msg3 = W._a2a_claim_acquire("t-x-A-001", "A", ttl=600)
    check("35.2c A 本人续期成功（非重抢）", ok3 is True and "renewed" in msg3, msg3)

    # 35.3 ③ fence：他人持有有效认领时，非持有方 write_guarded 被拒；持有方放行
    g, gm = W._a2a_write_guarded("t-x-A-001", "B")
    check("35.3 非持有方写入被 fence 拒（防脑裂）", g is False and "已被" in gm, gm)
    g2, _ = W._a2a_write_guarded("t-x-A-001", "A")
    check("35.3b 持有方写入放行", g2 is True)
    # 无认领的任务：放行（兼容未启用 claim 的历史流程）
    g3, _ = W._a2a_write_guarded("t-no-claim", "B")
    check("35.3c 无认领任务放行（向后兼容）", g3 is True)

    # 35.4 过期可接管：把 A 的 claim 改成已过期 → B 可接管且 fence+1
    _cp35 = W._a2a_claim_path("t-x-A-001")
    _c35 = json.loads(_cp35.read_text(encoding="utf-8"))
    _c35["expires_at"] = _t35.time() - 10
    _cp35.write_text(json.dumps(_c35, ensure_ascii=False), encoding="utf-8")
    ok4, msg4 = W._a2a_claim_acquire("t-x-A-001", "B", ttl=600)
    check("35.4 过期后 B 可接管", ok4 is True and "acquired" in msg4, msg4)
    _c35b = json.loads(_cp35.read_text(encoding="utf-8"))
    check("35.4b 接管后 fence 递增（防旧持有者复活写）", _c35b["fence"] > _c35.get("fence", 0),
          "old=%s new=%s" % (_c35.get("fence"), _c35b["fence"]))
    check("35.4c 接管后 owner=B", _c35b["owner"] == "B")

    # 35.5 ⑥ 写后回读：正常追加返回 True；事件 ID 落盘
    _p35 = W._a2a_journal_path("C")
    r = W._a2a_append_event(_p35, "C", {"event": "task", "id": "t-wc-C-900", "from": "C", "to": "A"})
    check("35.5 写后回读校验通过（事件落盘）", r is True)
    _ev35 = W._a2a_read_journal(_p35).get("events", [])
    check("35.5b 事件确实在文件里", any(e.get("id") == "t-wc-C-900" for e in _ev35))

    # 35.6 release-claim：本人可释；释他人被拒（无 force）；过期 claim 判非活跃
    ok5, _ = W._a2a_claim_release("t-x-A-001", owner="B")
    check("35.6 持有方 B 释放认领成功", ok5 is True)
    W._a2a_claim_acquire("t-y-A-002", "A", ttl=600)
    ok6, _ = W._a2a_claim_release("t-y-A-002", owner="B")
    check("35.6b 非持有方释放被拒", ok6 is False)
    _act, _ = W._a2a_claim_status("t-y-A-002")
    check("35.6c 有效认领判 active", _act is True)

    # 35.7 journal glob 不误收 .seq / claims 子目录
    check("35.7 a2a 目录含 .seq/claims 但 journal 只收 tasks-*.json",
          all(p.name.startswith("tasks-") for p in W._a2a_all_journals()))
finally:
    for _k, _v in _ot35.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp35, ignore_errors=True)


# ==========================================================================
print("\n=== 36. #5 sessions 本机开/关判定（复用 dialogue-registry + mtime 兜底）===")
import time as _t36
_tmp36 = tempfile.mkdtemp(prefix="wbsync-sess-")
_ot36 = {k: getattr(W, k, None)
         for k in ("_dialogue_registry_read", "_ws_projects_dir", "_my_session_id", "out", "info", "head")}
try:
    _now36 = _t36.time()
    _rmap36 = {
        "aaaaaaaa-0000": {"dialogue_id": "aaaaaaaa-0000", "state": "open", "last_seen_ts": _now36 - 60},
        "bbbbbbbb-0000": {"dialogue_id": "bbbbbbbb-0000", "state": "open", "last_seen_ts": _now36 - 12 * 60},
        "cccccccc-0000": {"dialogue_id": "cccccccc-0000", "state": "open", "last_seen_ts": _now36 - 90 * 60},
        "dddddddd-0000": {"dialogue_id": "dddddddd-0000", "state": "closed", "last_seen_ts": _now36 - 60},
    }
    # 36.1 三级判定（registry 主信号）
    check("36.1 registry 活跃(<5min) → active",
          W._session_local_status("aaaaaaaa-0000", _now36, _rmap36)[0] == "active")
    check("36.1b registry 空闲(5–30min) → idle",
          W._session_local_status("bbbbbbbb-0000", _now36, _rmap36)[0] == "idle")
    check("36.1c registry 久无活动(>30min 仍 open) → closed（SessionEnd 兜底）",
          W._session_local_status("cccccccc-0000", _now36, _rmap36)[0] == "closed")
    check("36.1d state=closed → closed",
          W._session_local_status("dddddddd-0000", _now36, _rmap36)[0] == "closed")
    # 36.2 无 registry 记录 → jsonl mtime 兜底
    check("36.2 无记录 + mtime 新(≤30min) → idle",
          W._session_local_status("norec-1", _now36 - 10 * 60, _rmap36)[0] == "idle")
    check("36.2b 无记录 + mtime 旧(>30min) → closed",
          W._session_local_status("norec-2", _now36 - 5 * 3600, _rmap36)[0] == "closed")

    # 36.3 cmd_sessions --json 携带 local_status + open/closed 计数，且开着的排前
    _wd36 = W.Path(_tmp36)
    for _n in ("aaaaaaaa-0000", "cccccccc-0000", "norecold-x"):
        (_wd36 / (_n + ".jsonl")).write_text("", encoding="utf-8")
    _old36 = _now36 - 6 * 3600
    os.utime(str(_wd36 / "norecold-x.jsonl"), (_old36, _old36))
    W._ws_projects_dir = lambda ws=None: _wd36
    W._dialogue_registry_read = lambda prune=True: list(_rmap36.values())
    W._my_session_id = lambda: "aaaaaaaa-0000"
    _cap36 = []
    W.out = lambda *a, **k: _cap36.append(" ".join(str(x) for x in a))
    W.info = lambda *a, **k: _cap36.append(" ".join(str(x) for x in a))
    W.head = lambda *a, **k: None

    class _A36:
        ws = None
        json = True
        limit = 10

    W.cmd_sessions(_A36())
    _j36 = json.loads(_cap36[-1])
    check("36.3 json 报告 open_count / closed_count",
          _j36.get("open_count") == 1 and _j36.get("closed_count") == 2,
          "open=%s closed=%s" % (_j36.get("open_count"), _j36.get("closed_count")))
    _by36 = {s["short"]: s["local_status"] for s in _j36["sessions"]}
    check("36.3b 每条会话带 local_status", _by36.get("aaaaaaaa") == "active" and _by36.get("cccccccc") == "closed",
          str(_by36))
    check("36.3c 开着的排在已关闭之前", _j36["sessions"][0]["local_status"] != "closed",
          _j36["sessions"][0]["local_status"])
finally:
    for _k, _v in _ot36.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp36, ignore_errors=True)


# ==========================================================================
print("\n=== 37. 组外留言 tell（纪律 §12）+ [外部·] 标头 ===")
_tmp37 = tempfile.mkdtemp(prefix="wbsync-tell-")
_ot37 = {k: getattr(W, k, None)
         for k in ("_my_comms_file", "_my_suffix_short", "out", "info", "head", "ok", "bad")}
try:
    _cf37 = W.Path(_tmp37) / "机器间沟通-A.md"
    W._my_comms_file = lambda: _cf37
    W._my_suffix_short = lambda: "A"
    _cap37 = []
    W.out = lambda *a, **k: _cap37.append(" ".join(str(x) for x in a))
    W.info = lambda *a, **k: _cap37.append(" ".join(str(x) for x in a))
    W.head = lambda *a, **k: _cap37.append(" ".join(str(x) for x in a))
    W.ok = lambda *a, **k: _cap37.append(" ".join(str(x) for x in a))
    W.bad = lambda *a, **k: _cap37.append(" ".join(str(x) for x in a))

    class _A37:
        text = "测试一句话"
        from_window = "求职库"
        body = ""

    check("37.1 tell 返回 0", W.cmd_tell(_A37()) == 0)
    _txt37 = _cf37.read_text(encoding="utf-8") if _cf37.exists() else ""
    check("37.1b 标题含 [外部·求职库] 前缀", "[外部·求职库]" in _txt37, _txt37[:120])
    check("37.1c 自动带 [A→全员] 方向头", "[A→全员]" in _txt37, _txt37[:120])

    class _A37b:
        text = ""
        from_window = ""
        body = ""

    _died37 = False
    try:
        W.cmd_tell(_A37b())
    except SystemExit:
        _died37 = True
    check("37.2 空摘要 → 报错退出（不写盘）", _died37)
finally:
    for _k, _v in _ot37.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp37, ignore_errors=True)


# ==========================================================================
print("\n=== 38. A2A 任务带「涉及文件」（fold 透传 + 跨机可比归一化）===")
_f38 = W._a2a_fold([
    {"event": "task", "id": "T38", "from": "A", "to": "B", "skill": "review",
     "files": ["coop/shared/x.md", "~/skills/y.py"], "text": "看看"},
])
check("38.1 fold 透传 files", _f38[0].get("files") == ["coop/shared/x.md", "~/skills/y.py"],
      str(_f38[0].get("files")))
check("38.1b 无 files 默认空列表",
      W._a2a_fold([{"event": "task", "id": "T39", "from": "A"}])[0].get("files") == [])
try:
    _k38 = W._reservation_key(str(W.Path(W.cfg_sync_root()) / "coop" / "shared" / "x.md"))
    check("38.2 同步根内归一为相对路径", _k38 == "coop/shared/x.md", _k38)
except Exception as _e38:
    check("38.2 同步根内归一为相对路径", False, str(_e38))


# ==========================================================================
print("\n=== 39. 安全加固 secure（.stignore 状态 / 演练 / 密钥扫描）===")


class _A39:
    action = "status"
    apply = False


_s39 = _capture(lambda: W.cmd_secure(_A39()))
check("39.1 secure status 输出含「同步夹」", "同步夹" in _s39, _s39[:80])
check("39.2 各夹给出加固结论",
      ("就绪" in _s39) or ("无 .stignore" in _s39) or ("缺 wbsync-secure 规则" in _s39), _s39[:120])
# 2026-10-18 用户拍板「全量镜像」：.stignore 只排系统垃圾，密钥/大件/冲突规则全部撤销
# →「有 .stignore 但缺 wbsync-secure 规则」是**预期状态**，不是故障；别再把它当失败。


class _A39on:
    action = "on"
    apply = False


_s39b = _capture(lambda: W.cmd_secure(_A39on()))
check("39.3 secure on 演练不写盘（提示演练/跳过）",
      ("演练" in _s39b) or ("跳过" in _s39b), _s39b[:80])


class _A39scan:
    action = "scan"
    apply = False


_s39c = _capture(lambda: W.cmd_secure(_A39scan()))
check("39.4 secure scan 给出结论", ("未发现" in _s39c) or ("发现" in _s39c), _s39c[:80])


# ==========================================================================
print("\n=== 40. #10 超时提醒（只算不写）===")
check("40.1 A2A 时间戳解析", W._parse_a2a_ts("2026-10-17T10:00:00+0800") > 0)
check("40.2 非法时间戳返回 0", W._parse_a2a_ts("bad") == 0)
check("40.3 超时阈值统一 6 小时（360 分钟，不再按 skill 分级）",
      W.OVERDUE_DEFAULT_MIN == 360 and not W.OVERDUE_MIN)


# ==========================================================================
print("\n=== 41. #13 A2A 打回返工（rejected / rework 状态机 + 认领/围栏联动）===")
_tmp41 = tempfile.mkdtemp(prefix="wbsync-rework-")
_ot41 = {k: getattr(W, k, None)
         for k in ("_a2a_dir", "_my_suffix_short", "_board_touch", "out", "info", "ok", "warn", "bad", "head")}
try:
    W._a2a_dir = lambda: W.Path(os.path.join(_tmp41, "a2a"))
    W._board_touch = lambda *a, **k: False   # #15：本节不测看板，别把测试数据写进真实 board/
    W._a2a_append_event(W._a2a_journal_path("A"), "A",
                        {"event": "task", "id": "T41", "from": "A", "to": "B", "skill": "review",
                         "state": "submitted", "at": "2026-10-17T10:00:00+0800", "text": "跑一遍"})

    def _task41():
        return [t for t in W._a2a_all_tasks() if t["id"] == "T41"][0]

    def _upd41(who, state, reason=None, text=""):
        W._my_suffix_short = lambda: who

        class _A41:
            id = "T41"
            text = ""
            artifact = None
            json = False
            action = "update"
        _A41.state = state
        _A41.reason = reason
        _A41.text = text
        return _capture(lambda: W.cmd_a2a(_A41()))

    # 41.1 12 条合法转换（含兼容边 submitted→completed）
    _legal41 = [
        ("submitted", "working", "B"), ("submitted", "completed", "B"), ("submitted", "canceled", "A"),
        ("working", "completed", "B"), ("working", "rejected", "A"), ("working", "canceled", "A"),
        ("completed", "rejected", "A"),
        ("rejected", "rework", "B"), ("rejected", "canceled", "B"),
        ("rework", "completed", "B"), ("rework", "rejected", "A"), ("rework", "canceled", "A"),
    ]
    for _f, _to, _who in _legal41:
        _ok41, _msg41 = W._a2a_transition_check(
            {"id": "X", "from": "A", "to": "B", "state": _f, "attempt": 1}, _to, _who)
        check("41.1 %s→%s（%s）合法" % (_f, _to, _who), _ok41, _msg41)

    # 41.2 非法转换被拒（含角色不符 / 真终态不可复活）
    _illegal41 = [
        ("working", "submitted", "B", "回退"),
        ("submitted", "rejected", "A", "还没做就打回"),
        ("submitted", "rework", "B", "还没打回就返工"),
        ("rejected", "completed", "B", "必须经 rework 才能完成"),
        ("canceled", "working", "B", "真终态不可复活"),
        ("failed", "working", "B", "failed 不可复活"),
        ("completed", "rejected", "B", "打回只能派活方发起"),
        ("working", "completed", "A", "完成只能接活方发起"),
        ("rejected", "rework", "A", "返工由挨打回方发起"),
    ]
    for _f, _to, _who, _why in _illegal41:
        _ok41b, _msg41b = W._a2a_transition_check(
            {"id": "X", "from": "A", "to": "B", "state": _f, "attempt": 1}, _to, _who)
        check("41.2 %s→%s（%s）被拒：%s" % (_f, _to, _who, _why), _ok41b is False, _msg41b)

    # 41.3 打回必须带理由；超 100 字截断
    _c41 = _upd41("A", "rejected")
    check("41.3 rejected 无 --reason 被拒", _task41()["state"] == "submitted", _c41[:60])
    _c41b = _upd41("B", "completed", text="做完了")
    check("41.3b 接活方 submitted→completed（兼容边）成功", _task41()["state"] == "completed", _c41b[:60])
    _long41 = "牙" * 130
    _c41c = _upd41("A", "rejected", reason=_long41)
    _t41c = _task41()
    check("41.3c completed→rejected（完成可被打回）成功", _t41c["state"] == "rejected", _c41c[:60])
    check("41.3d 理由超 100 字被截断", len(_t41c.get("reason") or "") == W.A2A_REASON_MAX,
          str(len(_t41c.get("reason") or "")))

    # 41.4 打回即交回认领（fence bump、owner 清空、不再有效）
    _cl41 = json.loads(W._a2a_claim_path("T41").read_text(encoding="utf-8")) if W._a2a_claim_path("T41").exists() else {}
    _act41, _ = W._a2a_claim_status("T41")
    check("41.4 打回后原认领失效", _act41 is False, str(_cl41.get("expires_at")))
    check("41.4b 打回后 owner 清空 + fence>0",
          (_cl41.get("owner") == "" and int(_cl41.get("fence") or 0) > 0), str(_cl41))
    _g41, _ = W._a2a_write_guarded("T41", "B")
    check("41.4c 打回后原执行方可再写（无有效认领）", _g41 is True)

    # 41.5 rework：接受打回 → attempt+1 + 重新认领（fence 继续递增）
    _fence_before41 = int(_cl41.get("fence") or 0)
    _c41d = _upd41("B", "rework", text="按理由重做")
    _t41d = _task41()
    _cl41b = json.loads(W._a2a_claim_path("T41").read_text(encoding="utf-8"))
    check("41.5 rework 成功", _t41d["state"] == "rework", _c41d[:60])
    check("41.5b attempt 递增到 2", int(_t41d.get("attempt") or 1) == 2, str(_t41d.get("attempt")))
    check("41.5c rework 自动重新认领（owner=B）", _cl41b.get("owner") == "B", str(_cl41b.get("owner")))
    check("41.5d fence 继续单调递增", int(_cl41b.get("fence") or 0) > _fence_before41,
          "before=%s after=%s" % (_fence_before41, _cl41b.get("fence")))
    _g41b, _gm41b = W._a2a_write_guarded("T41", "A")
    check("41.5e rework 后派活方写入被围栏拦住（B 持有）", _g41b is False, _gm41b)
    # 派活方仍可打回（穿透认领），这是修「做完但 claim 没释放就打不回」的关键
    _c41e = _upd41("A", "rejected", reason="第 3 项没覆盖")
    check("41.5f 派活方可穿透认领打回", _task41()["state"] == "rejected", _c41e[:60])

    # 41.6 返工上限：第 3 轮后再打回 → 只能 canceled
    _upd41("B", "rework")                 # attempt 2 → 3
    _upd41("A", "rejected", reason="还不行")
    _before41 = _task41()
    _c41f = _upd41("B", "rework")
    _after41 = _task41()
    check("41.6 达上限（attempt=%s）后 rework 被拒" % _before41.get("attempt"),
          _after41["state"] == "rejected" and "返工上限" in _c41f, _c41f[:80])
    _c41g = _upd41("B", "canceled")
    check("41.6b 达上限后仍可 canceled（唯一出口）", _task41()["state"] == "canceled", _c41g[:60])
    _c41h = _upd41("B", "rework")
    check("41.6c canceled 后不可复活", _task41()["state"] == "canceled", _c41h[:60])

    # 41.7 统计可区分「首次通过 / 返工后通过」
    _fold41 = W._a2a_fold([
        {"event": "task", "id": "F1", "from": "A", "to": "B", "skill": "x", "at": "1"},
        {"event": "status", "id": "F1", "state": "completed", "at": "2"},
        {"event": "task", "id": "F2", "from": "A", "to": "B", "skill": "x", "at": "1"},
        {"event": "status", "id": "F2", "state": "rejected", "reason": "不行", "at": "2"},
        {"event": "status", "id": "F2", "state": "rework", "at": "3"},
        {"event": "status", "id": "F2", "state": "completed", "at": "4"},
    ])
    _d41 = {t["id"]: t for t in _fold41}
    check("41.7 首次通过 attempt=1", int(_d41["F1"].get("attempt") or 1) == 1)
    check("41.7b 返工后通过 attempt=2（统计可区分）", int(_d41["F2"].get("attempt") or 1) == 2)
    check("41.7c 打回理由随任务保留", _d41["F2"].get("reason") == "不行")

    # 41.8 #10 超时阈值：rejected 固定 60m（不被 skill 的 10m 带偏）
    _old41 = W._a2a_all_tasks
    W._a2a_all_tasks = lambda: [{"id": "OD1", "state": "rejected", "skill": "notify",
                                 "updatedAt": "2026-10-17T10:00:00+0800", "attempt": 1}]
    try:
        _od41 = W._a2a_overdue(now=W._parse_a2a_ts("2026-10-17T11:30:00+0800"))
        check("41.8 未到 6 小时不计超时（阈值统一 360 分钟）", len(_od41) == 0, str(_od41))
        _od41b = W._a2a_overdue(now=W._parse_a2a_ts("2026-10-17T16:30:00+0800"))
        check("41.8b 超过 6 小时计超时（阈值 360）",
              len(_od41b) == 1 and _od41b[0][2] == 360, str(_od41b))
    finally:
        W._a2a_all_tasks = _old41
finally:
    for _k, _v in _ot41.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp41, ignore_errors=True)


# ==========================================================================
print("\n=== 42. #15 机群看板（每机单写者 + 事件驱动 + 三视图）===")
import time as _t42
_tmp42 = tempfile.mkdtemp(prefix="wbsync-board-")
_ot42 = {k: getattr(W, k, None)
         for k in ("coop_shared", "_a2a_all_tasks", "_a2a_overdue", "_dialogue_registry_read",
                   "_ws_my_generation", "_my_suffix_short", "_read_ws_registry",
                   "out", "info", "ok", "warn", "bad", "head")}
try:
    _root42 = W.Path(_tmp42) / "shared"
    _root42.mkdir(parents=True, exist_ok=True)
    W.coop_shared = lambda: _root42
    W._read_ws_registry = lambda: {"machines": {"A": {}, "B": {}, "C": {}}}
    W._dialogue_registry_read = lambda prune=True: [
        {"machine": "A", "dialogue_id": "abcdef12-3456-7890", "topic": "看板实现",
         "state": "open", "last_seen_ts": _t42.time()}]
    W._ws_my_generation = lambda my=None: 2
    W._a2a_all_tasks = lambda: [
        {"id": "t1", "from": "A", "to": "B", "state": "submitted", "skill": "review"},
        {"id": "t2", "from": "B", "to": "A", "state": "working", "skill": "fix"},
        {"id": "t3", "from": "A", "to": "C", "state": "completed", "skill": "notify"},
        {"id": "t4", "from": "A", "to": "C", "state": "rejected", "skill": "review"},
    ]
    W._a2a_overdue = lambda now=None: [(None, 90, 60)]
    W._my_suffix_short = lambda: "A"

    def _ago42(mins):
        _t = _t42.time() - mins * 60
        _lt = _t42.localtime(_t)
        return _t42.strftime("%Y-%m-%dT%H:%M:%S", _lt) + ".000" + _t42.strftime("%z", _lt)

    # 42.1 快照：schema / 世代 / 任务计数（终态不计；rejected 仍算待办）/ doing
    _s42 = W._board_snapshot("A", state="online")
    check("42.1 快照 schema 正确", _s42.get("schema") == "wbsync-board/1")
    check("42.1b 世代取自登记层", _s42.get("gen") == 2)
    check("42.1c 待我=1（t2；t3 completed 不计）", _s42["tasks"]["mine"] == 1, str(_s42["tasks"]))
    check("42.1d 待对方=2（t1 + t4 rejected，仍算待对方重做）",
          _s42["tasks"]["theirs"] == 2, str(_s42["tasks"]))
    check("42.1e 超时数来自 #10 只算不写", _s42["tasks"]["overdue"] == 1)
    check("42.1f doing 取最近活跃对话主题", _s42["doing"] == "看板实现" and _s42["dialogue"] == "abcdef12",
          "%s/%s" % (_s42["doing"], _s42["dialogue"]))

    # 42.2 事件驱动写：首次写；内容没变 → 不再写（防 churn）
    _w42a = W._board_touch("test", state="online", force=True)
    _p42 = W._board_path("A")
    check("42.2 首次落盘 board/A.json", _w42a is True and _p42.exists())
    _bytes42 = _p42.read_bytes()
    _w42b = W._board_touch("test-again", state="online")
    check("42.2b 内容未变 → 不重复写（防 Syncthing churn）",
          _w42b is False and _p42.read_bytes() == _bytes42)
    _w42c = W._board_touch("state-change", state="offline")
    check("42.2c 状态真的变了 → 立即写", _w42c is True and
          json.loads(_p42.read_text(encoding="utf-8"))["state"] == "offline")

    # 42.3 判活：≤1h 在线 / 1h–2h 可能过时 / >2h 疑似离线 / 自述离线优先
    _live42 = W._board_live({"state": "online", "updated_at": _ago42(5)})[0]
    check("42.3 5 分钟 → 在线", "在线" in _live42 and "🟢" in _live42, _live42)
    _live42b = W._board_live({"state": "online", "updated_at": _ago42(45)})[0]
    check("42.3b 45 分钟 → 仍算在线（用户：>1h 才算过时、才变灰）", "🟢" in _live42b, _live42b)
    _live42b2 = W._board_live({"state": "online", "updated_at": _ago42(75)})[0]
    check("42.3b2 75 分钟 → 可能已过时", "🟡" in _live42b2, _live42b2)
    _live42c = W._board_live({"state": "online", "updated_at": _ago42(200)})[0]
    check("42.3c 200 分钟 → 疑似离线（下线那条消息往往发不出来）", "⚪" in _live42c, _live42c)
    _live42d = W._board_live({"state": "offline", "updated_at": _ago42(1)})[0]
    check("42.3d 自述 offline 优先于时间新鲜度", "离线" in _live42d, _live42d)
    check("42.3e 阈值可读（60/120）", (W.BOARD_STALE_MIN, W.BOARD_OFFLINE_MIN) == (60, 120))

    # 42.4 多机合并读：各机一份文件、按机名排序、坏格式忽略
    (_root42 / "board").mkdir(parents=True, exist_ok=True)
    (_root42 / "board" / "C.json").write_text(json.dumps({
        "schema": "wbsync-board/1", "machine": "C", "gen": 1, "state": "online",
        "updated_at": _ago42(10), "doing": "跑测试", "dialogue": "",
        "tasks": {"mine": 0, "theirs": 0, "overdue": 0}, "alerts": ["同步 1 夹 e=2"]},
        ensure_ascii=False), encoding="utf-8")
    (_root42 / "board" / "junk.json").write_text("{\"schema\":\"other/9\"}", encoding="utf-8")
    _rows42 = W._board_read_all()
    check("42.4 合并 A/C 两份、忽略非本 schema", [r["machine"] for r in _rows42] == ["A", "C"],
          str([r.get("machine") for r in _rows42]))

    # 42.5 三视图同源：md / html 都带「数据截至」且含各机
    _md42 = W._board_md(_rows42)
    check("42.5 md 顶部自曝「数据截至」", "数据截至" in _md42)
    check("42.5b md 含 A/C 两机与告警", ("## A 机" in _md42) and ("## C 机" in _md42)
          and ("e=2" in _md42))
    _html42 = W._board_html(_rows42)
    check("42.5c html 自含样式与脚本（无外链）",
          "<style>" in _html42 and "<script>" in _html42 and "http" not in _html42)
    check("42.5d html 带 >60min 变灰逻辑", "classList.add('stale')" in _html42 and "数据截至" in _html42)
    check("42.5e 视图数字与数据层一致（同一份数据）",
          ("待我 0" in _html42 and "待对方 0" in _html42
           and "待我 0" in _md42 and "待对方 0" in _md42))

    # 42.6 共享视图单写者：非 A 机不渲染；--force 才越过
    W._my_suffix_short = lambda: "B"
    _r42a = W._board_render(quiet=True)
    check("42.6 没有新数据时谁都不渲染（v4.23 起靠租约，不再限定 A）", _r42a is False)
    _r42b = W._board_render(force=True, quiet=True)
    check("42.6b --force 可越过（应急）", _r42b is True)
    W._my_suffix_short = lambda: "A"
    _r42c = W._board_render(quiet=True)
    check("42.6c A 机渲染幂等（内容没变不重写）", _r42c is False)

    # 42.7 `wbsync board` 纯读：文件字节不变（方案验收第 7 条）
    _mb42 = {p: p.read_bytes() for p in [_p42, _root42 / "board" / "C.json",
                                         W._board_view_md(), W._board_view_html()]}

    class _A42:
        action = "show"
        json = False
        force = False
    _cap42 = []
    W.out = lambda *a, **k: _cap42.append(" ".join(str(x) for x in a))
    W.info = lambda *a, **k: _cap42.append(" ".join(str(x) for x in a))
    W.head = lambda *a, **k: None
    W.cmd_board(_A42())
    _txt42 = "\n".join(_cap42)
    check("42.7 board 输出含各机与「数据截至」", "数据截至" in _txt42 and "A 机" in _txt42, _txt42[:80])
    check("42.7b board 纯读（board/*.json 与共享视图字节不变）",
          all(p.read_bytes() == b for p, b in _mb42.items()))
finally:
    for _k, _v in _ot42.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp42, ignore_errors=True)


# ==========================================================================
print("\n=== 43. 看板主维护机（序号最小的在册机）+ 开 skill 即建立 ===")
_tmp43 = tempfile.mkdtemp(prefix="wbsync-board-owner-")
_ot43 = {k: getattr(W, k, None)
         for k in ("coop_shared", "_read_ws_registry", "_my_suffix_short",
                   "out", "info", "ok", "warn", "bad", "head")}
try:
    _root43 = W.Path(_tmp43) / "shared"
    (_root43 / "board").mkdir(parents=True, exist_ok=True)
    W.coop_shared = lambda: _root43
    W.info = lambda *a, **k: None
    W.ok = lambda *a, **k: None
    W.warn = lambda *a, **k: None

    # 43.1 主维护机 = 登记表里序号最小的在册机（不硬编码 "A"）
    W._read_ws_registry = lambda: {"machines": {"C": {}, "A": {}, "B": {}}}
    check("43.1 主维护机取序号最小（乱序登记也是 A）", W._board_primary() == "A",
          W._board_primary())
    check("43.1b A 在册时 A 是主维护机", W._board_is_primary("A") is True)
    check("43.1c B 不是主维护机", W._board_is_primary("B") is False)

    # 43.2 A 退役/不在册 → 自动顺延到 B（不会出现没人维护的死看板）
    W._read_ws_registry = lambda: {"machines": {"C": {}, "B": {}}}
    check("43.2 A 不在册 → B 接手", W._board_primary() == "B")
    W._read_ws_registry = lambda: {"machines": {"C": {}, "D": {}}}
    check("43.2b 只剩 C/D → C 接手（仍取最小）", W._board_primary() == "C")

    # 43.3 登记表读不到 → 兜底 A（唯一值，避免多机都不渲染）
    W._read_ws_registry = lambda: {"machines": {}}
    check("43.3 登记表空 → 兜底 A", W._board_primary() == "A")

    def _boom():
        raise RuntimeError("registry broken")
    W._read_ws_registry = _boom
    check("43.3b 登记表异常不炸（兜底 A）", W._board_primary() == "A")

    # 43.4 「一开 skill 就建立」：视图缺失时主维护机补齐，内容没变也不报错
    _s43 = {"schema": "wbsync-board/1", "machine": "A", "gen": 1, "state": "online",
            "updated_at": "2026-10-17T12:00:00.000+0800", "doing": "", "dialogue": "",
            "tasks": {"mine": 0, "theirs": 0, "overdue": 0}, "alerts": []}
    W._board_path("A").write_text(json.dumps(_s43, ensure_ascii=False), encoding="utf-8")
    check("43.4 初始无共享视图", not W._board_view_md().exists()
          and not W._board_view_html().exists())
    W._read_ws_registry = lambda: {"machines": {"A": {}, "B": {}}}
    W._my_suffix_short = lambda: "A"
    check("43.4b 主维护机 ensure → 建立 md+html", W._board_ensure_views() is True
          and W._board_view_md().exists() and W._board_view_html().exists())
    check("43.4c 已存在 → 幂等（不重写）", W._board_ensure_views() is False)

    # 43.5 非主维护机不建（只有主维护机写，物理上无互覆）
    _tmp43b = tempfile.mkdtemp(prefix="wbsync-board-owner-b-")
    _root43b = W.Path(_tmp43b) / "shared"
    (_root43b / "board").mkdir(parents=True, exist_ok=True)
    W.coop_shared = lambda: _root43b
    W._my_suffix_short = lambda: "B"
    check("43.5 非主维护机 ensure 不写", W._board_ensure_views() is False
          and not W._board_view_md().exists())
    W._my_suffix_short = lambda: "A"
    check("43.5b 主维护机在新目录重建", W._board_ensure_views() is True
          and W._board_view_md().exists())
    shutil.rmtree(_tmp43b, ignore_errors=True)

    # 43.6 挂点存在：wake 里既落快照又保证视图（开 skill 就会建看板）
    _src43 = (W.Path(W.__file__).read_text(encoding="utf-8", errors="replace")
              if hasattr(W, "__file__") else "")
    check("43.6 wake 挂点包含 _board_touch + _board_ensure_views",
          bool(_src43) and '_board_touch("wake"' in _src43
          and "_board_ensure_views()" in _src43)
finally:
    for _k, _v in _ot43.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp43, ignore_errors=True)


# ==========================================================================
print("\n=== 44. A2A 派活指针提示（纪律 §14：交代细节却没写文件 → 提醒，不阻断）===")
_tmp44 = tempfile.mkdtemp(prefix="wbsync-a2a-ptr-")
_ot44 = {k: getattr(W, k, None)
         for k in ("_a2a_dir", "_a2a_ensure_readme", "_a2a_next_task_id", "_my_suffix_short",
                   "_board_touch", "_a2a_identity", "info", "warn", "ok", "bad", "head", "out")}
try:
    W._a2a_dir = lambda: W.Path(_tmp44) / "a2a"
    W._a2a_ensure_readme = lambda: None
    W._a2a_next_task_id = lambda my=None, now=None: "t-ptr-1"
    W._a2a_identity = lambda my=None: {}
    W._my_suffix_short = lambda: "A"
    W._board_touch = lambda *a, **k: None
    _cap44 = []
    W.info = lambda *a, **k: _cap44.append(" ".join(str(x) for x in a))
    W.warn = lambda *a, **k: _cap44.append(" ".join(str(x) for x in a))
    W.ok = lambda *a, **k: None
    W.bad = lambda *a, **k: None
    W.head = lambda *a, **k: None
    W.out = lambda *a, **k: None

    class _A44:
        action = "send"
        to = "B"
        skill = "review"
        params = ""
        file = None
        text = "把这套并发互斥的机制按我们之前聊的那样改一版，注意认领租约和 fence 都要对上"

    W.cmd_a2a(_A44())
    check("44.1 长正文没写文件指针 → 提示纪律 §14", any("§14" in x for x in _cap44),
          str(_cap44)[:120])
    check("44.1b 只是提示、不阻断（任务照常落盘）",
          (W._a2a_dir() / "tasks-A.json").exists())

    _cap44.clear()

    class _A44b(_A44):
        text = "先读 coop/shared/方案-机群状态看板-20261017.md 的 §3 → 按之实施"

    W.cmd_a2a(_A44b())
    check("44.2 已写精确路径 → 不再提示", not any("§14" in x for x in _cap44),
          str(_cap44)[:120])

    _cap44.clear()

    class _A44c(_A44):
        text = "跑一下自检"

    W.cmd_a2a(_A44c())
    check("44.3 短正文（无需指针）→ 不提示，避免噪声",
          not any("§14" in x for x in _cap44), str(_cap44)[:120])
finally:
    for _k, _v in _ot44.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp44, ignore_errors=True)


# ==========================================================================
print("\n=== 45. P1 授权闸门 + STOP 刹车（高危动作需人签票 / 一键急停）===")
_tmp45 = tempfile.mkdtemp(prefix="wbsync-authz-")
_ot45 = {k: getattr(W, k, None)
         for k in ("coop_shared", "_a2a_dir", "_a2a_ensure_readme", "_a2a_next_task_id",
                   "_my_suffix_short", "_board_touch", "_a2a_identity",
                   "info", "warn", "ok", "bad", "head", "out")}
try:
    _root45 = W.Path(_tmp45) / "shared"
    _root45.mkdir(parents=True, exist_ok=True)
    W.coop_shared = lambda: _root45
    W._a2a_dir = lambda: W.Path(_tmp45) / "a2a"
    W._a2a_ensure_readme = lambda: None
    _seq45 = iter(range(1, 500))
    W._a2a_next_task_id = lambda my=None, now=None: "t-45-%03d" % next(_seq45)
    W._my_suffix_short = lambda: "A"
    W._a2a_identity = lambda my=None: {}
    W._board_touch = lambda *a, **k: None
    _cap45 = []
    W.info = lambda *a, **k: _cap45.append(" ".join(str(x) for x in a))
    W.warn = lambda *a, **k: _cap45.append(" ".join(str(x) for x in a))
    W.ok = lambda *a, **k: _cap45.append(" ".join(str(x) for x in a))
    W.bad = lambda *a, **k: _cap45.append(" ".join(str(x) for x in a))
    W.head = lambda *a, **k: None
    W.out = lambda *a, **k: _cap45.append(" ".join(str(x) for x in a))

    # 45.1 默认：无刹车、无票
    check("45.1 默认无刹车（正常态）", W._stop_active() is False)
    _ok, _msg = W._authz_check("prune", "X")
    check("45.2 高危动作没票 → 拒（--apply 不再是唯一门槛）",
          not _ok and "没有放行票" in _msg, _msg)

    # 45.3 人签票 → 通过；一次性（执行即消费）
    _p, _rec = W._authz_issue("prune", "X")
    _ok, _msg = W._authz_check("prune", "X")
    check("45.3 人签放行票 → 校验通过", _ok, _msg)
    _ok2, _msg2 = W._authz_check("prune", "X")
    check("45.4 一次性：用过即失效（文件被删除，防并发双用）",
          not _ok2 and "没有放行票" in _msg2, _msg2)
    check("45.4b 票里记了签发人/动作/对象/TTL",
          _rec.get("action") == "prune" and _rec.get("peer") == "X"
          and _rec.get("ttl") == W.AUTHZ_TTL_DEFAULT and _rec.get("issuedBy") == "A",
          str(_rec)[:120])

    # 45.5 过期票 → 拒（TTL 0 即过期；0 是合法值，不能被 `or` 吞成默认 300）
    W._authz_issue("uninstall", "A", ttl=0)
    _ok, _msg = W._authz_check("uninstall", "A")
    check("45.5 过期票 → 拒，并顺手删掉", not _ok and "过期" in _msg, _msg)

    # 45.6 指纹归一：动作小写 + 对象大写 + 去空格
    check("45.6 指纹归一（'Prune',' x ' ≡ 'prune','X'）",
          W._authz_key("Prune", " x ") == W._authz_key("prune", "X"))

    # 45.7 不限定对象的票：对任意对象都算数（仍是人签的）
    W._authz_issue("forget", "")
    _ok, _msg = W._authz_check("forget", "Z")
    check("45.7 通配票（未限对象）可用于任意对象", _ok, _msg)

    # 45.8 STOP 生效：有票也拒；写入口直接退出；只读（check）照常返回
    W._authz_issue("prune", "Y")
    W._stop_path().parent.mkdir(parents=True, exist_ok=True)
    W._stop_path().write_text("STOP test\n", encoding="utf-8")
    check("45.8 STOP 文件存在 → 刹车生效", W._stop_active() is True)
    _ok, _msg = W._authz_check("prune", "Y")
    check("45.8b STOP 优先于票：有票也拒", not _ok and "STOP" in _msg, _msg)
    _died45 = False
    try:
        W._stop_guard("prune")
    except SystemExit:
        _died45 = True
    check("45.8c 写操作入口被 STOP 拦下（die → SystemExit）", _died45)

    # 45.9 解除刹车
    W._stop_path().unlink()
    check("45.9 删除 STOP → 恢复正常", W._stop_active() is False)

    # 45.10 authorize 默认演练不落票（防 agent 代签）
    _cap45.clear()
    class _A45auth:
        action = "prune"
        peer = "Z"
        ttl = 300
        apply = False
    W.cmd_authorize(_A45auth())
    check("45.10 `authorize` 默认只演练，不落票（agent 不该代签）",
          not W._authz_ticket_path("prune", "Z").exists())

    # 45.11 A2A 任务风险分级：priority / requires 落到事件并被折叠读回
    class _A45send:
        action = "send"
        to = "B"
        skill = "deploy"
        params = ""
        file = None
        text = "先读 coop/shared/方案-X.md → 按之清理旧产物"
        priority = "P0"
        requires = "delete"
    W.cmd_a2a(_A45send())
    class _A45send2(_A45send):
        text = "回个状态"
        priority = "P3"
        requires = "none"
    W.cmd_a2a(_A45send2())
    _ts45 = W._a2a_all_tasks()
    _high = next((t for t in _ts45 if t.get("priority") == "P0"), None)
    _low = next((t for t in _ts45 if t.get("priority") == "P3"), None)
    check("45.11 任务带 priority/requires，且被折叠读回",
          _high and _high.get("requires") == "delete"
          and _low and _low.get("requires") == "none", str(_ts45)[:160])
    check("45.11b 老任务无字段 → 默认 P3 / none（向后兼容）",
          W._a2a_pri_rank({})[0] == 3 and W._a2a_pri_tag({}) == "")

    # 45.12 排序：P0 在 P3 之前（check 视图里先看到高危/紧急）
    _cap45.clear()
    class _A45send3(_A45send):
        to = "A"          # 落到「待我处理」，验证接活方的授权提示
        text = "清理本机旧目录"
    W.cmd_a2a(_A45send3())
    class _A45check:
        action = "check"
        json = False
        full = False
    W.cmd_a2a(_A45check())
    _txt45 = "\n".join(_cap45)
    _i_p0 = _txt45.find(_high["id"])
    _i_p3 = _txt45.find(_low["id"])
    check("45.12 check 里 P0 排在 P3 前", _i_p0 >= 0 and _i_p3 >= 0 and _i_p0 < _i_p3,
          "p0@%d p3@%d" % (_i_p0, _i_p3))
    check("45.12b 待我处理的高危任务给出 authorize 提示（接活方要票，派活方不要）",
          "🔒 高危：认领前需人签票" in _txt45, _txt45[:200])

    # 45.13 认领高危任务（requires=delete）无票 → 拒
    _died45b = False
    try:
        class _A45claim:
            action = "claim"
            id = _high["id"]
            ttl = 600
        W.cmd_a2a(_A45claim())
    except SystemExit:
        _died45b = True
    check("45.13 requires=delete 的任务：无票认领被拒（防『自信地执行危险派活』）", _died45b)

    # 45.14 STOP 期间 A2A 写操作被拒、只读不受影响
    W._stop_path().write_text("STOP test 2\n", encoding="utf-8")
    _died45c = False
    try:
        W.cmd_a2a(_A45send2())
    except SystemExit:
        _died45c = True
    check("45.14 STOP 期间 a2a send 被拒", _died45c)
    _cap45.clear()
    W.cmd_a2a(_A45check())
    check("45.14b STOP 期间 a2a check 仍可读，并明确提示刹车在生效",
          any("STOP" in x for x in _cap45), str(_cap45)[:160])
    W._stop_path().unlink()

    # 45.15 入口覆盖：三个高危命令都接了闸门（漏一个就有绕过）
    _src45 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "scripts", "wbsync.py"), encoding="utf-8").read()
    _missing45 = [a for a in ("prune", "uninstall", "forget")
                  if ('_require_authz("%s"' % a) not in _src45]
    check("45.15 prune / uninstall / forget 三个高危入口都接了 `_require_authz`",
          not _missing45, "缺：%s" % _missing45)
    check("45.15b 高危入口同时查 STOP（`_stop_guard`）",
          _src45.count("_stop_guard(") >= 5, "count=%d" % _src45.count("_stop_guard("))
finally:
    for _k, _v in _ot45.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp45, ignore_errors=True)


print("\n=== 46. #18 入口层：`wbsync do` 意图分发（纯关键词 · 不调模型 · 不写盘）===")
_tmp46 = tempfile.mkdtemp(prefix="wbsync-do-")
_ot46 = {k: getattr(W, k, None) for k in ("out", "info", "warn", "ok", "bad", "head", "coop_shared")}
try:
    W.coop_shared = lambda: W.Path(_tmp46)
    _src46 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()

    def _do46(intent="", as_json=False):
        class _A46:
            pass
        a = _A46()
        a.intent = intent.split() if intent else []
        a.json = as_json
        _cap = []
        W.out = lambda *x, **k: _cap.append(" ".join(str(i) for i in x))
        W.info = lambda *x, **k: _cap.append(" ".join(str(i) for i in x))
        W.warn = lambda *x, **k: _cap.append(" ".join(str(i) for i in x))
        W.ok = lambda *x, **k: _cap.append(" ".join(str(i) for i in x))
        W.head = lambda *x, **k: None
        W.cmd_do(a)
        return "\n".join(_cap)

    _ids46 = [r["id"] for r in W.DO_RULES]
    check("46.1 规则表非空且有 15 条上下（记住 1 条命令，而不是 52 条）",
          len(W.DO_RULES) >= 15, "共 %d 条" % len(W.DO_RULES))
    check("46.2 规则 id 唯一（否则 `do` 的候选会重号）",
          len(_ids46) == len(set(_ids46)), str(_ids46))
    _bad46 = [r["id"] for r in W.DO_RULES
              if not (r.get("id") and r.get("label") and r.get("cmd")
                      and isinstance(r.get("kw"), list) and len(r["kw"]) >= 2)]
    check("46.3 每条规则都有 id/label/cmd 且关键词 ≥2 个", not _bad46, str(_bad46))
    _one46 = [r["id"] for r in W.DO_RULES
              if any(len(str(k)) < 2 for k in r["kw"])]
    check("46.3b 不用单字关键词（『看』『删』会到处误命中）", not _one46, str(_one46))

    check("46.4 派活意图 → a2a send（含指针派活要点）",
          "a2a send" in _do46("帮我把这活派给 B"))
    check("46.4b 回执意图 → a2a update --state completed",
          "--state completed" in _do46("我把这活做完了"))
    check("46.4c 打回意图 → rejected，并说明 rework 规则",
          "--state rejected" in _do46("打回让 B 重做")
          and "rework" in _do46("打回让 B 重做"))
    check("46.5 看板意图 → board", "wbsync board" in _do46("看板"))
    check("46.5b 排查意图 → doctor（先归因再动手）", "wbsync doctor" in _do46("怎么不同步了"))
    check("46.5c 改共享文件 → 先占锁（同机 lock / 跨机 reserve）",
          "lock claim" in _do46("我要改共享文件") and "reserve claim" in _do46("我要改共享文件"))

    _dang46 = _do46("把 D 机删掉")
    check("46.6 危险意图 → 先 authorize 签票，并写明 agent 不代签",
          "authorize" in _dang46 and "不代签" in _dang46)
    _dang_acts46 = [a for r in W.DO_RULES if r.get("danger")
                    for a in str(r["danger"]).split("|")]
    check("46.6b 规则里标的危险动作都是真接了闸门的入口（不会指错票）",
          _dang_acts46 and not [a for a in _dang_acts46
                                if ('_require_authz("%s"' % a) not in _src46],
          str(_dang_acts46))

    _none46 = _do46("今天天气不错")
    check("46.7 没命中 → 明确说「不猜」，并给 3 条兜底候选",
          "没认出" in _none46 and "wake" in _none46 and "board" in _none46
          and "doctor" in _none46)
    _bare46 = _do46("")
    check("46.8 不带参数 → 列出全部规则（终端版决策树）",
          all(r["id"] in _bare46 for r in W.DO_RULES))

    _js46 = _do46("帮我把这活派给 B", as_json=True)
    _jd46 = json.loads(_js46)
    check("46.9 --json 给出 intent/matched/rules，便于别的程序编排",
          _jd46.get("intent") and _jd46.get("matched")
          and _jd46["matched"][0]["id"] == "dispatch"
          and _jd46.get("rules") == len(W.DO_RULES))

    # 46.10 只读：跑完不落任何文件（防 Syncthing churn / 防悄悄记账）
    _before46 = sorted(os.listdir(_tmp46))
    for _q46 in ("帮我把这活派给 B", "看板", "把 D 机删掉", "今天天气不错", ""):
        _do46(_q46)
    check("46.10 `do` 不写任何文件（纯建议，零副作用）",
          sorted(os.listdir(_tmp46)) == _before46)

    # 46.11 静态：分发器里不许出现模型/网络调用（否则每次问一句都要花钱）
    _seg46 = _src46.split("def cmd_do(", 1)[-1].split("\ndef ", 1)[0]
    _banned46 = [w for w in ("openai", "anthropic", "requests.", "urllib", "subprocess",
                             "write_text", "open(") if w in _seg46]
    check("46.11 cmd_do 内无模型/网络/写盘调用（纯关键词映射）", not _banned46, str(_banned46))
finally:
    for _k, _v in _ot46.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp46, ignore_errors=True)


print("\n=== 47. 任务行注明指向：`A → B` 箭头（看任务第一眼知道谁给谁的）===")
check("47.1 常规方向 → `A → B`",
      W._a2a_dir_tag({"from": "A", "to": "B"}) == "A → B",
      W._a2a_dir_tag({"from": "A", "to": "B"}))
check("47.2 世代 >1 标「第N代」（区分同序号历代机器）",
      W._a2a_dir_tag({"from": "C", "fromGen": 2, "to": "B"}) == "C(第2代) → B",
      W._a2a_dir_tag({"from": "C", "fromGen": 2, "to": "B"}))
check("47.3 来源缺失 → 「未知」，不出 `?` 也不出 `来自 None`",
      W._a2a_dir_tag({"to": "A"}) == "未知 → A", W._a2a_dir_tag({"to": "A"}))
check("47.3b 目标缺失同样标「未知」（退信/广播任务）",
      W._a2a_dir_tag({"from": "A"}) == "A → 未知", W._a2a_dir_tag({"from": "A"}))
check("47.4 与既有的 `_a2a_dir()`（A2A 目录路径）没有撞名",
      callable(getattr(W, "_a2a_dir", None)) and callable(getattr(W, "_a2a_dir_tag", None)))


print("\n=== 48. 看板任务区：三行计数 + 一行一条（数字与行数必须一致）===")
_rec48 = {"tasks": {"mine": 3, "theirs": 1, "overdue": 2,
                    "mine_items": [["D → A", "评审文档"], ["D → A", "卸载退出"], ["B → A", "状态回报"]],
                    "theirs_items": [["A → B", "清理残留痕迹"]],
                    "overdue_items": [["A → D", "环境搭建"], ["D → A", "卸载退出"]]}}
_t48 = W._board_task_lines(_rec48)
check("48.1 三行标题：待我 / 待对方 / ⚠️超时（超时才带警告符号）",
      [x for x in _t48 if not x.startswith("  ")] == ["待我 3", "待对方 1", "⚠️超时 2"], str(_t48))
check("48.2 一行一条：每组的明细行数 == 标题数字（不用 ×N / 另 N 组换算）",
      sum(1 for x in _t48 if x.startswith("  ")) == 3 + 1 + 2, str(_t48))
check("48.2b 方向 + 中文任务概况，不带乘号",
      "  D → A：评审文档" in _t48 and not any("×" in x for x in _t48), str(_t48))

# 48.3 不省略：超时多少条就列多少行（用户明确要求「不要省略」）
_many48 = {"tasks": {"mine": 0, "theirs": 0, "overdue": 9,
                     "overdue_items": [["A → B", "环境搭建"]] * 9}}
_od48 = [x for x in W._board_task_lines(_many48) if x.startswith("  ")]
check("48.3 超时 9 条 → 列 9 行，不折叠不省略", len(_od48) == 9, "共 %d 行" % len(_od48))

# 48.4 旧版对端快照（只有聚合 dirs）→ 退回按组显示，不空白也不报错
_old48 = {"tasks": {"mine": 3, "theirs": 0, "overdue": 0,
                    "mine_dirs": [["D → A", 2, "评审文档、卸载退出"], ["B → A", 1, "状态回报"]]}}
_old48b = W._board_task_lines(_old48)
check("48.4 旧快照（只有 dirs）→ 退回「2 条」写法，不空白",
      any("2 条" in x for x in _old48b), str(_old48b))
check("48.4b 兼容更早的明细对象（dict 形式）",
      any("评审文档" in x for x in W._board_task_lines(
          {"tasks": {"mine": 1, "mine_items": [{"dir": "D → A", "skill": "review"}]}})))

check("48.5 零计数不铺空行",
      W._board_task_lines({"tasks": {"mine": 0, "theirs": 0, "overdue": 0}}) ==
      ["待我 0", "待对方 0", "超时 0"])

check("48.6 skill 名翻成中文简述（review/uninstall → 评审文档、卸载退出）",
      W._board_brief(["review", "uninstall"]) == "评审文档、卸载退出",
      W._board_brief(["review", "uninstall"]))
check("48.6b 未收录的 skill 原样显示，不吞信息",
      "unknown.task" in W._board_brief(["unknown.task"]))
check("48.6c 超过 3 类折叠成「等 N 类」（概况本身不铺长句）",
      W._board_brief(["review", "uninstall", "prune", "setup"]).endswith("等 4 类"),
      W._board_brief(["review", "uninstall", "prune", "setup"]))

# 48.7 方向标签：from 缺失时按 fromDevice 反查（短 ID 前缀），实在查不到才「未知」
check("48.7 正常方向 → `A → B`", W._a2a_dir_tag({"from": "A", "to": "B"}) == "A → B")
check("48.7b 世代 >1 标「第N代」", W._a2a_dir_tag({"from": "C", "fromGen": 2, "to": "B"}) == "C(第2代) → B")
_ot48 = {k: getattr(W, k, None) for k in ("_read_ws_registry", "_ws_suffix_of_device")}
try:
    W._read_ws_registry = lambda: {"machines": {}, "retired": [
        {"suffix": "D", "deviceID": "Q6XGQMY-AJRF", "registeredAt": 1}]}
    W._ws_suffix_of_device = lambda reg, dev: next(
        (e["suffix"] for e in reg.get("retired", []) if e.get("deviceID") == dev), None)
    W._DEV_SUFFIX_CACHE["reg"] = None
    check("48.7c from 为空 + 7 位短 ID → 解析出 D，不再显示问号",
          W._a2a_dir_tag({"from": None, "fromDevice": "Q6XGQMY", "to": "A"}) == "D → A",
          W._a2a_dir_tag({"from": None, "fromDevice": "Q6XGQMY", "to": "A"}))
    check("48.7d 两条路都查不到 → 「未知」，不是 `?`",
          W._a2a_dir_tag({"from": None, "fromDevice": "ZZZZZZZ", "to": "A"}) == "未知 → A",
          W._a2a_dir_tag({"from": None, "fromDevice": "ZZZZZZZ", "to": "A"}))
finally:
    for _k, _v in _ot48.items():
        if _v is not None:
            setattr(W, _k, _v)
    W._DEV_SUFFIX_CACHE["reg"] = None


print("\n=== 49. 任务计数不许「跳变」（跨机同步窗口内 journal 读不出来 ≠ 任务消失）===")
_tmp49 = tempfile.mkdtemp(prefix="wbsync-journal-")
try:
    _j49 = os.path.join(_tmp49, "tasks-B.json")
    _payload = {"machine": "B", "events": [
        {"event": "task", "id": "t-49-1", "from": "B", "to": "A", "skill": "report",
         "state": "submitted", "at": "2026-10-17T10:00:00.000+0800"},
        {"event": "task", "id": "t-49-2", "from": "B", "to": "A", "skill": "report",
         "state": "submitted", "at": "2026-10-17T10:01:00.000+0800"}]}
    with open(_j49, "w", encoding="utf-8") as f:
        json.dump(_payload, f, ensure_ascii=False)

    _r49 = W._a2a_read_journal(W.Path(_j49))
    check("49.1 正常 journal 可读，且记入 last-good",
          len(_r49.get("events") or []) == 2 and
          len(W._A2A_JOURNAL_LAST_GOOD.get(_j49, {}).get("events") or []) == 2)

    # 模拟 Syncthing 替换窗口：文件暂时读不出来（半截 JSON）
    with open(_j49, "w", encoding="utf-8") as f:
        f.write('{"machine": "B", "events": [')      # 半截
    _r49b = W._a2a_read_journal(W.Path(_j49))
    check("49.2 读到半截 JSON → 退回上次成功内容（任务不凭空消失）",
          len(_r49b.get("events") or []) == 2, str(_r49b)[:120])

    os.remove(_j49)
    _r49c = W._a2a_read_journal(W.Path(_j49))
    check("49.3 文件暂时不存在 → 仍用上次内容，不清空",
          len(_r49c.get("events") or []) == 2)

    _never = os.path.join(_tmp49, "tasks-Z.json")
    _r49d = W._a2a_read_journal(W.Path(_never))
    check("49.4 从未成功读过的 journal → 空事件且不抛异常（新机首次）",
          (_r49d.get("events") or []) == [])
finally:
    for _k in list(W._A2A_JOURNAL_LAST_GOOD):
        if _tmp49 in _k:
            W._A2A_JOURNAL_LAST_GOOD.pop(_k, None)
    shutil.rmtree(_tmp49, ignore_errors=True)


print("\n=== 50. B 提交的代码缺陷回归（R-20261017-B-代码v4：§1 崩溃 / §2 --file 路径 / §3 认领可见）===")
import time as _t50
_tmp50 = tempfile.mkdtemp(prefix="wbsync-fix-")
_ot50 = {k: getattr(W, k, None) for k in ("folder_table", "out", "info", "ok", "warn", "bad",
                                          "head", "coop_shared", "_a2a_claim_path")}
try:
    # ── §1 secure on --apply 写入失败时必须打印 ✗，不能抛 UnboundLocalError ──
    _ro50 = os.path.join(_tmp50, "ro")
    os.makedirs(_ro50, exist_ok=True)
    os.chmod(_ro50, 0o500)                     # 只读 → 写入必失败，走 except 分支
    W.folder_table = lambda: [("wb-skills", "工作区技能", _ro50, True)]
    _cap50 = []
    for _fn in ("out", "info", "ok", "warn", "bad"):
        setattr(W, _fn, (lambda _n: (lambda *a, **k: _cap50.append(str(a[0]) if a else "")))(_fn))
    W.head = lambda *a, **k: None

    class _A50:
        action = "on"
        apply = True
    _boom = None
    try:
        W.cmd_secure(_A50())
    except Exception as e:
        _boom = e
    check("50.1 secure on --apply 写失败 → 打印 ✗ 且不抛异常（bad 变量遮蔽已修）",
          _boom is None and any("写入失败" in x for x in _cap50),
          "%s / %s" % (_boom, str(_cap50)[:120]))

    # 静态兜底：cmd_secure 里不许再出现把 bad 当变量的写法
    _src50 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
    _i50 = _src50.find("def cmd_secure(")
    _j50 = _src50.find("\ndef ", _i50 + 10)
    _seg50 = _src50[_i50:_j50 if _j50 > 0 else len(_src50)]
    import re as _re50
    _clash50 = _re50.findall(r"(?<![A-Za-z_])bad\s*(?:=|\.|\+=)", _seg50)
    check("50.1b cmd_secure 内不再把 `bad` 当变量（防再遮蔽模块函数）",
          not _clash50, "命中 %s / seg=%d" % (_clash50[:3], len(_seg50)))

    # ── §2 --file 走「相对同步根」语义，不按 CWD 补全 ──
    check("50.2 相对路径原样保留（不按 CWD 变成绝对路径）",
          W._to_syncroot_rel("coop/shared/reviews/R-x.md") == "coop/shared/reviews/R-x.md",
          W._to_syncroot_rel("coop/shared/reviews/R-x.md"))
    check("50.2b a2a send 的 --file 已改用 `_to_syncroot_rel`（`_reservation_key` 留给 reserve）",
          "_to_syncroot_rel(_f)" in _src50 and "_reservation_key(_f)" not in _src50)

    # ── §3 认领在 check 里看得见 ──
    _claims50 = os.path.join(_tmp50, "claims")
    os.makedirs(_claims50, exist_ok=True)
    W._a2a_claim_path = lambda tid: W.Path(_claims50) / ("%s.json" % tid)
    _tid50 = "t-50-1"
    json.dump({"task_id": _tid50, "owner": "B", "fence": 1,
               "created_at": _t50.time(), "renewed_at": _t50.time(),
               "expires_at": _t50.time() + 600, "ttl": 600},
              open(os.path.join(_claims50, _tid50 + ".json"), "w", encoding="utf-8"))
    _tag50 = W._a2a_claim_tag(_tid50, "working")
    check("50.3 已认领 → `🔒 B@HH:MM`", _tag50.startswith("🔒 B@") and len(_tag50) >= 7, _tag50)
    check("50.3b 未认领（submitted）→ 「未认领」",
          W._a2a_claim_tag("t-50-none", "submitted") == "未认领")
    check("50.3c 终态不显示认领标记（已完成的任务不占行）",
          W._a2a_claim_tag("t-50-none", "completed") == "")
    json.dump({"task_id": _tid50, "owner": "B", "fence": 1,
               "created_at": _t50.time() - 900, "renewed_at": _t50.time() - 900,
               "expires_at": _t50.time() - 300, "ttl": 600},
              open(os.path.join(_claims50, _tid50 + ".json"), "w", encoding="utf-8"))
    check("50.3d 过期认领视为未认领（租约到期别人可接手）",
          W._a2a_claim_tag(_tid50, "working") == "未认领")
finally:
    try:
        os.chmod(_ro50, 0o700)
    except Exception:
        pass
    for _k, _v in _ot50.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp50, ignore_errors=True)


print("\n=== 51. 看板「超时」只列涉及本机的（别机之间的卡活不上本机看板）===")
check("51.1 我是接活方 → 涉及本机", W._a2a_relates_to({"from": "D", "to": "A"}, "A"))
check("51.2 我是派活方 → 涉及本机", W._a2a_relates_to({"from": "A", "to": "B"}, "A"))
check("51.3 别机之间（C → B）→ 不涉及", not W._a2a_relates_to({"from": "C", "to": "B"}, "A"))
check("51.4 广播（to=全员）算涉及本机", W._a2a_relates_to({"from": "C", "to": "全员"}, "A"))
check("51.5 畸形输入 → 保留（不静默吞掉）", W._a2a_relates_to(None, "A"))

_tmp51 = tempfile.mkdtemp(prefix="wbsync-od-")
_ot51 = {k: getattr(W, k, None) for k in ("coop_shared", "_a2a_all_tasks", "_a2a_overdue",
                                          "_board_doing", "_ws_my_generation",
                                          "_my_suffix_short", "out", "info", "ok", "warn", "bad")}
try:
    _root51 = W.Path(_tmp51) / "shared"
    _root51.mkdir(parents=True, exist_ok=True)
    W.coop_shared = lambda: _root51
    W._a2a_all_tasks = lambda: []
    W._board_doing = lambda my=None: ("测试", "")
    W._ws_my_generation = lambda my=None: 1
    W._my_suffix_short = lambda: "A"
    _mix51 = [
        ({"id": "t1", "from": "D", "to": "A", "skill": "fix"}, 900, 60),   # 待我
        ({"id": "t2", "from": "A", "to": "B", "skill": "setup"}, 900, 240),  # 待对方
        ({"id": "t3", "from": "C", "to": "B", "skill": "review"}, 900, 60),  # 别机之间 → 排除
    ]
    W._a2a_overdue = lambda now=None: _mix51
    _snap51 = W._board_snapshot("A", state="online")
    check("51.6 快照只统计涉及本机的超时（3 条里留 2 条）",
          _snap51["tasks"]["overdue"] == 2, str(_snap51["tasks"]["overdue"]))
    _dirs51 = [x[0] for x in (_snap51["tasks"].get("overdue_items") or [])]
    check("51.6b 明细里也没有别机之间的任务", all("→ B" not in d or "A → B" in d for d in _dirs51),
          str(_dirs51))
finally:
    for _k, _v in _ot51.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp51, ignore_errors=True)


print("\n=== 52. 任务概况：15 字以内中文短句（无省略号、尽量无英文）===")
_s52 = W._board_summary({"messages": ["suffix 解析应回退 retired；prune 前置会死锁待退机"]})
check("52.1 去掉英文词只留中文（suffix/prune/retired 都不出现）",
      not any(w in _s52 for w in ("suffix", "prune", "retired")), _s52)
check("52.2 不带省略号（是一句概括，不是被剪断的原文）", "…" not in _s52, _s52)
check("52.3 15 字以内", len(_s52) <= 15, "%s（%d 字）" % (_s52, len(_s52)))
check("52.4 单句太短时会合并下一分句（信息更完整）", "，" in _s52, _s52)

_s52b = W._board_summary({"messages": ["D 机即将退出协作组并卸载同步。请在本机运行 wbsync uninstall"]})
check("52.5 机器代号 D 保留（删了就变成「机即将…」读不通）", "D" in _s52b, _s52b)
check("52.6 【】/[] 开头标签去掉",
      W._board_summary({"messages": ["【A→D·请自卸】恢复现役请自卸"]}) == "恢复现役请自卸" and
      W._board_summary({"messages": ["[D 退出收尾·请处理] 自卸完成并退役"]}) == "自卸完成并退役",
      W._board_summary({"messages": ["【A→D·请自卸】恢复现役请自卸"]}))
check("52.7 版本号 v0.1 去掉、汉字间不留空格",
      W._board_summary({"messages": ["派活配额闸门 v0.1 已评审，方向对"]}) == "派活配额闸门已评审，方向对",
      W._board_summary({"messages": ["派活配额闸门 v0.1 已评审，方向对"]}))
check("52.8 半截括号不留（`…同步（记忆` 这种）",
      "（" not in W._board_summary({"messages": ["用户已授权 D 彻底退出同步（记忆彻底删除"]}),
      W._board_summary({"messages": ["用户已授权 D 彻底退出同步（记忆彻底删除"]}))
check("52.9 全是英文/路径 → 退回 skill 中文名（不显示空白）",
      W._board_summary({"skill": "prune", "messages": ["coop/shared/a/b.md wbsync --apply"]}) ==
      "清理残留痕迹",
      W._board_summary({"skill": "prune", "messages": ["coop/shared/a/b.md wbsync --apply"]}))
check("52.10 畸形输入 → 空串，不抛异常", W._board_summary(None) == "")
check("52.11 删英文后只剩断句残渣 → 退回 skill 中文名（B 实测「在必失败」）",
      W._board_summary({"skill": "report",
                        "messages": ["board push在Win必失败(LIBRARY_DB_DIR 写死mac路径)"]}) == "状态回报",
      W._board_summary({"skill": "report",
                        "messages": ["board push在Win必失败(LIBRARY_DB_DIR 写死mac路径)"]}))
check("52.12 跨平台找资料库脚本目录（不写死 mac 路径）",
      W._library_db_dir().endswith(os.path.join("skills", "library", "database")),
      W._library_db_dir())


print("\n=== 53. 「正在做」不许被空主题记录盖掉（hook 会不断登记空 topic）===")
_ot53 = getattr(W, "_dialogue_registry_read", None)
try:
    import time as _t53
    W._dialogue_registry_read = lambda prune=True: [
        {"machine": "A", "dialogue_id": "hook-sid", "topic": "", "state": "open",
         "last_seen_ts": _t53.time()},                                   # 最新，但没主题
        {"machine": "A", "dialogue_id": "790fbc63-aaaa", "topic": "看板任务区改造",
         "state": "open", "last_seen_ts": _t53.time() - 300},           # 旧，但有主题
    ]
    _d53, _id53 = W._board_doing("A")
    check("53.1 有主题的对话优先（不被更新的空主题记录盖掉）",
          _d53 == "看板任务区改造" and _id53 == "790fbc63", "%s/%s" % (_d53, _id53))

    W._dialogue_registry_read = lambda prune=True: [
        {"machine": "A", "dialogue_id": "hook-sid", "topic": "", "state": "open",
         "last_seen_ts": _t53.time()},
    ]
    _d53b, _id53b = W._board_doing("A")
    check("53.2 全都无主题 → 退回最新那条并标「未声明主题」",
          _d53b == "（未声明主题）" and _id53b == "hook-sid", "%s/%s" % (_d53b, _id53b))

    # 占位文案「（未声明主题）」是非空字符串，不能当成"有主题"（否则 hook 记录照样盖住真主题）
    W._dialogue_registry_read = lambda prune=True: [
        {"machine": "A", "dialogue_id": "hook-sid", "topic": "（未声明主题）", "state": "open",
         "last_seen_ts": _t53.time()},
        {"machine": "A", "dialogue_id": "790fbc63-aaaa", "topic": "真实主题",
         "state": "open", "last_seen_ts": _t53.time() - 60},
    ]
    _d53c, _id53c = W._board_doing("A")
    check("53.3 占位文案不算「有主题」→ 仍取真实主题",
          _d53c == "真实主题" and _id53c == "790fbc63", "%s/%s" % (_d53c, _id53c))
finally:
    if _ot53 is not None:
        W._dialogue_registry_read = _ot53


print("\n=== 54. 「要对方做的事」：--do 优先 + retag 补写 ===")
check("54.1 看板概况优先用 params.do（比 summary / 正文更贴合口径）",
      W._board_summary({"skill": "uninstall",
                        "params": {"do": "退出并删除记忆", "summary": "用户已授权彻底退出"},
                        "messages": ["用户已授权 D 彻底退出同步"]}) == "退出并删除记忆",
      W._board_summary({"skill": "uninstall",
                        "params": {"do": "退出并删除记忆", "summary": "用户已授权彻底退出"},
                        "messages": ["用户已授权 D 彻底退出同步"]}))

_ev54 = [
    {"event": "task", "id": "t-54", "from": "A", "to": "B", "skill": "prune",
     "at": "2026-10-17T10:00:00.000+0800", "text": "D 机已自卸完成并退役", "params": {}},
    {"event": "retag", "id": "t-54", "at": "2026-10-17T12:00:00.000+0800",
     "params": {"do": "清理 D 的注册与残留"}},
]
_t54 = W._a2a_fold(_ev54)[0]
check("54.2 retag 事件把 --do 折进任务参数",
      (_t54.get("params") or {}).get("do") == "清理 D 的注册与残留", str(_t54.get("params")))
check("54.2b retag **不改状态**（补一句话 ≠ 有人推进过）",
      _t54.get("state") == "submitted", _t54.get("state"))
check("54.2c retag **不改 updatedAt**（否则超时计时被重置）",
      _t54.get("updatedAt") == "2026-10-17T10:00:00.000+0800", _t54.get("updatedAt"))
check("54.3 折叠后看板概况直接显示「要做的事」",
      W._board_summary(_t54) == "清理 D 的注册与残留", W._board_summary(_t54))

_src54 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
check("54.4 CLI 有 --do 且 a2a 支持 retag 子动作",
      '"--do"' in _src54 and '"retag"' in _src54)


print("\n=== 55. 写 A2A 日志前必须严格读（防「读失败→覆盖历史」）===")
_tmp55 = tempfile.mkdtemp(prefix="wbsync-append-")
try:
    _j55 = os.path.join(_tmp55, "tasks-A.json")
    json.dump({"machine": "A", "events": [
        {"event": "task", "id": "x1", "at": "2026-10-17T10:00:00.000+0800"},
        {"event": "task", "id": "x2", "at": "2026-10-17T10:01:00.000+0800"}]},
        open(_j55, "w", encoding="utf-8"))
    _ok55 = W._a2a_append_event(W.Path(_j55), "A",
                                {"event": "status", "id": "x1", "state": "completed",
                                 "at": "2026-10-17T11:00:00.000+0800"})
    _n55 = len(json.load(open(_j55, encoding="utf-8"))["events"])
    check("55.1 正常情形追加一条（2 → 3）", _ok55 and _n55 == 3, "events=%d" % _n55)

    # 半截 JSON：必须抛异常且**不覆盖**（这就是 10-17 那次 148→1 的事故形状）
    with open(_j55, "w", encoding="utf-8") as f:
        f.write('{"machine":"A","events":[{"event":"task","id":"x1"')
    _before55 = open(_j55, encoding="utf-8").read()
    _raised55 = False
    try:
        W._a2a_append_event(W.Path(_j55), "A",
                            {"event": "status", "id": "x9", "state": "completed",
                             "at": "2026-10-17T12:00:00.000+0800"})
    except Exception:
        _raised55 = True
    check("55.2 日志读不出来 → 抛异常中止（不写）", _raised55)
    check("55.2b 损坏的日志**字节未变**（历史没被覆盖）",
          open(_j55, encoding="utf-8").read() == _before55)

    _j55b = os.path.join(_tmp55, "tasks-new.json")
    _ok55b = W._a2a_append_event(W.Path(_j55b), "A",
                                 {"event": "task", "id": "n1",
                                  "at": "2026-10-17T13:00:00.000+0800"})
    check("55.3 文件不存在 → 正常新建（1 条）",
          _ok55b and len(json.load(open(_j55b, encoding="utf-8"))["events"]) == 1)
finally:
    shutil.rmtree(_tmp55, ignore_errors=True)


print("\n=== 56. 跨机自报 → 主维护机自动重渲染（.render-pending 握手）===")
_tmp56 = tempfile.mkdtemp(prefix="wbsync-pending-")
_ot56 = {k: getattr(W, k, None) for k in ("_board_dir", "_board_render", "_board_is_primary")}
try:
    _bd56 = W.Path(_tmp56) / "board"
    _bd56.mkdir(parents=True, exist_ok=True)
    W._board_dir = lambda: _bd56

    _ok56 = W._board_mark_pending("B", "board write")
    _pend56 = _bd56 / ".render-pending"
    check("56.1 非主维护机自报后写「待渲染」标记",
          _ok56 and _pend56.exists() and _pend56.read_text(encoding="utf-8").startswith("B "),
          _pend56.read_text(encoding="utf-8") if _pend56.exists() else "缺失")

    _calls56 = []
    W._board_render = lambda **kw: _calls56.append(kw) or True
    _drained56 = W._board_drain_pending()
    check("56.2 主维护机消费标记 → 渲染一次并删除标记",
          _drained56 and len(_calls56) == 1 and not _pend56.exists(),
          "drained=%s calls=%d exists=%s" % (_drained56, len(_calls56), _pend56.exists()))

    check("56.3 没有标记时消费是空操作", W._board_drain_pending() is False)

    _src56 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
    check("56.4 board_touch 在非主维护机时改为举手（而非静默丢弃）",
          "_board_mark_pending(my, reason)" in _src56 and
          "if _board_is_primary(my):\n            _board_autorender(my)" in _src56)
finally:
    for _k, _v in _ot56.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp56, ignore_errors=True)


print("\n=== 57. 渲染不再限定主维护机（租约 + 新鲜度）===")
import time as _t57
_tmp57 = tempfile.mkdtemp(prefix="wbsync-lease-")
_ot57 = {k: getattr(W, k, None) for k in ("_board_dir", "_board_view_md", "_board_view_html",
                                          "_board_read_all", "_my_suffix_short", "_board_primary")}
try:
    _bd57 = W.Path(_tmp57) / "board"
    _bd57.mkdir(parents=True, exist_ok=True)
    W._board_dir = lambda: _bd57
    W._board_view_md = lambda: W.Path(_tmp57) / "v.md"
    W._board_view_html = lambda: W.Path(_tmp57) / "v.html"
    W._board_read_all = lambda: []
    W._board_primary = lambda: "A"

    check("57.1 租约：A 先拿到", W._board_lease_acquire("A", ttl=180) is True)
    check("57.2 租约被占时 B 拿不到（同一时刻只许一台渲染）",
          W._board_lease_acquire("B", ttl=180) is False)
    W._board_lease_release()
    check("57.3 释放后可再拿", W._board_lease_acquire("B", ttl=180) is True)
    W._board_lease_release()

    # 过期租约可被抢占（崩溃兜底）
    _lock57 = _bd57 / ".render-lock"
    _lock57.write_text("A %d\n" % (int(_t57.time()) - 5), encoding="utf-8")
    check("57.4 过期租约可被抢占（渲染机崩溃后不锁死）",
          W._board_lease_acquire("B", ttl=180) is True)
    W._board_lease_release()

    W._my_suffix_short = lambda: "B"          # 非主维护机
    _r57 = W._board_render(quiet=True)
    check("57.5 非主维护机在「视图落后」时也能渲染（不再被 primary 拦）",
          _r57 is True and W._board_view_md().exists(), str(_r57))

    _r57b = W._board_render(quiet=True)
    check("57.6 渲染后再渲染是幂等空操作", _r57b is False, str(_r57b))

    # C 2026-10-17 P3：直接 render 成功也要清 pending，否则下次白跑一遍
    _pend57 = _bd57 / ".render-pending"
    _pend57.write_text("B now\n", encoding="utf-8")
    W._board_view_md().unlink(missing_ok=True)
    W._board_view_html().unlink(missing_ok=True)
    _r57c = W._board_render(quiet=True)
    check("57.7 渲染成功后清掉 .render-pending（别让标记留到下次白跑）",
          _r57c is True and not _pend57.exists(),
          "rendered=%s pending=%s" % (_r57c, _pend57.exists()))
finally:
    for _k, _v in _ot57.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp57, ignore_errors=True)


print("\n=== 58. 回执即终点：notify 不该要求回话（防 ack 往返）===")
check("58.1 notify + 「回执」→ 提示改用 verify",
      "verify" in W._a2a_notify_hint("notify", "读规范并按之执行，回落一句话", ""))
check("58.2 notify 纯告知 → 不提示",
      W._a2a_notify_hint("notify", "规范已定稿，供知悉", "知悉即可") == "")
check("58.3 verify 要回执 → 不提示（那是它的正常语义）",
      W._a2a_notify_hint("verify", "请回执结果", "") == "")


print("\n=== 59. 快照与数据表两条通道不能脱节（board write 自动 push）===")
_src59 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
check("59.1 board write 成功后自动推数据表（页面读表，不读快照）",
      "_board_push(my)" in _src59.split("if action == \"write\":")[1].split("if action == \"push\":")[0])
check("59.2 提供 --no-push 关闭（离线/无授权时可用）", '"--no-push"' in _src59)
check("59.3 推送失败不阻断快照写入（best-effort）",
      "数据表未同步（%s）" in _src59)


print("\n=== 60. 日志写入的第二道网：事件数只能增 + 写前留 .bak ===")
_tmp60 = tempfile.mkdtemp(prefix="wbsync-guard-")
try:
    _j60 = W.Path(_tmp60) / "tasks-A.json"
    json.dump({"machine": "A", "events": [{"event": "task", "id": "a"},
                                          {"event": "task", "id": "b"},
                                          {"event": "task", "id": "c"}]},
              open(_j60, "w", encoding="utf-8"))
    W._a2a_append_event(_j60, "A", {"event": "status", "id": "c", "state": "completed",
                                    "at": "2026-10-17T22:00:00.000+0800"})
    _n60 = len(json.load(open(_j60, encoding="utf-8"))["events"])
    check("60.1 正常追加：3 → 4", _n60 == 4, "events=%d" % _n60)

    _bak60 = W.Path(str(_j60) + ".bak")
    _nbak60 = len(json.load(open(_bak60, encoding="utf-8"))["events"]) if _bak60.exists() else -1
    check("60.2 写前留了上一版 .bak（出事能回滚）", _nbak60 == 3, "bak events=%d" % _nbak60)

    # 第二道网：即便严格读成功，只要事件数没增长就拒写（防"读到截断但合法的文件"）
    _src60 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
    check("60.3 有「事件数未增长就拒绝写入」的守卫",
          "事件数未增长" in _src60 and "<= _n_before" in _src60)
finally:
    shutil.rmtree(_tmp60, ignore_errors=True)


print("\n=== 61. 闭合 D 的建议2/3：prune 前置校验 + 退出闭环三件套 + health ===")
_src61 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
check("61.1 prune 有「对端已自卸」前置校验（D 建议2）",
      "拒绝 prune" in _src61 and "force_order" in _src61)
check("61.2 对已 retired 的机器允许重复 prune（幂等）",
      "允许重复 prune（幂等）" in _src61)
_rows61 = W._doctor_exit_closure()
_d61 = {r[0]: r for r in _rows61}
check("61.3 退出闭环三件套对 D 判定为齐全（产物/retired/无残留，D 建议3）",
      "D" in _d61 and _d61["D"][1] is True and _d61["D"][2] is True and _d61["D"][3] is False,
      str(_d61.get("D")))
check("61.4 同后缀新旧代不误判（C 第1代 retired、第2代在役 → still_live=True）",
      "C" in _d61 and _d61["C"][5] is True, str(_d61.get("C")))
check("61.5 health 是独立命令（不与 doctor 冲突）",
      'add_parser("health"' in _src61 and _src61.count('def cmd_health(') == 1)


print("\n=== 62. Windows 入口与「? 源头文件」（B 实测的两类问题）===")
_cmd62 = os.path.join(HERE, "..", "scripts", "wbsync.cmd")
_d62 = open(_cmd62, "rb").read() if os.path.exists(_cmd62) else b""
check("62.1 wbsync.cmd 是 CRLF（LF-only 会让 cmd 解析 for/do 错乱）",
      _d62.count(b"\r") > 0 and _d62.count(b"\r") == _d62.count(b"\n"),
      "CR=%d LF=%d" % (_d62.count(b"\r"), _d62.count(b"\n")))
check("62.2 用子程序 call :try_py（循环里不再 goto :eof）",
      b"call :try_py" in _d62 and b"for /d" in _d62)
check("62.3 过滤 0 字节的商店别名桩（%%~zs LSS 1024）", b"%%~zs LSS 1024" in _d62)
check("62.4 支持 WBSYNC_PYTHON 显式指定", b"WBSYNC_PYTHON" in _d62)

_junk62 = os.path.join(HERE, "..", "..", "..", "..", "WorkBuddySync", "coop", "shared")
_has_q = False
try:
    for _root, _dirs, _files in os.walk(_junk62):
        if ".stversions" in _root:
            continue
        if any("?" in f for f in _files):
            _has_q = True
            break
except Exception:
    pass
check("62.5 同步区里不再有带 ? 的文件名（Windows 无法同步，已移出）", not _has_q)

_src62 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
check("62.6 资料库目录探测覆盖非系统盘（B 装在 E:\\workbuddy）",
      'for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"' in _src62)


print("\n=== 63. 高风险共享文件：强制互斥（自动认领锁，零摩擦）===")
_g63 = os.path.join(HERE, "..", "scripts", "wbsync-write-guard.sh")
_gd63 = open(_g63, encoding="utf-8").read() if os.path.exists(_g63) else ""
check("63.1 护栏定义 HIGH_RISK（skill 代码 / 规范 / 纪律 / README / 登记表）",
      "HIGH_RISK" in _gd63 and "/coop/shared/规范-" in _gd63
      and "/coop/shared/纪律-" in _gd63 and "工作空间登记.json" in _gd63)
check("63.2 高风险文件无锁时自动认领（不是人工 claim，也不放行裸写）",
      "自动锁·高风险共享文件" in _gd63 and "_risk_scope" in _gd63)
check("63.3 他人持锁 → exit 2 拦截（有原因 + 处理建议）",
      "已阻止写入共享文件" in _gd63 and "sys.exit(2)" in _gd63)
check("63.4 覆盖提醒仍在（不阻断，只提示先读最新）",
      "覆盖提醒" in _gd63 and "本条不阻断写入" in _gd63)


print("\n=== 64. 会话身份加固 + 推表失败重试队列 ===")
_src64 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
check("64.1 会话身份降级时按 cwd 隔离缓存（不再全机共用一个 id）",
      "sha1(str(Path.cwd())" in _src64 and "_log_sid_degraded" in _src64)
check("64.2 降级会留日志（只保留最近 20 行，不写 stdout）",
      "wbsync-sid-degraded.log" in _src64 and "[-19:]" in _src64)

_tmp64 = tempfile.mkdtemp(prefix="wbsync-pushq-")
_ot64 = {k: getattr(W, k, None) for k in ("_board_dir",)}
try:
    _bd64 = W.Path(_tmp64) / "board"
    _bd64.mkdir(parents=True, exist_ok=True)
    W._board_dir = lambda: _bd64
    check("64.3 推表失败登记待补推", W._board_push_pending_set("A", "E_AUTH_FAILED"))
    _q64 = json.loads((_bd64 / ".pending-push.json").read_text(encoding="utf-8"))
    check("64.4 队列内容含机器与原因", "A" in _q64 and "E_AUTH" in _q64["A"]["reason"], str(_q64))
    W._board_push_pending_clear("A")
    _q64b = json.loads((_bd64 / ".pending-push.json").read_text(encoding="utf-8"))
    check("64.5 推送成功后清掉该机记录", "A" not in _q64b, str(_q64b))
    check("64.6 无待补推时 flush 是空操作", W._board_flush_pending() == 0)
finally:
    for _k, _v in _ot64.items():
        if _v is not None:
            setattr(W, _k, _v)
    shutil.rmtree(_tmp64, ignore_errors=True)


print("\n=== 65. A2A 能区分「哪个对话」（fromSession）+ 冲突副本清理 ===")
check("65.1 任务身份含 fromSession（发任务时写入对话短码）",
      "fromSession" in W._a2a_identity("A"))
check("65.2 有会话码 → 显示〔会话 xxxx〕",
      W._a2a_sess_tag({"fromSession": "790fbc63"}) == "〔会话 790fbc63〕")
check("65.3 老任务无该字段 → 静默（不显示占位符）", W._a2a_sess_tag({}) == "")

_f65 = W._a2a_fold([{"event": "task", "id": "T65", "from": "A", "to": "B", "skill": "notify",
                     "fromSession": "abcd1234", "at": "2026-10-17T23:00:00.000+0800"}])
check("65.4 fold 保留 fromSession（否则显示层拿不到）",
      _f65[0].get("fromSession") == "abcd1234", str(_f65[0].get("fromSession")))

_conf65 = os.path.join(HERE, "..", "..", "..", "..", "WorkBuddySync")
_n65, _n65_board = 0, 0
try:
    for _r65, _d65, _fs65 in os.walk(_conf65):
        if ".stversions" in _r65:
            continue
        # 2026-10-18：历史冲突副本**按用户要求归档在 coop/shared/历史协作/冲突副本归档-<日期>/**
        # 它们本身就是档案，算「已收好」，不算残留（残留 = 散落在别处的 *.sync-conflict-*）。
        if "冲突副本归档" in _r65 or ".stversions" in _r65:
            continue
        for _f65 in _fs65:
            if ".sync-conflict-" not in _f65:
                continue
            # `.render-pending` 是**跨机重渲染握手标记**（V4.22）：非主维护机写、主维护机读后删。
            # 这个建-删动作本身就是设计行为，同步时偶尔来不及对账就会留一份冲突副本，
            # 属于握手成本，不算「残留」。其余冲突副本才算失败。
            if "render-pending" in _f65:
                _n65_board += 1
            else:
                _n65 += 1
except Exception:
    pass
check("65.5 同步区内没有残留的 sync-conflict 副本（render-pending 握手副本除外）",
      _n65 == 0, "count=%d（另 render-pending %d 个，属握手成本）" % (_n65, _n65_board))


print("\n=== 66. 预约锁：TTL 30 分钟 + fence 序号（问题队列 #19） ===")
check("66.1 默认 TTL = 30 分钟（60→30，机器随手关时别把文件锁死）",
      W.RESERVE_DEFAULT_TTL_MIN == 30, "= %s" % W.RESERVE_DEFAULT_TTL_MIN)

_P66 = __import__("pathlib").Path
_tmp66 = tempfile.mkdtemp(prefix="wbsync-t66-")
_ot66 = {"_reserve_path": W._reserve_path, "_my_session_id": W._my_session_id,
         "_my_suffix_short": W._my_suffix_short, "_dialogue_id": W._dialogue_id}
try:
    W._reserve_path = lambda: _P66(_tmp66) / ".file-reservations.json"
    W._my_suffix_short = lambda: "A"
    W._my_session_id = lambda: "sid-A"
    W._dialogue_id = lambda: "d-A"
    _f66 = os.path.join(_tmp66, "共享.md")
    _k66 = W._reservation_key(_f66)

    _ok66, _m66 = W._reservation_claim(_f66, reason="首次", ttl=30)
    check("66.2 首次 claim → fence=1", _ok66 and W._reserve_read()[_k66].get("fence") == 1, _m66)

    _ok66b, _m66b = W._reservation_claim(_f66, reason="续租", ttl=30)
    check("66.3 同会话重复 claim = 续租，fence 不涨（不是接管）",
          W._reserve_read()[_k66].get("fence") == 1, _m66b)

    W._my_session_id = lambda: "sid-B"
    W._my_suffix_short = lambda: "B"
    _ok66c, _m66c = W._reservation_claim(_f66, reason="别机接管", ttl=30, force=True)
    check("66.4 另机 --force 接管 → fence 递增到 2",
          _ok66c and W._reserve_read()[_k66].get("fence") == 2, _m66c)

    W._my_session_id = lambda: "sid-A"
    W._my_suffix_short = lambda: "A"
    _fok66, _fmsg66 = W._reservation_fence_status(_f66, 1)
    check("66.5 被接管后旧持有者拿 fence=1 → 失效（不许继续写）", not _fok66, _fmsg66)

    _fok66b, _ = W._reservation_fence_status(_f66, 2)
    check("66.6 非持有者拿当前 fence=2 → 仍拒（序号对但不是你的锁）", not _fok66b)

    W._my_session_id = lambda: "sid-B"
    W._my_suffix_short = lambda: "B"
    _fok66c, _ = W._reservation_fence_status(_f66, 2)
    check("66.7 持有者本人 fence=2 → 有效", _fok66c)

    _ch66, _cmsg66 = W._reservation_release(_f66, fence=1)
    check("66.8 release 带过期 fence → 拒绝（防误删他人锁）",
          (not _ch66) and ("失效" in _cmsg66), _cmsg66)
    check("66.9 拒绝后预约仍在（没被误删）", _k66 in W._reserve_read())

    _ch66b, _ = W._reservation_release(_f66, fence=2)
    check("66.10 release 带当前 fence → 成功释放",
          _ch66b and _k66 not in W._reserve_read())

    # 相对路径的基准：同步根优先（否则会锁到一个不存在的键，写护栏按同步根算 → 查不到 → 静默失效）
    _sr66 = _P66(_tmp66) / "sr"
    os.makedirs(os.path.join(str(_sr66), "coop", "shared"), exist_ok=True)
    _rf66 = os.path.join(str(_sr66), "coop", "shared", "rel.md")
    with open(_rf66, "w", encoding="utf-8") as _fh66:
        _fh66.write("x")
    _ocfg66 = W.cfg_sync_root
    W.cfg_sync_root = lambda: _sr66
    try:
        _k_rel66 = W._reservation_key("coop/shared/rel.md")
        _k_abs66 = W._reservation_key(_rf66)
    finally:
        W.cfg_sync_root = _ocfg66
    check("66.11 相对路径按**同步根**解析（与绝对路径同键，不再是 CWD 键）",
          _k_rel66 == _k_abs66 == "coop/shared/rel.md", _k_rel66)
finally:
    for _k66, _v66 in _ot66.items():
        setattr(W, _k66, _v66)
    shutil.rmtree(_tmp66, ignore_errors=True)


print("\n=== 67. A2A 池化：--to any 归一 / 可见性 / 角色（问题队列 #20） ===")
check("67.1 any → 池标记 '*'", W._a2a_norm_target("any") == W.A2A_POOL_TARGET)
check("67.2 pool / '*' / 池 都归一成池",
      W._a2a_norm_target("pool") == "*" and W._a2a_norm_target("*") == "*"
      and W._a2a_norm_target("池") == "*")
check("67.3 all → 全员（广播语义不变）", W._a2a_norm_target("all") == "全员")
check("67.4 小写机名 → 大写后缀", W._a2a_norm_target("b") == "B")
check("67.5 空值不炸（由调用方 die）", W._a2a_norm_target("") == "")
check("67.6 方向标签显示「A → 池」（不是 A → *）",
      W._a2a_dir_tag({"from": "A", "to": "*"}).endswith("→ 池"),
      W._a2a_dir_tag({"from": "A", "to": "*"}))
check("67.7 池任务：非创建者是 receiver（认领后能走状态机）",
      W._a2a_role({"from": "A", "to": "*"}, "C") == "receiver")
check("67.8 池任务：创建者仍是 assigner（不能自己接自己派的活）",
      W._a2a_role({"from": "A", "to": "*"}, "A") == "assigner")
_ok67, _ = W._a2a_transition_check({"id": "T67", "from": "A", "to": "*", "state": "submitted"},
                                   "working", "C")
check("67.9 池任务 submitted→working 对认领者放行", _ok67)
_ok67b, _msg67b = W._a2a_transition_check({"id": "T67", "from": "A", "to": "*", "state": "submitted"},
                                          "working", "A")
check("67.10 创建者自己走 submitted→working 被角色规则挡住", not _ok67b, _msg67b)
_f67 = W._a2a_fold([{"event": "task", "id": "T67", "from": "A", "to": "*", "skill": "verify",
                     "at": "2026-10-18T10:40:00.000+0800"}])
check("67.11 fold 保留 to='*'（否则收件过滤拿不到池标记）",
      _f67[0].get("to") == "*", str(_f67[0].get("to")))


print("\n=== 68. A2A journal 跨进程互斥：并发追加不丢事件（问题队列 #21） ===")
import multiprocessing as _mp68
import time as _t68
from pathlib import Path as _P68

_tmp68 = tempfile.mkdtemp(prefix="wbsync-t68-")
_j68 = os.path.join(_tmp68, "tasks-A.json")


def _w68(path, i, q):
    try:
        W._a2a_append_event(_P68(path), "A",
                            {"event": "task", "id": "T68-%d" % i, "from": "A", "to": "B",
                             "at": "2026-10-18T11:40:00.000+0800"})
        q.put("ok")
    except Exception as e:                      # 子进程异常要回传，否则父进程只能看到 exitcode
        q.put("err:%s" % e)


try:
    _ctx68 = _mp68.get_context("fork")
    _q68 = _ctx68.Queue()
    _ps68 = [_ctx68.Process(target=_w68, args=(_j68, i, _q68)) for i in range(6)]
    for _p in _ps68:
        _p.start()
    for _p in _ps68:
        _p.join(20)
    _d68 = W._a2a_read_journal(_P68(_j68))
    _ids68 = {e.get("id") for e in _d68.get("events", [])}
    _msgs68 = []
    while not _q68.empty():
        _msgs68.append(_q68.get())
    check("68.1 6 进程并发追加 → 6 条全在（无 lost update）",
          _ids68 == {"T68-%d" % i for i in range(6)}, "实际 %d 条 / 回传 %d 个" % (len(_ids68), len(_msgs68)))
    check("68.2 子进程无异常", all(m == "ok" for m in _msgs68), str(_msgs68[:2]))

    # 锁被占用时**不能静默放行**：等锁超时要抛错（宁可让人重试，不能丢事件）
    _lk68 = W._a2a_journal_lock_path(_j68)
    _lk68.parent.mkdir(parents=True, exist_ok=True)
    _lk68.write_text('{"pid": 1}', encoding="utf-8")
    _raised68 = False
    try:
        W._a2a_journal_lock(_j68, wait=0.2)
    except RuntimeError:
        _raised68 = True
    check("68.3 锁被占用 → 超时抛错（不静默放行）", _raised68)

    # 持锁进程崩了不会删锁 → 超过 stale 秒数应自动清理
    _old68 = _t68.time() - 60
    os.utime(str(_lk68), (_old68, _old68))
    _got68 = W._a2a_journal_lock(_j68, wait=0.5)
    check("68.4 过期锁（>15s）自动清理后可获取", _got68 is not None)
    W._a2a_journal_unlock(_got68)
    check("68.5 锁文件落在本机 ~/.workbuddy/.wbsync-locks（不进同步目录）",
          ".wbsync-locks" in str(W._a2a_journal_lock_path("/tmp/x/a2a/tasks-A.json")))
finally:
    shutil.rmtree(_tmp68, ignore_errors=True)


print("\n=== 69. 看板自动推表：60 秒防抖 / 查询不推 / 可关闭 / 失败登记（2026-10-18） ===")
_tmp69 = tempfile.mkdtemp(prefix="wbsync-t69-")
_P69 = __import__("pathlib").Path
_ot69 = {k: getattr(W, k) for k in ("_board_lastpush_path", "_board_push",
                                    "_board_push_pending_clear", "_board_push_pending_set",
                                    "_board_mark_pushed", "_board_snapshot",
                                    "_board_row_from_snapshot", "_lib_db")}
_old_env69 = os.environ.get("WBSYNC_BOARD_AUTOPUSH")
try:
    _lp69 = _P69(_tmp69) / "lastpush.json"
    _calls69, _pend69 = [], []
    W._board_lastpush_path = lambda: _lp69
    W._board_push = lambda my: (_calls69.append(my), "updated")[1]
    W._board_push_pending_clear = lambda my: None
    W._board_push_pending_set = lambda my, reason="": _pend69.append(reason)

    W._board_maybe_autopush("A")
    check("69.1 从没推过 → 推一次", _calls69 == ["A"], str(_calls69))
    W._board_maybe_autopush("A")
    check("69.2 60 秒内再调 → 不重复推（防抖）", _calls69 == ["A"], str(_calls69))

    if _lp69.exists():
        _lp69.unlink()
    os.environ["WBSYNC_BOARD_AUTOPUSH"] = "0"
    W._board_maybe_autopush("A")
    check("69.3 WBSYNC_BOARD_AUTOPUSH=0 → 完全不推", _calls69 == ["A"], str(_calls69))
    os.environ.pop("WBSYNC_BOARD_AUTOPUSH", None)

    if _lp69.exists():
        _lp69.unlink()
    def _boom69(my):
        raise RuntimeError("E_AUTH_FAILED")
    W._board_push = _boom69
    _raised69 = False
    try:
        W._board_maybe_autopush("A")
    except Exception:
        _raised69 = True
    check("69.4 推表失败 → 不抛异常（看板不能拖累主流程）", not _raised69)
    check("69.5 失败登记「待补推」", bool(_pend69) and "失败" in _pend69[0], str(_pend69[:1]))
    check("69.6 失败也记时间戳（避免每条命令白试）", _lp69.exists())

    # 60 秒防抖 + 纯查询不推（2026-10-18 用户：「不能每次沟通就更新么」）
    _lp69.unlink()
    _calls69.clear()
    W._board_push = lambda my: (_calls69.append(my), "updated")[1]
    W._board_maybe_autopush("A", "a2a check")
    check("69.7 纯查询（a2a check）不推表、不记时间",
          _calls69 == [] and not _lp69.exists(), str(_calls69))
    W._board_maybe_autopush("A", "a2a send")
    check("69.8 沟通动作（a2a send）照常推（≤60s 内更新）", _calls69 == ["A"], str(_calls69))

    # 显式 board push 也要记时间戳，否则手动推完 60 秒内还会被自动推一次（v4.40）
    _marked69 = []
    W._board_push = _ot69["_board_push"]      # 恢复真实实现（69.7/69.8 用的是计数器桩）
    W._board_mark_pushed = lambda my: _marked69.append(my)
    W._board_snapshot = lambda my=None, **k: {"machine": my or "A", "updated_at": "2026-10-18T12:00:00.000+0800",
                                              "state": "online", "tasks": {}}
    W._board_row_from_snapshot = lambda snap: {"机器": "A"}
    W._lib_db = lambda *a, **k: {"results": [{"机器": "A", "record_id": "rid69"}]}
    W._board_push_pending_clear = lambda my: None
    _what69 = W._board_push("A")
    check("69.9 显式 _board_push 成功 → 记防抖时间戳（防重复推）",
          _what69 == "updated" and _marked69 == ["A"], "%s / %s" % (_what69, _marked69))
finally:
    for _k69, _v69 in _ot69.items():
        setattr(W, _k69, _v69)
    if _old_env69 is None:
        os.environ.pop("WBSYNC_BOARD_AUTOPUSH", None)
    else:
        os.environ["WBSYNC_BOARD_AUTOPUSH"] = _old_env69
    shutil.rmtree(_tmp69, ignore_errors=True)


print("\n=== 70. WorkBuddy 安装根探测：Windows 非系统盘也能找到（R-20261017-B-board-push-win） ===")
_tmp70 = tempfile.mkdtemp(prefix="wbsync-t70-")
_P70 = __import__("pathlib").Path
_oldwin70 = W.IS_WIN
_oldla70 = os.environ.get("LOCALAPPDATA")
_oldenv70 = {k: os.environ.get(k) for k in ("WBSYNC_LIBRARY_SPACE", "WBSYNC_WORKBUDDY_HOME")}
try:
    # 造一个「Windows 装在 LOCALAPPDATA/Programs」的假安装
    _la70 = _P70(_tmp70) / "la"
    _lib70 = (_la70 / "Programs" / "WorkBuddy" / "resources" / "app.asar.unpacked"
              / "resources" / "plugins" / "workbuddy-builtin" / "skills" / "library")
    (_lib70 / "database").mkdir(parents=True, exist_ok=True)
    (_lib70 / "space_api.py").write_text("# stub\n", encoding="utf-8")
    W.IS_WIN = True
    os.environ["LOCALAPPDATA"] = str(_la70)
    _db70 = W._library_script("database")
    check("70.1 Windows 分支命中 LOCALAPPDATA 安装（database）",
          _db70 == _lib70 / "database" and _db70.exists(), str(_db70))
    _sp70 = W._library_script("space_api.py")
    check("70.2 space_api.py 用同一套探测（原来只写死 mac 路径）",
          _sp70 == _lib70 / "space_api.py" and _sp70.exists(), str(_sp70))

    # env 兜底优先
    _c70 = _P70(_tmp70) / "custom_space_api.py"
    _c70.write_text("# custom\n", encoding="utf-8")
    os.environ["WBSYNC_LIBRARY_SPACE"] = str(_c70)
    check("70.3 WBSYNC_LIBRARY_SPACE 优先级最高",
          W._library_script("space_api.py", "WBSYNC_LIBRARY_SPACE") == _c70)
    os.environ.pop("WBSYNC_LIBRARY_SPACE", None)

    # 找不到时返回首选路径（让报错可读，而不是空字符串）
    _miss70 = W._library_script("definitely_missing_xyz.py")
    check("70.4 全都找不到 → 返回首个候选（报错路径可读）",
          str(_miss70).endswith("definitely_missing_xyz.py"), str(_miss70))
finally:
    W.IS_WIN = _oldwin70
    if _oldla70 is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _oldla70
    for _k70, _v70 in _oldenv70.items():
        if _v70 is None:
            os.environ.pop(_k70, None)
        else:
            os.environ[_k70] = _v70
    shutil.rmtree(_tmp70, ignore_errors=True)


print("\n=== 71. 债务清偿：reviews 提醒 / list 优先级 / 渲染主维护机（#22 + B 方案 v0.2） ===")
_tmp71 = tempfile.mkdtemp(prefix="wbsync-t71-")
_P71 = __import__("pathlib").Path
_old71 = {k: getattr(W, k) for k in ("coop_shared", "_board_is_primary", "_board_pending_path",
                                     "_board_render")}
try:
    _rv71 = _P71(_tmp71) / "shared" / "reviews"
    _rv71.mkdir(parents=True, exist_ok=True)
    (_rv71 / "R-1-open.md").write_text("review-id: R-1\nto: A\nstate: open\n", encoding="utf-8")
    (_rv71 / "R-2-closed.md").write_text("review-id: R-2\nto: A\nstate: closed\n", encoding="utf-8")
    (_rv71 / "R-3-others.md").write_text("review-id: R-3\nto: C\nstate: open\n", encoding="utf-8")
    (_rv71 / "R-4-legacy.md").write_text("review-id: R-4\n# 老格式无 state 头\n", encoding="utf-8")
    W.coop_shared = lambda: _P71(_tmp71) / "shared"
    _n71, _last71, _leg71 = W._reviews_open("A")
    check("71.1 只算「发给我的 + 未关闭」（open=1，closed/他人/无头不计入）",
          _n71 == 1 and _last71 == "R-1-open.md", "%s / %s" % (_n71, _last71))
    check("71.2 无 state 头的老文件单独计数", _leg71 == 1, str(_leg71))

    # 渲染：非主维护机不得消费「举手」（原来任何机器都 drain → 双渲染冲突）
    _pend71 = _P71(_tmp71) / ".render-pending"
    _pend71.write_text("x", encoding="utf-8")
    _rendered = []
    W._board_pending_path = lambda: _pend71
    W._board_render = lambda **k: (_rendered.append(1), True)[1]
    W._board_is_primary = lambda my=None: False
    _did71 = W._board_drain_pending("B")
    check("71.3 非主维护机不兑现「举手」（不渲染）", _did71 is False and _rendered == [], str(_rendered))
    W._board_is_primary = lambda my=None: True
    _did71b = W._board_drain_pending("A")
    check("71.4 主维护机照常兑现并删除标记",
          _did71b is True and _rendered == [1] and not _pend71.exists(), str(_rendered))

    # B [P0-1] 的源码级守卫：cmd_secure 里不许再出现 `bad = 0`（局部遮蔽模块函数 → UnboundLocalError）
    _src71 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
    # 注意：不能用 `"bad = 0" in src`——`nbad = 0` 会误命中；要用词边界
    import re as _re71
    _shadow71 = _re71.search(r"(?<![A-Za-z0-9_])bad\s*=\s*0", _src71)
    check("71.5 cmd_secure 无 `bad = 0` 局部遮蔽（P0-1 回归守卫）",
          _shadow71 is None, str(_shadow71))
finally:
    for _k71, _v71 in _old71.items():
        setattr(W, _k71, _v71)
    shutil.rmtree(_tmp71, ignore_errors=True)


print("\n=== 72. 命令名不许被覆盖：stop 与 brake 各归各位（C 报告 ①） ===")
_src72 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
import re as _re72
_stops72 = _re72.findall(r"(?m)^def cmd_stop\(", _src72)
_brakes72 = _re72.findall(r"(?m)^def cmd_brake\(", _src72)
check("72.1 `def cmd_stop` 只有一份（后定义会静默覆盖前一份）", len(_stops72) == 1, "count=%d" % len(_stops72))
check("72.2 刹车逻辑独立成 `cmd_brake`", len(_brakes72) == 1, "count=%d" % len(_brakes72))
check("72.3 brake 子命令绑的是 cmd_brake", "set_defaults(func=cmd_brake)" in _src72)
check("72.4 stop 子命令仍绑 cmd_stop（真停 Syncthing）", "set_defaults(func=cmd_stop)" in _src72)
# 同名覆盖是通用坑：模块内顶层 def 名重复即为事故
_names72 = _re72.findall(r"(?m)^def ([A-Za-z_][A-Za-z0-9_]*)\(", _src72)
_dup72 = sorted({n for n in _names72 if _names72.count(n) > 1})
check("72.5 模块内没有重复的顶层函数名（通用守卫）", _dup72 == [], str(_dup72[:5]))


print("\n=== 72. 命令名不许被覆盖：stop 与 brake 各归各位（C 报告 ①） ===")
_src72 = open(os.path.join(HERE, "..", "scripts", "wbsync.py"), encoding="utf-8").read()
import re as _re72
_stops72 = _re72.findall(r"(?m)^def cmd_stop\(", _src72)
_brakes72 = _re72.findall(r"(?m)^def cmd_brake\(", _src72)
check("72.1 `def cmd_stop` 只有一份（后定义会静默覆盖前一份）", len(_stops72) == 1, "count=%d" % len(_stops72))
check("72.2 刹车逻辑独立成 `cmd_brake`", len(_brakes72) == 1, "count=%d" % len(_brakes72))
check("72.3 brake 子命令绑的是 cmd_brake", "set_defaults(func=cmd_brake)" in _src72)
check("72.4 stop 子命令仍绑 cmd_stop（真停 Syncthing）", "set_defaults(func=cmd_stop)" in _src72)
# 同名覆盖是通用坑：模块内顶层 def 名重复即为事故
_names72 = _re72.findall(r"(?m)^def ([A-Za-z_][A-Za-z0-9_]*)\(", _src72)
_dup72 = sorted({n for n in _names72 if _names72.count(n) > 1})
check("72.5 模块内没有重复的顶层函数名（通用守卫）", _dup72 == [], str(_dup72[:5]))


# ==========================================================================
print("\n" + "=" * 60)
print("  通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    ✗ %s" % f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
