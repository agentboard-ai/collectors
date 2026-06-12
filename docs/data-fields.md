# Uploaded Data Fields

The complete reference of every field a collector can upload. One payload is POSTed per session (or per day for daily aggregates) to the AgentBoard API endpoint configured in `~/.agentboard/config.json`. The server validates shape and clamps all numeric fields to safe maximums before storing.

If a field is not in this table, it is not uploaded.

## Identity

| Field | Type | Contents | Notes |
| --- | --- | --- | --- |
| `token` | string | device auth token | server stores hash only; revocable from your account |
| `session_id` | string | tool's session id, namespaced | `claude:` / `codex:` / `gemini:` / `opencode:` / `openclaw:` / `cowork:` prefix |
| `source` | string | `claude_code`, `codex`, `gemini_cli`, `opencode`, `openclaw`, `claude_cowork` | |
| `date` | string | `YYYY-MM-DD` | |
| `device_name` | string | sanitized hostname | alphanumeric/dot/dash/underscore, ≤80 chars; override with `$AGENTBOARD_DEVICE_NAME` |
| `platform` | string | `macos` / `win32` / `linux` | |
| `collector_version` | string | script release date string | e.g. `2026-05-14` |
| `full_rescan` | bool | whether this sync reprocessed history | |

## Time

| Field | Type | Contents | Notes |
| --- | --- | --- | --- |
| `coding_time_mins` | int | engaged minutes | 10-min idle gap rule, clamped ≤480/session, ≤960/day |
| `ai_time_mins` | int | estimated AI-output reading time | derived: output_tokens ÷ 300 |
| `first_event_at` | string | ISO 8601 timestamp | |
| `last_event_at` | string | ISO 8601 timestamp | |
| `engaged_windows` | array | `{start_at, end_at}` pairs | timestamps only, no content |

## Tokens

| Field | Type | Notes |
| --- | --- | --- |
| `input_tokens` | int | raw input as the tool reports it (Codex: already includes cache) |
| `output_tokens` | int | |
| `cache_read_tokens` | int | separate field for Claude-style sources; informational for Codex |
| `cache_creation_tokens` | int | always 0 for Gemini (not reported by tool) |
| `thoughts_tokens` | int | reasoning tokens where the tool reports them |
| `tool_tokens` | int | tool-use prompt tokens where reported |
| `provider_total_tokens` | int | canonical total — see [token-accounting.md](./token-accounting.md) |
| `tokens_used` | int | legacy: input + output, no cache |

## Activity Counts

| Field | Type | Contents | Privacy note |
| --- | --- | --- | --- |
| `messages` | int | user + assistant message count | count only |
| `assistant_messages` | int | | count only |
| `sessions` | int | sessions in this payload (1) | |
| `tool_calls` | int | total tool invocations | count only |
| `tool_breakdown` | array | `{tool, count, percentage}` top 4 | tool **names** only (e.g. `Edit`, `Bash`) — never arguments |
| `lines_added` | int | from edit-tool call metadata | counts, not content |
| `lines_removed` | int | | counts, not content |
| `lines_changed` | int | added − removed | |
| `projects` | int | distinct projects touched | **count only — names/paths never sent** |
| `files_touched` | int | distinct files touched | **count only — names/paths never sent** |

## Skills (Claude Code / Cowork only)

| Field | Type | Contents | Privacy note |
| --- | --- | --- | --- |
| `skill_breakdown` | array | `{skill_key, skill_name, source, count, last_used}` | names/keys from `SKILL.md` frontmatter — metadata that skill authors publish by design |
| `installed_skills` | array | installed skill inventory (key, name, description, scope, origin repo) | `origin_repo` is the `owner/repo` slug inferred from the skill's git remote — never a full URL, never credentials, never local paths |
| `installed_skill_snapshots` | array | `{source, scope, skill_keys}` | |

> If you keep private skills whose *names* you consider sensitive, note that skill names and `SKILL.md` description lines are uploaded as part of the inventory. Skill body content is never read beyond frontmatter metadata, and never uploaded.

## Inventory Mode

A second payload type (`mode: "inventory"`) uploads the deduplicated list of namespaced `session_ids` per source, so the server can reconcile deleted/renamed local sessions. No other data accompanies it.

## What You Will Not Find Here

No prompt text, no response text, no code, no diffs, no file contents, no file/project names or paths, no terminal output, no environment variable values, no emails, no usernames, no raw machine identifiers. Parsing happens in memory on your machine and only the aggregates above are transmitted.
