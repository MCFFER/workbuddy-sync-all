#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_skill.py —— SKILL.md 的 frontmatter 严格校验（零依赖）

为什么需要它
------------
skill-creator 自带的 quick_validate.py 检查结构、命名、描述长度，
但**不做真正的 YAML 解析**。所以下面这种文件它会报「valid」，而平台侧一上传就炸：

    description_en: Sync across all your computers. Two layers matter: files ...

问题出在 `matter: files` 这个「冒号 + 空格」—— YAML 会把它当成嵌套映射，
于是报 `mapping values are not allowed in this context`。

本脚本专门查这一类：**未加引号的长文本里混入了 YAML 元字符**。
不需要安装任何依赖；若环境里有 PyYAML，会额外做一次权威解析。

用法
----
    python3 check_skill.py                 # 检查同目录上层的 SKILL.md
    python3 check_skill.py <SKILL.md 路径>
"""

import io
import os
import re
import sys

# 未加引号的纯量里，这些模式会让 YAML 解析出错或改变语义
# 注意：前两条对任何纯量都成立；后两条只对「普通纯量」成立 ——
# 流式集合、块标量、锚点本身就以指示符开头，属合法写法，不能误判。
DANGEROUS_ALWAYS = [
    (re.compile(r":\s"), "冒号 + 空格（YAML 会当成嵌套映射的键值分隔）"),
    (re.compile(r"\s#"), "空格 + 井号（YAML 会当成行内注释）"),
]
DANGEROUS_PLAIN_ONLY = [
    (re.compile(r"^[-?]\s"), "行首的 - 或 ?（YAML 会当成序列项或复杂键）"),
    (re.compile(r"^[%@`]"), "行首的保留指示符 % @ `（纯量不能以它开头）"),
]
# 以这些开头的值是合法的非普通纯量，跳过「行首指示符」检查
FLOW_OR_BLOCK = re.compile(r"^(?:\[.*\]|\{.*\}|[|>*&])", re.S)

# 官方必填字段（open.workbuddy.cn/docs/skill）
REQUIRED = ["description", "description_zh", "description_en", "version", "author"]

# 注入物体量阈值（超限**只告警、不阻断**；可用环境变量覆盖）
# 中文内容按行数为主、字节为辅（中文行比英文长）。
SKILL_WARN_KB = float(os.environ.get("WBSYNC_SKILL_WARN_KB") or 22)
SKILL_WARN_LINES = int(os.environ.get("WBSYNC_SKILL_WARN_LINES") or 200)
MEM_WARN_KB = float(os.environ.get("WBSYNC_MEM_WARN_KB") or 2.5)
MEM_WARN_LINES = int(os.environ.get("WBSYNC_MEM_WARN_LINES") or 26)

QUOTED = re.compile(r"^(?:'[^']*(?:''[^']*)*'|\"[^\"]*\")$")


def scan_value(val):
    """返回该字段值里的 YAML 隐患说明列表。"""
    problems = []
    for pat, why in DANGEROUS_ALWAYS:
        if pat.search(val):
            problems.append(why)
    stripped = val.strip()
    if not FLOW_OR_BLOCK.match(stripped):
        for pat, why in DANGEROUS_PLAIN_ONLY:
            if pat.search(stripped):
                problems.append(why)
    return problems


def split_frontmatter(text):
    if not text.startswith("---"):
        return None, "文件没有以 --- 开头，缺少 frontmatter"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, "frontmatter 未闭合（找不到第二个 ---）"
    return parts[1], None


def main():
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if _args:
        path = _args[0]
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "SKILL.md")
    path = os.path.abspath(path)

    if not os.path.isfile(path):
        print("✗ 找不到文件：%s" % path)
        return 2

    text = io.open(path, encoding="utf-8").read()
    print("检查 %s" % path)
    print()

    fm, err = split_frontmatter(text)
    if err:
        print("✗ %s" % err)
        return 1

    lines = fm.split("\n")
    fields = {}
    problems = []

    for idx, line in enumerate(lines, start=2):  # 文件第 1 行是 ---
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if not m:
            if not line.startswith((" ", "\t")):
                problems.append((idx, "无法解析的行", line[:70]))
            continue
        key, val = m.group(1), m.group(2)
        fields[key] = val

        if not val:
            continue
        if QUOTED.match(val.strip()):
            continue  # 已加引号，YAML 元字符不再有害

        for why in scan_value(val):
            problems.append((idx, why, val[:70]))

    print("字段 %d 个：%s" % (len(fields), ", ".join(sorted(fields))))
    print()

    if problems:
        print("✗ 发现 %d 处 YAML 隐患：" % len(problems))
        for idx, why, snippet in problems:
            print("  第 %d 行 —— %s" % (idx, why))
            print("      %s…" % snippet)
        print()
        print("修法：给该字段的整段值加单引号（内部的 ' 写成 ''）。")
        print("      用单引号而非双引号 —— 双引号里反斜杠是转义符，路径容易踩雷。")
    else:
        print("✓ 未发现未加引号引起的 YAML 隐患")

    print()
    missing = [k for k in REQUIRED if k not in fields or not fields[k].strip()]
    if missing:
        print("✗ 缺少官方必填字段：%s" % ", ".join(missing))
    else:
        print("✓ 官方必填字段齐全（%s）" % ", ".join(REQUIRED))

    # 版本一致性：SKILL.md 的 version 必须与 wbsync.py 的 VERSION 一致
    # （防「只 bump 一处」——B 机曾据此抓到 SKILL.md=2.6.6 而代码还是 2.6.3）
    print()
    ver_fm = (fields.get("version") or "").strip().strip("'\"")
    ver_code = None
    wbsync_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wbsync.py")
    try:
        wtxt = io.open(wbsync_path, encoding="utf-8").read()
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', wtxt, re.M)
        ver_code = m.group(1) if m else None
    except Exception:
        ver_code = None
    if ver_code is None:
        print("· 未找到 wbsync.py 的 VERSION 常量，跳过版本一致性检查")
    elif ver_fm and ver_fm != ver_code:
        print("✗ 版本不一致：SKILL.md version=%s，但 wbsync.py VERSION=%s（漏 bump 了一处）" % (ver_fm, ver_code))
        problems.append((-1, "version-mismatch", "%s != %s" % (ver_fm, ver_code)))
    else:
        print("✓ 版本一致（SKILL.md version == wbsync.py VERSION == %s）" % ver_fm)

    # 注入物体量：SKILL.md 每次触发技能都完整载入 → 超阈值**只告警、不阻断**。
    # MEMORY.md（工作区注入摘要、会话前注入）需 `--ws=<工作区>` 才查（它不在 skill 内）。
    print()
    _bloat = []
    _kb = len(text.encode("utf-8")) / 1024.0
    _n = text.count("\n") + 1
    if _kb >= SKILL_WARN_KB or _n >= SKILL_WARN_LINES:
        _bloat.append("SKILL.md %.1fKB / %d 行（阈值 %.0fKB / %d）" % (_kb, _n, SKILL_WARN_KB, SKILL_WARN_LINES))
    else:
        print("✓ SKILL.md 体量 %.1fKB / %d 行（阈值 %.0fKB / %d 行）" % (_kb, _n, SKILL_WARN_KB, SKILL_WARN_LINES))
    _ws = next((a[5:] for a in sys.argv[1:] if a.startswith("--ws=")), None)
    if _ws:
        _mp = os.path.join(_ws, ".workbuddy", "memory", "MEMORY.md")
        if os.path.isfile(_mp):
            _mt = io.open(_mp, encoding="utf-8").read()
            _mkb = len(_mt.encode("utf-8")) / 1024.0
            _mn = _mt.count("\n") + 1
            if _mkb >= MEM_WARN_KB or _mn >= MEM_WARN_LINES:
                _bloat.append("MEMORY.md %.1fKB / %d 行（阈值 %.1fKB / %d）" % (_mkb, _mn, MEM_WARN_KB, MEM_WARN_LINES))
            else:
                print("✓ MEMORY.md 体量 %.1fKB / %d 行（阈值 %.1fKB / %d 行）" % (_mkb, _mn, MEM_WARN_KB, MEM_WARN_LINES))
        else:
            print("· 未找到 %s，跳过 MEMORY.md 体量检查" % _mp)
    for _b in _bloat:
        print("⚠️ 注入物偏大：%s —— 每次会话 / 触发都会带上，建议精简或下沉到 references/（渐进式披露）。" % _b)
    # 注：体量超限只提示、不阻断（不加入 problems）

    # 有 PyYAML 就做一次权威解析
    print()
    try:
        import yaml  # noqa
    except ImportError:
        print("· 未安装 PyYAML，跳过权威解析（上面已做零依赖静态检查）")
    else:
        try:
            d = yaml.safe_load(fm)
            print("✓ PyYAML 解析通过，共 %d 个字段" % len(d))
        except Exception as e:
            print("✗ PyYAML 解析失败：%s" % str(e).replace("\n", " | "))
            problems.append((-1, "yaml", str(e)))

    print()
    if problems or missing:
        print("结果：不通过")
        return 1
    print("结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
