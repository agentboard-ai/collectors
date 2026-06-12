# Privacy

This document describes exactly what AgentBoard collectors read, what they upload, and what stays on your machine. It is written against the collector source code in this repository — every claim here can be verified by reading the scripts in [`collectors/`](./collectors/).

## Principles

1. **Local-first.** All log parsing happens on your machine. Raw logs are never transmitted.
2. **Metadata only.** Only aggregate statistics (counts, durations, token numbers) are uploaded.
3. **Auditable.** Collectors are standalone Python scripts with zero external dependencies. What you read is what runs.
4. **Dry-run before trust.** Every collector supports `--summary`, which prints the exact aggregate locally without uploading.

## What Collectors Read (Locally)

Collectors scan session logs written by AI coding tools:

| Source | Locations |
| --- | --- |
| Claude Code | `~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects` (JSONL transcripts) |
| Codex CLI | `~/.codex/sessions`, `~/.codex/archived_sessions`, `$CODEX_HOME`, `%APPDATA%/codex`, `%LOCALAPPDATA%/codex` |
| Gemini CLI | `~/.gemini/tmp`, `$GEMINI_CLI_HOME/tmp` |
| OpenCode | `~/.local/share/opencode`, `$OPENCODE_HOME`, `$OPENCODE_DB` |
| OpenClaw | `~/.openclaw`, `$OPENCLAW_HOME`, `$OPENCLAW_DIR` |
| Claude Cowork | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Skills (Claude Code) | `~/.claude/skills`, `~/.codex/skills`, project-level `.claude/skills` (`SKILL.md` metadata only) |

Reading these files locally is necessary to compute statistics. The contents of these files — your prompts, the assistant's replies, your code — are parsed in memory and discarded. They are not stored by the collector and not transmitted.

## What Is Uploaded

One JSON payload per session/day, containing only:

**Identity & device**
- `session_id` — the tool's own session id, namespaced (`claude:`, `codex:`, `gemini:`, `cowork:`, …)
- `source`, `date`, `collector_version`
- `device_name` — your hostname, sanitized to alphanumerics/dot/dash/underscore (max 80 chars). Override it with `$AGENTBOARD_DEVICE_NAME` if you don't want your hostname pattern visible.
- `platform` — `macos` / `win32` / `linux`

**Time**
- `coding_time_mins`, `ai_time_mins` (derived estimates, clamped to sane maximums)
- `first_event_at`, `last_event_at`, `engaged_windows` (timestamps only)

**Tokens**
- `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `thoughts_tokens`, `tool_tokens`, `provider_total_tokens`, `tokens_used`

**Activity counts**
- `messages`, `assistant_messages`, `tool_calls`
- `tool_breakdown` — top tool **names** (e.g. "Edit", "Bash") with counts and percentages
- `lines_added`, `lines_removed`, `lines_changed` — counts derived from edit-tool calls
- `projects` — **count** of distinct projects touched
- `files_touched` — **count** of distinct files touched

**Skills (Claude Code / Cowork only)**
- `skill_breakdown` and `installed_skills` — skill names/keys and descriptions taken from `SKILL.md` frontmatter (metadata that is public by design in skill repositories), usage counts, and — where a skill lives in a git repo — the `owner/repo` slug inferred from the git remote. No skill body content, no credentials, no local paths.

See [docs/data-fields.md](./docs/data-fields.md) for the complete field-by-field reference.

## What Is Never Uploaded

- Prompts, assistant responses, conversation content of any kind
- Source code, diffs, file contents
- File names, file paths, project names, project paths (only **counts** are sent)
- Terminal output
- Environment variable values
- Usernames, emails, raw machine identifiers, IP-derived identity
- Git credentials or full remote URLs (only `owner/repo` slugs for installed skills, as described above)

## Authentication

- A device token is created when you link a device and stored in `~/.agentboard/config.json` (file on your machine, readable by you).
- The server stores only a **hash** of the token. Tokens can be revoked from your AgentBoard account at any time; revoked tokens are rejected on the next sync.
- No other credentials are read or stored. Collectors never read your API keys for Claude/OpenAI/Google.

## Local State

Collectors keep their own state under `~/.agentboard/`:

- `config.json` — API endpoint, device token, device name
- `*-sync-state.*.json` — which sessions were already synced (ids and signatures, no content)
- `logs/*.log` — sync logs, rotated at 10 MB
- `inventory-cache-*.json` — cached skill metadata

Delete `~/.agentboard/` at any time to remove all local state ([UNINSTALL.md](./UNINSTALL.md)).

## Server-Side Validation & Retention

The AgentBoard API validates payload shape and clamps every numeric field to safe maximums before storing. Data is stored per account; deleting your AgentBoard account deletes your usage data. For account-level questions, contact official@agentboard.cc.

## Verifying These Claims

```bash
# 1. See exactly what would be uploaded for your machine — nothing is sent:
python3 collectors/collect.py --summary

# 2. Read the payload construction in source:
#    collect.py        — build_payload / post_checkin
#    collect_codex.py  — same structure for Codex
#    hook.sh           — the only thing Claude Code triggers

# 3. Watch the network: collectors POST to a single endpoint (the `api` value
#    in ~/.agentboard/config.json) with Content-Type: application/json.
```

If you find any discrepancy between this document and the code, please report it — see [SECURITY.md](./SECURITY.md). We treat privacy documentation drift as a bug of the highest severity.
