# 参考：以 A 为主的自检与问答闭环

> 本文件是 `workbuddy-sync-all` 技能的参考文档，由 SKILL.md 拆出（内容与原文一致）。


**目标**：建立连接后，由 **A 机（主节点）** 牵头做三重自检，发现问题就告诉用户 + 指派其他机器操作，直到检测全部通过；A 机给别的机器下指令，别的机器必须「回执」，形成问答闭环。

### 一、连接后自检（`wbsync verify`）

每次建立连接、新机器加入、或用户提到「同步没通/工作空间没建」时，**主节点（A 机）** 跑：

```bash
wbsync verify           # 人类可读的三重自检
wbsync --json verify    # JSON，供 Agent 脚本化判断
```

三查内容：

| 检查 | 看什么 | 不通过时 |
|---|---|---|
| ① 连接 | 每个对端是否 `connected` | 提示对方上线 / 检查网络 |
| ② 工作空间 | 登记表里每个 suffix（A/B/C/D）的本地目录是否建立 | 让缺的那台跑 `workspace init --apply` |
| ③ 同步 | `sync-workspace-all` 夹每个对端的 `remoteState` + `completion` | `notSharing` 让对方共享；未 100% 等它同步完 |

`verify` 返回非 0 表示有缺口，`--json` 输出 `issues` 数组（逐条待办）。

### 二、以 A 为主的分派与回执

- **A 机是主节点**：诊断、决策、给用户讲清楚、给其他机器下指令，都由 A 机牵头。
- **下指令 = 建任务**：A 机用 `wbsync a2a send --to B --skill <能力> --text "要做什么"` 把任务委托给 B 机（状态 `submitted`）。
- **别的机器必须回执**：B 机上线后 `wbsync a2a check` 看到发给自己的任务，执行完就 `wbsync a2a update --id <ID> --state completed --text ... --artifact ...`。
- **A 机跟进直到闭环**：A 机再 `wbsync a2a check` 时，「待对方确认」列表会显示自己发出、对方还没回的任务，A 机据此持续跟进，直到全部 `completed`。

### 三、任务状态机

任务状态依次流转：

```
submitted  →  working  →  completed / failed / canceled
（已委托）    （处理中）      （完成 / 失败 / 取消）
```

`wbsync a2a check` 会同时报告两类：

1. **待我处理**：别人发给本机、还没到终态的任务；
2. **待对方确认**：本机发出、对方还没完成的任务（主节点据此跟进）。

只有这两类都清空，才算这轮自检/协作真正闭环。

### 四、循环检测直到通过

主节点（A 机）的 Agent 在「建立连接 → verify → 发现问题 → 下指令 → 等回执 → 再 verify」的循环里，**每次用户打开对话或提到同步相关事务时都跑一遍 verify**，直到 `issues` 为空、`awaiting_ack` 为空，才停止提醒。

---

