#!/bin/bash
# AgentBoard session hook — called by Claude Code on every Stop event
#
# PRIVACY: This hook ONLY sends aggregate usage stats (time, token count,
# code change counts, session count). It NEVER reads, stores, or transmits
# any conversation content, code, prompts, or responses.

AB_DIR="$HOME/.agentboard"
[ ! -f "$AB_DIR/config.json" ] && exit 0
[ ! -f "$AB_DIR/collect.py" ] && exit 0
command -v python3 > /dev/null 2>&1 || exit 0

detect_device_name() {
    local name
    name="${AGENTBOARD_DEVICE_NAME:-}"
    if [ -z "$name" ]; then
        name=$(hostname 2>/dev/null || uname -n 2>/dev/null || printf "")
    fi
    printf "%s" "$name"
}

agentboard_host_id() {
    local raw
    raw="${AGENTBOARD_HOST_ID:-$(detect_device_name)}"
    raw=$(printf "%s" "$raw" | tr -c 'A-Za-z0-9_.-' '_' | cut -c 1-80)
    [ -n "$raw" ] || raw="unknown"
    printf "%s" "$raw"
}

AB_HOST_ID="$(agentboard_host_id)"
export AGENTBOARD_HOST_ID="$AB_HOST_ID"

CLAUDE_PID_FILE="$AB_DIR/claude-sync.$AB_HOST_ID.pid"
CLAUDE_LAUNCH_AGENT_LABEL="cc.agentboard.claude-sync"
CLAUDE_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$CLAUDE_LAUNCH_AGENT_LABEL.plist"
CODEX_PID_FILE="$AB_DIR/codex-sync.$AB_HOST_ID.pid"
CODEX_LAUNCH_AGENT_LABEL="cc.agentboard.codex-sync"
CODEX_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$CODEX_LAUNCH_AGENT_LABEL.plist"
GEMINI_PID_FILE="$AB_DIR/gemini-sync.$AB_HOST_ID.pid"
GEMINI_LAUNCH_AGENT_LABEL="cc.agentboard.gemini-sync"
GEMINI_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$GEMINI_LAUNCH_AGENT_LABEL.plist"
OPENCODE_PID_FILE="$AB_DIR/opencode-sync.$AB_HOST_ID.pid"
OPENCODE_LAUNCH_AGENT_LABEL="cc.agentboard.opencode-sync"
OPENCODE_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$OPENCODE_LAUNCH_AGENT_LABEL.plist"
OPENCLAW_PID_FILE="$AB_DIR/openclaw-sync.$AB_HOST_ID.pid"
OPENCLAW_LAUNCH_AGENT_LABEL="cc.agentboard.openclaw-sync"
OPENCLAW_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$OPENCLAW_LAUNCH_AGENT_LABEL.plist"
KIMI_PID_FILE="$AB_DIR/kimi-sync.$AB_HOST_ID.pid"
KIMI_LAUNCH_AGENT_LABEL="cc.agentboard.kimi-sync"
KIMI_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$KIMI_LAUNCH_AGENT_LABEL.plist"
COWORK_DIR="$HOME/Library/Application Support/Claude/local-agent-mode-sessions"
COWORK_PID_FILE="$AB_DIR/cowork-sync.$AB_HOST_ID.pid"
COWORK_LAUNCH_AGENT_LABEL="cc.agentboard.cowork-sync"
COWORK_LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$COWORK_LAUNCH_AGENT_LABEL.plist"

has_path_from_list() {
    local value="$1"
    local old_ifs="$IFS"
    local candidate
    [ -z "$value" ] && return 1
    IFS=",:"
    for candidate in $value; do
        [ -n "$candidate" ] && [ -e "$candidate" ] && {
            IFS="$old_ifs"
            return 0
        }
    done
    IFS="$old_ifs"
    return 1
}

has_opencode_data() {
    has_path_from_list "${OPENCODE_DB:-}" && return 0
    has_path_from_list "${OPENCODE_DATA_DIR:-}" && return 0
    has_path_from_list "${OPENCODE_HOME:-}" && return 0
    [ -d "$HOME/.local/share/opencode" ]
}

has_openclaw_data() {
    has_path_from_list "${OPENCLAW_DIR:-}" && return 0
    has_path_from_list "${OPENCLAW_HOME:-}" && return 0
    [ -d "$HOME/.openclaw" ] || [ -d "$HOME/.clawdbot" ] || [ -d "$HOME/.moltbot" ] || [ -d "$HOME/.moldbot" ]
}

has_kimi_data() {
    has_path_from_list "${KIMI_CODE_DIR:-}" && return 0
    has_path_from_list "${KIMI_CODE_HOME:-}" && return 0
    [ -d "$HOME/.kimi-code/sessions" ]
}

ensure_claude_sync_daemon() {
    CLAUDE_ENV_PROJECTS=""
    [ -n "${CLAUDE_CONFIG_DIR:-}" ] && CLAUDE_ENV_PROJECTS="$CLAUDE_CONFIG_DIR/projects"
    if [ ! -d "$HOME/.claude/projects" ] && { [ -z "$CLAUDE_ENV_PROJECTS" ] || [ ! -d "$CLAUDE_ENV_PROJECTS" ]; }; then
        return
    fi

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$CLAUDE_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$CLAUDE_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$CLAUDE_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$CLAUDE_PID_FILE" ]; then
        EXISTING_PID=$(cat "$CLAUDE_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect.py" --daemon >/dev/null 2>&1 &
    echo $! > "$CLAUDE_PID_FILE"
}

ensure_codex_sync_daemon() {
    [ ! -f "$AB_DIR/collect_codex.py" ] && return
    CODEX_ENV_SESSIONS=""
    [ -n "${CODEX_HOME:-}" ] && CODEX_ENV_SESSIONS="$CODEX_HOME/sessions"
    if [ ! -d "$HOME/.codex/sessions" ] && { [ -z "$CODEX_ENV_SESSIONS" ] || [ ! -d "$CODEX_ENV_SESSIONS" ]; }; then
        return
    fi

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$CODEX_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$CODEX_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$CODEX_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$CODEX_PID_FILE" ]; then
        EXISTING_PID=$(cat "$CODEX_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_codex.py" --daemon >/dev/null 2>&1 &
    echo $! > "$CODEX_PID_FILE"
}

ensure_gemini_sync_daemon() {
    [ ! -f "$AB_DIR/collect_gemini.py" ] && return
    GEMINI_ENV_TMP=""
    [ -n "${GEMINI_CLI_HOME:-}" ] && GEMINI_ENV_TMP="$GEMINI_CLI_HOME/tmp"
    if [ ! -d "$HOME/.gemini/tmp" ] && { [ -z "$GEMINI_ENV_TMP" ] || [ ! -d "$GEMINI_ENV_TMP" ]; }; then
        return
    fi

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$GEMINI_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$GEMINI_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$GEMINI_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$GEMINI_PID_FILE" ]; then
        EXISTING_PID=$(cat "$GEMINI_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_gemini.py" --daemon >/dev/null 2>&1 &
    echo $! > "$GEMINI_PID_FILE"
}

ensure_cowork_sync_daemon() {
    [ ! -f "$AB_DIR/collect_claude_cowork.py" ] && return
    [ ! -d "$COWORK_DIR" ] && return

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$COWORK_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$COWORK_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$COWORK_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$COWORK_PID_FILE" ]; then
        EXISTING_PID=$(cat "$COWORK_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_claude_cowork.py" --daemon "$COWORK_DIR" >/dev/null 2>&1 &
    echo $! > "$COWORK_PID_FILE"
}

ensure_opencode_sync_daemon() {
    [ ! -f "$AB_DIR/collect_opencode.py" ] && return
    has_opencode_data || return

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$OPENCODE_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$OPENCODE_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$OPENCODE_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$OPENCODE_PID_FILE" ]; then
        EXISTING_PID=$(cat "$OPENCODE_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_opencode.py" --daemon >/dev/null 2>&1 &
    echo $! > "$OPENCODE_PID_FILE"
}

ensure_openclaw_sync_daemon() {
    [ ! -f "$AB_DIR/collect_openclaw.py" ] && return
    has_openclaw_data || return

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$OPENCLAW_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$OPENCLAW_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$OPENCLAW_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$OPENCLAW_PID_FILE" ]; then
        EXISTING_PID=$(cat "$OPENCLAW_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_openclaw.py" --daemon >/dev/null 2>&1 &
    echo $! > "$OPENCLAW_PID_FILE"
}

ensure_kimi_sync_daemon() {
    [ ! -f "$AB_DIR/collect_kimi.py" ] && return
    has_kimi_data || return

    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl > /dev/null 2>&1; then
        if [ -f "$KIMI_LAUNCH_AGENT_PLIST" ]; then
            launchctl bootstrap "gui/$(id -u)" "$KIMI_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
            launchctl kickstart -k "gui/$(id -u)/$KIMI_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 && return
        fi
    fi

    if [ -f "$KIMI_PID_FILE" ]; then
        EXISTING_PID=$(cat "$KIMI_PID_FILE" 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            return
        fi
    fi

    nohup python3 "$AB_DIR/collect_kimi.py" --daemon >/dev/null 2>&1 &
    echo $! > "$KIMI_PID_FILE"
}

# Check that we have a token (not just a claim_code)
TOKEN_STATUS=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        c = json.load(f)
    if c.get('token'):
        print('has_token')
    elif c.get('claim_code'):
        print('has_claim')
    else:
        print('nothing')
except: print('nothing')
" "$AB_DIR/config.json" 2>/dev/null)

# If we have a claim_code but no token, try to recover the token
if [ "$TOKEN_STATUS" = "has_claim" ]; then
    python3 -c "
import json, os, ssl, sys, urllib.request

def build_ssl_context():
    candidates = []
    env_bundle = os.environ.get('SSL_CERT_FILE')
    if env_bundle:
        candidates.append(env_bundle)
    try:
        import certifi
        candidates.append(certifi.where())
    except Exception:
        pass
    try:
        defaults = ssl.get_default_verify_paths()
        candidates.extend([defaults.cafile, defaults.openssl_cafile])
    except Exception:
        pass
    candidates.extend([
        '/etc/ssl/cert.pem',
        '/private/etc/ssl/cert.pem',
        '/etc/ssl/certs/ca-certificates.crt',
        '/opt/homebrew/etc/openssl@3/cert.pem',
        '/usr/local/etc/openssl@3/cert.pem',
    ])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen or not os.path.exists(candidate):
            continue
        seen.add(candidate)
        try:
            return ssl.create_default_context(cafile=candidate)
        except Exception:
            continue
    return ssl.create_default_context()

config_path = sys.argv[1]
with open(config_path, encoding='utf-8') as f:
    config = json.load(f)

claim_code = config.get('claim_code', '')
api_base = config.get('api', '').replace('/api/checkin', '')
if not claim_code or not api_base:
    sys.exit(1)

try:
    req = urllib.request.Request(
        f'{api_base}/api/claim/{claim_code}/status',
        headers={'User-Agent': 'AgentBoard-CLI/1.0'},
    )
    resp = urllib.request.urlopen(req, timeout=5, context=build_ssl_context())
    data = json.loads(resp.read())
    if data.get('status') == 'claimed' and data.get('token'):
        config['token'] = data['token']
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
except Exception:
    pass
" "$AB_DIR/config.json" 2>/dev/null

    # Re-check after recovery attempt
    TOKEN_STATUS=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        c = json.load(f)
    print('has_token' if c.get('token') else 'nothing')
except: print('nothing')
" "$AB_DIR/config.json" 2>/dev/null)
fi

[ "$TOKEN_STATUS" != "has_token" ] && exit 0

ensure_claude_sync_daemon
ensure_cowork_sync_daemon

# Read stdin (Claude Code passes JSON with session_id and transcript_path)
SESSION_DATA=""
[ ! -t 0 ] && SESSION_DATA=$(cat)
[ -z "$SESSION_DATA" ] && exit 0

# Extract session_id, transcript_path, and cwd
SESSION_ID=$(echo "$SESSION_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null)
TRANSCRIPT=$(echo "$SESSION_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('transcript_path',''))" 2>/dev/null)
CWD=$(echo "$SESSION_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null)

[ -z "$SESSION_ID" ] || [ -z "$TRANSCRIPT" ] && exit 0

# ── Throttle: max once per 5 minutes per session ──
THROTTLE_DIR="$AB_DIR/throttle"
mkdir -p "$THROTTLE_DIR"
THROTTLE_FILE="$THROTTLE_DIR/$SESSION_ID"

if [ -f "$THROTTLE_FILE" ]; then
    LAST=$(stat -f %m "$THROTTLE_FILE" 2>/dev/null || stat -c %Y "$THROTTLE_FILE" 2>/dev/null)
    NOW=$(date +%s)
    ELAPSED=$(( NOW - LAST ))
    if [ "$ELAPSED" -lt 300 ]; then
        exit 0
    fi
fi

# Touch throttle file to mark this report
touch "$THROTTLE_FILE"

# Clean up old throttle files (>24h)
find "$THROTTLE_DIR" -mmin +1440 -type f -delete 2>/dev/null

# Run collector in background — don't block Claude Code
python3 "$AB_DIR/collect.py" "$SESSION_ID" "$TRANSCRIPT" "$CWD" &

# Opportunistically start other source daemons when available. The daemons own
# full-history sync; this hook only reports the active Claude session to avoid
# overlapping scanners on every Claude Stop event.
if [ -f "$AB_DIR/collect_codex.py" ] && [ -d "$HOME/.codex/sessions" ]; then
    ensure_codex_sync_daemon
fi

if [ -f "$AB_DIR/collect_gemini.py" ]; then
    ensure_gemini_sync_daemon
fi

if [ -f "$AB_DIR/collect_opencode.py" ]; then
    ensure_opencode_sync_daemon
fi

if [ -f "$AB_DIR/collect_openclaw.py" ]; then
    ensure_openclaw_sync_daemon
fi

if [ -f "$AB_DIR/collect_kimi.py" ]; then
    ensure_kimi_sync_daemon
fi

if [ -f "$AB_DIR/collect_claude_cowork.py" ] && [ -d "$COWORK_DIR" ]; then
    ensure_cowork_sync_daemon
fi

exit 0
