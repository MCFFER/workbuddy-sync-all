# 参考：全新机器首次引导 & 设备 ID 变更

> 本文件是 `workbuddy-sync-all` 技能的参考文档，由 SKILL.md 拆出（内容与原文一致）。


本 skill 每次在**一台全新电脑**上首次运行时，都按下面这条固定流程走一遍，保证「每次打包、部署都是一致的新流程」。

### 第 0 步 · 判断是否首次

跑 `wbsync --json status` 读 `first_use`：

- `true` → 本账号**第一次**用本 skill，走完整引导（下面第 1~4 步）；
- `false` → 老用户，跳过重复询问，直接用 config 里已记住的位置继续。

`initialized` 标记存在 `~/.workbuddy/sync-assistant/config.json`（每个 OS 账号一份），多人多机天然隔离，skill 只读当前账号自己的 config。

### 第 1 步 · 资料库登记（自动交换设备 ID、判断第几台，AI 全自动）

**原理**：WorkBuddy「资料库」的「我的资料」空间（`category=personal`）账号私有，只有当前账号能看到。
skill 在其中维护一个登记文件夹，每台电脑写入自己的设备信息，同账号的其他电脑登录后读取 → 自动拿到对方设备 ID、判断自己是第几台。

- 登记文件夹：`我的资料` 根目录下，固定名 **`多机同步登记（勿删）`**；内含每台电脑一条「设备登记」文档 + 一个 README（README 写明「请勿手动改动」）。
- 每台电脑的登记文档含：`deviceID` / `hostname` / `identityKey` / `suffix`。

**AI 全自动执行**（无需用户干预）：
1. `wbsync --json identity` 取本机 `identityKey`、`device_id`；
2. 资料库脚本建/找登记文件夹，`list-nodes` 列出同账号已登记设备；
3. **登记即命名**：已登记 N 台 → 本机 = 第 N+1 台 = 对应字母（A/B/C/D…），suffix 一并写进登记文档；
4. **采集网络信息一并登记**：跑 `wbsync netinfo`（或 `wbsync registry --json` 里已带），
   把 `lanIP` / 网关 / 掩码 / 网卡类型 / 全局 IPv6 有无 / **ipconfig（或 ifconfig）原文** 都写进本机登记文档。
   这样后续任何机器一读登记表就能知道每台机器的网络情况，排查直连/隔离问题不用再问人。
5. 发现其他设备 → 取其 `deviceID` 自动 `wbsync pair <ID> --apply` 配对（等价幂等 REST，不会误删已配对的对端）。
6. **登记完自己的 doc 后，跑一次 `wbsync registry-order --apply` 把列表排整齐**——固定次序：
   README（置顶）→ 现役机登记（按后缀 A/B/C…）→ 归档文件夹「已退出机器登记」（置末）。
   只调顺序、不增删节点、幂等（已有序时不动）。`workspace init` 也会自动调用一次，故通常无需手动跑；
   手工在界面补写/拖动过登记 doc 后，跑一次即可复原。

> 资料库节点带 `createdBy`（= 账号 identityKey），天然隔离甲乙：乙用别的账号看不到甲的登记文件夹。
>
> **序号一律以资料库登记表为准**：本地 `probe`/协同目录登记表可能因对端离线、同步未完成而缺失或过时，
> 绝不能用本地残留判断自己是第几台。资料库才是权威——见第 2 步。

### 第 2 步 · 编号确认（问用户「从 N 开始」还是「顶替前面的序号」）

检测到本机是第 N 台后，**必须用 `AskUserQuestion` 确认编号方式**，不能直接默认：

- **选项 1（默认）：从第 N 个字母开始** —— 例：第 3 台 → C（`sync-all-C`）；
- **选项 2：顶替前面已存在的序号** —— 只能顶替 N 前面**已存在**的旧序号（A/B 等）。

**顶替处理（按序，不能乱）**：定位被顶替旧文档 → **不改名不删节点，改写正文**：读旧正文（`get_doc_reviews.py --page-id <旧nodeId>`），底部追加小字备注

> ⚠️ 本机编号 A 已于 <日期> 被新设备顶替（新设备后缀 C）。原设备信息：deviceID=… / hostname=… / identityKey=…

再用 `create_doc.py --node-block-id <旧nodeId> --space-id <空间> --confirm-overwrite --content-file <追加备注后的全文>` 全量写回（旧正文+备注）→ 写本机新登记文档（suffix=被顶替字母）→ 本机沿用该字母建盘。

> 编号计算：底部带「已被顶替」备注的旧文档视为停用、不再占字母，原信息保留可追溯。

**清理旧配置机及旧序号（顶替后必做）**：被顶替的旧机器可能仍在运行、仍占着旧的 Syncthing 配对与同步夹，
若不清理会导致「新机器顶了旧序号、但旧机器还在连」的混乱。顶替完成后：

1. 在**新机器的 Syncthing** 里确认设备列表只保留「当前有效的对端」，把已退役旧机器的设备条目移除
   （`wbsync doctor` 的「配对状态」段会列出所有已配对设备，逐个核对 deviceID 是否仍在资料库登记表里）；
2. 若旧机器还能连，在旧机器上跑 `wbsync stop --apply` 停掉它的 Syncthing，避免它继续反向同步；
3. 核对资料库登记表里只有「当前 + 已备注顶替」的设备，多余/重复的登记文档由用户确认后清理。

### 第 3 步 · 问位置（仅首次）

用 `AskUserQuestion` 问「盘（`sync-all-*` 根目录）放哪」，问一次写入 config 的 `disk_root`/`workspace_root`，之后复用不再问。给 2 个平台默认 + 1 个「自定义位置」：

- **Windows**：`C:\Users\<用户>\WorkBuddySync\workspaces`（推荐）/ `D:\WorkBuddySync\workspaces` / 自定义
- **macOS**：`~/WorkBuddySync/workspaces`（推荐）/ `~/Documents/WorkBuddySync/workspaces` / 自定义
- **Linux**：`~/WorkBuddySync/workspaces`（推荐）/ `~/Documents/WorkBuddySync/workspaces` / 自定义

拿到答案后：workspace 场景传 `--path`、建盘场景传 `--location`，两套通常指向同一根目录。判断系统用 `platform.system()`。

### 第 4 步 · 收尾

跑 `wbsync mark-initialized --apply` 标记本账号已初始化，下次直接跳过引导。

### 第 4.5 步 · 询问是否启用「打开对话自启动同步」（hook）

**必须在首次引导收尾时，用 `AskUserQuestion` 主动询问用户是否启用「打开对话自启动同步」，
不要等用户自己跑去 `/hooks`。** 这是本 skill 的标准体验之一，默认推荐启用。

询问话术要点：

- 说明这个能力做什么：打开任意对话 → 自动拉起 Syncthing；关闭 WB 客户端 → 看门狗约 30 秒内自动停掉 Syncthing。不依赖开机自启、不用手动跑命令。
- 说明「需要一次 hook 信任确认」：写入 settings.json 的 `SessionStart` hook 不会热加载，用户需在 WB 里输入 `/hooks` 确认一次，之后永久生效、不再重复询问。
- 默认选项设为「启用」，用户可拒绝。

询问示例：

> 是否启用「打开对话自启动同步」？打开对话时自动启动同步、关闭 WB 时自动停止，
> 不用开机常驻、也不用手动跑命令。启用后需要你在 `/hooks` 面板确认一次（仅一次）。
> 选项：① 启用（推荐）② 不启用 ③ 暂不处理

**启用时执行**（AI 代劳，勿让用户手动）：

1. 跑 `wbsync hook on --apply`：自动把 skill 自带的 `scripts/wbsync-autostart-hook.sh` 复制到
   `~/.workbuddy/scripts/` 并 `chmod +x`，同时在 `~/.workbuddy/settings.json` 写入 `SessionStart` hook
   （内部已做合并、保留原字段、写前备份，幂等可重复跑）；
2. 提示用户：在 WB 里输入 `/hooks` 确认一次（仅此一次），之后每次打开对话自动生效。
   （`wbsync hook status` 可查看当前状态，`wbsync hook off --apply` 可关闭。）

> hook 结构（WorkBuddy 桌面版）：
> ```json
> "hooks": {
>   "SessionStart": [
>     { "hooks": [ { "type": "command", "command": "/Users/<用户>/.workbuddy/scripts/wbsync-autostart-hook.sh" } ] }
>   ]
> }
> ```
> 写入 settings.json 时**必须**用脚本合并（`json.load` → 加字段 → `json.dump`），保留其他所有字段，
> 且绝不能用 `cat >` 覆盖整文件。settings.json 位于 `~/.workbuddy/settings.json`。

**边界（务必如实告知用户，不可夸大）：**

- 触发时机是「打开对话」，不是「打开 WB 客户端」——停在主界面不触发，点开任意对话才触发；
- 「关闭 WB 时关闭」靠看门狗实现（WB 没有「退出应用」hook 事件）：检测 WB 主进程消失 → 约 30 秒后停 Syncthing；
- 看门狗启动脚本幂等（检测 pidfile + 进程存活），重复触发不会起多个 Syncthing 实例；
- **看门狗循环里会每约 10 秒扫一次「机器间沟通记事本」**，发现新留言就更新未读提示文件
  （`$TMPDIR/wbsync-comms-unread.json`）。所以只要 WB 客户端保持打开，本机 Agent 就能在约 10 秒内
  感知到对端的新留言（准实时）；但这只是「发现」——真正「理解并执行」仍需 Agent 被对话唤醒。
- **改名兜底（`fixnames`）**：在某些 macOS 环境（WorkBuddy 沙箱会把 Syncthing 进程单独限制 rename），
  Syncthing 能把对端文件下载成 `.syncthing.<name>.tmp`，却**无法改名成正式文件**，导致同步卡住
  （`db/status` 报 `errors>0, needFiles>0`，日志报 `operation not permitted`）。看门狗循环每约 10 秒
  调用一次 `wbsync fixnames`，**替 Syncthing 完成最后的改名**，让同步自愈。此步纯本地文件操作，
  **不调用 AI、不消耗 token**。手动排查时也可直接跑 `wbsync fixnames [--dry-run]`。

**排障：看门狗「不见了」（2026-10-14 修复两处，务必知悉）**

1. **pidfile 记错 PID。** 原先在后台子壳里写 `echo $$ > $TMPDIR/wbsync-watchdog.pid`。POSIX 语义下
   子壳里的 `$$` 仍等于**父壳** PID，而父壳紧接着就 `exit` 了；macOS 自带 `/bin/bash` 是 3.2，
   也没有 `$BASHPID` 可用。后果两条：① 幂等保护（`kill -0 pidfile`）**永远失效** → 每次开会话都会
   多起一个看门狗；② 任何状态检查都会把**存活的**看门狗误报成「已死」。
   现改为由**父壳**用 `$!` 取后台子壳真实 PID 后写入。
2. **`wb_alive` 命中不到主进程。** 原先只匹配 `/Applications/WorkBuddy.app/Contents/MacOS/Electron`，
   实测该主进程在部分执行环境里**对 `pgrep` 不可见**（Helper/GPU/Renderer 可见、主进程不可见），
   于是 `wb_alive` 永远为假 → 看门狗在 60 秒观察窗后必然判「WB 已退出」→ 执行 `wbsync stop --apply`
   并自杀，表现为「看门狗总是莫名其妙不见了」。现放宽为匹配路径片段 `WorkBuddy\.app/Contents`。

3. **看门狗日志**：关键决策会追加到 `$TMPDIR/wbsync-watchdog.log`（启动 / 就绪 / 判定 WB 消失）。
   排查「看门狗还在不在」时**先看这个日志**，再看 `$TMPDIR/wbsync-watchdog.pid` 配合
   `kill -0`、或 `pgrep -f 'wbsync-autostart-hook[.]sh'`（方括号写法可避免 pgrep 命中你自己的命令行）。

> 改了 `scripts/wbsync-autostart-hook.sh` 后，**每台机器都要在本机重跑一次 `wbsync hook on --apply`**
> 才会更新 `~/.workbuddy/scripts/` 下的实际执行副本（脚本随 `wb-skills` 同步，但安装动作是本机行为）。

### 第 5 步 · 连接（中继优先）与开机自启（双击脚本）

**连接策略（务必按此优先级，今天实战沉淀的结论）：**

| 优先级 | 方式 | 说明 |
|---|---|---|
| ① | **免费中继** | 首选。跑 `wbsync network config --apply`，自动把内置免费中继池写进 config.xml 并**同时打开全局发现**。绕开 AP 隔离与被墙的官方发现服务器 |
| ② | **局域网发现 / IPv6 直连** | 次选。同一局域网且未被隔离时最快 |
| ③ | 都连不上 | 提示用户「可能存在 AP 隔离」，给出解决思路（关路由器 AP 隔离 / 插网线 / 用中继）|

**关键认知（务必记住）：**
- **中继和全局发现必须一起开**。只配中继不开发现 → 中继返回 `not found`、发现 404；只开发现不配中继 → 被墙时仍连不上。
- **中继地址写 config.xml 时，每个地址一个独立的 `<listenAddress>` 标签**（与 `<listenAddress>default</listenAddress>` 并列），
  绝不能用逗号/空格塞进同一个标签——否则报 `malformed listener URL`，中继一个都挂不上。
- 免费中继是「能连上」的兜底，节点动态变化、稳定性无保证。长期稳定请自建中继（`relay://你的服务器:22067?id=...`）或 EasyTier。
- 诊断命令：`wbsync network diag`（看中继/发现/AP 隔离状态）、`wbsync network test-relays`（测中继可达性）。

**开机自启（macOS 必须用双击脚本，不能靠 Agent 代劳）：**

Agent 进程受沙箱限制，`launchctl bootstrap` 必然失败（`Bootstrap failed: 5: Input/output error`）。
所以 macOS 上**不要用 `wbsync autostart on --apply` 里的 launchctl 装载**（那一步会失败），
而是引导用户**双击** `install-autostart.command`（在 skill 目录，已随 wb-skills 同步夹分发）：

> 让用户双击 `<skill目录>/install-autostart.command`（或 `~/WorkBuddySync/一键装载开机自启.command`），
> 脚本会先写 plist、再幂等地 `launchctl bootstrap` 装载，并在服务已在跑时自动跳过。
> **只暴露这一个双击入口，不要弹其他辅助文件。**

**同步排查有没有没用的、仍在挂起的流程**：装载后跑 `wbsync doctor` 确认：
- 进程只应有**单个** Syncthing 实例（出现多个会互相抢锁，日志刷 `Failed to acquire lock`）；
- 若发现双实例，跑 `wbsync stop --apply` 全收掉，launchd 的 KeepAlive 会自动拉起单个干净实例；
- 核对设备列表里没有已退役的旧机器残留（见第 2 步「清理旧配置机」）。

### 打包/分发前清理（防旧机 ID 残留泄漏）

把 skill 拷贝或同步到新电脑前，跑 `wbsync package`（演练）/ `wbsync package --apply`（真删），它会清掉：

- `*.sync-conflict-*`（Syncthing 冲突副本，文件名带设备 ID 片段）
- `*.wbsync-bak-*`（脚本备份）
- `_user_meta.json` 里的 `installedAt`（本机安装时间戳使用痕迹）

保证新电脑初次使用是干净的、不带旧机 ID 与使用记录。

> **「深处记忆」里的旧对端 ID 是另一回事**：它存在本机 `~/.workbuddy/sync-assistant/config.json`
> 的 `peer_id` / `peer_name` / `dead_ids` 字段（`pair` 时记住的对端 ID），不在 skill 目录里、
> 不会被 `package` 清到。当对端重建 Syncthing、设备 ID 变化时，本机 config 里就还存着旧 ID，
> 导致「B 机说 A 的 ID 要更新」。此时应跑 `wbsync forget --apply` 清掉本机记住的旧对端 ID，
> 再重新 `wbsync pair <新ID> --apply`。打包分发到新电脑时，也应先在新电脑上 `wbsync forget --apply`
> 清掉可能随账号配置带过来的旧 ID。

---


**这是最容易踩的坑**：Syncthing 的设备 ID 由本机配置目录里的密钥对（`cert.pem`/`key.pem`）生成，
是这台机器 Syncthing 的「身份证指纹」。一旦配置目录被删除/重建，就会生成**全新 ID**。
而资料库登记表只是「抄下 ID 存档」，不是 ID 的生成源——ID 变了没有任何机制自动通知对端。

**症状**：对端重装后，本机 `wbsync doctor` 显示对方「离线」、`connected: false`、address 为空，
但对方明明在运行。根因就是本机 config 和设备表还挂着对方的**旧 ID**。

**处理顺序（务必按序）**：

1. **对端先报新 ID**：在对端跑 `wbsync id` 取到它当前的（新）ID；
2. **本机 `wbsync forget --apply`**：一键清掉本机记住的旧对端 ID，**同时把 Syncthing 设备表里的旧条目也移除**；
3. **本机 `wbsync pair <新ID> --apply`**：用新 ID 重新配对。`pair` 现在会自动：
   - 若 config 里还记着「不同的旧对端 ID」，先自动 forget（换对端防护）；
   - 共享**当前已注册的所有同步夹**（含 `sync-workspace-all` 工作空间夹，不再是只有 4 个固定夹）；
4. 对端也反向 `forget` + `pair <本机ID>`；
5. 两端配好后，在 Web 界面 `http://127.0.0.1:8384` 互相接受对方证书。

**治本（让 ID 永不变化）**：

- 重装 Syncthing 前，先备份配置目录里的 `cert.pem` / `key.pem`（或整个 Syncthing 配置目录）；
- 迁移新机器时把旧配置目录一并带过去，ID 就固定不变，无需重新配对。

**防呆（每次操作前核实真实 ID）**：

- 因为「本机 ID 会随配置重建而变」，**每次做同步操作前先跑 `wbsync id` 取当前值**，不要沿用记忆里的旧值；
- 对端 ID 一律以资料库登记表为准（`wbsync doctor` 会提示 config 记住的对端是否已不在设备表里）。

---

## 没有多余电脑时：用本机第二实例演练接入（2026-10-14 实测可行）

**结论：不需要额外硬件。** 在同一台 Mac 上另起一个 Syncthing 实例，就等价于一台新机器。

**原理**：Syncthing 的设备 ID 由配置目录里的 `cert.pem` / `key.pem` 生成。给第二个实例一个独立的
`--home`，它就拥有**全新设备 ID**；wbsync 侧再用独立的 `WORKBUDDY_HOME` 造出第二套「本机身份」——
其 `config.json` 的 `syncthing_home` 指向该实例。两边都与生产环境隔离，互不干扰。

**用法（脚本已随 skill 分发）**：

```bash
bash <skill目录>/scripts/simulate-new-machine.sh up            # 起模拟机（幂等）
bash <skill目录>/scripts/simulate-new-machine.sh run probe --json   # 看它被判为第几台（预期 D）
bash <skill目录>/scripts/simulate-new-machine.sh run workspace init --path <模拟目录>/ws --apply
bash <skill目录>/scripts/simulate-new-machine.sh down          # 停实例 + 清理
```

模拟机默认落在 `/tmp/wbsync-sim-new`、管理端口 `18394`（可用 `WBSYNC_SIM_DIR` / `WBSYNC_SIM_GUI` 改）。
`up` 会把生产 `coop` 的**快照副本**拷进隔离 sync_root，让模拟机能「看到」已有的 A/B/C —— 副本而非软链，
任何写入都不会回流到生产。

**能演练**：编号分配（probe → D/E…）、建盘（`workspace init`）、配对与共享夹、A2A / 记事本通道、权限与目录布局。

**不能演练**（务必如实告知，不可夸大）：

- **跨网络的发现 / 中继 / AP 隔离** —— 两个实例同机，走的是回环，测不出局域网发现与中继链路；要测那一段得用真设备（借一部安卓手机装 Syncthing-Fork 最省事）或虚拟机；
- **WorkBuddy 客户端层** —— 资料库登记、hook 交互、`AskUserQuestion` 问编号，这些依赖客户端，模拟机跑不到（`identity_key` 在隔离环境取不到是正常现象）。

**依赖的行为（2026-10-14 修）**：管理 API 端口必须**跟随各自的 config.xml**。
原先 API 地址硬编码 `127.0.0.1:8384`，同机的第二个实例会被生产实例遮蔽——`wbsync id` 拿到的是生产机
ID，`probe` 据此判错编号。现在 `Syncthing` 对象绑定自己 `config.xml` 的 `<gui><address>` 端口，
`WBSYNC_GUI_ADDR` 可显式覆盖。回归断言见 `tests/test_wbsync.py` 第 13 节。

---

