# 症状 → 根因对照表

排错时先跑 `python3 scripts/wbsync.py doctor`，它会把下列多数问题直接指出来。
本表用于理解**为什么**，以及 doctor 没覆盖到的情况。

---

## 一、先分诊：是文件没到，还是登记没修？

这是最高频的分岔，问错方向会浪费大量时间。

| 观察 | 结论 | 下一步 |
|---|---|---|
| 目标机 `marketplaces/my-experts/` **里没有**该专家目录 | **文件层**没通 | 查 Syncthing 是否在跑、夹子是否注册、是否已配对、证书是否已互相接受 |
| 目录**在**，但专家用不了 / 是旧版本 | **登记层**没修 | 跑 `wbsync expert --apply`，然后重启 WorkBuddy |
| 目录在、登记也指向本机，但仍不生效 | 看 §四 | 查版本漂移或缓存 |

一条命令同时判断两层：

```bash
python3 scripts/wbsync.py plan
```

看「四个同步夹」段（文件层）与「登记层」段（登记层）。

---

## 二、文件层

| 症状 | 根因 | 处理 |
|---|---|---|
| 一个字节都不过去，连上就断 | 两端 `folder id` 不一致 | 四个 id 必须逐字相同：`wb-skills` / `wb-experts` / `wb-experts-cache` / `wb-coop` |
| 单边共享，另一边没反应 | 只做了一侧配对 | **两端都要**执行 `pair`，把对端 ID 加进来并共享夹子 |
| 一直显示"已连接但同步中"不动 | 首次配对后未接受对方证书 | 到两端 `http://127.0.0.1:8384` 的 Web 界面**互相接受证书**（安全设计，无法自动化） |
| 某个夹永久不同步 | 夹子的 `path` 指向了不存在的目录 | `wbsync setup` 会重建；确认目录真的存在 |
| 同步了但内容是空的 | 夹子指向的是空目录（如源机该目录本来就空） | `my-experts` 在没装过专家时**本就是空目录**，属正常 |
| 开机不自启 | 自启登记缺失或指向旧路径 | `wbsync autostart status` 查看，`wbsync autostart on --apply` 重建 |

---

## 三、登记层

| 症状 | 根因 | 处理 |
|---|---|---|
| 专家列表里有，但调用不生效 / 跑旧版 | `installed_plugins.json` 里的路径还是别台机器的 | `wbsync expert --apply`，然后**重启 WorkBuddy** |
| 路径里出现 `C:\Users\...` 或别的用户名 | 登记文件被原样同步过来了 | 同上 |
| `expert` 报"本机不存在" | 对应的运行副本还没同步过来 | 先解决文件层；等同步完再跑一次 |
| 修完没反应 | 登记要重启才重读 | 重启 WorkBuddy |
| 改了登记文件后 WorkBuddy 行为异常 | 可能误写了内置插件条目 | 用同目录下的 `*.wbsync-bak-*` 还原 |

**自愈工具绝不会碰**：`isBuiltIn` 的市场、应用自带插件（`.app/`、`Program Files` 下的路径）、
以及目标目录不存在的条目。

---

## 四、其他

| 症状 | 根因 | 处理 |
|---|---|---|
| 装完启动报 `/etc/rc.subr: No such file or directory` | 解压时拿到了 FreeBSD 的包装脚本而非真二进制 | 见 `platform-matrix.md` §三 坑 1；重装即修 |
| 首启失败，日志含 `operation not permitted`，端口 8384 无响应 | 受限环境禁止 `rename config.xml` | 换到用户自己的终端里跑 |
| 已装好的 Syncthing 加设备/夹时 REST 一律返回 **HTTP 500**（`GET` 正常） | 同一 `rename` 受限：配置写不进 `config.xml` | 见 `platform-matrix.md` §三 坑 2「手工改名绕过」六步 |
| doctor 说"无法枚举进程" | 当前环境限制 `ps` / `pgrep` | **这不是"没在运行"**；改看端口 8384 是否有响应 |
| 装了 Windows 版二进制却装在 Mac 上 | 选包逻辑只认 `windows-amd64` | 用本技能重装（会自动选 `macos-universal`） |
| 同步把 `.tmp`、`.lock` 之类也带过来了 | 未配置忽略规则 | 在 Syncthing 的夹子设置里加忽略模式 |
| `~/WorkBuddy/` 下出现 `2026-10-13-18-48-15` 这类时间命名空目录 | 平台每次跑 skill/会话时建的临时工作空间，会话结束遗弃成空壳 | `wbsync workspace cleanup`（默认只列，`--apply` 才删）。只删严格时间戳命名且为空的目录，绝不误删用户项目（如 `Claw`） |
| 删这些空壳会不会搞坏 `workbuddy.db`？ | 这些空壳**从未登记**进 `workbuddy.db` 的 `workspaces` 表（实测仅 `sync-all-A` 在册），所以删空壳不碰库 | 无需担心 `BEGIN IMMEDIATE` / WAL 锁；若日后真要改 `workspaces` 表，本工具已用 `WAL + busy_timeout` 排队写（`_register_workbuddy_workspace`） |

### 4.1 关于 `BEGIN IMMEDIATE` / WAL 锁坑

WorkBuddy 运行时会以 WAL 模式持有 `~/.workbuddy/workbuddy.db` 的写连接。外部进程若直接
`INSERT` 且用 `BEGIN IMMEDIATE` 抢写，容易撞 `database is locked` / `attempt to write a
readonly database`。本工具写该表时（`_register_workbuddy_workspace`）已显式：

```python
conn = sqlite3.connect(db, timeout=15, isolation_level=None)
conn.execute("PRAGMA busy_timeout=15000")
conn.execute("PRAGMA journal_mode=WAL")
```

让其排队等待而非立即失败；失败时降级为**警告**（便利功能，不应阻断建盘）。**日常清理时间命名
空壳不触发此路径**——它们不在库里。

---

## 五、判定"到底在不在同步"

不要靠感觉，用数据：

```bash
python3 scripts/wbsync.py plan          # 看夹子注册与共享状态
python3 scripts/wbsync.py doctor        # 看同步错误计数与对端在线状态
```

另外可直接查询 Syncthing 接口（需要 API key，`wbsync` 会从 `config.xml` 自动读取）：

- `GET /rest/system/status` —— 本机设备 ID、运行状态
- `GET /rest/db/status?folder=<id>` —— 该夹的 `errors`、`globalFiles`、`localFiles`
- `GET /rest/system/connections` —— 各对端是否 `connected`

判据：**对端 `connected: true` 且该夹 `errors: 0`**，才算这条链路是通的。

---

## 六、网络连接故障（2026-10-13 实战沉淀，务必先看这节）

这是本技能最容易卡住、最耗时间的一类问题。症状与根因对照如下。

### 6.1 连接方式与优先级

| 优先级 | 方式 | 何时用 |
|---|---|---|
| ① | 免费中继 | 首选。`wbsync network config --apply` 一键配置（中继 + 全局发现） |
| ② | 局域网发现 / IPv6 直连 | 同一局域网且未被隔离 |
| ③ | 都不行 | 提示 AP 隔离，给解决思路 |

### 6.2 症状 → 根因对照

| 症状 | 根因 | 处理 |
|---|---|---|
| 官方中继池 `relays.syncthing.net`、发现服务器 `discovery.syncthing.net` 连不上（TLS 握手超时，`http=000`） | 官方服务器在国内被墙（SNI 阻断）。注意：`nc -z ... 443` 可能通、但 TLS 不通，别被误导 | 用国内/亚太可达的免费中继；`wbsync network test-relays` 验证 |
| 中继配好了但日志报 `Skipping malformed listener URL ... first path segment in URL cannot contain colon` | **中继地址写进了同一个 `<listenAddress>` 标签**（用逗号/空格拼的）。Syncthing 要求**每个地址一个独立标签**，与 `<listenAddress>default</listenAddress>` 并列 | 用 `wbsync network config --apply` 重写（已按正确格式） |
| 中继返回 `not found` / 全局发现查对端 404 | 对端**没注册到这些中继**、或**全局发现没开**（没上报公告）。中继连接是「双方都上报 → 对方经发现服务器查到 → 握手」，缺一不可 | 两端都跑 `wbsync network config --apply`（中继 + 全局发现一起开） |
| 双方同网段、网关都通、各能看到同网段几十台机器，但**互相 ping 不通、ARP 表对方是 `(incomplete)`** | **无线 AP 隔离**（有线↔无线被接入层隔离）或访客网络隔离 | 关路由器「AP 隔离/客户端隔离」；或无线机器改插网线；或继续用中继绕开 |
| 有 IPv6 但只有 `fe80::` 链路本地 | 无公网 IPv6（`2xxx:`），链路本地地址同样受二层隔离，救不了 AP 隔离场景 | 别指望 IPv6 直连，走中继 |
| `listenAddress` 里 `default` 和 relay 混写后 Syncthing 起不来 / exit status 3 | `default` 不能和其他地址混在一个标签里 | 保持 `default` 独立成标签，relay 各占独立标签 |

### 6.3 诊断三步

```bash
wbsync network diag              # 看本机 IP/网关/中继/发现/AP 隔离状态
wbsync network test-relays       # 测免费中继可达性
wbsync doctor                    # 看对端 connected 状态 + 同步错误
```

关键判据：对端 `connected: true` 且 `type=tcp-client` = 打洞成功、P2P 直连（最优）；
`type=relay-client` = 走中继转发（可用但较慢）。两者都算通。

### 6.4 免费中继的边界

内置免费中继池（`wbsync.py` 里的 `RELAY_POOL`）是**能连上的兜底**，节点动态变化、可用性无保证。
长期稳定请二选一：
- **自建中继**：一台公网服务器跑 `strelaysrv`，A/B 都填 `relay://你的服务器:22067?id=<中继ID>`；
- **EasyTier**：国内有官方公共节点 `public.easytier.cn`，建虚拟网后再映射虚拟 IP 直连。
