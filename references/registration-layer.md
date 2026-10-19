# 登记层：机制与自愈规则

本文解释为什么"文件同步好了"不等于"专家能用了"，以及如何修。
这是本技能的核心认知，排错时优先读这份。

> 连接器（connector）与专家同理：两者登记在同一个 `installed_plugins.json`，
> 路径重建规则完全一致，下文说的"专家"同样适用于连接器。

---

## 一、为什么需要独立的一层

WorkBuddy 把"装了哪些专家"记在**本机的登记文件**里，
而这些文件里存的是**含用户名的绝对路径**。

实测样本（macOS，`~/.workbuddy/plugins/installed_plugins.json`）：

```json
{
  "version": 2,
  "plugins": {
    "ardot@workbuddy-builtin": [
      {
        "scope": "user",
        "installPath": "/Users/<you>/.workbuddy/plugins/cache/workbuddy-builtin/ardot/5.8.1-wb.41036298...",
        "version": "5.8.1-wb.41036298...",
        "installedAt": "2026-10-09T14:01:26.699Z",
        "lastUpdated": "2026-10-12T10:55:02.275Z"
      }
    ]
  }
}
```

该文件实测含 **41 条绝对路径**。Windows 上的同一条目形如：

```
C:\Users\alice\.workbuddy\plugins\cache\my-experts\demo-expert\1.0.0
```

把它直接同步到 Mac 上，路径必然不存在。而**界面不会报任何错** ——
表现为"专家列表里看着有，实际跑的是旧版本或干脆不生效"。

> 这是本类工具最危险的一类故障：**错误表现为静默**。
> 因此不要以"界面没报错"作为判断依据，要以登记文件的实际内容为依据。

---

## 二、涉及的登记文件

| 文件（相对 `~/.workbuddy/`） | 作用 | 是否需逐机重建 |
|---|---|---|
| `plugins/installed_plugins.json` | 插件/专家的安装登记与运行路径 | **是** |
| `plugins/known_marketplaces.json` | 市场来源与安装位置 | **部分是**（见 §四） |
| `plugins/configured-plugins.json` | 启用/停用清单，只有名称与作用域 | 否（无绝对路径） |

判断某个文件是否属于登记层，用一条准则：
**文件里是否出现 `/Users/<名>/`、`/home/<名>/` 或 `\Users\<名>\`。**
出现即是本机相关，跨机复制就会坏。

---

## 三、重建规则（核心）

**关键洞察：`installPath` 的尾部结构是机器无关的。**

```
<根>/plugins/cache/<市场名>/<专家名>/<版本>
```

注意上面三段的来源：
- `<市场名>` 来自条目的键 `名称@市场名`
- `<专家名>` 来自键的前半段
- `<版本>` 来自条目自身的 `version` 字段

**三者都已在文件里，且都不含用户名。** 所以完全不必解析旧路径 ——
直接按本机根目录重建即可。这带来两个好处：

1. 天然跨平台，Windows ↔ macOS ↔ Linux 任意方向都对
2. 顺手把路径分隔符修正为当前平台的形式（反斜杠问题一并解决）

对应实现见 `scripts/wbsync.py` 的 `heal_installed_plugins()`。

---

## 四、判定边界（哪些能改、哪些绝不能碰）

判据的方向很重要：**只排除「明确属于应用内部」的路径**，
而不是「只处理含 `/Users/` 的路径」。后者在非标准安装位置
（如 `D:\Apps\.workbuddy`）下，会把该修的条目误判成内置而跳过。

| 情况 | 处理 | 理由 |
|---|---|---|
| 路径含 `.app/`、`/Applications/`、`Program Files` | **不动** | 应用自带，与本机用户无关 |
| 路径已存在且是本机真实目录 | **原样保留**，也不算问题 | 它已经是对的。这条能挡住「版本号与目录名写法不一致」造成的误报 |
| 路径不存在，但能按规则重建出真实存在的目标 | **重建** | 跨机同步过来的路径需要改写 |
| 路径不存在，且重建目标也不存在 | **保持原样并报告** | 版本还没同步过来；硬写会造成"指向空目录"，比不修更糟 |
| 条目缺 `version` 字段 | 报告，不猜 | 无法可靠重建 |

**改动前一律备份**，备份文件名形如
`installed_plugins.json.wbsync-bak-20261012-194620`，与源文件同目录。

### 重建时要处理的写法差异

**登记文件里的 `version` 与磁盘上的目录名可能只差一个分隔符。**

实测样本（本机 `installed_plugins.json` 里的 `wind-finance`）：

| | 值 |
|---|---|
| 登记文件 `version` | `1.2.2`**`+`**`wb.78fa61451b4bf44c` |
| 磁盘目录名 | `1.2.2`**`-`**`wb.78fa61451b4bf44c` |

直接拿 `version` 拼路径会得到不存在的路径，从而**误报「无法重建」**。
所以精确匹配失败时，要把 `+` 归一成 `-` 再找一次，
且**只有唯一命中才采用** —— 避免在有多个相似目录时猜错。

对应实现见 `scripts/wbsync.py` 的 `_resolve_version_dir()`；
回归用例见 `test_wbsync.py` 第 7 组的 `plusy` 场景。

---

## 五、验证自愈是否生效

### 干跑（推荐先做）

```bash
python3 scripts/wbsync.py expert          # 默认演练，不落盘
```

演练模式必须**逐字节**不动文件。验证方式：比对运行前后文件字节，
而不是比对是否含某个子串（JSON 会把反斜杠转义成 `\\`，子串匹配会误判）。

### 落盘后核对

```bash
python3 scripts/wbsync.py expert --apply
```

然后确认三件事：

1. 本机相关的条目已指向本机路径
2. 目标不存在的条目**仍是原值**（没有误写）
3. 内置插件条目**仍是原值**

`scripts/test_wbsync.py` 的第 7 组断言完整覆盖了以上三点，改动后可回归。

---

## 六、版本漂移：另一种静默故障

即使路径全部正确，仍可能出现**"装了但跑旧版本"**：
源包已同步成 v2，而登记里仍指向 v1 的运行副本。

`wbsync expert` 与 `wbsync doctor` 会比对：

- **源包版本** ← `<根>/plugins/marketplaces/my-experts/<专家>/plugin.json` 的 `version`
- **运行版本** ← `installed_plugins.json` 中该专家的 `version` 列表

两者不一致即报"静默跑旧版"。

> 源包清单文件的相对路径因版本而异，实现按
> `plugin.json`、`.codebuddy-plugin/plugin.json`、`.workbuddy-plugin/plugin.json`、
> `manifest.json`、`.claude-plugin/plugin.json` 依次探测；
> 全都探不到就跳过，**不误报**。

---

## 七、生效时机

登记文件被修改后，WorkBuddy **需要重启**才会重新读取。
自愈完成后应提示用户重启，而不是断言"现在就能用了"。
