# 参考：对话式安装 / 标准作业流程 / 区分自己人

> 本文件是 `workbuddy-sync-all` 技能的参考文档，由 SKILL.md 拆出（内容与原文一致）。


在对话里说一句「帮我在这台电脑上装同步」即可，全程不敲命令：

1. 自动跑 `wbsync plan`（只读），把「下一步要做什么」整理成确认清单；
2. 弹出确认（下载什么、建哪些目录、改哪些文件）；
3. 你确认后逐条自动执行：**资料库登记（自动交换 ID）** → `install` → `setup` → `pair`（自动配对）→
   `expert`（修登记层）→ `autostart`（自启）；
4. 只有两处需要你**手动点一下**（Syncthing 安全设计，无法自动化）：
   - 在 Web 界面 `http://127.0.0.1:8384` 点「接受」对方设备证书；
   - 首次配对时确认对方设备 ID。
5. **安装收尾时主动询问**（用 `AskUserQuestion`）是否启用「打开对话自启动同步」——见「首次引导」第 4.5 步。默认推荐启用，启用后需用户在 `/hooks` 确认一次（仅一次）。

> **关键**：文件同步过去 ≠ 专家/连接器能用。每台机器都要在本机跑一次
> `wbsync expert --apply` 重建登记，然后重启 WorkBuddy。漏了就会「装了但不出现 / 跑旧版本 / 界面不报错」。

---


按序执行。**所有会写盘的命令默认只演练，确认无误再加 `--apply`。**

```bash
# 1 看现状
wbsync plan

# 2 资料库登记（自动交换设备 ID、判断第几台，见「首次引导」第 1 步）
wbsync identity                  # 看本机账号 identityKey
#   由 AI 通过资料库登记文件夹自动完成配对，无需手抄 ID

# 3 装 Syncthing（自动按平台选官方产物、校验 SHA256）
wbsync install --apply

# 4 建同步夹并注册
wbsync setup --apply

# 5 配对（登记表里已有对端 ID 时自动；也可手动或用信箱）
wbsync pair <对端ID> --apply      # 手动配对（备用）
wbsync mailbox put --apply        # 或用 wb-coop 信箱跨网交换（备用）

# 6 修登记层（别漏，专家/连接器都要）
wbsync expert --apply            # 然后重启 WorkBuddy

# 7 配置网络连接（中继优先 + 全局发现，绕开 AP 隔离/被墙发现服务器）
wbsync network config --apply    # 写免费中继 + 开全局发现
wbsync network diag              # 诊断连接/AP 隔离状态

# 8 开机自启（macOS 用双击脚本，见「首次引导」第 5 步；Windows/Linux 可自动）
wbsync autostart on --apply      # Windows/Linux

# 9 体检
wbsync doctor
```

设备 ID 也可手动交换（同一局域网时更简单）：A 机 `wbsync id`，B 机 `wbsync pair <A的ID> --apply`，
再反向做一次。


同一互联网上可能同时出现多台机器的配对申请（甲的多台 + 乙的机器混在一起）。设备 ID 是「机器」指纹、
看不出主人是谁，所以本技能用 **WorkBuddy 账号 identityKey**（账号 UUID，同一个人登录同一账号则跨机器一致）
作为「人」的凭证来区分：

- `wbsync identity` → 看本机当前账号的 identityKey；
- `mailbox put` 写入设备 ID 时同时带上 identityKey；
- `mailbox get` 配对前比对：**同账号 → 自动配对；不同账号 → 标红提示、不自动连**，需用户用 `wbsync pair <ID> --apply` 手动确认才连。

这样「甲只连甲、不误连乙」默认成立；乙的申请会被拦下，除非甲明确手动放行。

> 说明：identityKey 读取依赖 WorkBuddy 客户端的私有状态（`~/.workbuddy/plugin-account-state/<uuid>.json`），
> 属「尽力而为」——读不到时（未登录/格式变化）`mailbox get` 会退回旧行为（提示但需人工确认），不会误连。

---

