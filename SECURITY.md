# Security Policy

## Reporting a Vulnerability

If you find a security issue in the collectors, the install script, or the sync protocol — or any discrepancy between [PRIVACY.md](./PRIVACY.md) and what the code actually does — please report it privately:

- Email: **official@agentboard.cc**
- Please include: affected script/version (`collector_version` in each script header), reproduction steps, and impact.

We will acknowledge reports within 72 hours. Please do not open public issues for unpatched vulnerabilities.

## Scope

In scope:

- Collector scripts (`collectors/*.py`, `collectors/hook.sh`)
- The install flow served from `agentboard.cc/install`
- Payload handling between collectors and the AgentBoard API (data leakage, token handling)

Especially severe (please report immediately):

- Any path where conversation content, code, file paths, or credentials could reach the network
- Device token leakage or privilege escalation via the daemon/launchd setup
- Remote code execution via crafted local log files

Out of scope:

- Vulnerabilities in the AI tools whose logs are scanned (Claude Code, Codex, Gemini, etc.)
- Leaderboard gaming by editing one's own local logs (see "What AgentBoard Is Not" in the README — local logs are user-controlled by design)

## Supply Chain Notes

- Collectors use the Python standard library only. There are no third-party dependencies to compromise.
- The installer embeds collector code directly; nothing is fetched and executed at runtime after install.
- Versions are pinned date strings in each script header (`AGENTBOARD_SCRIPT_RELEASE`); there is no auto-update mechanism.
