---
name: workbuddy-sync-all
display_name: WorkBuddy 全平台多机同步 · 协同
display_name_en: WorkBuddy Multi-Device Sync · Collaboration
description: '打造你的 AI 小团队：多台电脑的 WorkBuddy 合成一套，AI 互相派活接力。① 多机一体同步：全平台多设备，同步共同协作空间的技能/专家/连接器/配置/工作区，装一次处处可用。② 多机协同分工：多机间无缝交流，派活走 A2A 任务单、留言走记事本、进展写共享日志。六道机制：防碰撞/防误删/预演练/免服务器安装/增量省流/无痕退出。'
description_zh: "打造你的 AI 小团队——多台电脑的 WorkBuddy 合成一套。\n\n【两个核心】\n① 多机一体同步：全平台多设备，同步共同协作空间的技能、专家、连接器、配置、工作区，装一次处处可用。\n② 多机协同分工：多机间无缝交流，派活走 A2A 任务单、留言走记事本、进展写共享日志。\n\n【六道机制】① 防碰撞 ② 防误删 ③ 预演练 ④ 免服务器安装 ⑤ 增量省流 ⑥ 无痕退出\n\n⚠️ 资料库「多机同步登记（勿删）」和各机「设备登记」是通讯录，别删别改。\n\n试试问我：① 怎么同步到另一台电脑？② 家里 mac、公司 win 能同步吗？③ 开始同步吧。④ 公司 AI 没做完的工作，回家怎么接着做？"
description_en: "Build your AI team — turn every computer's WorkBuddy into one crew.\n\n(1) One-crew sync: all platforms; syncs skills, experts, connectors, config and workspaces — install once, use everywhere.\n(2) Teamwork: jobs as A2A tickets, messages to the shared notebook, progress to the shared log.\n\nSix safeguards: collision guard · undo vault · dry-run · no-server setup · lean flow · clean exit.\nNote: the Library multi-device sync registry (do not delete) and per-machine device registrations are the address book — do not delete or edit."
category: 效率工具
tags: [多设备同步, 全平台, 多机协同, 跨平台, 技能同步, 专家同步, 连接器同步, 配置迁移, Syncthing, 跨互联网, 设备ID自动交换, 跨机文件预约, 省token, 增量读取, 干净退出]
version: 4.42
author: WorkBuddy
agent_created: true
---

# WorkBuddy 全平台多机同步 · 协同

几台电脑上的 WorkBuddy **合成一套**：同步共同协作空间里的技能 / 专家 / 连接器 / 配置 / 工作区，并让它们的 AI **互相派活、接力交付**。产品文案在 frontmatter `description_*`，本文件只讲**怎么干**。

- `scripts/wbsync.py` —— 单文件 CLI，纯 Python 标准库，三平台通用
- `tests/test_wbsync.py` —— 自测套件，不需要真实 Syncthing（`--list` / `--section N[,M]` 分段跑）
- `vendor/` —— 内置 macOS universal 与 Windows amd64 离线包（各带 `.sha256`），断网也能装

> ⚠️ 资料库「多机同步登记（勿删）」与各机「设备登记」是**互相认出的通讯录**，别删别改，否则同步会断。

---

## 版本变更

当前 **V4.35**（分发版；原语义版本 2.14.2），V4 起版本号改用分发口径（对齐打包 V4）；4.1 任务行加 `A → B` 指向箭头 · 4.2/4.3 看板任务区三组细分（待我/待对方/超时 → 按方向 + 中文任务概况）。此前为 2.x 语义版本，最近三条：2.14.2 入口层 `wbsync do` 意图分发 + 决策树（P0-1）· 2.14.1 SKILL.md 瘦身 22.3KB→12.1KB · 2.14.0 P1 授权闸门（人签票 + `brake` 急停 + 任务分级）。

## ★ 唤醒即必做（最高优先级，所有机器一律遵守）

**仅当话题涉及同步 / 多机 / 跨机协作 / 工作空间时**（即确实会碰到别的机器），**第一件事跑一条命令**：

```bash
wbsync wake   # 本机后缀 + 同步健康 + A2A 待办 + 未读 + 对端状态卡片（≤20 行）
```

有发给本机的任务 → **先 `wbsync a2a check --full` 看全文**（默认只显示前 60 字、**会漏掉关键指令**！），确认无误再执行并用 `wbsync a2a update --id <ID> --state completed` 回执；没有 → 继续正常服务。
不要等用户说「去看看有没有留言」——这是 Agent 的主动职责。

> **省 token**：看状态一律走轻量命令——**不跑** `plan` / `doctor`，**不全量 `Read`** 大日志或专线文件。
> 轻量梯度：`wake`（首选，一条拿全）→ `brief`（更简）→ `comms read` / `a2a check`（看细节，
> 默认截断 60 字，要全文才加 `--full`）。**纯本机任务不跑唤醒检查**（不涉及其它机器就无唤醒成本）。

---

## 不知道用哪条命令？先 `wbsync do "<一句话>"`

**纯关键词映射，不调模型、不执行、不写盘**——给一条建议 + 要点。记不住 53 条命令就只记这一条（没命中会退回 3 条候选，并明说"不猜"）。

| 想做什么 | 用哪条 |
|---|---|
| 派活 / 回执 / 打回 | `a2a send` / `update --state completed` / `--state rejected` |
| 看全局（现算，永远新鲜） | `wake --full`（只要一行摘要 `brief`） |
| 各机在干什么（快照，>1h 变灰） | `board` |
| 改共享文件 | 同机 `lock claim` / 跨机 `reserve claim` |
| 危险动作（删/卸载/踢机） | **人签** `authorize` → 再跑真命令 |
| 出问题 | `doctor`（先归因再动）；跨网连不上 `network diag` |
| 换人接着做 | `handoff` / `pickup` |

---

## 用户要「退出 / 卸载」时（对话式，勿直接执行）

当用户用自然语言说「退出」「卸载同步」「离开协作组」「把 X 机移除 / 踢掉」「不玩了」等——**绝不直接执行**：

1. **先跑演练**：`wbsync uninstall`（不加 `--apply`，只读）。
2. **用自然语言讲清会发生什么**：停同步并卸载 Syncthing、删本机在协作区的东西（**这些删除会传播到其他机器**，对端自写文件不动）、广播退出、登记退役、**不可逆**（回来要重装且设备 ID 会变）。
3. **必问日志留存**：留在其他机器 → `--memory archive`；不留 → `--memory delete`。
4. 用户确认后才 `wbsync uninstall --apply --memory archive|delete`。

**发起权限**：只 **A 机或本机**可发起（`--by`）；用户在本机（如 B）要求踢 C → 拒绝（B 无权）。逐步细节见 `@references/uninstall-flow.md`。

---

## 核心认知：同步分「三层」（排错前必读）

| 层 | 内容 | 谁负责 | 自动？ |
|---|---|---|---|
| **文件层** | 技能 / 专家 / 连接器 / 协同目录的文件 | Syncthing | ✅ 双向 |
| **登记层** | 让专家与连接器在界面生效的登记文件 | **本技能工具** | ❌ 每机各修一次 |
| **工作空间** | `sync-all-A/B/C/D` 协作目录 | `wbsync workspace` | ⚠️ 每机 `init` 一次 |

**只做文件层 = 静默失败**：专家/连接器看着装了，实际跑旧版本、**界面一个错都不报**——登记文件存的是含用户名的绝对路径（`C:\Users\alice\...`），无法跨机复制，必须在目标机重建。细节 `@references/registration-layer.md`。
**同步范围**：技能 `wb-skills` → `~/.workbuddy/skills`；专家 `wb-experts[-cache]` → `plugins/marketplaces|cache/my-experts`；协同 `wb-coop` → `~/WorkBuddySync/coop`（设备 ID 信箱 + 登记表 + 记事本 + A2A 日志）。连接器登记由 `wbsync expert` 一并重建。

---

## 两条机器间通道（并存，互不替代）

- **要回执 / 闭环 → A2A**：`a2a send|check|update|claim|list`，七态（submitted / working / completed / **rejected·rework** / failed / canceled），能数「欠几条」。打回返工：派活方 `--state rejected --reason "≤100字"`（completed 也能打回）→ 接活方 `rework`（同 id 内 `attempt`+1，上限 3 轮）或 `canceled`，**不接受文字申辩**。
- **要人读 / 广播 → 记事本**：`comms send|read|check|cursor`，自由文本、可 `--to 全员`。
- 同一件事**别两边各发一份**：A2A 放「任务 + 指针」，正文留记事本。

> **四个易错点**：① `a2a send` **非幂等**（重试会重复建任务）；② 回执全文要 `--full`（默认截 60 字，artifact 亦然）；③ 派活必须带**精确文件路径**（纪律 §14：`先读 coop/shared/方案-X.md（头部状态块 + §N）→ 实施；找不到再翻 coop/shared 与 a2a/comms 历史`），涉及文件加 `--file`；④ `prune / uninstall / forget --apply` 与 `requires=delete` 的认领，还要**人签一次性票** `authorize <动作> --peer X --apply`（300s 失效，**agent 不代签**）；`brake --on` 全组急停（写操作全拒、只读不受影响）。
> **组外留言**：其他工作空间要对小组发声，只跑 `comms send --to 全员 --title "[外部·<窗口名>] 摘要"`（`wbsync tell` 是它的一键版），**只发不碰**（禁 `a2a` / `lock` / `reserve` / 写协作文件），见纪律 §12。
> 传输全走 Syncthing 文件同步，零端口零额外安全面；细则 `@references/legacy-comms.md`、`@references/verify-and-closure.md`。

---

## 常用命令（参数细节与完整表见 `@references/commands.md`）

- **入口**：`do "<一句话意图>"`（不知道跑哪条就问它；纯关键词、不调模型、只建议）
- **自检**：`wake [--apply]`（开场必跑）· `brief` · `plan` · `doctor` · `verify` · `status` · `folders` · `id`
- **搭建**：`install [--from-file|--mirror]` · `setup` · `pair <ID>` · `mailbox` · `start` · `stop` · `newdisk` · `autostart` · `hook`
- **工作空间**：`workspace init|seed|list|grant|revoke|log|cleanup|hide`（`seed` 补种占位会话让左侧可见，老机各跑一次+重启生效；`init` 带旧根守卫、同名不同根即停、遇冲突副本显式告警）
- **护城河**：`guard on|off|status`（trashcan 版本控制，删了可找回；**每机各跑一次**，`--apply` 写 config.xml）
- **防冲突**：`ignore on|off|status`（排除本机私有元数据，从根上不产生冲突副本；**每机各跑一次**）
- **对端**：`accept-peer --device <ID> [--apply]`（重入机反向配对）· `prune --peer <后缀> [--apply]`（清已退机痕迹）· `registry-order [--apply]`
- **自愈 / 分发**：`expert` · `fixnames` · `package [--zip|--scope|--slim]` · `archive-doc <文件> [--apply]`
- **网络**：`network config|test-relays|diag`
- **通道**：`a2a send|check|update|claim|release-claim|list`（`--priority P0-P3`、`--requires delete`）· `authorize <动作> --apply` · `brake --on|--off` · `comms` · `mem read|cursor|index`
- **看板**：`board [show|write|render]`（每机只写自己那份 `board/<后缀>.json`；`render` 出 `机群看板.md/.html`，仅主维护机=序号最小的在册机，`wake` 自动自愈；判活 ≤1h 🟢 / 1–2h 🟡 / >2h ⚪，**>1h 才变灰**）
- **互斥 / 交接**：`lock` · `wguard`（同机防覆盖）· `reserve claim|verify|release|status`（跨机预约，带 `fence` 序号）· `handoff` / `pickup`（指针交接）· `sessions` · `dialogue`（`dialogue-hook` 注册对话框生命周期 hook）
- **退出**：`uninstall [--apply] [--by A|本机] [--memory delete|archive]`（两阶段、仅 A 或本机可发起；流程见上节）

全局 `--json` 输出机器可读结果；写盘类命令**默认演练**，加 `--apply` 才落盘。

---

## 红线

1. **未确认不写盘**（默认演练，用户确认后才加 `--apply`）。
2. **不要断言「界面没报错就是好了」**——登记层故障是静默的，以登记文件实际内容为准。
3. **不碰内置插件**（`isBuiltIn`、`.app/`、`Program Files` 下的路径）。
4. **目标目录不存在时不硬写路径**，先报出来让用户解决文件层。
5. **不要声称证书接受能自动化**，必须用户手动完成。
6. **修改用户文件前先备份**（工具已自动备份，还原用同目录 `*.wbsync-bak-*`）。
7. **不绕过 guard 闸门**：5 个同步夹应长期开着 trashcan 版本控制，删除才可恢复；不要 `guard off` 关掉它、也不要清 `.stversions` 来绕过。
8. **skill 代码全局单写者（用户强条 · 2026-10-16 拍板）**：同一时刻**全集群只允许「一个机器的一个对话」**修改本 skill（`~/.workbuddy/skills/workbuddy-sync-all/`）。**同机**：改 skill 时写护栏会**自动认领**一条 `scope=skill` 的编辑锁（TTL 20min，零摩擦，无需手动 claim）——他人已持锁则**直接拦截写入**并报持锁人（`wbsync-write-guard.sh` / PreToolUse）。**跨机**：`wbsync reserve claim <skill 内目标文件> --reason "改 skill"`（随 `coop/shared` 同步可见）。**已被持锁 / 已预约 → 停手**（等人放下或改走其成果），**严禁并行改**（同机=静默覆盖、跨机=生 `.sync-conflict`）。手动释放：`wbsync lock release` / `wbsync reserve release`。

---

## 参考文档 · 自测（按需查）

`onboarding.md` 新机引导 · `commands.md` **完整命令表** · `cli-flows.md` 安装/作业流程 · `workspace.md` 同步盘与工作空间 · `uninstall-flow.md` 退出卸载 · `legacy-comms.md` 记事本读位 · `verify-and-closure.md` 自检闭环 · `platform-matrix.md` 平台差异 · `registration-layer.md` 登记层 · `troubleshooting.md` 疑难排查

```bash
python3 scripts/check_skill.py       # 改 frontmatter 后必做（查 YAML 元字符）
python3 tests/test_wbsync.py --list  # 改脚本后先看节号；--section N 只跑受影响节
python3 tests/test_wbsync.py         # 收尾整跑，不需要真实 Syncthing
```
