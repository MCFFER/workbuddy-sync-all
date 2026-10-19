# 参考：同步范围 / 文件同步盘 / 多机协作工作空间

> 本文件是 `workbuddy-sync-all` 技能的参考文档，由 SKILL.md 拆出（内容与原文一致）。


**只要用户的话里透露出「想在两台电脑之间传文件 / 同步 / 建个共享盘」的意图，就立刻走建盘引导，**
**不要等用户说出精确命令。** 典型触发语：帮我同步、建个盘、弄个共享文件夹、两台电脑传文件、给我一个能两边同步的目录……

**同步盘与工作空间已合并为同一套机制**：两者共用 A/B/C/D 后缀命名与登记表（`工作空间登记.json`），
位置也统一由「盘根目录」管理。区别只在语义：工作空间强调「每台机器各自的空间 + 授权写」，
同步盘强调「两台电脑之间传文件」——底层是同一段代码。

**引导流程（AI 内部执行）：**

1. 先跑 `wbsync probe` 探明本机是同步组第几台、该分到哪个后缀（A/B/C/D，按加入顺序）；
2. 弹交互问用户「放哪个位置」，并在弹窗里说明：**「监测到你是第 N 台，默认同步盘名称是 sync-all-<后缀>」**（首次问一次，写入 config 的 `disk_root` 记住，之后复用不再问）；
3. 用户确认 → 跑 `wbsync newdisk [--name 备注] --location <目录> --apply`；
4. 建好后告知本地盘路径、共享目录，以及「对端机器也会自动对接」。

**命名规则**：统一 A/B/C/D 后缀，按加入顺序自动分配，无需手动指定。第一台 A、第二台 B、第三台 C……
每台机器分到自己的后缀，其盘的落地目录为 `<根目录>/sync-all-<后缀>`。**所有机器的这个盘共用同一个 Syncthing 同步夹 id `sync-workspace-all`**（这是跨机同步的锚点，A/B/C/D 各自本地目录名不同，但夹 id 统一，勿改）。

```bash
wbsync probe                              # 探测自己是第几台（只读，不写盘）
wbsync newdisk --apply                    # 本机自动分配后缀并建盘（位置首次问后记住）
wbsync newdisk --location ~/Data --apply  # 指定盘根目录（同时写入 config 记住）
wbsync listdisks                          # 看登记表里有哪些盘 / 工作空间
```

---


给「几台电脑各有一个工作空间、能互相看到在干什么、接着别人的活继续干」的场景用。后缀 A/B/C/D 按加入顺序自动分配，无需手动指定。

**每台机器都应自动建立自己的工作空间**：首次引导或对话式安装时，AI 应主动执行
`wbsync workspace init --apply`（先 `probe` 判断第几台），不要等用户手动敲命令。`init` 会自动：

1. 分配后缀（A/B/C/D，按登记表顺序）；
2. 建本地目录 `sync-all-<后缀>/`（含 `<后缀>/` 工作区 + `shared/` 公共区）；
3. 注册 Syncthing 同步夹 `sync-workspace-all`，**并自动把它共享给所有已配对的对端设备**
   （不再出现「B 机看不到 A 机工作空间」的情况）；
4. 登记到 WorkBuddy 客户端 `workbuddy.db` 的 workspaces 表，让它在「新建任务」下拉菜单里可见
   （写入失败时只警告、不阻断——那是便利功能，且需重启 WB 客户端后下拉菜单才刷新）。

**目录结构**（每台机器各自本地，根目录默认 `~/WorkBuddySync/workspaces`，可用 `--path` 指定）：

```
<根目录>/
├── sync-all-A/
│   ├── A/          # A 机工作区（其他机器可读、不可改）
│   └── shared/     # 公共协作区（所有机器可读写）
├── sync-all-B/     # 第二台，加入时自动分配
│   ├── B/
│   └── shared/
└── ...
```

> 注意：`sync-all-A`、`sync-all-B`… 只是各机器本地的目录名，**底层是同一个 Syncthing 夹 id `sync-workspace-all`**，因此内容会跨机双向同步到一致。

**权限是「约定层」**：`grant` 记录「谁被允许写我的空间」、`revoke` 收回，`log` 能查到谁何时改了什么（写进 `coop/shared/工作空间变更.json`，随 Syncthing 同步）。注意这是约定而非强制——Syncthing 本身是双向同步，无法在文件层强制「只读」。

```bash
wbsync workspace init --path <目录> --apply   # 本机分配后缀并建工作空间（自动共享对端+登记下拉菜单）
wbsync workspace list                         # 看各机器工作空间
wbsync workspace grant B --apply              # 授权 B 可写我的空间（约定层）
wbsync workspace revoke B --apply             # 收回
wbsync workspace log                          # 变更记录
```

---

