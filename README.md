# AgentBoard Collectors

**English** | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md) | [Português (BR)](./README.pt-BR.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

Open-source local collectors for AI coding usage.

Part of [AgentBoard](https://agentboard.cc), the leaderboard for AI coding usage — [see it live →](https://agentboard.cc/leaderboard)

AgentBoard collectors scan the logs that AI coding tools already keep on your machine, aggregate them into usage stats, and sync **metadata only** to [AgentBoard](https://agentboard.cc). They never upload prompts, completions, source code, diffs, file contents, file paths, or terminal output.

Every line of code that touches your local logs is in this repository, written in plain Python with **zero external dependencies** — you can read exactly what is scanned and exactly what is sent.

## Supported Sources

| Source | Status | Local data scanned |
| --- | --- | --- |
| Claude Code | ✅ Supported | `~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ Supported | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ Supported | `~/.codex/sessions`, `~/.codex/archived_sessions`, `$CODEX_HOME`, `%APPDATA%/codex` |
| Gemini | ✅ Supported | `~/.gemini/tmp`, `$GEMINI_CLI_HOME/tmp` |
| OpenCode | ✅ Supported | `~/.local/share/opencode`, `$OPENCODE_HOME`, `$OPENCODE_DB` |
| OpenClaw | ✅ Supported | `~/.openclaw`, `$OPENCLAW_HOME`, `$OPENCLAW_DIR` |

**How sources map.** Each collector reads a tool's local session store (the paths above), not a specific client — so any client that writes its sessions there is collected, not just the CLI. On the AgentBoard leaderboard these roll up into five badges: **Claude** (Claude Code + Cowork), **Codex**, **Gemini**, **OpenCode**, **OpenClaw**.

Details per source: [docs/supported-sources.md](./docs/supported-sources.md)

## What Gets Uploaded

AgentBoard uploads **aggregate usage metadata only**:

- date, namespaced session id (e.g. `claude:<id>`), source name
- device name (sanitized hostname, overridable via `$AGENTBOARD_DEVICE_NAME`) and platform
- active coding time, engaged time windows (timestamps only)
- token counts: input, output, cache read, cache creation, reasoning, provider total
- message counts, tool call counts, top tool **names** with counts
- lines added / removed (counts from edit tools)
- **counts** of projects and files touched — never their names or paths
- skill usage (Claude Code): skill names/keys from public `SKILL.md` metadata and usage counts
- collector version

## What Never Leaves Your Machine

- ❌ Prompts and assistant responses
- ❌ Source code, diffs, file contents
- ❌ File paths and project paths (only counts are uploaded)
- ❌ Terminal output and environment variables
- ❌ Usernames, emails, raw machine identifiers

Full field-by-field reference: [docs/data-fields.md](./docs/data-fields.md) · Privacy policy: [PRIVACY.md](./PRIVACY.md)

## Install

macOS / Linux:

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows (PowerShell):

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

The installer copies these collectors to `~/.agentboard/`, registers a background sync (launchd on macOS, scheduled task on Windows), and hooks into Claude Code's session-stop event. Nothing is executed remotely after install.

## Try It Locally First

Every collector has a `--summary` mode that scans your logs and prints the aggregate JSON **without uploading anything**. Run it before you install, and see exactly what AgentBoard would receive:

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## How It Works

1. **Hook** — Claude Code's `Stop` event triggers `hook.sh`, which kicks the collector for the finished session (throttled to once per 5 minutes per session).
2. **Daemon** — a lightweight background process re-scans every 180 seconds, so usage from Codex, Gemini, and other tools stays in sync.
3. **Aggregate locally** — collectors parse JSONL/JSON/SQLite session logs into per-day, per-session stats. Parsing happens entirely on your machine.
4. **Sync metadata** — aggregates are POSTed to the AgentBoard API with a device token stored in `~/.agentboard/config.json`. The server stores token **hashes**, never raw tokens.

## Token Accounting

Different tools report tokens differently. AgentBoard keeps both the provider's own total and a full breakdown, so leaderboard numbers match official usage dashboards:

- **Claude Code / Cowork / OpenCode / OpenClaw** — cache tokens are separate fields; `provider_total = input + output + cache_read + cache_creation`.
- **Codex** — reported input already includes cached tokens; `provider_total = input + output`. Cache fields are kept for breakdown only, never double-counted.
- **Gemini** — uses the provider's `totalTokenCount`; cache creation is not reported by Gemini.

`non_cache_total = provider_total − cache_read − cache_creation` is available everywhere for cache-free comparisons. Full semantics: [docs/token-accounting.md](./docs/token-accounting.md)

## What AgentBoard Is Not

AgentBoard is not a billing meter and not a cryptographic proof of work. Local AI tool logs can be edited by their owner, and different tools expose token data differently. AgentBoard focuses on transparent, source-aware usage analytics — personal stats, team visibility, and trends.

## Development

Collectors are standalone Python 3 scripts using only the standard library — no `pip install` required. Each script is intentionally self-contained so it can be audited in one read.

```bash
python3 collectors/collect.py --summary            # dry-run against your real logs
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # token accounting deep-dive
```

Issues and PRs welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md). To remove everything, see [UNINSTALL.md](./UNINSTALL.md).

## License

[MIT](./LICENSE)
