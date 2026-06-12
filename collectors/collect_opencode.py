#!/usr/bin/env python3
"""
AgentBoard OpenCode session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from OpenCode local
usage stores. It NEVER reads, stores, or transmits conversation content, code,
prompts, or responses.
"""

import glob
import json
import os
import platform as platform_module
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
SYNC_INTERVAL_SECS = 300
IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
AGENTBOARD_SCRIPT_RELEASE = "2026-06-11"
__version__ = AGENTBOARD_SCRIPT_RELEASE
STATE_VERSION = f"{AGENTBOARD_SCRIPT_RELEASE}:opencode.1"
COMMON_CA_BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)
DB_NAME_RE = re.compile(r"^opencode(?:-[A-Za-z0-9._-]+)?\.db$")


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
SYNC_LOG_PATH = os.path.join(LOG_DIR, "opencode-sync.log")
SYNC_LOCK_PATH = os.path.join(AGENTBOARD_DIR, f"opencode-sync.{HOST_ID}.lock")
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
    line = f"[agentboard-opencode] {message}"
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


def emit_json(payload):
    try:
        print(json.dumps(payload))
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass


def safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def normalize_ts(value):
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number > 1_000_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number).astimezone()
        except Exception:
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def clamp_minutes(active_seconds, max_minutes):
    if active_seconds <= 0:
        return 0
    return min(max_minutes, int(round(active_seconds / 60.0)))


def merge_windows(windows):
    if not windows:
        return []
    ordered = sorted(windows)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def build_engaged_windows(events, gap_cap_secs, tail_secs):
    if not events:
        return []

    windows = []
    sorted_events = sorted(events, key=lambda e: e[1])
    for index, (_, ts, *_rest) in enumerate(sorted_events):
        start = ts.timestamp()
        if index + 1 < len(sorted_events):
            next_start = sorted_events[index + 1][1].timestamp()
            end = min(start + gap_cap_secs, next_start)
        else:
            end = start + tail_secs
        if end > start:
            windows.append((start, end))

    return merge_windows(windows)


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


def post_session(config, session_entry, full_rescan=False):
    payload = json.dumps(
        {
            "token": config["token"],
            "source": "opencode",
            "full_rescan": full_rescan,
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


def split_paths(value):
    if not value:
        return []
    parts = []
    for raw in str(value).replace(os.pathsep, ",").split(","):
        path = raw.strip()
        if path:
            parts.append(path)
    return parts


def resolve_data_dirs(root_dir=""):
    candidates = []
    if root_dir:
        candidates.append(root_dir)
    else:
        candidates.extend(split_paths(os.environ.get("OPENCODE_DATA_DIR", "")))
        candidates.extend(split_paths(os.environ.get("OPENCODE_HOME", "")))
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        app_data = os.environ.get("APPDATA", "")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "opencode"))
        if app_data:
            candidates.append(os.path.join(app_data, "opencode"))
        candidates.append("~/.local/share/opencode")

    roots = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen or not os.path.isdir(normalized):
            continue
        seen.add(normalized)
        roots.append(normalized)
    return roots


def iter_db_files(root_dir=""):
    candidates = []
    if not root_dir:
        candidates.extend(split_paths(os.environ.get("OPENCODE_DB", "")))
    for path in candidates:
        normalized = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(normalized) and DB_NAME_RE.match(os.path.basename(normalized)):
            yield normalized

    seen = set(os.path.abspath(os.path.expanduser(path)) for path in candidates)
    for root in resolve_data_dirs(root_dir):
        try:
            names = sorted(os.listdir(root))
        except Exception:
            continue
        for name in names:
            if not DB_NAME_RE.match(name):
                continue
            path = os.path.join(root, name)
            normalized = os.path.abspath(path)
            if normalized in seen or not os.path.isfile(normalized):
                continue
            seen.add(normalized)
            yield normalized


def iter_legacy_message_files(root_dir=""):
    seen = set()
    for root in resolve_data_dirs(root_dir):
        pattern = os.path.join(root, "storage", "message", "**", "*.json")
        for path in glob.glob(pattern, recursive=True):
            if os.path.isfile(path) and path not in seen:
                seen.add(path)
                yield path


def sidecar_signature(path):
    parts = []
    for candidate in (path, f"{path}-wal"):
        try:
            stat_result = os.stat(candidate)
        except OSError:
            continue
        parts.append(f"{os.path.basename(candidate)}:{stat_result.st_mtime_ns}:{stat_result.st_size}")
    return "|".join(parts)


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


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
        "assistant_msgs": 0,
        "projects": set(),
        "tool_counts": Counter(),
        "tool_calls": 0,
        "files_touched": set(),
        "seen": set(),
        "records": [],
    }


def message_fingerprint(message, message_id, session_id):
    tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    time_obj = message.get("time") if isinstance(message.get("time"), dict) else {}
    key = message_id or ""
    if key:
        return f"id:{key}"
    values = [
        "fp",
        session_id or "",
        str(time_obj.get("created") or ""),
        str(time_obj.get("completed") or ""),
        str(message.get("providerID") or ""),
        str(message.get("modelID") or ""),
        str(tokens.get("input") or ""),
        str(tokens.get("output") or ""),
        str(tokens.get("reasoning") or ""),
        str(tokens.get("total") or ""),
        str(cache.get("read") or ""),
        str(cache.get("write") or ""),
        str(message.get("cost") or ""),
    ]
    return ":".join(values)


def tool_names_from_message(message):
    names = []
    for key in ("toolCalls", "tool_calls", "tools"):
        calls = message.get(key)
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                name = call.get("name") or call.get("tool") or call.get("toolName")
                if name:
                    names.append(str(name))
    return names


def collect_tool_names(value):
    names = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return names
    if isinstance(value, list):
        for item in value:
            names.extend(collect_tool_names(item))
        return names
    if not isinstance(value, dict):
        return names

    type_hint = str(value.get("type") or value.get("kind") or value.get("partType") or "").lower()
    looks_like_tool = "tool" in type_hint or any(
        key in value for key in ("tool", "toolName", "tool_name", "toolCall", "tool_call")
    )
    if looks_like_tool:
        name = (
            value.get("name")
            or value.get("tool")
            or value.get("toolName")
            or value.get("tool_name")
        )
        if isinstance(name, str) and name.strip():
            names.append(name.strip())

    for key in (
        "toolCall",
        "toolCalls",
        "tool_call",
        "tool_calls",
        "tools",
        "parts",
        "content",
        "data",
        "input",
    ):
        nested = value.get(key)
        if isinstance(nested, (dict, list, str)):
            names.extend(collect_tool_names(nested))
    return names


def extract_workspace_root(message):
    path_obj = message.get("path") if isinstance(message.get("path"), dict) else {}
    return (
        path_obj.get("root")
        or message.get("directory")
        or message.get("cwd")
        or "OpenCode"
    )


def parse_message(message, message_id=None, session_id=None, extra_tool_names=None):
    if not isinstance(message, dict):
        return None
    tokens = message.get("tokens")
    if not isinstance(tokens, dict):
        return None
    session_id = session_id or message.get("sessionID") or message.get("sessionId") or "unknown"
    time_obj = message.get("time") if isinstance(message.get("time"), dict) else {}
    created = normalize_ts(time_obj.get("created") or message.get("timestamp"))
    if not created:
        return None
    completed = normalize_ts(time_obj.get("completed"))

    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    input_tokens = safe_int(tokens.get("input"))
    output_tokens = safe_int(tokens.get("output"))
    reasoning_tokens = safe_int(tokens.get("reasoning"))
    cache_read = safe_int(cache.get("read") or tokens.get("cacheRead") or tokens.get("cache_read"))
    cache_write = safe_int(cache.get("write") or tokens.get("cacheWrite") or tokens.get("cache_write"))
    total = safe_int(tokens.get("total") or tokens.get("totalTokens"))

    if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0 and total > 0:
        output_tokens = total

    provider_total = total or input_tokens + output_tokens + cache_read + cache_write
    if provider_total <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return None

    end = completed.timestamp() if completed and completed > created else created.timestamp() + SESSION_TAIL_SECS
    start = created.timestamp()
    fingerprint = message_fingerprint(message, message_id, session_id)
    return {
        "fingerprint": fingerprint,
        "session_id": str(session_id),
        "created": created,
        "window": (start, end),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "provider_total_tokens": provider_total,
        "thoughts_tokens": reasoning_tokens,
        "tool_tokens": 0,
        "workspace_root": str(extract_workspace_root(message)),
        "tool_names": tool_names_from_message(message) + list(extra_tool_names or []),
    }


def add_record(days, record):
    date_key = record["created"].astimezone().strftime("%Y-%m-%d")
    day = days[(record["session_id"], date_key)]
    if record["fingerprint"] in day["seen"]:
        return
    day["seen"].add(record["fingerprint"])
    day["records"].append(record)
    day["events"].append(("assistant_message", record["created"], record["window"]))
    day["assistant_msgs"] += 1
    day["input_tokens"] += record["input_tokens"]
    day["output_tokens"] += record["output_tokens"]
    day["cache_read_tokens"] += record["cache_read_tokens"]
    day["cache_creation_tokens"] += record["cache_creation_tokens"]
    day["provider_total_tokens"] += record["provider_total_tokens"]
    day["thoughts_tokens"] += record["thoughts_tokens"]
    day["tool_tokens"] += record["tool_tokens"]
    day["projects"].add(record["workspace_root"])
    for tool_name in record["tool_names"]:
        day["tool_counts"][tool_name] += 1
        day["tool_calls"] += 1


def open_db(path):
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 1000")
    return conn


def table_columns(conn, table):
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def parse_part_tool_names(conn):
    columns = table_columns(conn, "part")
    if not columns:
        return defaultdict(list)

    message_col = None
    for candidate in ("message_id", "messageID", "messageId", "message"):
        if candidate in columns:
            message_col = candidate
            break
    if not message_col:
        return defaultdict(list)

    select_cols = [message_col]
    for candidate in ("data", "json", "part", "content", "type", "name", "tool", "toolName", "tool_name"):
        if candidate in columns and candidate not in select_cols:
            select_cols.append(candidate)
    if len(select_cols) == 1:
        return defaultdict(list)

    tool_names = defaultdict(list)
    query = f"select {', '.join(select_cols)} from part"
    try:
        for row in conn.execute(query):
            message_id = row[0]
            if not message_id:
                continue
            row_payload = {}
            for index, column in enumerate(select_cols[1:], start=1):
                value = row[index]
                if column in ("data", "json", "part", "content"):
                    for name in collect_tool_names(value):
                        tool_names[str(message_id)].append(name)
                elif value not in (None, ""):
                    row_payload[column] = value
            for name in collect_tool_names(row_payload):
                tool_names[str(message_id)].append(name)
    except Exception:
        return defaultdict(list)
    return tool_names


def parse_database(path):
    days = defaultdict(new_day)
    try:
        conn = open_db(path)
    except Exception:
        return days
    try:
        columns = table_columns(conn, "message")
        if not columns:
            return days
        id_col = "id" if "id" in columns else None
        session_col = "session_id" if "session_id" in columns else ("sessionID" if "sessionID" in columns else None)
        data_col = "data" if "data" in columns else ("json" if "json" in columns else None)
        if not data_col:
            return days
        select_cols = [
            id_col or "NULL",
            session_col or "NULL",
            data_col,
        ]
        tool_names_by_message = parse_part_tool_names(conn)
        query = f"select {', '.join(select_cols)} from message"
        for message_id, session_id, raw_data in conn.execute(query):
            try:
                message = json.loads(raw_data)
            except Exception:
                continue
            record = parse_message(
                message,
                message_id=message_id,
                session_id=session_id,
                extra_tool_names=tool_names_by_message.get(str(message_id), []),
            )
            if record:
                add_record(days, record)
    except Exception:
        return days
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return days


def parse_legacy_message_file(path):
    days = defaultdict(new_day)
    try:
        with open(path, encoding="utf-8") as f:
            message = json.load(f)
    except Exception:
        return days
    record = parse_message(
        message,
        message_id=message.get("id") if isinstance(message, dict) else None,
        session_id=message.get("sessionID") if isinstance(message, dict) else None,
    )
    if record:
        add_record(days, record)
    return days


def stats_from_days(days):
    results = {}
    for (session_id, date_str), day in days.items():
        events = sorted(day["events"], key=lambda e: e[1])
        if not events:
            continue
        windows = build_engaged_windows(events, IDLE_GAP_SECS, SESSION_TAIL_SECS)
        active_seconds = int(sum(end - start for start, end in windows))
        coding_time_mins = clamp_minutes(active_seconds, MAX_MINS_PER_SESSION)
        ai_time_mins = (
            max(1, int(day["output_tokens"] / HUMAN_TOKENS_PER_MIN))
            if day["output_tokens"] > 0
            else 0
        )
        results[f"opencode:{session_id}:{date_str}"] = {
            "session_id": f"opencode:{session_id}",
            "date": date_str,
            "coding_time_mins": coding_time_mins,
            "ai_time_mins": ai_time_mins,
            "tokens_used": day["input_tokens"] + day["output_tokens"],
            "provider_total_tokens": day["provider_total_tokens"],
            "input_tokens": day["input_tokens"],
            "output_tokens": day["output_tokens"],
            "cache_read_tokens": day["cache_read_tokens"],
            "cache_creation_tokens": day["cache_creation_tokens"],
            "thoughts_tokens": day["thoughts_tokens"],
            "tool_tokens": day["tool_tokens"],
            "lines_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "messages": day["assistant_msgs"],
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


def merge_days(target, incoming):
    for day in incoming.values():
        for record in day.get("records", []):
            add_record(target, record)


def parse_all(root_dir=""):
    days = defaultdict(new_day)
    for db_file in iter_db_files(root_dir):
        merge_days(days, parse_database(db_file))
    for message_file in iter_legacy_message_files(root_dir):
        merge_days(days, parse_legacy_message_file(message_file))
    return stats_from_days(days)


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"opencode-sync-state.{HOST_ID}.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            raw_state = json.load(f)
    except Exception:
        return state_path, {}, True
    if not isinstance(raw_state, dict) or raw_state.get("_collector_version") != STATE_VERSION:
        return state_path, {}, True
    files = raw_state.get("files")
    if isinstance(files, dict):
        return state_path, files, False
    return state_path, {}, True


def save_sync_state(state_path, state):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {"_collector_version": STATE_VERSION, "files": state},
            f,
            indent=2,
            sort_keys=True,
        )


def acquire_sync_lock():
    try:
        import fcntl
    except Exception:
        return None
    os.makedirs(AGENTBOARD_DIR, exist_ok=True)
    lock_file = open(SYNC_LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    return lock_file


def sync_mode(root_dir="", verbose=False, force_rescan=False):
    config = load_config()
    if not config:
        return
    sync_lock = acquire_sync_lock()
    if sync_lock is False:
        if verbose:
            log_sync("another OpenCode sync is already running; skipping")
        return
    state_path, state, state_invalidated = load_sync_state()
    if force_rescan:
        state_invalidated = True
    next_state = {}
    pending_state = {}
    changed_days = defaultdict(new_day)

    for db_file in iter_db_files(root_dir):
        signature = sidecar_signature(db_file)
        if not state_invalidated and state.get(db_file) == signature:
            next_state[db_file] = signature
            continue
        try:
            merge_days(changed_days, parse_database(db_file))
        except Exception as error:
            log_sync_error(f"failed to parse {db_file}", error)
            continue
        pending_state[db_file] = signature

    for message_file in iter_legacy_message_files(root_dir):
        try:
            signature = file_signature(message_file)
        except OSError:
            continue
        if not state_invalidated and state.get(message_file) == signature:
            next_state[message_file] = signature
            continue
        try:
            merge_days(changed_days, parse_legacy_message_file(message_file))
        except Exception as error:
            log_sync_error(f"failed to parse {message_file}", error)
            continue
        pending_state[message_file] = signature

    try:
        full_days = defaultdict(new_day)
        if changed_days:
            for db_file in iter_db_files(root_dir):
                merge_days(full_days, parse_database(db_file))
            for message_file in iter_legacy_message_files(root_dir):
                merge_days(full_days, parse_legacy_message_file(message_file))
        selected_days = defaultdict(new_day)
        for key in changed_days.keys():
            if key in full_days:
                selected_days[key] = full_days[key]
        full_rescan_mode = bool(force_rescan or state_invalidated)
        for stats in stats_from_days(selected_days).values():
            post_session(config, stats, full_rescan=full_rescan_mode)
    except Exception as error:
        log_sync_error("failed to sync aggregated OpenCode sessions", error)
        return

    next_state.update(pending_state)

    try:
        save_sync_state(state_path, next_state)
    except Exception as error:
        log_sync_error("failed to save sync state", error)


def summary_mode(root_dir=""):
    sessions = list(parse_all(root_dir).values())
    daily = {}
    for stats in sessions:
        date_str = stats["date"]
        existing = daily.get(date_str)
        if not existing:
            daily[date_str] = dict(stats)
            daily[date_str]["sessions"] = 1
            continue
        for key in (
            "coding_time_mins",
            "ai_time_mins",
            "tokens_used",
            "provider_total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "thoughts_tokens",
            "tool_tokens",
            "lines_changed",
            "lines_added",
            "lines_removed",
            "messages",
            "assistant_messages",
            "projects",
            "tool_calls",
            "files_touched",
        ):
            existing[key] += stats[key]
        existing["sessions"] += 1
        existing["tool_breakdown"] = build_tool_breakdown(
            Counter({entry["tool"]: entry["count"] for entry in existing.get("tool_breakdown", [])})
            + Counter({entry["tool"]: entry["count"] for entry in stats.get("tool_breakdown", [])})
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
        "total_sessions": len(sessions),
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

    emit_json({"summary": totals, "sessions": sessions, "daily": daily})


def daemon_mode(root_dir=""):
    while True:
        try:
            sync_mode(root_dir)
        except Exception as error:
            log_sync_error("daemon sync iteration failed", error)
        time.sleep(SYNC_INTERVAL_SECS)


def parse_root_and_flags(args):
    root_dir = ""
    force_rescan = False
    for arg in args:
        if arg in ("--force-rescan", "--full-rescan"):
            force_rescan = True
        elif arg == "--json":
            continue
        elif not arg.startswith("--") and not root_dir:
            root_dir = arg
    return root_dir, force_rescan


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--summary":
        root_dir, _ = parse_root_and_flags(sys.argv[2:])
        summary_mode(root_dir)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--sync":
        root_dir, force_rescan = parse_root_and_flags(sys.argv[2:])
        sync_mode(root_dir, force_rescan=force_rescan)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--daemon":
        root_dir, _ = parse_root_and_flags(sys.argv[2:])
        daemon_mode(root_dir)
        return
    sys.exit(0)


if __name__ == "__main__":
    main()
