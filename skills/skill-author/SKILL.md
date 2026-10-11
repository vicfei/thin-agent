---
name: skill-author
description: 把一段可复用的操作流程沉淀为新技能（生成符合《Skill 最小规范》的 SKILL.md 与脚本骨架）。当用户说"把这个流程做成技能 / 以后都这么干 / 帮我沉淀一下"时使用。
metadata:
  owner: example
---

# skill-author

本技能没有脚本，你就是脚手架。按以下步骤执行：

## 步骤
1. 与用户确认技能名（kebab-case，`^[a-z0-9][a-z0-9-]*$`）和触发条件。
2. 在 `skills/<name>/` 下生成：
   - `SKILL.md`：frontmatter 含 name、description（必须写"当……时使用"）；按需声明
     `requires`（bins 用行内列表写法，如 `bins: [jq, ffmpeg]`）；正文覆盖
     "何时使用/何时不用、用法、输出格式、失败处理"四节；
   - `scripts/`：优先 bash 或纯标准库 python3（语法地板 3.6）；脚本须非交互、
     argv 传参、0/非 0 退出码、stdout 只出精炼结果（大结果落文件只回路径）；
   - 引用脚本一律写 `{SKILL_DIR}` 占位符，不写绝对路径。
3. 生成后向用户复述触发条件，并提示：新技能下次启动 agent 时自动加载（探测不过会被过滤并记审计）。

## 反模式（不要做）
- 把业务逻辑写进内核 agent.py —— 能力一律进 skill；
- 在脚本里 pip install —— 依赖只能 vendor 纯 Python 包进技能目录；
- description 只写功能不写触发条件。
