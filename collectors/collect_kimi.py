#!/usr/bin/env python3
"""
AgentBoard Kimi Code session data collector.

PRIVACY: This script only discovers and reads files matching
~/.kimi-code/sessions/*/session_*/agents/*/wire.jsonl. It never reads Kimi
credentials, server.token, session_index.jsonl, prompt/response content, tool
arguments, or code. It aggregates top-level usage.record entries with
usageScope=turn plus structural turn.prompt and tool.call event metadata.

This collector is upload/update only. It never posts an account-level
inventory (mode: "inventory"), because the server prunes sessions missing
from an inventory account-wide and cannot yet tell "deleted by the user"
apart from "lives on another device". Re-enable only after the server
supports device-scoped inventories.

Starting with the 2026-08-25 release, every uploaded session/day is an
authoritative snapshot of its existing wire.jsonl contents. If an existing
file is shortened, the next upload intentionally follows the smaller local
snapshot. If a file disappears entirely, its server rows are retained because
this collector deliberately does not send an account-wide inventory.
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
from collections import defaultdict
from datetime import datetime

MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
SYNC_INTERVAL_SECS = 300
IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
AGENTBOARD_SCRIPT_RELEASE = "2026-08-25"
__version__ = AGENTBOARD_SCRIPT_RELEASE
STATE_VERSION = f"{AGENTBOARD_SCRIPT_RELEASE}:kimi.4"
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
        return socket.gethostname().strip()
    except Exception:
        return ""


def get_host_id():
    return sanitize_host_id(os.environ.get("AGENTBOARD_HOST_ID", "") or detect_device_name())


def normalize_device_platform(value):
    normalized = (value or "").strip().lower()
    if normalized.startswith("darwin") or normalized in ("mac", "macos", "mac os", "mac os x"):
        return "macos"
    if normalized.startswith("win") or "windows" in normalized:
        return "win32"
    if normalized.startswith("linux"):
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
SYNC_LOG_PATH = os.path.join(LOG_DIR, "kimi-sync.log")
SYNC_LOCK_PATH = os.path.join(AGENTBOARD_DIR, f"kimi-sync.{HOST_ID}.lock")
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_log(path):
    try:
        if not os.path.exists(path) or os.path.getsize(path) < MAX_LOG_BYTES:
            return
        if os.path.exists(path + ".2"):
            os.remove(path + ".2")
        if os.path.exists(path + ".1"):
            os.replace(path + ".1", path + ".2")
        os.replace(path, path + ".1")
    except Exception:
        pass


def log_sync(message):
    line = f"[agentboard-kimi] {message}"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rotate_log(SYNC_LOG_PATH)
        with open(SYNC_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
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
        return max(0, int(value or 0))
    except Exception:
        return 0


def normalize_ts(value):
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number > 1_000_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number).astimezone()
        except Exception:
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
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


def build_engaged_windows(events):
    windows = []
    ordered = sorted(events)
    for index, start in enumerate(ordered):
        if index + 1 < len(ordered):
            end = min(start + IDLE_GAP_SECS, ordered[index + 1])
        else:
            end = start + SESSION_TAIL_SECS
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


def load_config():
    config_path = os.path.expanduser("~/.agentboard/config.json")
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception:
        return None
    if not config.get("api") or not config.get("token"):
        return None
    return {
        "api": config["api"],
        "token": config["token"],
        "device_name": config.get("device_name") or detect_device_name(),
        "platform": config.get("platform") or detect_platform_name(),
    }


def post_json(config, payload):
    request = urllib.request.Request(
        config["api"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AgentBoard-CLI/1.0"},
        method="POST",
    )
    response = urllib.request.urlopen(request, timeout=10, context=SSL_CONTEXT)
    try:
        body = response.read() if hasattr(response, "read") else b""
        if body:
            return json.loads(body.decode("utf-8"))
    except Exception:
        pass
    finally:
        if hasattr(response, "close"):
            response.close()
    return {}


def post_session(config, session_entry, full_rescan=False):
    return post_json(
        config,
        {
            "token": config["token"],
            "source": "kimi_code",
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
            "thoughts_tokens": 0,
            "tool_tokens": 0,
            "lines_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "sessions": 1,
            "messages": session_entry["messages"],
            "assistant_messages": session_entry["assistant_messages"],
            "projects": session_entry["projects"],
            "tool_calls": session_entry["tool_calls"],
            "tool_breakdown": session_entry.get("tool_breakdown", []),
            "files_touched": 0,
            "first_event_at": session_entry["first_event_at"],
            "last_event_at": session_entry["last_event_at"],
            "engaged_windows": session_entry["engaged_windows"],
            "collector_version": __version__,
        },
    )


def split_paths(value):
    if not value:
        return []
    result = []
    for raw in str(value).replace(os.pathsep, ",").split(","):
        if raw.strip():
            result.append(raw.strip())
    return result


def resolve_session_roots(root_dir=""):
    candidates = []
    if root_dir:
        candidates.append(root_dir)
    else:
        candidates.extend(split_paths(os.environ.get("KIMI_CODE_DIR", "")))
        candidates.extend(split_paths(os.environ.get("KIMI_CODE_HOME", "")))
        candidates.append("~/.kimi-code")

    roots = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        sessions_root = normalized if os.path.basename(normalized) == "sessions" else os.path.join(normalized, "sessions")
        if not os.path.isdir(sessions_root) or os.path.islink(sessions_root):
            continue
        real_root = os.path.realpath(sessions_root)
        if real_root in seen:
            continue
        seen.add(real_root)
        roots.append(sessions_root)
    return roots


def has_symlink_component(path, root):
    current = os.path.abspath(path)
    boundary = os.path.abspath(root)
    while current != boundary:
        if os.path.islink(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent
    return os.path.islink(boundary)


def identity_from_path(path):
    normalized = os.path.normpath(path)
    if os.path.basename(normalized) != "wire.jsonl":
        return None
    agent_id = os.path.basename(os.path.dirname(normalized))
    agents_dir = os.path.dirname(os.path.dirname(normalized))
    session_dir = os.path.basename(os.path.dirname(agents_dir))
    if os.path.basename(agents_dir) != "agents" or not session_dir.startswith("session_"):
        return None
    session_id = session_dir[len("session_") :]
    valid_component = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not session_id or not valid_component.fullmatch(session_id):
        return None
    if not agent_id or not valid_component.fullmatch(agent_id):
        return None
    return f"kimi:{agent_id}:{session_id}"


def iter_session_files(root_dir=""):
    seen = set()
    for root in resolve_session_roots(root_dir):
        pattern = os.path.join(root, "*", "session_*", "agents", "*", "wire.jsonl")
        for path in sorted(glob.glob(pattern)):
            if path in seen or not os.path.isfile(path) or has_symlink_component(path, root):
                continue
            if not identity_from_path(path):
                continue
            seen.add(path)
            yield path


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def signature_size(signature):
    try:
        return int(str(signature).rsplit(":", 1)[1])
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
        "messages": 0,
        "assistant_messages": 0,
        # Kimi wire logs do not expose a privacy-safe project identity.
        "projects": set(),
        "tool_counts": defaultdict(int),
        "tool_calls": 0,
        "seen": set(),
        "records": [],
        "user_message_records": [],
        "tool_call_records": [],
    }


def usage_fingerprint(session_id, created, model, usage):
    values = [
        "kimi",
        session_id,
        created.isoformat(),
        str(model or ""),
        safe_int(usage.get("inputOther")),
        safe_int(usage.get("output")),
        safe_int(usage.get("inputCacheRead")),
        safe_int(usage.get("inputCacheCreation")),
    ]
    return json.dumps(values, separators=(",", ":"))


def add_record(days, record):
    date_key = record["created"].astimezone().strftime("%Y-%m-%d")
    day = days[(record["session_id"], date_key)]
    if record["fingerprint"] in day["seen"]:
        return
    day["seen"].add(record["fingerprint"])
    day["records"].append(record)
    day["events"].append(record["created"].timestamp())
    day["input_tokens"] += record["input_tokens"]
    day["output_tokens"] += record["output_tokens"]
    day["cache_read_tokens"] += record["cache_read_tokens"]
    day["cache_creation_tokens"] += record["cache_creation_tokens"]
    day["provider_total_tokens"] += record["provider_total_tokens"]
    day["messages"] += 1
    day["assistant_messages"] += 1


def add_user_message(days, record):
    date_key = record["created"].astimezone().strftime("%Y-%m-%d")
    day = days[(record["session_id"], date_key)]
    if record["fingerprint"] in day["seen"]:
        return
    day["seen"].add(record["fingerprint"])
    day["user_message_records"].append(record)
    day["events"].append(record["created"].timestamp())
    day["messages"] += 1


def add_tool_call(days, record):
    date_key = record["created"].astimezone().strftime("%Y-%m-%d")
    day = days[(record["session_id"], date_key)]
    if record["fingerprint"] in day["seen"]:
        return
    day["seen"].add(record["fingerprint"])
    day["tool_call_records"].append(record)
    day["events"].append(record["created"].timestamp())
    day["tool_calls"] += 1
    day["tool_counts"][record["tool_name"]] += 1


def build_tool_breakdown(tool_counts, top_n=4):
    total_calls = sum(tool_counts.values())
    if total_calls <= 0:
        return []
    ordered = sorted(
        tool_counts.items(), key=lambda item: (-item[1], item[0].lower())
    )[:top_n]
    return [
        {
            "tool": tool,
            "count": count,
            "percentage": int(round((count / total_calls) * 100)),
        }
        for tool, count in ordered
    ]


def parse_session_file(path):
    session_id = identity_from_path(path)
    if not session_id:
        return defaultdict(new_day), {"unsupported_usage_scopes": 0}
    days = defaultdict(new_day)
    unsupported_scopes = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            created = normalize_ts(record.get("time"))
            if not created:
                continue

            # A turn.prompt with origin.kind=user is the stable user-turn
            # envelope. Never inspect or retain its input payload.
            if record_type == "turn.prompt":
                origin = record.get("origin")
                if isinstance(origin, dict) and origin.get("kind") == "user":
                    add_user_message(
                        days,
                        {
                            "session_id": session_id,
                            "created": created,
                            "fingerprint": json.dumps(
                                ["kimi-user", session_id, created.isoformat()],
                                separators=(",", ":"),
                            ),
                        },
                    )
                continue

            # Tool calls are counted from their structural envelope only.
            # args/display/description/trace fields are intentionally ignored.
            if record_type == "context.append_loop_event":
                event = record.get("event")
                if isinstance(event, dict) and event.get("type") == "tool.call":
                    tool_name = event.get("name")
                    if not isinstance(tool_name, str) or not tool_name.strip():
                        tool_name = "unknown"
                    tool_call_id = event.get("toolCallId") or event.get("uuid")
                    fingerprint_id = (
                        str(tool_call_id)
                        if tool_call_id
                        else f"{created.isoformat()}:{line_number}"
                    )
                    add_tool_call(
                        days,
                        {
                            "session_id": session_id,
                            "created": created,
                            "fingerprint": json.dumps(
                                ["kimi-tool", session_id, fingerprint_id],
                                separators=(",", ":"),
                            ),
                            "tool_name": tool_name.strip()[:120],
                        },
                    )
                continue

            if record_type != "usage.record":
                continue
            if record.get("usageScope") != "turn":
                unsupported_scopes += 1
                continue
            usage = record.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens = safe_int(usage.get("inputOther"))
            output_tokens = safe_int(usage.get("output"))
            cache_read_tokens = safe_int(usage.get("inputCacheRead"))
            cache_creation_tokens = safe_int(usage.get("inputCacheCreation"))
            provider_total_tokens = (
                input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
            )
            if provider_total_tokens <= 0:
                continue
            parsed = {
                "session_id": session_id,
                "created": created,
                "fingerprint": usage_fingerprint(
                    session_id, created, record.get("model"), usage
                ),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "provider_total_tokens": provider_total_tokens,
            }
            add_record(days, parsed)
    return days, {"unsupported_usage_scopes": unsupported_scopes}


def merge_days(target, incoming):
    for day in incoming.values():
        for record in day.get("records", []):
            add_record(target, record)
        for record in day.get("user_message_records", []):
            add_user_message(target, record)
        for record in day.get("tool_call_records", []):
            add_tool_call(target, record)


def stats_from_days(days):
    results = {}
    for (session_id, date_str), day in days.items():
        events = sorted(day["events"])
        if not events:
            continue
        windows = build_engaged_windows(events)
        active_seconds = int(sum(end - start for start, end in windows))
        output_tokens = day["output_tokens"]
        results[f"{session_id}:{date_str}"] = {
            "session_id": session_id,
            "date": date_str,
            "coding_time_mins": clamp_minutes(active_seconds, MAX_MINS_PER_SESSION),
            "ai_time_mins": max(1, int(output_tokens / HUMAN_TOKENS_PER_MIN)) if output_tokens else 0,
            "tokens_used": day["input_tokens"] + output_tokens,
            "provider_total_tokens": day["provider_total_tokens"],
            "input_tokens": day["input_tokens"],
            "output_tokens": output_tokens,
            "cache_read_tokens": day["cache_read_tokens"],
            "cache_creation_tokens": day["cache_creation_tokens"],
            "thoughts_tokens": 0,
            "tool_tokens": 0,
            "lines_changed": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "messages": day["messages"],
            "assistant_messages": day["assistant_messages"],
            "projects": len(day["projects"]),
            "tool_calls": day["tool_calls"],
            "tool_breakdown": build_tool_breakdown(day["tool_counts"]),
            "files_touched": 0,
            "first_event_at": datetime.fromtimestamp(events[0]).astimezone().isoformat(),
            "last_event_at": datetime.fromtimestamp(events[-1]).astimezone().isoformat(),
            "engaged_windows": windows_to_payload(windows),
            "collector_version": __version__,
        }
    return results


def parse_all_days(root_dir=""):
    days = defaultdict(new_day)
    diagnostics = {"unsupported_usage_scopes": 0}
    for session_file in iter_session_files(root_dir):
        parsed, file_diagnostics = parse_session_file(session_file)
        merge_days(days, parsed)
        diagnostics["unsupported_usage_scopes"] += file_diagnostics["unsupported_usage_scopes"]
    return days, diagnostics


def parse_all(root_dir=""):
    days, diagnostics = parse_all_days(root_dir)
    return stats_from_days(days), diagnostics


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"kimi-sync-state.{HOST_ID}.json")
    try:
        with open(state_path, encoding="utf-8") as handle:
            raw_state = json.load(handle)
    except Exception:
        return state_path, {}, True
    if not isinstance(raw_state, dict) or raw_state.get("_collector_version") != STATE_VERSION:
        return state_path, {}, True
    files = raw_state.get("files")
    return state_path, files if isinstance(files, dict) else {}, not isinstance(files, dict)


def save_sync_state(state_path, state):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"_collector_version": STATE_VERSION, "files": state},
            handle,
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
            log_sync("another Kimi sync is already running; skipping")
        return

    state_path, state, state_invalidated = load_sync_state()
    files = list(iter_session_files(root_dir))
    next_state = {}
    changed_files = []
    shrunken_files = 0
    for session_file in files:
        try:
            signature = file_signature(session_file)
        except OSError:
            continue
        next_state[session_file] = signature
        previous_size = signature_size(state.get(session_file))
        current_size = signature_size(signature)
        if (
            previous_size is not None
            and current_size is not None
            and current_size < previous_size
        ):
            shrunken_files += 1
        if force_rescan or state_invalidated or state.get(session_file) != signature:
            changed_files.append(session_file)

    membership_changed = set(state) != set(next_state)
    if not changed_files and not membership_changed:
        return

    if shrunken_files:
        log_sync(
            f"detected {shrunken_files} shortened Kimi log file(s); "
            "server metrics will follow the current local snapshots"
        )

    try:
        full_days = defaultdict(new_day)
        changed_keys = set()
        diagnostics = {"unsupported_usage_scopes": 0}
        changed_set = set(changed_files)
        for session_file in files:
            parsed, file_diagnostics = parse_session_file(session_file)
            merge_days(full_days, parsed)
            diagnostics["unsupported_usage_scopes"] += file_diagnostics["unsupported_usage_scopes"]
            if session_file in changed_set:
                changed_keys.update(parsed.keys())
        if diagnostics["unsupported_usage_scopes"]:
            log_sync(
                "ignored "
                f"{diagnostics['unsupported_usage_scopes']} usage.record entries with unsupported usageScope"
            )

        all_stats = stats_from_days(full_days)
        full_rescan_mode = bool(force_rescan or state_invalidated)
        for key, stats in all_stats.items():
            raw_key = (stats["session_id"], stats["date"])
            if full_rescan_mode or raw_key in changed_keys:
                post_session(config, stats, full_rescan=full_rescan_mode)

        # This collector deliberately never posts a `mode: "inventory"`
        # payload: the server-side prune treats one device's inventory as the
        # whole account's session list, so any inventory from a machine that
        # holds only part of the user's Kimi sessions can delete another
        # device's data. Until the server supports device-scoped inventories,
        # Kimi is upload/update only — locally deleted sessions are simply
        # left in place server-side.
        save_sync_state(state_path, next_state)
    except Exception as error:
        log_sync_error("failed to sync Kimi sessions", error)


def summary_mode(root_dir=""):
    raw_days, diagnostics = parse_all_days(root_dir)
    parsed = stats_from_days(raw_days)
    sessions = sorted(parsed.values(), key=lambda item: (item["date"], item["session_id"]))
    daily = {}
    daily_tool_counts = defaultdict(lambda: defaultdict(int))
    for (_, date_str), day in raw_days.items():
        for tool_name, count in day["tool_counts"].items():
            daily_tool_counts[date_str][tool_name] += count
    additive_keys = (
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
    )
    for stats in sessions:
        date_str = stats["date"]
        if date_str not in daily:
            daily[date_str] = dict(stats)
            daily[date_str]["sessions"] = 1
            continue
        for key in additive_keys:
            daily[date_str][key] += stats[key]
        daily[date_str]["sessions"] += 1

    for date_str, stats in daily.items():
        stats["tool_breakdown"] = build_tool_breakdown(daily_tool_counts[date_str])

    totals = {
        "total_coding_mins": sum(item["coding_time_mins"] for item in daily.values()),
        "total_ai_mins": sum(item["ai_time_mins"] for item in daily.values()),
        "total_tokens": sum(item["provider_total_tokens"] for item in daily.values()),
        "total_input_tokens": sum(item["input_tokens"] for item in daily.values()),
        "total_output_tokens": sum(item["output_tokens"] for item in daily.values()),
        "total_lines_changed": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "total_sessions": len(sessions),
        "total_messages": sum(item["messages"] for item in daily.values()),
        "total_assistant_messages": sum(item["assistant_messages"] for item in daily.values()),
        "total_tool_calls": sum(item["tool_calls"] for item in daily.values()),
        "total_files_touched": 0,
        "total_days": len(daily),
    }
    emit_json(
        {
            "summary": totals,
            "sessions": sessions,
            "daily": daily,
            "diagnostics": diagnostics,
        }
    )


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
