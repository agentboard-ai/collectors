# Supported Sources

One collector script per source. All collectors share the same payload schema, CLI shape (`--summary` / `--sync` / `--daemon`), config (`~/.agentboard/config.json`), and privacy guarantees; they differ in discovery paths and log-format parsing.

Collectors are keyed to each tool's local session **store**, not to a particular client — any client that writes its sessions to those paths is collected. On the AgentBoard leaderboard, sources roll up into five badges: **Claude** (Claude Code + Cowork), **Codex**, **Gemini**, **OpenCode**, **OpenClaw**.

## Claude Code — `collect.py`

- **Discovery:** `$CLAUDE_CONFIG_DIR/projects` if set, else `~/.claude/projects`
- **Format:** JSONL transcripts, one file per session
- **Session namespace:** `claude:<session_id>`
- **Trigger:** Claude Code `Stop` hook (`hook.sh`) + 180 s daemon
- **Extras:** skill usage tracking (`SKILL.md` metadata from `~/.claude/skills`, `~/.codex/skills`, and project-level `.claude/skills`), line-change counts from Edit/Write tool calls
- **Token policy:** cache fields separate; `provider_total = input + output + cache_read + cache_creation`

## Codex — `collect_codex.py`

- **Discovery:** `$CODEX_HOME/sessions` if set, else `~/.codex/sessions`; also sibling `archived_sessions`; Windows: `%APPDATA%/codex/sessions`, `%LOCALAPPDATA%/codex/sessions`
- **Client-agnostic:** reads the Codex home dir, so any Codex client that writes sessions there is included (the CLI today)
- **Format:** JSONL session files
- **Session namespace:** `codex:<session_id>`
- **Extras:** replay-prefix detection (resumed sessions re-emit token counters; the collector tracks baselines and counts deltas only); `--diagnose-date` / `--diagnose-range` token diagnostics
- **Token policy:** input already includes cache; `provider_total = input + output`

## Gemini — `collect_gemini.py`

- **Discovery:** `$GEMINI_CLI_HOME/tmp` if set, else `~/.gemini/tmp`; matches `session-*.json`, `session-*.jsonl`, `chats/**/*.jsonl`
- **Format:** JSON and JSONL session records with `usageMetadata`
- **Session namespace:** `gemini:<session_id>`
- **Extras:** uses Gemini's `projectHash` (already hashed by the tool) for project counting
- **Token policy:** uses provider `totalTokenCount`; no cache-creation reporting

## OpenCode — `collect_opencode.py`

- **Discovery:** `$OPENCODE_HOME`, `~/.local/share/opencode`, `$OPENCODE_DB`
- **Format:** OpenCode local storage (JSON message store / SQLite)
- **Session namespace:** `opencode:<session_id>`
- **Token policy:** provider `totalTokens` when present, else `input + output + cache_read + cache_creation` (cache fields separate, Claude-style)

## OpenClaw — `collect_openclaw.py`

- **Discovery:** `$OPENCLAW_DIR`, `$OPENCLAW_HOME`, `~/.openclaw` (plus legacy dirs `~/.clawdbot`, `~/.moltbot`, `~/.moldbot`)
- **Format:** OpenClaw session logs (JSONL with `usage` blocks)
- **Session namespace:** `openclaw:<session_id>`
- **Token policy:** provider `totalTokens` when present, else `input + output + cache_read + cache_creation` (cache fields separate, Claude-style)

## Claude Cowork — `collect_claude_cowork.py`

- **Discovery:** `~/Library/Application Support/Claude/local-agent-mode-sessions` (`local_*.json` metadata + embedded `.claude/projects` transcripts)
- **Format:** JSON metadata + JSONL transcripts
- **Session namespace:** `cowork:<session_id>`
- **Implementation note:** imports and reuses `collect.py` (same parsing, same payload, same privacy posture)
- **Leaderboard:** rolls up into the **Claude** badge alongside Claude Code (shown as a single source, not a separate one)
- **Token policy:** same as Claude Code

## Common Behavior

- **Device identity:** sanitized hostname (alphanumeric/dot/dash/underscore, ≤80 chars), overridable via `$AGENTBOARD_DEVICE_NAME`; platform from `sys.platform`, overridable via `$AGENTBOARD_PLATFORM`
- **Multi-device:** sync state is namespaced per host id, so several machines (including shared/NFS home dirs) can sync the same account without clobbering each other
- **State:** `~/.agentboard/*-sync-state.*.json` records synced session signatures; `--force-rescan` (where supported) reprocesses everything
- **Logs:** `~/.agentboard/logs/<source>-sync.log`, rotated at 10 MB
- **Dependencies:** Python 3 standard library only, on every source
