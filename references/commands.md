# 完整命令表

> 从 `SKILL.md` 下沉而来（主文件只留常用命令，降触发时的加载成本）。
> 全局参数：`--json` 输出机器可读结果。**写盘类命令默认演练，加 `--apply` 才落盘。**

## 自检 / 状态（只读）

| 命令 | 说明 |
|---|---|
| `do "<一句话意图>" [--json]` | **意图分发（入口层 · #18）**：把自然语言意图映射到该跑的命令——**纯关键词表（18 条规则），绝不调模型、绝不执行、绝不写盘**，只给一条建议 + 要点 + 备选。命中不了就退回 3 条兜底候选（`status`/`board`/`troubleshoot`）并明说"不做语义猜测"。不带参数 → 列出全部规则（终端版决策树）。**目的：记不住 53 条命令时只记这一条** |
| `wake [--peers B,C] [--apply]` | **一体化唤醒摘要**（本机 + 同步健康 + A2A 待办 + 未读 + 对端状态卡片，≤20 行）。**待配对自检**：扫描待处理 A2A 任务的 `params.deviceID`，若对端已声明、本机设备表却没有（重入换 ID 场景）→ 末尾输出一行 `⚠️ X 机声明新设备ID … → wbsync accept-peer --device <ID> --apply`；`--apply` 直接自动完成反向配对（幂等）。常态**0 额外行**，`--json` 增 `pending_peers` 字段；新 ID 取自任务参数、**不读资料库登记表**（token 安全） |
| `brief [--json]` | 精简状态摘要（引擎/对端/A2A 待办/同步，输出 ≤10 行）。**超阈值时另给两行省 token 提示**：①「本对话上下文 X MB，建议收口另起新对话」（`≥5MB` 提示、`≥20MB` 明确要求；体量由心跳随每轮 stat 记入对话记录，零额外开销）；②「日志偏大可蒸馏」（单份每日日志 `≥50KB`） |
| `plan` | 现状体检并给出下一步（只读） |
| `status [--json]` | 看本账号身份 + 是否首次使用（只读，判断走不走引导） |
| `identity [--json]` | 看本账号 WorkBuddy identityKey（区分同人多机 vs 陌生人） |
| `probe [--json]` | 探测本机是同步组第几台（后缀 A/B/C/D，只读不写盘） |
| `registry [--suffix X] [--occupied A,B] [--json]` | 输出登记本机所需信息（登记即命名，含自动算好的编号） |
| `folders` | 列出同步夹及注册状态 |
| `id` | 显示本机设备 ID |
| `listdisks` | 列出登记表里的同步盘 / 工作空间 |
| `netinfo` | 采集本机网络信息（ipconfig/ifconfig 原文 + IP/网关/掩码/网卡类型/全局 IPv6） |
| `doctor` | 全面体检（含 hook 状态 + 对端设备残留检测） |
| `verify` | 连接后自检（连接 / 工作空间 / 同步三查，供主节点判断） |
| `sessions [--ws <路径>] [--limit N]` | **列出本机并行对话 + 跨机活跃对话框**（含机器 / 世代 / 主题）：WorkBuddy 会话上下文不共享、但记忆文件共享，同机并行对话会写同一批文件。命令把「还有哪些并行对话」摊开，自动标出「本对话」（`CLAUDE_SESSION_ID`）；并读 `coop/shared/.dialogue-registry/` 列出其它机器的活跃对话框（随 Syncthing 同步）。`--ws` 指定工作空间（默认当前），`--limit N` 显示最近 N 个（默认 8）。纯本地读取，不依赖联网 |
| `dialogue register --topic "<主题>" [--locked 文件…]` / `dialogue leave` / `dialogue ls [--json]` | **跨对话框协作身份**：每个对话框在 `coop/shared/.dialogue-registry/<id>.json` 写一条记录（机器 / 世代 / 主题 / 是否开着 / 正在改的文件），随 Syncthing 同步 → 任意机器·任意世代可见。`register` 宣告自己在做什么（协作信号，别人能避开同文件冲突）；`leave` 标记为「已关闭」（保留记录供回溯）；`ls` 按**开着 / 已关闭**分组展示。`wake`/`brief` 会自动登记一次 |
| `dialogue-hook on\|off\|status [--apply]` | **注册对话框生命周期 hook**（`SessionStart` / `UserPromptSubmit` / `SessionEnd`）——让「开着 / 已关闭」由**显式事件**判定，而不是猜文件时间。`SessionStart`→open、`SessionEnd`→closed（带 close_reason）、`UserPromptSubmit`→保活；未收到关闭事件的按时间兜底显示「疑似已关闭」。脚本**完全静默、恒 exit 0**（不污染上下文、不影响会话）。**每台机器各注册一次** |

## 同机编辑锁（防静默覆盖）

| 命令 | 说明 |
|---|---|
| `lock [status]` | 看当前同机编辑锁（持锁者 / 用途 / 剩余分钟），自动标出本对话。锁文件 `~/.workbuddy/.wbsync-edit-locks.json`，机器本地、带 TTL 自动过期（防卡死） |
| `lock claim "<用途>" [--ttl 30] [--force]` | **占锁**：改**共享文件**（记忆 / skill 代码 / coop 文档）前先占锁，避免同机多对话**静默覆盖**（后写盖先写、无冲突副本）。他人持锁时拒绝并提示（退出码 2）；确认无冲突再 `--force` 强占。`--ttl` 锁自动过期分钟数（默认 30） |
| `lock release` | 释放本对话占的锁 |

> 配合 `wbsync dialogue register --topic "<主题>"` 在改共享文件前**宣告自己在做什么**——别人在 `sessions` / `dialogue ls` 里能立刻看到「谁在动哪份文件」，提前避开同文件冲突。**登记是可见性、锁是互斥、纪律是主干，三者互补**（详见 `.workbuddy/memory/DETAIL.md`「跨对话框协作身份」）。

## 跨机器互斥 + 指针式交接（防 A/B/C 同时改同一份）

> `lock` 是**单机本地锁**（不同步），只防同机多对话；**防不了两台机同时改**。跨机用写进 `coop/shared`（随同步可见）的共享预约。**文件类型无关**——媒体 / 文档 / 代码 / 工程文件同理。

| 命令 | 说明 |
|---|---|
| `reserve claim <文件> [--ttl 30] [--reason …] [--force]` | **跨机器文件预约**：改任意共享文件前先占。他人（他机 / 他会话）持有效预约时拒绝（退出码 2），确认无冲突再 `--force` 强占。记录写 `coop/shared/.file-reservations.json`。**默认 30 分钟**（2026-10-18 由 60 下调：机器会随手关，长 TTL 会把文件锁死）。**claim 会打印 `fence=N`（单调递增序号）**：续租不涨、被接管才 +1。**主键 = 跨机可比路径**：① 在同步根内 → 相对同步根的路径；② 否则在主目录内 → `~/…` 相对键（如 skill 代码 `~/.workbuddy/skills/workbuddy-sync-all/…`，三机同构，因此「A/B/C 同时改 skill 代码」也有互斥）；③ 都不在 → 绝对路径（仅本机可见，无法跨机比对） |
| `reserve verify <文件> --fence N` / `reserve release <文件> [--fence N]` | **写前复核 / 释放**。`verify` 用 claim 拿到的序号复核「锁是不是还是我的」：过期或已被接管（fence 变了）→ 退出码 2，**别再写，重新 claim**。`release --fence N` 可防「自己早被接管却把别人的锁删了」。查看：`reserve status [文件]`（含持有机器·会话·剩余分钟·fence） |
| `handoff <log_ref> --summary "TL;DR" --to B [--file <共享文件>] [--state …]` | **指针式交接**：生成 A2A 任务（`skill=handoff`），只带 TL;DR + `log_ref`（`<相对同步根路径>::<标题>`），**自动 reserve 目标文件**。重内容留在你的日志（已随 Syncthing 同步），协调通道只带摘要 + 指针 |
| `pickup <任务ID>` | **接手**：读 handoff 摘要、跳转到 A 的日志段（按标题取段）、**接管该文件预约** |

> **自动释放 / 续期**：`SessionEnd` 自动释放本对话全部预约（防死锁）；对话心跳（`UserPromptSubmit`）自动续期本对话预约（长任务不中途失效）。

## 写护栏（PreToolUse 硬闸门）

| 命令 | 说明 |
|---|---|
| `wguard on [--apply]` | 装护栏脚本到 `~/.workbuddy/scripts/`，并写入 settings.json 的 `hooks.PreToolUse`（**每台机器各一次**） |
| `wguard off [--apply]` | 从 settings.json 移除写护栏（保留其他 hook） |
| `wguard status` | 看脚本是否已装、hook 是否已写入、脚本是否过期 |

**判定（三条全中才阻止）**：① 工具是 Write / Edit / MultiEdit / NotebookEdit；② `file_path` 落在协作共享区（`~/WorkBuddySync/workspaces/*/`、`~/WorkBuddySync/coop/shared/`、skill 目录；可用环境变量 `WBSYNC_SHARED_ROOTS` 追加）；③ 存在**他人**持有且未过期的编辑锁 **或跨机预约**（`coop/shared/.file-reservations.json` 里他机/他会话持有效预约，主键=相对同步根路径）。否则**静默放行**（exit 0、零输出）。

**实测（A 机）**：他人持锁时 Write 被真实拦下、原因回传 Agent；无锁时正常写入；放行路径在会话记录中**零残留**（token 成本 0）。

**延迟（配对实测，本机）**：锁与预约**都为空**时 **约 6ms/次**（= bash 进程启动地板价；同步根从 `config.json` 用 bash 内建读取，无子进程，逻辑开销≈0）；**有锁记录（或他机预约）**时约 **133ms/次**（多做一次 python 精判）。前者是常态——锁只在「改共享文件」期间短暂持有。判定分三层 bash 内建快筛（非写文件工具 → 不在共享区 → **无锁且无预约**记录），只有第三层不满足才起 python。

**前提**：共享区根 = 本机配置 `~/.workbuddy/sync-assistant/config.json` 里的 `sync_root`（**与 `reserve` 同源**，默认 `~/WorkBuddySync`）——所以非默认同步根同样受保护；另有 skill 代码目录 `~/.workbuddy/skills/workbuddy-sync-all` 恒为共享区。同步根之外还要保护的自定义目录，用环境变量 `WBSYNC_SHARED_ROOTS`（冒号分隔）追加，否则漏保护（方向是 fail-open，不会误拦）。

**会话身份（唯一出口）**：本对话标识统一由 `wbsync.py` 的 `_session_id()` 产出——`CLAUDE_SESSION_ID` → `WB_SESSION_ID` → `WORKBUDDY_SESSION_ID` → 稳定缓存文件 `~/.workbuddy/.wbsync-dialogue-id` → 生成 uuid。`lock` / `reserve` / `dialogue` / 护栏的「自己」集合**共用同一口径**，避免「只注入 `WORKBUDDY_SESSION_ID` 时自己把自己拦下」；退化值不依赖 `pid`，同一对话的后台任务 / 子 agent 跨进程仍同一身份。

**安全特性**：任何异常一律放行（fail-open），不会因护栏故障瘫痪文件写入；自己的锁 / 自己的预约不拦；锁/预约过期自动放行。豁免 `.dialogue-registry`（心跳）、`.stversions`、锁文件、`.file-reservations.json`（预约台账自身）、护栏脚本自身。

**已知限制**：只拦 agent 工具写入，**不拦**用户在编辑器里的手工修改、也不拦 Bash 内的 `sed -i`。

## 安装 / 配对 / 同步

| 命令 | 说明 |
|---|---|
| `install [--apply] [--force] [--from-file <包>] [--mirror <前缀>]` | 装 Syncthing（自动选包、校验 SHA256）。**新机加速三招**：① **优先本地/内置包**（有 `vendor/` 命中即零下载）；② `--from-file <syncthing-*.zip>` **用本地包离线装**（零下载，最快——可从 U 盘/资料库/另一台机拿）；③ `--mirror <前缀>` **镜像加速**（GitHub 慢/被墙时用；也可设环境变量 `WBSYNC_DL_MIRROR`）。在线下载时 **macOS 优先下「架构专用」小包**（约 universal 的一半）。**健壮性**：`api.github.com` 被墙/超时时**自动退回 `github.com` 网页重定向**解析版本号并拼出官方下载 URL；下载**失败自动重试 3 次**，半截包（字节数不符）判失败重试 |
| `setup [--apply]` | 建同步夹并注册 |
| `pair <ID> [--name N] [--apply]` | 手动配对；自动清旧对端 + 共享所有已注册夹（含工作空间） |
| `forget [--apply]` | 清除 config 记的对端 ID + Syncthing 设备表旧条目（换对端一键清理） |
| `mailbox put\|get [--apply]` | 通过 wb-coop 信箱交换 ID（跨互联网） |
| `start` / `stop [--apply]` | 启停 Syncthing |
| `autostart status\|on\|off [--apply]` | 开机自启（launchd / 注册表 / systemd） |
| `hook on\|off\|status [--apply]` | 随 WB 会话启停同步（装脚本 + 写 SessionStart hook）。**可选便利**：核心同步靠 `autostart` 系统自启，hook 只额外提供「开/关 WB 时启停 Syncthing」；`on --apply` 后只需在 WB 输入 `/hooks` 点一次【确认】（不想确认可直接跳过，不影响同步）。脚本更新后重跑 `hook on --apply` 即可刷新、无需重新确认（`status`/`doctor` 会提示「脚本已过期」） |
| `fixnames [--min-age N] [--dry-run]` | 补改名兜底：把 Syncthing 未能改名的 `.syncthing.*.tmp` 改成正式文件 |
| `package [--apply] [--zip <路径>] [--scope skill\|skills] [--include-versions] [--slim]` | 打包/分发前清理设备残留（sync-conflict 副本、wbsync-bak、`_user_meta.json` 的 installedAt）；**删除走系统回收站**（可恢复），不可用时降级到 `~/.wbsync-package-trash-*`。**`--zip` = 清理后直接打干净包**（排除残留 / `__pycache__` / `*.pyc` / `.DS_Store` / `.git` / 本机 `_user_meta.json`，打印文件数·大小·SHA256）；**存在文件级残留且未 `--apply` 时拒绝打包**。`--scope skill`（默认）只扫本 skill 目录；`--scope skills` 扫**整个 skills 夹**（清其它 skill 目录里的历史残留，如 `_skillhub_meta.sync-conflict-*`）。默认**跳过 `.stversions/`**（guard 的恢复仓库），`--include-versions` 才连版本一起清。**`--slim` = 精简包**：额外排除 `vendor/`（内置 Syncthing 离线二进制 ~34MB）→ 包体约 **0.2MB**，用于「上传有体积上限」（如工作台 3MB 限制）的分发；二进制由 `wbsync install` 自动下载，已装好的机器直接用本包覆盖代码即可 |
| `ignore on\|off\|status [--folder <夹ID>] [--all-folders] [--apply]` | **冲突根治 / 机器私有元数据排除出同步**：`_skillhub_meta.json`（含本机绝对路径 `iconLocalPath` + `installedAt`）、`.DS_Store`、`__pycache__`、`*.pyc` —— 这些三机互写会**持续**刷出 `*.sync-conflict-*` 副本（2026-10-15 C-g2 实测 skills 夹 20+ 个）。忽略 = 不传 → 从根上不冲突；清理残留只是治标。默认作用于 `~/.workbuddy/skills` 所在夹（自动定位），`--folder` / `--all-folders` 可指定。默认演练、`--apply` 落盘；`.stignore` **每台机本地**，三机各跑一次 |
| `mark-initialized [--apply]` | 把本账号标记为已完成初始化（首次引导后调用） |

## 网络（中继优先 / AP 隔离）

| 命令 | 说明 |
|---|---|
| `network config [--apply]` | 配置中继 + 全局发现（写免费中继池，中继优先连接） |
| `network test-relays` | 测试内置免费中继的可达性 |
| `network diag` | 诊断 AP 隔离 / 中继 / 全局发现状态 |

## 专家 / 连接器 / 工作空间

| 命令 | 说明 |
|---|---|
| `expert [--apply]` | 专家/连接器登记层自愈 + 版本漂移检查 |
| `newdisk [--name 备注] [--location 目录] [--apply]` | 新建文件同步盘（统一 A/B/C/D 后缀，位置首次询问后记住） |
| `workspace init [--path 目录] [--reuse-existing] [--force-root] [--apply]` | 首次使用自动分配后缀 A/B/C/D 建工作空间。**v2.8.0 守卫**：缺 `--path` 时**保留已记住的根**（不再无条件打回 `~/WorkBuddy`）；检测到「同名不同根」→ **停止 / 不写盘 / 退出码 2**（`--reuse-existing` 沿用旧目录、`--force-root` 强制用当前根、或显式 `--path`）；落地自动补 `.stfolder` |
| `workspace seed [--path 目录] [--apply]` | **补种占位会话**（幂等）：给已建好的工作空间补一条空会话，使其出现在客户端**左侧空间列表**。**为什么需要**：左侧列表按**会话**分组，客户端 `filterTaskBlocks()` 只保留「有会话」的分组（空分组仅当 `kind==='project'` 且搜索结果命中时才入列）→ 没有会话的空间只在输入框下拉里可见、左侧不显示。⚠️ 属**客户端未公开接口**（写 `workbuddy.db` 的 `sessions` 表），客户端升级可能失效 → 全程 **fail-open**，绝不阻断；生效需**重启一次客户端**；占位会话被删则空间再次隐藏（重跑本命令补回）。老机各跑一次；`workspace init` 末尾也会自动种。**细节（v2.10.14 据 C/D 复审加固）**：① 幂等判定**排除 `archived`**——客户端「归档」只改 `status` 不动 `deleted_at`，但普通列表过滤 `archived` 行，若只判 `deleted_at`，用户归档掉空间最后一条会话后空间会消失、而 seed 还判「已有会话」拒绝补种；② 状态**探测本机库真实取值**（优先 `completed`/`idle`，避开进行中值；写死任何值都可能在用户按状态筛选时让空间二次消失）；③ 路径**归一化**比较（尾斜杠/软链/大小写差异不重复种）；④ `--path` **只允许指向本机工作空间**（越界拒绝，防在本机凭空造出他机分组） |
| `workspace list` | 列出所有机器的工作空间 |
| `workspace grant <后缀> [--apply]` | 授权某台机器可写你的工作空间（约定层） |
| `workspace revoke <后缀> [--apply]` | 收回某台机器的写权限 |
| `workspace apply-grants [--apply]` | 按登记表重置本地各工作区目录的读写权限 |
| `workspace log` | 查看工作空间变更记录（谁、何时、改了什么） |
| `workspace cleanup [--apply]` | 清理平台遗弃的「时间命名空壳目录」（如 `~/WorkBuddy/2026-10-13-18-48-15`）**+ `workbuddy.db` 里时间戳命名的残留登记**（UI 工作空间下拉里删不掉的「时间文件夹」）。空壳只认严格时间戳命名且为空的目录；db 只删时间戳命名记录，绝不误删 `sync-all-X` / 用户项目（如 `Claw`）。默认只列，`--apply` 才落盘（删 db 前自动备份）；db 有 WAL 锁用 busy_timeout 重试；**清后需完全退出并重开 WorkBuddy**，下拉才刷新 |
| `workspace hide [--apply]` | 隐藏本机磁盘上的 A/B/C（及后续 D…）工作空间目录。封装各平台可靠隐藏法：macOS `chflags hidden`、Windows 用 PowerShell 对 `Attributes` 位或 `+h`（绕开 Git Bash `attrib +h` 中文编码/沙箱导致的「看似成功实则不生效」坑）。默认只列，`--apply` 才落盘 |
| `guard on\|off\|status [--apply] [--cleanout N]` | **Syncthing 版本控制硬闸门（护城河）**：给 5 个同步夹开 trashcan 版本控制，任何删除（含误删、三机传播删）都进 `.stversions` 保留 N 天（默认 30）可找回，从根上堵住「删了就没」的不可恢复风险。**不必拆夹、不必锁权限位、也不依赖平台 sandbox 规则**（后者实测会被平台重写、不持久）。写入 Syncthing 自己的 `config.xml`（可持久、平台不覆盖）。`--apply` 才落盘；config.xml 不在同步夹内，**三机各跑一次**。`off` 关闭、`status` 看状态、`doctor` 提示未启用 |
| `uninstall [--apply] [--by A\|本机] [--memory delete\|archive] [--purge] [--wait N]` | **退出机制 / 卸载**：让本机干净退出协作组。**发起权限：仅 A 机或本机**（`--by`；B 不能远程踢 C，否则报错退出）。**两阶段**——**阶段一（同步仍开启，只做需传播的事 + 确认送达）**：① 发退出通知（A2A + 记事本）② 归档长期记忆/日志到共享区（若 archive）③ **清本机自己在协作区的产物——同步仍在运行，删除会传播到其他机器一并清除**（只删本机私有区/专线/能力卡，对端自写文件不动）④ **等送达**（触发 rescan → 本机 idle → 在线对端完成度 100% → 宽限）；**阶段二（本机收尾，不传播）**：⑤ **注销 hook 注册并移走 hook 脚本**（双保险：仅注册残留时，脚本没了也不会把引擎拉回、生成新身份）→ 停自启/停引擎脱组 ⑥ 资料库自己的登记 doc **移入「已退出机器登记」归档子文件夹**（无删除 API，但支持 move-node——「移走」即从主登记清出去；该归档子文件夹由 skill **自动建好**（首次 init 时，用 manage/create_folder.py 建，找不到则自动创建）） ⑦ 清理本地同步基础设施（Syncthing 配置/plist/同步夹元数据，`--purge` 再加二进制；**hook 脚本仍在则跳过清配置**，避免生成新身份）⑧ **收尾自检**（hook 已注销 / 脚本已移走 / 引擎已停）+ 打印降级暂存位置与恢复方法。**幂等**：已退出过的机器重跑会提示「已退出」而非硬失败。**序号复用靠世代区分**：退役写 `retired[]`（世代/设备ID/时间），继任者占同序号世代+1（C→g2/g3）；A2A 任务带 `fromDevice/fromGen`、归档目录名带 `genN`。长期记忆/日志运行时询问：`delete` / `archive`（非交互默认 archive）。默认演练，删除走系统回收站 |
| `prune --peer <后缀> [--apply]` | **对端侧收尾**：清理某台已退出机器**在本机**的痕迹——① 协作区残留（其私有区 / 专线 / 能力卡；同步态删除会传播给其他对端一并清，兼作 uninstall 未送达时的兜底）② 登记表把该槽位移入 `retired` ③ 从 Syncthing 设备表 + 各夹授权移除该设备。退出机无法远程改对端的设备表，所以收到他机退出通知后，**各机自己跑这条**。默认演练 |
| `accept-peer --device <完整ID> [--name X] [--apply]` | **反向配对 / 对端入组**：对端（如重装后的新机、换了 device ID 的机器）已把你加入它的设备表并共享夹，但你这边还没有它时——把对端完整 deviceID 加入本机 Syncthing 设备表，并共享**所有**同步夹。幂等、默认演练。**`wbsync pair` 已收敛为等价的幂等语义**（v2.7.9 起不再写 peer_id、不再删任何对端），配对统一走 REST、多机安全 |
| `registry-order [--apply]` | **重排资料库「多机同步登记」次序**：README（置顶）→ 现役机登记（按后缀 A/B/C…）→ 其它 doc → 归档文件夹「已退出机器登记」（置末）。只调顺序、不增删节点；幂等（已有序时不发起任何移动）。**每台新机写完自己的登记 doc 后跑一次**即可保持列表整齐；`workspace init` 与 `uninstall`（归档登记 doc 后）都会自动调用。默认演练 |
| `archive-doc <文件名> [--reason …] [--into …] [--apply]` · `archive-doc --list` | **协作文档归档**：把 `coop/shared/` 里**已消化 / 已闭环 / 已作废**的文档移入 `coop/shared/历史协作/` —— ① 文件头插归档标记（`<!-- wbsync-archived -->` + 原因 / 消化去向 / 归档机 / 原位置）② 移动 ③ 在 `历史协作/README.md` 追加登记一行。**不删除**（判断错了能翻回来）、**幂等**（重跑报「已归档」不报错）。**例外不归档**：`机器间沟通.md`（冻结兼容文件，代码仍在读，命令直接拒绝）、各机活专线、`机器间沟通-归档.md`、`a2a/` 与 `工作空间登记.json` 等活数据。规则见 `coop/shared/纪律-协作行为.md` §11 |

## 机器间通道

| 命令 | 说明 |
|---|---|
| `a2a card --apply` | 发布/更新本机能力卡（Agent Card） |
| `a2a send --to B/any/全员 --skill verify --do "一句话要做的事" --text "…" [--file <路径>] [--priority P0-P3] [--requires none\|write\|delete]` | 委派任务（要回执的通道）。**`--to` 三种语义**：`B` 指定机；`any` = **投池**（谁空闲谁认领，账上记 `to="*"`、显示 `A → 池`；**创建机不参与认领**，且任务被某机认领后**别机的 `check` 就不再显示它**）；`全员` = 广播（人人都当待办）。⚠️ **非幂等**：命令重试会产生重复任务。**必须带指针（纪律 §14）**：`--text` 写**精确文件路径**，模板 `【<动作>】先读 coop/shared/方案-X.md（头部状态块 + §N）→ 按之实施；找不到再翻 coop/shared/ 与 a2a/comms 历史`；涉及文件用 `--file`。别把整篇塞进 `--text`（对方默认只见前 60 字）。**风险分级（P1）**：`--priority`（P0–P3，默认 P3）只影响排序/提示；`--requires delete` 才触发放行票闸门。**`--do`（推荐）= 一句话「要对方做什么」**（≤30 字中文）：看板任务概况与 `a2a check` 的「▶ 要做」行直接显示它——`--text` 通常写背景/指针，读不出「干什么」 |
| `a2a retag --id <ID> --do "一句话"` | 给**已派出**的任务补写「要做的事」（append `retag` 事件，**不改状态、不重置超时计时**）。用于存量任务正文只交代背景、看板上读不出要干什么 |
| `a2a check` / `a2a list [--full]` | 看待我处理 / 待对方确认。**按 priority 排序（P0 最前）**并标 🔴P0/🟠P1/🟡P2；`requires=delete` 会提示「认领前需 authorize 票」。默认截断消息 60 字（被截断会**显式提示**共多少字）；`--full` 显示消息与**产物（artifact）全文** |
| `a2a update --id <ID> --state completed --text "…" [--artifact "k=v"]` | 回执（状态机：submitted → working → completed / **rejected** / canceled；`completed` 可被打回） |
| `a2a update --id <ID> --state rejected --reason "≤100字"` | **打回**（派活方发起，**必带理由**，完成也能被打回）。自动**交回认领**（原执行方失去写权，fence+1） |
| `a2a update --id <ID> --state rework` | **接受打回、正在重做**（挨打回方发起；同一任务 id 内演进，`attempt`+1，上限 3 轮；自动重新认领） |
| `a2a claim --id <ID> [--ttl 600]` | 认领（一任务一对话）。`requires=delete` 的任务认领前需人签放行票（见下） |
| `authorize <动作> [--peer X] [--ttl 300] --apply` | **高危动作放行票（P1）**：动作 = `prune` / `uninstall` / `forget` / `a2a-task`。指纹 = sha12(动作\|对象)，票在 `coop/shared/.a2a-auth/<指纹>.json`，**TTL 300 秒、一次性**（执行即删）。默认只演练。**必须由人在终端签，agent 不代签** —— `--apply` 只防手滑，票才防「自信地误判」 |
| `brake --on` / `--off` / （无参=状态） | **STOP 急停（P1）**：`--on` 写 `coop/shared/.a2a-auth/STOP`，经 Syncthing 秒级传播全组 → A2A 写操作（send/claim/update/release-claim）与 `prune/uninstall/forget --apply` 全部拒绝；**只读不受影响**（wake/brief/a2a check\|list/board/brake）。`--off` 解除。无参看状态 + 未消费票 |
| `board [show\|write\|render] [--json] [--force]` | **机群看板**（#15）：`show` 纯读现算终端视图；`write` 发布本机快照 `coop/shared/board/<后缀>.json`；`render` 渲染 `机群看板.md` / `.html`（**仅主维护机**=登记表里序号最小的在册机，现为 A；A 退役自动顺延）。**主维护机每次 `wake` 自动建立/刷新共享视图**（删了也自愈）。事件驱动、不心跳刷盘；**≤1h 🟢 / 60–120min 🟡 / >2h ⚪ 疑似离线**；页面自曝「数据截至」，**>1h 才变灰** |
| `comms send --to B --title … --body … [--status …] [--reply-to …]` | 留一条留言（要人读的通道；`--to 全员` 广播） |
| `comms check` | 看待我回复 / 待对方确认的留言，末尾附未读概览 |
| `comms read [--peer X] [--all] [--no-mark]` | 只输出未读并自动推进读位 |
| `comms cursor [--mark] [--peer X]` | 看/推进「本机读到各机专线的位置」 |
| `comms cleanup [--apply]` | 归档已闭环的旧留言（移动而非删除） |
| `mem read [--peer X] [--all]` | memory 日志**增量读**：只输出对方当日日志新增的 `##` 段 |
| `mem cursor [--mark] [--peer X]` | memory 读位看位 / 置零 |
| `mem index [--peer X] [--days N] [--logs-only] [--titles N] [--json]` | memory **全貌索引**（只读）：把（本机+各对端）`memory/` 渲染成「文件×日期×距今×档位×段标题」地图，agent 先看地图再定向 `Read`，避免全量读大日志。档位由文件名日期现算（today/d3/d7/d30/older/longterm/state/legacy），不落盘、不推进读位；`--days` 只滤带日期条目、`--logs-only` 只要日志/归档、`--titles` 限每条目标题数、`--json` 机器消费 |

## 脚本（非 wbsync 子命令）

| 脚本 | 说明 |
|---|---|
| `scripts/simulate-new-machine.sh up\|status\|run\|down` | 用本机第二个 Syncthing 实例模拟一台新机器，演练接入（没有多余硬件时用）。详见 `references/onboarding.md` 末节 |
| `scripts/wbsync-autostart-hook.sh` | SessionStart 看门狗（由 `wbsync hook on --apply` 安装到 `~/.workbuddy/scripts/`） |

## 全局命令入口（让 `wbsync` 在任意目录可用）

skill 内脚本不在 PATH，排查时 `which wbsync` 为空曾被误判为「功能不存在」。已内置**跨机通用**包装（不写死 python 路径）：优先用 PATH 的 `python3`，否则自动扫描 `~/.workbuddy/binaries/python/versions/*`，A/C 用户名不同也适用。

| 文件 | 适用 | 启用方式 |
|---|---|---|
| `scripts/wbsync` | Mac / Linux（A、C 机） | `chmod +x` 后软链到 `~/.workbuddy/bin/wbsync`，并在 `~/.zshrc` 加 `export PATH="$HOME/.workbuddy/bin:$PATH"`（**新开终端**生效） |
| `scripts/wbsync.cmd` | Windows | 把 `scripts/` 加入 PATH，或直接从该目录跑 `wbsync.cmd` |

> `~/.workbuddy/bin/` 与 shell rc **不跨机同步**（且路径含用户名），B/C 各装一次。A 机已落地。
