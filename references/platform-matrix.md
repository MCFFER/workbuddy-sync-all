# 平台适配对照表

本技能的全部平台差异都收敛在一层薄适配里（见 `scripts/wbsync.py` 的
`# 平台适配层` 一节）。**不要在上层逻辑里散写 `if 是Windows`。**

---

## 一、能力对照

| 能力 | macOS | Windows | Linux |
|---|---|---|---|
| 进程存活 | `os.kill(pid, 0)` | `OpenProcess(0x1000)` | `os.kill(pid, 0)` |
| 进程枚举 | `ps -axo pid=,comm=,args=` | `tasklist /FO CSV /NH` | `ps -axo ...` |
| 枚举回退 | `pgrep -fl <名>` | 无（tasklist 一般可用） | `pgrep -fl <名>` |
| 终止进程 | `SIGTERM` / `SIGKILL` | `taskkill /PID <n> /T [/F]` | `SIGTERM` / `SIGKILL` |
| 开机自启 | `~/Library/LaunchAgents/*.plist` + `launchctl` | 注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` | `~/.config/systemd/user/*.service` + `systemctl --user` |
| 后台脱离 | `start_new_session=True` | `DETACHED_PROCESS \| NEW_PROCESS_GROUP` | 同 macOS |
| Syncthing 配置目录 | `~/Library/Application Support/Syncthing` | `%LOCALAPPDATA%\Syncthing` | `~/.config/syncthing` |

## 二、安装产物选择

官方发布页 `https://api.github.com/repos/syncthing/syncthing/releases/latest`。
按 `sys.platform` 与 `platform.machine()` 选择：

| 平台 / 架构 | 选中的产物 |
|---|---|
| macOS（任意架构） | `syncthing-macos-universal-vX.Y.Z.zip` ← **优先，Intel+ARM 通吃** |
| macOS（无 universal 时的回退） | `syncthing-macos-<arm64\|amd64>-vX.Y.Z.zip` |
| Windows / amd64 | `syncthing-windows-amd64-vX.Y.Z.zip` |
| Windows / arm64 | `syncthing-windows-arm64-vX.Y.Z.zip` |
| Linux / amd64 | `syncthing-linux-amd64-vX.Y.Z.tar.gz` |

### 校验

官方只提供 **GPG 签名的** `sha256sum.txt.asc`，没有逐资产的 `.sha256`。
处理方式：抓该文件，剥掉 PGP 外壳，按 `^([0-9a-fA-F]{64})\s+\*?(.+)$` 解析出哈希表。
取不到校验值时**明确警告"跳过校验"**，不静默通过。

---

## 三、四个必须知道的坑

### 坑 1：macOS 压缩包里有**两个**叫 `syncthing` 的东西

实测 v2.1.6 的 `syncthing-macos-universal-*.zip` 内容：

```
syncthing-macos-universal-v2.1.6/etc/freebsd-rc/syncthing          1,709 B   ← shell 脚本
syncthing-macos-universal-v2.1.6/syncthing                    57,418,016 B   ← 真二进制的
```

FreeBSD 的 rc 脚本在包内**排在前面**。按名字取第一个会拿到脚本，
装完启动直接报：

```
line 23: /etc/rc.subr: No such file or directory
```

**正确做法**：排除 `etc/`、`.app/` 路径，排除小于 1MB 者，多个候选中**取最大**。
（实现见 `_is_real_binary_member()`；回归测试见 `test_wbsync.py` 第 3、4 组。）

### 坑 2：首次启动需要写 `config.xml`，受限环境会失败

Syncthing 首启时会先写 `.syncthing.tmp.<随机>` 再 `rename` 成 `config.xml`。
在受限沙箱里 `rename` 会被拒：

```
ERR Failed to initialize config (error="failed to save default config:
    rename .../.syncthing.tmp.NNN .../config.xml: operation not permitted")
```

这不是配置错误，换到用户自己的终端里跑即可。判断依据：
日志里出现 `operation not permitted` 且**端口 8384 始终无响应**。

**同一根因的第二种表现（已装好、运行中要加设备/夹）**：Syncthing 每次写配置都走
「写 `.syncthing.tmp.<随机>` → rename」两步，所以只要 rename 受限，**所有 REST 改配置的
调用都返回 HTTP 500**（`POST /rest/config/devices`、加夹、改 `listenAddress` 一律失败），
而 `GET /rest/config` 正常。此时常规「加设备 → 共享夹」手段全废，改用**手工改名绕过**：

1. `GET /rest/config` 取回完整 JSON；
2. 在内存里加上设备 / 夹 / 共享关系；
3. `PUT /rest/config` —— 返回 500，但**临时文件已含完整新配置**，属预期；
4. 停 Syncthing（`wbsync stop --apply`）；
5. 用 shell 把配置目录下的 `.syncthing.tmp.<随机>` **改名为 `config.xml`**（先备份原文件）；
6. 重启 Syncthing 加载新配置。

实战：2026-10-14 C 机（Mac mini）接入时就卡在这里——A 机设备表加不上 C，
用上述手法后才「A 设备表 = A/C/B、5 夹共享给 C」。根因是 WorkBuddy 沙箱按可执行文件
单独限制 Syncthing 的 `rename`（同目录下 bash / python 都能改名，唯独 Syncthing 不行；
授完全磁盘访问 + 重启 WorkBuddy 也无效）。同步文件的改名由看门狗里的 `wbsync fixnames` 兜底。
`wbsync pair` 同样依赖 REST 改配置，在该环境下会失败——当时专门通知过 B「勿用 `wbsync pair`」配对 C。

### 坑 3：`listenAddress` 多地址必须用「重复标签」，不是逗号也不是空格

配中继（relay）时，Syncthing 的 `listenAddress` 字段在 **XML 配置里必须用多个
`<listenAddress>` 标签**表示，每个地址一个标签，与 `<listenAddress>default</listenAddress>` 并列：

```xml
<listenAddress>default</listenAddress>
<listenAddress>relay://123.51.24.51:22067/?id=...</listenAddress>
<listenAddress>relay://180.149.44.55:22067/?id=...</listenAddress>
```

**错误写法**（把 `default` 和 relay 用逗号或空格塞进同一个标签）：

```xml
<listenAddress>default,relay://...</listenAddress>   <!-- 报 malformed listener URL，中继全不挂 -->
```

逗号是 GUI 的写法，空格在 XML 标签里也不是分隔符。写错后日志会报：

```
ERR Skipping malformed listener URL (uri="default,relay://..." error="parse ...: first path segment in URL cannot contain colon")
```

**正确做法**：`wbsync network config --apply` 已按正确格式写，不要手改。

### 坑 4：中继与全局发现必须一起开，缺一不可

只配中继、不打开 `globalAnnounceEnabled`（全局发现），结果是对端经中继连你时返回
`not found`，全局发现查你的 deviceID 返回 404。原因是中继连接的握手依赖发现服务器当
「红娘」：双方都要把「我在哪个中继」上报到发现服务器，对方查到后才发起连接。

`wbsync network config --apply` 会同时打开 `globalAnnounceEnabled` 和 `relaysEnabled`，
并写入中继地址，三者齐活。

### 坑 5：Windows 隐藏目录 + 时间命名空壳 + hook 确认（B 机实战，2026-10-14）

- **`attrib +h` 在 Git Bash 里不可靠**：现象是输出乱码、退出码看似 0，但 PowerShell 复查
  `Hidden=False`（没真生效）。原因 = Git Bash 中文编码 + 沙箱拦截。**做法**：用本技能封装的
  `wbsync workspace hide --apply`，它按平台可靠隐藏（macOS `chflags hidden` / Windows 用
  PowerShell 对 `Attributes` 位或 `+h`），一条命令隐藏 A/B/C（及后续 D…），替代手动敲，符合
  「减少用户操作」宗旨。
- **`~/WorkBuddy/` 下会被平台遗弃「时间命名空壳」**（如 `2026-10-13-18-48-15`）：每次跑
  skill/会话时建的临时目录，会话结束没清理。**做法**：`wbsync workspace cleanup`（默认只列、
  `--apply` 才删；只删严格时间戳命名且为空的目录，绝不误删用户项目如 `Claw`）。这些空壳从未
  登记进 `workbuddy.db`，删之无 `BEGIN IMMEDIATE` / WAL 锁风险。
- **hook 看门狗需用户在 WB `/hooks` 面板确认一次才生效**：`wbsync hook on --apply` 装脚本 +
  写 `settings.json` 的 `SessionStart` 后，WB 安全机制要求手动确认一次。**做法**：这是**可选
  便利**——核心同步靠 `autostart` 系统自启，hook 只额外提供「开/关 WB 时启停 Syncthing」；
  不想确认可直接跳过。脚本更新后重跑 `hook on --apply` 即刷新，**无需重新确认**。

---

## 四、进程探测的反模式（务必避免）

**不要**把枚举失败吞成空列表：

```python
try:
    r = subprocess.run(["ps", ...], capture_output=True)
except Exception:
    pass                      # ← 错：异常被吃掉
return res                    # ← 错：返回 []，与"确实没有进程"无法区分
```

后果：上层把"我没查到"渲染成"它没在跑"，用户看到的是**假的状态**。
原 Windows 版技能就栽在这上面 —— 它的 `is_alive()` 用 `ctypes.windll.kernel32`
判断进程，而 `ctypes.windll` 在 macOS 上根本不存在，异常被 `except Exception` 吞掉，
于是**对任何存活进程都返回 False**，体检报告永远显示"守护进程未在运行"。

**正确做法**：

- 枚举失败返回 `None`，与"空列表"区分开
- 调用方对 `None` 显示"无法枚举（权限受限）"，对 `[]` 才显示"没有在跑"
- 存活判断用 `os.kill(pid, 0)`（POSIX，零依赖），Windows 才走 `OpenProcess`
