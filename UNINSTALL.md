# Uninstall

AgentBoard installs everything under `~/.agentboard/` plus a few registrations. Removing it is fully reversible and leaves your AI tools untouched.

## macOS

```bash
# 1. Stop and remove background sync agents
launchctl bootout "gui/$(id -u)/cc.agentboard.claude-sync" 2>/dev/null
launchctl bootout "gui/$(id -u)/cc.agentboard.codex-sync" 2>/dev/null
launchctl bootout "gui/$(id -u)/cc.agentboard.gemini-sync" 2>/dev/null
launchctl bootout "gui/$(id -u)/cc.agentboard.cowork-sync" 2>/dev/null
rm -f ~/Library/LaunchAgents/cc.agentboard.*.plist

# 2. Remove the Claude Code hook
#    Open ~/.claude/settings.json and delete the hook entry whose command
#    points to ~/.agentboard/hook.sh

# 3. Remove all collector code, config, state, and logs
rm -rf ~/.agentboard

# 4. (Optional) Remove the PATH line referencing ~/.agentboard/bin
#    from ~/.zshrc or ~/.bashrc
```

## Linux

```bash
# 1. Kill running daemons
pkill -f "collect.*--daemon" 2>/dev/null

# 2. Remove the Claude Code hook from ~/.claude/settings.json
#    (delete the entry pointing to ~/.agentboard/hook.sh)

# 3. Remove everything
rm -rf ~/.agentboard

# 4. (Optional) Remove the PATH line from your shell rc file
```

## Windows

```powershell
irm https://agentboard.cc/uninstall.ps1 | iex
# add -Purge to also delete config and logs
```

Or manually: remove the "AgentBoard Claude Sync" / "AgentBoard Codex Sync" scheduled tasks, delete the AgentBoard startup registry entries, and delete `%USERPROFILE%\.agentboard`.

## Server-Side Data

Uninstalling stops all future syncs but does not delete already-synced stats. To remove those, revoke the device and delete data from your AgentBoard account settings, or contact official@agentboard.cc.
