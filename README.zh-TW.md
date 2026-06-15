# AgentBoard Collectors

[English](./README.md) | [简体中文](./README.zh-CN.md) | **繁體中文** | [日本語](./README.ja.md) | [한국어](./README.ko.md) | [Português (BR)](./README.pt-BR.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

開源的 AI 編程用量本地採集器。

它們是 [AgentBoard](https://agentboard.cc)（AI 編程用量排行榜）的開源採集元件 —— [查看即時排行榜 →](https://agentboard.cc/leaderboard)

AgentBoard 採集器會掃描 AI 編程工具在你本機已有的日誌，在本地聚合成用量統計，只把**中繼資料**同步到 [AgentBoard](https://agentboard.cc)。它**永遠不會**上傳提示詞、模型回覆、原始碼、diff、檔案內容、檔案路徑或終端機輸出。

所有會接觸你本地日誌的程式碼都在這個儲存庫裡，以純 Python 撰寫，**零外部相依套件**——掃了什麼、傳了什麼，你都可以逐行讀到。

## 支援的來源

| 來源 | 狀態 | 掃描的本地資料 |
| --- | --- | --- |
| Claude Code | ✅ 已支援 | `~/.claude/projects`、`$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ 已支援 | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ 已支援 | `~/.codex/sessions`、`~/.codex/archived_sessions`、`$CODEX_HOME`、`%APPDATA%/codex` |
| Gemini | ✅ 已支援 | `~/.gemini/tmp`、`$GEMINI_CLI_HOME/tmp` |
| OpenCode | ✅ 已支援 | `~/.local/share/opencode`、`$OPENCODE_HOME`、`$OPENCODE_DB` |
| OpenClaw | ✅ 已支援 | `~/.openclaw`、`$OPENCLAW_HOME`、`$OPENCLAW_DIR` |

**來源如何歸併。** 每個採集器讀取的是某個工具的本地工作階段儲存（上表路徑），而不是某個特定用戶端——所以只要用戶端把工作階段寫進這些路徑就會被採集，不限於 CLI。在 AgentBoard 排行榜上，它們歸併為五個 badge：**Claude**（Claude Code + Cowork）、**Codex**、**Gemini**、**OpenCode**、**OpenClaw**。

各來源細節見 [docs/supported-sources.md](./docs/supported-sources.md)

## 會上傳什麼

AgentBoard 只上傳**聚合後的用量中繼資料**：

- 日期、帶前綴的工作階段 id（如 `claude:<id>`）、來源名稱
- 裝置名稱（淨化後的 hostname，可用 `$AGENTBOARD_DEVICE_NAME` 覆寫）和平台
- 活躍的寫程式時長、活躍時間區間（僅時間戳記）
- token 計數：input、output、cache 讀取、cache 建立、思考、provider 總量
- 訊息數、工具呼叫數、最常用工具的**名稱**和次數
- 增刪行數（來自編輯類工具的計數）
- 涉及專案數和檔案數——只傳**數量**，絕不傳名稱或路徑
- Skill 使用情況（Claude Code）：來自公開 `SKILL.md` 中繼資料的 skill 名稱/key 和使用次數
- 採集器版本號

## 永遠不會離開你機器的東西

- ❌ 提示詞和模型回覆
- ❌ 原始碼、diff、檔案內容
- ❌ 檔案路徑和專案路徑（只上傳數量）
- ❌ 終端機輸出和環境變數
- ❌ 使用者名稱、電子郵件、原始機器識別碼

逐欄位說明見 [docs/data-fields.md](./docs/data-fields.md) · 隱私政策見 [PRIVACY.md](./PRIVACY.md)

## 安裝

macOS / Linux：

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows（PowerShell）：

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

安裝腳本會把這些採集器複製到 `~/.agentboard/`，註冊背景同步（macOS 用 launchd，Windows 用排程工作），並掛接 Claude Code 的工作階段結束事件。安裝完成後沒有任何遠端執行。

## 先在本地試執行

每個採集器都有 `--summary` 模式：掃描你的日誌、印出聚合後的 JSON，**不上傳任何資料**。安裝前先跑一遍，親眼看 AgentBoard 會收到什麼：

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## 運作原理

1. **Hook** —— Claude Code 的 `Stop` 事件觸發 `hook.sh`，對剛結束的工作階段執行採集（同一工作階段 5 分鐘內最多觸發一次）。
2. **Daemon** —— 輕量背景程序每 180 秒重新掃描一次，確保 Codex、Gemini 等其他工具的用量持續同步。
3. **本地聚合** —— 採集器把 JSONL/JSON/SQLite 工作階段日誌解析成按天、按工作階段的統計。解析完全在你的機器上完成。
4. **同步中繼資料** —— 聚合結果透過 `~/.agentboard/config.json` 中的裝置 token POST 到 AgentBoard API。伺服器端只存 token 的**雜湊**，不存原文。

## Token 口徑

不同工具回報 token 的方式不同。AgentBoard 同時保存 provider 自己的總量和完整細分，確保榜單數字與官方用量面板對得上：

- **Claude Code / Cowork / OpenCode / OpenClaw** —— cache token 是獨立欄位；`provider_total = input + output + cache_read + cache_creation`。
- **Codex** —— 日誌中的 input 已包含 cache token；`provider_total = input + output`。cache 欄位僅作細分顯示，絕不重複計算。
- **Gemini** —— 直接使用 provider 的 `totalTokenCount`；Gemini 不回報 cache 建立。

所有來源都提供 `non_cache_total = provider_total − cache_read − cache_creation`，用於去除 cache 的比較。完整語意見 [docs/token-accounting.md](./docs/token-accounting.md)

## AgentBoard 不是什麼

AgentBoard 不是計費表，也不是密碼學意義上的工作量證明。本地 AI 工具日誌可以被其擁有者編輯，不同工具揭露 token 資料的方式也不同。AgentBoard 專注於透明、區分來源的用量分析——個人統計、團隊可見性和趨勢。

## 開發

採集器是獨立的 Python 3 腳本，只用標準函式庫——不需要 `pip install`。每個腳本刻意保持自包含，一次通讀即可完成稽核。

```bash
python3 collectors/collect.py --summary            # 對真實日誌做 dry-run
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # token 口徑深查
```

歡迎 Issue 和 PR——見 [CONTRIBUTING.md](./CONTRIBUTING.md)。完整解除安裝見 [UNINSTALL.md](./UNINSTALL.md)。

## 授權條款

[MIT](./LICENSE)
