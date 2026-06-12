#!/usr/bin/env python3
"""
AgentBoard OpenClaw session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from OpenClaw local
JSONL session files. It NEVER reads, stores, or transmits conversation content,
code, prompts, or responses.
"""

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

MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
SYNC_INTERVAL_SECS = 300
IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
AGENTBOARD_SCRIPT_RELEASE = "2026-06-11"
__version__ = AGENTBOARD_SCRIPT_RELEASE
STATE_VERSION = f"{AGENTBOARD_SCRIPT_RELEASE}:openclaw.1"
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
SYNC_LOG_PATH = os.path.join(LOG_DIR, "openclaw-sync.log")
SYNC_LOCK_PATH = os.path.join(AGENTBOARD_DIR, f"openclaw-sync.{HOST_ID}.lock")
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
    line = f"[agentboard-openclaw] {message}"
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
            "source": "openclaw",
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


def resolve_roots(root_dir=""):
    candidates = []
    if root_dir:
        candidates.append(root_dir)
    else:
        candidates.extend(split_paths(os.environ.get("OPENCLAW_DIR", "")))
        candidates.extend(split_paths(os.environ.get("OPENCLAW_HOME", "")))
        candidates.extend(["~/.openclaw", "~/.clawdbot", "~/.moltbot", "~/.moldbot"])

    roots = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen or not os.path.isdir(normalized):
            continue
        seen.add(normalized)
        roots.append(normalized)
    return roots


def is_session_file(name):
    index = name.find(".jsonl")
    if index < 0:
        return False
    suffix = name[index:]
    return (
        suffix == ".jsonl"
        or suffix.startswith(".jsonl.deleted.")
        or suffix.startswith(".jsonl.reset.")
    )


def iter_session_files(root_dir=""):
    seen = set()
    for root in resolve_roots(root_dir):
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(current, name))]
            for filename in sorted(filenames):
                if not is_session_file(filename):
                    continue
                path = os.path.join(current, filename)
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                yield path


def session_id_from_path(path):
    filename = os.path.basename(path)
    index = filename.find(".jsonl")
    if index < 0:
        return os.path.splitext(filename)[0] or "unknown"
    stem = filename[:index]
    return stem or filename


def agent_id_from_path(path):
    parts = os.path.normpath(path).split(os.sep)
    for index, part in enumerate(parts):
        if part == "agents" and index + 1 < len(parts):
            return parts[index + 1] or "default"
    return "default"


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
        "messages": 0,
        "assistant_msgs": 0,
        "projects": set(["OpenClaw"]),
        "tool_counts": Counter(),
        "tool_calls": 0,
        "files_touched": set(),
        "seen": set(),
        "records": [],
    }


def is_model_change(record):
    if record.get("type") == "model_change":
        return True
    return record.get("type") == "custom" and record.get("customType") == "model-snapshot"


def model_source(record):
    data = record.get("data") if isinstance(record.get("data"), dict) else record
    return data if isinstance(data, dict) else {}


def message_fingerprint(session_key, ts, model, provider, usage, provider_total, cost):
    return ":".join(
        [
            "openclaw",
            session_key,
            str(ts.timestamp()),
            model or "",
            provider or "",
            str(usage.get("input") or ""),
            str(usage.get("output") or ""),
            str(usage.get("cacheWrite") or ""),
            str(usage.get("cacheRead") or ""),
            str(provider_total or ""),
            str(cost or ""),
        ]
    )


def collect_tool_names(value, inside_tool_call=False):
    names = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return names
    if isinstance(value, list):
        for item in value:
            names.extend(collect_tool_names(item, inside_tool_call=inside_tool_call))
        return names
    if not isinstance(value, dict):
        return names

    type_hint = str(value.get("type") or value.get("kind") or value.get("customType") or "").lower()
    normalized_type = re.sub(r"[^a-z0-9]+", "", type_hint)
    if normalized_type in ("toolresult", "toolcallresult") or (
        normalized_type.startswith("tool") and normalized_type.endswith("result")
    ):
        return names
    looks_like_tool = (
        inside_tool_call
        or "toolcall" in normalized_type
        or "toolCall" in value
        or "tool_call" in value
        or "toolCalls" in value
        or "tool_calls" in value
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
        "message",
    ):
        nested = value.get(key)
        if isinstance(nested, (dict, list, str)):
            names.extend(
                collect_tool_names(
                    nested,
                    inside_tool_call=key in ("toolCall", "toolCalls", "tool_call", "tool_calls"),
                )
            )
    return names


def parse_message_record(record, session_key, current_model, current_provider, fallback_ts):
    if record.get("type") != "message":
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    role = str(message.get("role") or "").lower()
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    tool_names = collect_tool_names(message)

    ts = normalize_ts(message.get("timestamp") or record.get("timestamp")) or fallback_ts
    if not ts:
        return None

    if not usage:
        if role not in ("user", "assistant") and not tool_names:
            return None
        start = ts.timestamp()
        event_id = record.get("id") or message.get("id") or ""
        fingerprint = (
            f"openclaw-event-id:{session_key}:{event_id}"
            if event_id
            else f"openclaw-event:{session_key}:{role or 'event'}:{start}:{json.dumps(tool_names, sort_keys=True)}"
        )
        return {
            "fingerprint": fingerprint,
            "session_key": session_key,
            "created": ts,
            "window": (start, start + SESSION_TAIL_SECS),
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "provider_total_tokens": 0,
            "thoughts_tokens": 0,
            "tool_tokens": 0,
            "role": role,
            "counts_message": role in ("user", "assistant"),
            "tool_names": tool_names,
        }

    input_tokens = safe_int(usage.get("input"))
    output_tokens = safe_int(usage.get("output"))
    cache_read = safe_int(usage.get("cacheRead"))
    cache_write = safe_int(usage.get("cacheWrite"))
    total = safe_int(usage.get("totalTokens"))
    if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0 and total > 0:
        output_tokens = total
    provider_total = total or input_tokens + output_tokens + cache_read + cache_write
    if provider_total <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return None

    model = (
        message.get("modelId")
        or message.get("model")
        or current_model
        or "unknown"
    )
    provider = message.get("provider") or current_provider or ""
    cost = 0
    cost_obj = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
    if isinstance(cost_obj, dict):
        try:
            cost = float(cost_obj.get("total") or 0)
        except Exception:
            cost = 0

    start = ts.timestamp()
    end = start + SESSION_TAIL_SECS
    fingerprint = message_fingerprint(session_key, ts, str(model), str(provider), usage, provider_total, cost)
    return {
        "fingerprint": fingerprint,
        "session_key": session_key,
        "created": ts,
        "window": (start, end),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "provider_total_tokens": provider_total,
        "thoughts_tokens": 0,
        "tool_tokens": 0,
        "role": role or "assistant",
        "counts_message": role in ("user", "assistant") or bool(usage),
        "tool_names": tool_names,
    }


def parse_tool_record(record, session_key, fallback_ts):
    tool_names = collect_tool_names(record)
    if not tool_names:
        return None
    ts = normalize_ts(record.get("timestamp") or record.get("createdAt") or record.get("created_at")) or fallback_ts
    if not ts:
        return None
    start = ts.timestamp()
    event_id = record.get("id") or record.get("toolCallId") or record.get("tool_call_id") or ""
    fingerprint = (
        f"openclaw-tool-id:{session_key}:{event_id}"
        if event_id
        else f"openclaw-tool:{session_key}:{start}:{json.dumps(tool_names, sort_keys=True)}"
    )
    return {
        "fingerprint": fingerprint,
        "session_key": session_key,
        "created": ts,
        "window": (start, start + SESSION_TAIL_SECS),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "provider_total_tokens": 0,
        "thoughts_tokens": 0,
        "tool_tokens": 0,
        "role": "",
        "counts_message": False,
        "tool_names": tool_names,
    }


def add_record(days, record):
    date_key = record["created"].astimezone().strftime("%Y-%m-%d")
    day = days[(record["session_key"], date_key)]
    if record["fingerprint"] in day["seen"]:
        return
    day["seen"].add(record["fingerprint"])
    day["records"].append(record)
    day["events"].append((record.get("role") or "event", record["created"], record["window"]))
    if record.get("counts_message"):
        day["messages"] += 1
    if record.get("role") == "assistant":
        day["assistant_msgs"] += 1
    day["input_tokens"] += record["input_tokens"]
    day["output_tokens"] += record["output_tokens"]
    day["cache_read_tokens"] += record["cache_read_tokens"]
    day["cache_creation_tokens"] += record["cache_creation_tokens"]
    day["provider_total_tokens"] += record["provider_total_tokens"]
    day["thoughts_tokens"] += record["thoughts_tokens"]
    day["tool_tokens"] += record["tool_tokens"]
    for tool_name in record.get("tool_names", []):
        day["tool_counts"][tool_name] += 1
        day["tool_calls"] += 1


def parse_session_file(path):
    session_id = session_id_from_path(path)
    agent_id = agent_id_from_path(path)
    session_key = f"{agent_id}:{session_id}"
    days = defaultdict(new_day)
    try:
        fallback_ts = datetime.fromtimestamp(os.path.getmtime(path)).astimezone()
    except Exception:
        fallback_ts = datetime.fromtimestamp(0).astimezone()
    current_model = None
    current_provider = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                if is_model_change(record):
                    source = model_source(record)
                    current_model = source.get("modelId") or source.get("model") or current_model
                    current_provider = source.get("provider") or current_provider
                    continue
                parsed = parse_message_record(
                    record,
                    session_key,
                    current_model,
                    current_provider,
                    fallback_ts,
                )
                if not parsed:
                    parsed = parse_tool_record(record, session_key, fallback_ts)
                if parsed:
                    add_record(days, parsed)
    except Exception:
        return days
    return days


def merge_days(target, incoming):
    for day in incoming.values():
        for record in day.get("records", []):
            add_record(target, record)


def stats_from_days(days):
    results = {}
    for (session_key, date_str), day in days.items():
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
        results[f"openclaw:{session_key}:{date_str}"] = {
            "session_id": f"openclaw:{session_key}",
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
            "messages": day["messages"],
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


def parse_all(root_dir=""):
    days = defaultdict(new_day)
    for session_file in iter_session_files(root_dir):
        merge_days(days, parse_session_file(session_file))
    return stats_from_days(days)


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"openclaw-sync-state.{HOST_ID}.json")
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
            log_sync("another OpenClaw sync is already running; skipping")
        return
    state_path, state, state_invalidated = load_sync_state()
    if force_rescan:
        state_invalidated = True
    next_state = {}
    pending_state = {}
    changed_days = defaultdict(new_day)

    for session_file in iter_session_files(root_dir):
        try:
            signature = file_signature(session_file)
        except OSError:
            continue
        if not state_invalidated and state.get(session_file) == signature:
            next_state[session_file] = signature
            continue
        try:
            merge_days(changed_days, parse_session_file(session_file))
        except Exception as error:
            log_sync_error(f"failed to parse {session_file}", error)
            continue
        pending_state[session_file] = signature

    try:
        full_days = defaultdict(new_day)
        if changed_days:
            for session_file in iter_session_files(root_dir):
                merge_days(full_days, parse_session_file(session_file))
        selected_days = defaultdict(new_day)
        for key in changed_days.keys():
            if key in full_days:
                selected_days[key] = full_days[key]
        full_rescan_mode = bool(force_rescan or state_invalidated)
        for stats in stats_from_days(selected_days).values():
            post_session(config, stats, full_rescan=full_rescan_mode)
    except Exception as error:
        log_sync_error("failed to sync aggregated OpenClaw sessions", error)
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
