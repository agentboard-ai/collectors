# AgentBoard Collectors

[English](./README.md) | **简体中文** | [日本語](./README.ja.md) | [한국어](./README.ko.md) | [Português (BR)](./README.pt-BR.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

开源的 AI 编程用量本地采集器。

它们是 [AgentBoard](https://agentboard.cc)（AI 编程用量排行榜）的开源采集组件 —— [查看实时榜单 →](https://agentboard.cc/leaderboard)

AgentBoard 采集器扫描 AI 编程工具在你本机已有的日志，在本地聚合成用量统计，只把**元数据**同步到 [AgentBoard](https://agentboard.cc)。它**永远不会**上传提示词、模型回复、源代码、diff、文件内容、文件路径或终端输出。

所有会接触你本地日志的代码都在这个仓库里，用纯 Python 编写，**零外部依赖**——扫了什么、传了什么，你可以逐行读到。

## 支持的来源

| 来源 | 状态 | 扫描的本地数据 |
| --- | --- | --- |
| Claude Code | ✅ 已支持 | `~/.claude/projects`、`$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ 已支持 | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ 已支持 | `~/.codex/sessions`、`~/.codex/archived_sessions`、`$CODEX_HOME`、`%APPDATA%/codex` |
| Gemini | ✅ 已支持 | `~/.gemini/tmp`、`$GEMINI_CLI_HOME/tmp` |
| OpenCode | ✅ 已支持 | `~/.local/share/opencode`、`$OPENCODE_HOME`、`$OPENCODE_DB` |
| OpenClaw | ✅ 已支持 | `~/.openclaw`、`$OPENCLAW_HOME`、`$OPENCLAW_DIR` |

**来源如何归并。** 每个采集器读取的是某个工具的本地会话存储(上表路径),而不是某个特定客户端——所以只要客户端把会话写进这些路径就会被采集,不限于 CLI。在 AgentBoard 排行榜上,它们归并为五个 badge:**Claude**(Claude Code + Cowork)、**Codex**、**Gemini**、**OpenCode**、**OpenClaw**。

各来源细节见 [docs/supported-sources.md](./docs/supported-sources.md)

## 会上传什么

AgentBoard 只上传**聚合后的用量元数据**：

- 日期、带前缀的会话 id（如 `claude:<id>`）、来源名称
- 设备名（净化后的 hostname，可用 `$AGENTBOARD_DEVICE_NAME` 覆盖）和平台
- 活跃编码时长、活跃时间窗口（仅时间戳）
- token 计数：input、output、cache 读取、cache 创建、思考、provider 总量
- 消息数、工具调用数、最常用工具的**名称**和次数
- 增删行数（来自编辑类工具的计数）
- 涉及项目数和文件数——只传**数量**，绝不传名称或路径
- Skill 使用情况（Claude Code）：来自公开 `SKILL.md` 元数据的 skill 名称/key 和使用次数
- 采集器版本号

## 永远不会离开你机器的东西

- ❌ 提示词和模型回复
- ❌ 源代码、diff、文件内容
- ❌ 文件路径和项目路径（只上传数量）
- ❌ 终端输出和环境变量
- ❌ 用户名、邮箱、原始机器标识

逐字段说明见 [docs/data-fields.md](./docs/data-fields.md) · 隐私政策见 [PRIVACY.md](./PRIVACY.md)

## 安装

macOS / Linux：

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows（PowerShell）：

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

安装脚本会把这些采集器复制到 `~/.agentboard/`，注册后台同步（macOS 用 launchd，Windows 用计划任务），并挂接 Claude Code 的会话结束事件。安装完成后没有任何远程执行。

## 先在本地试运行

每个采集器都有 `--summary` 模式：扫描你的日志、打印聚合后的 JSON，**不上传任何数据**。装之前先跑一遍，亲眼看 AgentBoard 会收到什么：

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## 工作原理

1. **Hook** —— Claude Code 的 `Stop` 事件触发 `hook.sh`，对刚结束的会话执行采集（同一会话 5 分钟内最多触发一次）。
2. **Daemon** —— 轻量后台进程每 180 秒重扫一次，保证 Codex、Gemini 等其他工具的用量持续同步。
3. **本地聚合** —— 采集器把 JSONL/JSON/SQLite 会话日志解析成按天、按会话的统计。解析完全在你的机器上完成。
4. **同步元数据** —— 聚合结果通过 `~/.agentboard/config.json` 中的设备 token POST 到 AgentBoard API。服务端只存 token 的**哈希**，不存原文。

## Token 口径

不同工具的 token 报告方式不同。AgentBoard 同时保存 provider 自己的总量和完整细分，保证榜单数字与官方用量面板对得上：

- **Claude Code / Cowork / OpenCode / OpenClaw** —— cache token 是独立字段；`provider_total = input + output + cache_read + cache_creation`。
- **Codex** —— 日志中的 input 已包含 cache token；`provider_total = input + output`。cache 字段仅作细分展示，绝不重复计算。
- **Gemini** —— 直接使用 provider 的 `totalTokenCount`；Gemini 不报告 cache 创建。

所有来源都提供 `non_cache_total = provider_total − cache_read − cache_creation`，用于去 cache 对比。完整语义见 [docs/token-accounting.md](./docs/token-accounting.md)

## AgentBoard 不是什么

AgentBoard 不是计费表，也不是密码学意义上的工作量证明。本地 AI 工具日志可以被其所有者编辑，不同工具暴露 token 数据的方式也不同。AgentBoard 专注于透明、区分来源的用量分析——个人统计、团队可见性和趋势。

## 开发

采集器是独立的 Python 3 脚本，只用标准库——不需要 `pip install`。每个脚本刻意保持自包含，一次通读即可完成审计。

```bash
python3 collectors/collect.py --summary            # 对真实日志做 dry-run
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # token 口径深查
```

欢迎 Issue 和 PR——见 [CONTRIBUTING.md](./CONTRIBUTING.md)。完整卸载见 [UNINSTALL.md](./UNINSTALL.md)。

## 许可证

[MIT](./LICENSE)
