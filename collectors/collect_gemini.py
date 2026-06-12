#!/usr/bin/env python3
"""
AgentBoard Gemini CLI session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from Gemini CLI
session files. It NEVER reads, stores, or transmits conversation content,
code, prompts, or responses.
"""

import glob
import json
import os
import platform as platform_module
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

MAX_MINS_PER_DAY = 960
MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
SYNC_INTERVAL_SECS = 300
GEMINI_IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
AGENTBOARD_SCRIPT_RELEASE = "2026-04-30"
__version__ = AGENTBOARD_SCRIPT_RELEASE
COMMON_CA_BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


def build_ssl_context():
    candidates = []
    env_bundle = os.environ.get("SSL_CERT_FILE")
    if env_bundle:
        candidates.append(env_bundle)
    try:
        import certifi  # type: ignore

        candidates.append(certifi.where())
    except Exception:
        pass
    try:
        defaults = ssl.get_default_verify_paths()
        candidates.extend([defaults.cafile, defaults.openssl_cafile])
    except Exception:
        pass
    candidates.extend(COMMON_CA_BUNDLE_PATHS)
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


def sanitize_host_id(value):
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._")
    return sanitized[:80] or "unknown"


def detect_device_name():
    env_value = os.environ.get("AGENTBOARD_DEVICE_NAME", "").strip()
    if env_value:
        return env_value
    try:
        hostname = socket.gethostname().strip()
        if hostname:
            return hostname
    except Exception:
        pass
    return ""


def get_host_id():
    return sanitize_host_id(os.environ.get("AGENTBOARD_HOST_ID", "") or detect_device_name())


def normalize_device_platform(value):
    normalized = (value or "").strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("darwin") or normalized in ("mac", "macos", "mac os", "mac os x"):
        return "macos"
    if normalized.startswith("win") or "windows" in normalized:
        return "win32"
    if normalized.startswith("linux") or "linux" in normalized:
        return "linux"
    return normalized


def detect_platform_name():
    env_value = os.environ.get("AGENTBOARD_PLATFORM", "").strip()
    if env_value:
        return normalize_device_platform(env_value)
    try:
        return normalize_device_platform(platform_module.system())
    except Exception:
        return ""


SSL_CONTEXT = build_ssl_context()
AGENTBOARD_DIR = os.path.expanduser("~/.agentboard")
HOST_ID = get_host_id()
LOG_DIR = os.path.join(AGENTBOARD_DIR, "logs")
SYNC_LOG_PATH = os.path.join(LOG_DIR, "gemini-sync.log")
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_log(path):
    try:
        if not os.path.exists(path) or os.path.getsize(path) < MAX_LOG_BYTES:
            return
        backup_one = path + ".1"
        backup_two = path + ".2"
        if os.path.exists(backup_two):
            os.remove(backup_two)
        if os.path.exists(backup_one):
            os.replace(backup_one, backup_two)
        os.replace(path, backup_one)
    except Exception:
        pass


def log_sync(message):
    line = f"[agentboard-gemini] {message}"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rotate_log(SYNC_LOG_PATH)
        with open(SYNC_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stderr.isatty():
        print(line, file=sys.stderr)


def log_sync_error(context, error):
    detail = str(error)
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {error.code}: {error.reason}"
        if body:
            detail += f" body={body}"
    log_sync(f"{context}: {detail}")


def clamp_minutes(active_seconds, max_minutes):
    if active_seconds <= 0:
        return 0
    return min(max_minutes, int(round(active_seconds / 60.0)))


def build_engaged_windows(events, gap_cap_secs, tail_secs):
    if not events:
        return []
    windows = []
    sorted_events = sorted(events, key=lambda e: e[1])
    for index, (_, ts) in enumerate(sorted_events):
        start = ts.timestamp()
        if index + 1 < len(sorted_events):
            next_start = sorted_events[index + 1][1].timestamp()
            end = min(start + gap_cap_secs, next_start)
        else:
            end = start + tail_secs
        if end > start:
            windows.append((start, end))
    if not windows:
        return []
    merged = [list(windows[0])]
    for start, end in windows[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


def windows_to_payload(windows):
    return [
        {
            "start_at": datetime.fromtimestamp(start).astimezone().isoformat(),
            "end_at": datetime.fromtimestamp(end).astimezone().isoformat(),
        }
        for start, end in windows
    ]


def build_tool_breakdown(tool_counts, top_n=4):
    total_calls = sum(tool_counts.values())
    if total_calls <= 0:
        return []
    ordered = sorted(tool_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:top_n]
    return [
        {
            "tool": tool,
            "count": count,
            "percentage": int(round((count / total_calls) * 100)),
        }
        for tool, count in ordered
    ]


def load_config():
    config_path = os.path.expanduser("~/.agentboard/config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return None
    api = config.get("api", "")
    token = config.get("token", "")
    if not api or not token:
        return None
    return {
        "token": token,
        "api": api,
        "device_name": config.get("device_name") or detect_device_name(),
        "platform": config.get("platform") or detect_platform_name(),
    }


def post_session(config, session_entry):
    payload = json.dumps(
        {
            "token": config["token"],
            "source": "gemini_cli",
            "device_name": config.get("device_name", ""),
            "platform": config.get("platform", ""),
            "session_id": session_entry["session_id"],
            "date": session_entry["date"],
            "coding_time_mins": session_entry["coding_time_mins"],
            "ai_time_mins": session_entry["ai_time_mins"],
            "tokens_used": session_entry["tokens_used"],
            "provider_total_tokens": session_entry["provider_total_tokens"],
            "input_tokens": session_entry["input_tokens"],
            "output_tokens": session_entry["output_tokens"],
            "cache_read_tokens": session_entry["cache_read_tokens"],
            "cache_creation_tokens": session_entry["cache_creation_tokens"],
            "thoughts_tokens": session_entry["thoughts_tokens"],
            "tool_tokens": session_entry["tool_tokens"],
            "lines_changed": session_entry["lines_changed"],
            "lines_added": session_entry["lines_added"],
            "lines_removed": session_entry["lines_removed"],
            "sessions": 1,
            "messages": session_entry["messages"],
            "assistant_messages": session_entry["assistant_messages"],
            "projects": session_entry["projects"],
            "tool_calls": session_entry["tool_calls"],
            "tool_breakdown": session_entry.get("tool_breakdown", []),
            "files_touched": session_entry["files_touched"],
            "first_event_at": session_entry["first_event_at"],
            "last_event_at": session_entry["last_event_at"],
            "engaged_windows": session_entry["engaged_windows"],
            "collector_version": __version__,
        }
    )
    req = urllib.request.Request(
        config["api"],
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AgentBoard-CLI/1.0"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)


def normalize_ts(ts_str):
    if isinstance(ts_str, (int, float)):
        try:
            value = float(ts_str)
            if value > 1_000_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value).astimezone()
        except Exception:
            return None
    if not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def new_day():
    return {
        "events": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "provider_total_tokens": 0,
        "thoughts_tokens": 0,
        "tool_tokens": 0,
        "user_msgs": 0,
        "assistant_msgs": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "projects": set(),
        "tool_calls": 0,
        "tool_counts": Counter(),
        "files_touched": set(),
    }


def safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def first_dict(*values):
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def token_value(source, *keys):
    if not isinstance(source, dict):
        return 0
    for key in keys:
        if key in source:
            return safe_int(source.get(key))
    return 0


def token_stats(message):
    nested_message = message.get("message") if isinstance(message.get("message"), dict) else {}
    response = message.get("response") if isinstance(message.get("response"), dict) else {}
    tokens = first_dict(message.get("tokens"), nested_message.get("tokens"))
    if tokens:
        input_tokens = token_value(tokens, "input", "input_tokens", "inputTokens")
        output_tokens = token_value(tokens, "output", "output_tokens", "outputTokens")
        cache_read_tokens = token_value(tokens, "cached", "cache_read", "cacheRead")
        thoughts_tokens = token_value(tokens, "thoughts", "thoughts_tokens", "thoughtsTokens")
        tool_tokens = token_value(tokens, "tool", "tool_tokens", "toolTokens")
        total_tokens = token_value(tokens, "total", "total_tokens", "totalTokens")
        return {
            "input": input_tokens,
            "output": output_tokens,
            "cached": cache_read_tokens,
            "thoughts": thoughts_tokens,
            "tool": tool_tokens,
            "total": total_tokens or input_tokens + output_tokens,
        }

    usage = first_dict(
        message.get("usageMetadata"),
        nested_message.get("usageMetadata"),
        response.get("usageMetadata"),
        message.get("usage"),
        nested_message.get("usage"),
    )
    if usage:
        input_tokens = token_value(
            usage,
            "promptTokenCount",
            "inputTokenCount",
            "input_tokens",
            "inputTokens",
        )
        output_tokens = token_value(
            usage,
            "candidatesTokenCount",
            "outputTokenCount",
            "output_tokens",
            "outputTokens",
        )
        cache_read_tokens = token_value(
            usage,
            "cachedContentTokenCount",
            "cacheReadTokenCount",
            "cache_read_tokens",
            "cacheReadTokens",
        )
        thoughts_tokens = token_value(
            usage,
            "thoughtsTokenCount",
            "thoughtTokenCount",
            "thoughts_tokens",
            "thoughtsTokens",
        )
        tool_tokens = token_value(
            usage,
            "toolUsePromptTokenCount",
            "toolTokenCount",
            "tool_tokens",
            "toolTokens",
        )
        total_tokens = token_value(
            usage,
            "totalTokenCount",
            "total_tokens",
            "totalTokens",
        )
        return {
            "input": input_tokens,
            "output": output_tokens,
            "cached": cache_read_tokens,
            "thoughts": thoughts_tokens,
            "tool": tool_tokens,
            "total": total_tokens or input_tokens + output_tokens,
        }

    return {
        "input": 0,
        "output": 0,
        "cached": 0,
        "thoughts": 0,
        "tool": 0,
        "total": 0,
    }


def session_id_from_path(session_file):
    base = os.path.basename(session_file)
    for suffix in (".jsonl", ".json"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return os.path.splitext(base)[0]


def message_candidates(record):
    if not isinstance(record, dict):
        return []
    messages = record.get("messages")
    if isinstance(messages, list):
        return [message for message in messages if isinstance(message, dict)]
    nested = record.get("message")
    if isinstance(nested, dict):
        merged = dict(nested)
        for key in ("timestamp", "sessionId", "projectHash"):
            if not merged.get(key) and record.get(key):
                merged[key] = record.get(key)
        for key in ("tokens", "usageMetadata", "usage", "toolCalls", "tool_calls", "response"):
            if not merged.get(key) and record.get(key):
                merged[key] = record.get(key)
        if not merged.get("type") and record.get("type") in ("user", "gemini", "assistant", "model"):
            merged["type"] = record.get("type")
        return [merged]
    return [record]


def load_session_records(session_file):
    session_id = session_id_from_path(session_file)
    project_hash = None
    messages = []

    if session_file.endswith(".jsonl"):
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict):
                    session_id = record.get("sessionId") or record.get("session_id") or session_id
                    project_hash = record.get("projectHash") or record.get("project_hash") or project_hash
                    messages.extend(message_candidates(record))
        return session_id, project_hash, messages

    with open(session_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        session_id = data.get("sessionId") or data.get("session_id") or session_id
        project_hash = data.get("projectHash") or data.get("project_hash")
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    return session_id, project_hash, [message for message in messages if isinstance(message, dict)]


def message_timestamp(message):
    nested_message = message.get("message") if isinstance(message.get("message"), dict) else {}
    return (
        message.get("timestamp")
        or nested_message.get("timestamp")
        or message.get("createdAt")
        or message.get("created_at")
        or message.get("time")
    )


def message_type(message):
    stats = token_stats(message)
    raw_type = str(message.get("type") or "").lower()
    role = str(message.get("role") or "").lower()
    if raw_type in ("user", "human") or role == "user":
        return "user"
    if raw_type in ("gemini", "assistant", "model") or role in ("assistant", "model"):
        return "gemini"
    if stats["total"] or stats["input"] or stats["output"] or stats["cached"]:
        return "gemini"
    return raw_type


def message_tool_calls(message):
    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    return tool_calls if isinstance(tool_calls, list) else []


def parse_session(session_file):
    session_id, project_hash, messages = load_session_records(session_file)
    days = defaultdict(new_day)

    for message in messages:
        ts = normalize_ts(message_timestamp(message))
        if not ts:
            continue
        date_key = ts.astimezone().strftime("%Y-%m-%d")
        day = days[date_key]
        if project_hash:
            day["projects"].add(project_hash)
        msg_type = message_type(message)
        if msg_type == "user":
            day["events"].append(("user_message", ts))
            day["user_msgs"] += 1
            continue
        if msg_type != "gemini":
            continue
        day["events"].append(("assistant_message", ts))
        day["assistant_msgs"] += 1
        tokens = token_stats(message)
        day["input_tokens"] += tokens["input"]
        day["output_tokens"] += tokens["output"]
        day["cache_read_tokens"] += tokens["cached"]
        day["thoughts_tokens"] += tokens["thoughts"]
        day["tool_tokens"] += tokens["tool"]
        day["provider_total_tokens"] += tokens["total"]
        for tool_call in message_tool_calls(message):
            tool_name = tool_call.get("name", "")
            if tool_name:
                day["tool_counts"][tool_name] += 1
            day["tool_calls"] += 1
            args = tool_call.get("args") or {}
            if isinstance(args, dict):
                file_path = args.get("file_path") or args.get("path")
                if file_path:
                    day["files_touched"].add(file_path)
                if tool_name == "write_file":
                    content = args.get("content", "")
                    day["lines_added"] += len(content.splitlines()) if content else 0
                elif tool_name == "replace":
                    old = args.get("old_string", "")
                    new = args.get("new_string", "")
                    day["lines_removed"] += len(old.splitlines()) if old else 0
                    day["lines_added"] += len(new.splitlines()) if new else 0

    results = {}
    for date_str, day in days.items():
        events = sorted(day["events"], key=lambda e: e[1])
        if not events:
            continue
        windows = build_engaged_windows(events, GEMINI_IDLE_GAP_SECS, SESSION_TAIL_SECS)
        active_seconds = int(sum(end - start for start, end in windows))
        coding_time_mins = clamp_minutes(active_seconds, MAX_MINS_PER_SESSION)
        ai_time_mins = (
            max(1, int(day["output_tokens"] / HUMAN_TOKENS_PER_MIN))
            if day["output_tokens"] > 0
            else 0
        )
        results[date_str] = {
            "session_id": f"gemini:{session_id}",
            "date": date_str,
            "coding_time_mins": coding_time_mins,
            "ai_time_mins": ai_time_mins,
            "tokens_used": day["input_tokens"] + day["output_tokens"],
            "provider_total_tokens": day["provider_total_tokens"],
            "input_tokens": day["input_tokens"],
            "output_tokens": day["output_tokens"],
            "cache_read_tokens": day["cache_read_tokens"],
            "cache_creation_tokens": 0,
            "thoughts_tokens": day["thoughts_tokens"],
            "tool_tokens": day["tool_tokens"],
            "lines_changed": day["lines_added"] - day["lines_removed"],
            "lines_added": day["lines_added"],
            "lines_removed": day["lines_removed"],
            "messages": day["user_msgs"] + day["assistant_msgs"],
            "assistant_messages": day["assistant_msgs"],
            "projects": len(day["projects"]),
            "tool_calls": day["tool_calls"],
            "tool_breakdown": build_tool_breakdown(day["tool_counts"]),
            "files_touched": len(day["files_touched"]),
            "first_event_at": events[0][1].isoformat(),
            "last_event_at": events[-1][1].isoformat(),
            "engaged_windows": windows_to_payload(windows),
        }
    return results


def resolve_root_dirs(root_dir=""):
    candidates = []
    if root_dir:
        candidates.append(root_dir)
    else:
        gemini_home = os.environ.get("GEMINI_CLI_HOME", "")
        if gemini_home:
            candidates.append(os.path.join(gemini_home, "tmp"))
        candidates.append(os.path.expanduser("~/.gemini/tmp"))

    roots = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen or not os.path.isdir(normalized):
            continue
        seen.add(normalized)
        roots.append(normalized)
    return roots


def iter_session_files(root_dir=""):
    seen = set()
    patterns = (
        "**/session-*.json",
        "**/session-*.jsonl",
        "**/chats/**/*.jsonl",
    )
    for root in resolve_root_dirs(root_dir):
        for pattern in patterns:
            for path in glob.glob(os.path.join(root, pattern), recursive=True):
                if os.path.isfile(path) and path not in seen:
                    seen.add(path)
                    yield path


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"gemini-sync-state.{HOST_ID}.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            raw_state = json.load(f)
    except Exception:
        return state_path, {}, False

    if not isinstance(raw_state, dict):
        return state_path, {}, False

    if raw_state.get("_collector_version") != __version__:
        return state_path, {}, True

    files = raw_state.get("files")
    if isinstance(files, dict):
        return state_path, files, False
    return state_path, raw_state, False


def save_sync_state(state_path, state):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {"_collector_version": __version__, "files": state},
            f,
            indent=2,
            sort_keys=True,
        )


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def sync_mode(root_dir):
    config = load_config()
    if not config or not resolve_root_dirs(root_dir):
        return
    state_path, state, state_invalidated = load_sync_state()
    next_state = {}
    for session_file in iter_session_files(root_dir):
        try:
            signature = file_signature(session_file)
        except OSError:
            continue
        if not state_invalidated and state.get(session_file) == signature:
            next_state[session_file] = signature
            continue
        try:
            for stats in parse_session(session_file).values():
                post_session(config, stats)
        except Exception as error:
            log_sync_error(f"failed to sync {session_file}", error)
            continue
        next_state[session_file] = signature
    save_sync_state(state_path, next_state)


def summary_mode(root_dir):
    all_sessions = []
    daily = {}

    for session_file in iter_session_files(root_dir):
        try:
            session_days = parse_session(session_file)
        except Exception:
            continue

        for date_str, stats in session_days.items():
            all_sessions.append(stats)
            existing = daily.get(date_str)
            if not existing:
                daily[date_str] = dict(stats)
                continue

            existing["coding_time_mins"] += stats["coding_time_mins"]
            existing["ai_time_mins"] += stats["ai_time_mins"]
            existing["tokens_used"] += stats["tokens_used"]
            existing["provider_total_tokens"] += stats["provider_total_tokens"]
            existing["input_tokens"] += stats["input_tokens"]
            existing["output_tokens"] += stats["output_tokens"]
            existing["cache_read_tokens"] += stats["cache_read_tokens"]
            existing["cache_creation_tokens"] += stats["cache_creation_tokens"]
            existing["thoughts_tokens"] += stats["thoughts_tokens"]
            existing["tool_tokens"] += stats["tool_tokens"]
            existing["lines_changed"] += stats["lines_changed"]
            existing["lines_added"] += stats["lines_added"]
            existing["lines_removed"] += stats["lines_removed"]
            existing["messages"] += stats["messages"]
            existing["assistant_messages"] += stats["assistant_messages"]
            existing["projects"] += stats["projects"]
            existing["tool_calls"] += stats["tool_calls"]
            existing["files_touched"] += stats["files_touched"]
            existing["sessions"] = existing.get("sessions", 1) + 1
            existing["tool_breakdown"] = build_tool_breakdown(
                Counter(
                    {
                        entry["tool"]: entry["count"]
                        for entry in existing.get("tool_breakdown", [])
                    }
                )
                + Counter(
                    {
                        entry["tool"]: entry["count"]
                        for entry in stats.get("tool_breakdown", [])
                    }
                )
            )

    totals = {
        "total_coding_mins": 0,
        "total_ai_mins": 0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_lines_changed": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "total_sessions": len(all_sessions),
        "total_messages": 0,
        "total_assistant_messages": 0,
        "total_tool_calls": 0,
        "total_files_touched": 0,
        "total_days": len(daily),
    }
    for stats in daily.values():
        totals["total_coding_mins"] += stats["coding_time_mins"]
        totals["total_ai_mins"] += stats["ai_time_mins"]
        totals["total_tokens"] += stats["provider_total_tokens"]
        totals["total_input_tokens"] += stats["input_tokens"]
        totals["total_output_tokens"] += stats["output_tokens"]
        totals["total_lines_changed"] += stats["lines_changed"]
        totals["total_lines_added"] += stats["lines_added"]
        totals["total_lines_removed"] += stats["lines_removed"]
        totals["total_messages"] += stats["messages"]
        totals["total_assistant_messages"] += stats["assistant_messages"]
        totals["total_tool_calls"] += stats["tool_calls"]
        totals["total_files_touched"] += stats["files_touched"]

    print(json.dumps({"summary": totals, "sessions": all_sessions, "daily": daily}))


def daemon_mode(root_dir):
    while True:
        try:
            sync_mode(root_dir)
        except Exception as error:
            log_sync_error("daemon sync iteration failed", error)
        time.sleep(SYNC_INTERVAL_SECS)


def main():
    gemini_dir = ""
    if len(sys.argv) >= 2 and sys.argv[1] == "--summary":
        if len(sys.argv) >= 3:
            gemini_dir = sys.argv[2]
        summary_mode(gemini_dir)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--sync":
        if len(sys.argv) >= 3:
            gemini_dir = sys.argv[2]
        sync_mode(gemini_dir)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--daemon":
        if len(sys.argv) >= 3:
            gemini_dir = sys.argv[2]
        daemon_mode(gemini_dir)
        return
    sys.exit(0)


if __name__ == "__main__":
    main()
