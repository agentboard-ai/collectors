#!/usr/bin/env python3
"""
AgentBoard session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from Claude Code
transcripts. It NEVER reads, stores, or transmits any conversation content,
code, prompts, or responses.

Called by hook.sh with arguments:
  1. session_id
  2. transcript_path
  3. cwd (optional — project directory)
"""

import glob
import hashlib
import json
import os
import platform as platform_module
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

MAX_MINS_PER_DAY = 960
MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
CLAUDE_IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
SYNC_INTERVAL_SECS = 180
INVENTORY_CACHE_TTL_SECS = 300
AGENTBOARD_SCRIPT_RELEASE = "2026-05-14"
__version__ = AGENTBOARD_SCRIPT_RELEASE
REPARSE_ON_UPGRADE_RELEASES = {"2026-05-14"}
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
SYNC_LOG_PATH = os.path.join(LOG_DIR, "claude-sync.log")
LAST_SUCCESS_PATH = os.path.join(LOG_DIR, "claude-last-success.txt")
SYNC_LOCK_PATH = os.path.join(AGENTBOARD_DIR, f"claude-sync.{HOST_ID}.lock")
INVENTORY_CACHE_PATH = os.path.join(AGENTBOARD_DIR, "inventory-cache-claude.json")
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
    line = f"[agentboard-claude] {message}"
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


def mark_last_success():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAST_SUCCESS_PATH, "w", encoding="utf-8") as f:
            f.write(datetime.now().astimezone().isoformat() + "\n")
    except Exception:
        pass


def emit_json(payload):
    try:
        print(json.dumps(payload))
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass


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


def estimate_engaged_seconds(events, gap_cap_secs, tail_secs):
    """Estimate engaged time from event gaps with an idle cap."""
    windows = build_engaged_windows(events, gap_cap_secs, tail_secs)
    return int(sum(end - start for start, end in windows))


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

    ordered = sorted(
        tool_counts.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )[:top_n]
    return [
        {
            "tool": tool,
            "count": count,
            "percentage": int(round((count / total_calls) * 100)),
        }
        for tool, count in ordered
    ]


def build_skill_breakdown(skill_counts, skill_last_used):
    total_calls = sum(skill_counts.values())
    if total_calls <= 0:
        return []

    ordered = sorted(
        skill_counts.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )
    return [
        {
            "skill_key": name,
            "skill_name": name,
            "source": "claude_code",
            "count": count,
            "last_used": skill_last_used.get(name).isoformat()
            if skill_last_used.get(name)
            else None,
        }
        for name, count in ordered
    ]


def normalize_inventory_name(rel_path):
    normalized = rel_path.replace(os.sep, "/").strip("/")
    if normalized.lower().endswith(".md"):
        normalized = normalized[:-3]
    return normalized


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def compact_text(value):
    if not value:
        return None
    collapsed = " ".join(value.strip().split())
    return collapsed or None


def parse_skill_metadata(path):
    text = read_text_file(path)
    if not text:
        return {}

    metadata = {}
    lines = text.splitlines()
    body_lines = lines

    if len(lines) >= 3 and lines[0].strip() == "---":
        frontmatter = {}
        for index in range(1, len(lines)):
            line = lines[index].strip()
            if line == "---":
                body_lines = lines[index + 1 :]
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip().lower()] = value.strip().strip('"').strip("'")
        metadata.update(frontmatter)

    heading = None
    name = metadata.get("name")
    description = metadata.get("description")
    author = metadata.get("author")
    version = metadata.get("version")
    origin_repo = (
        metadata.get("origin_repo")
        or metadata.get("repo")
        or metadata.get("repository")
        or metadata.get("source_repo")
    )

    body_paragraph = []
    for raw_line in body_lines:
        line = raw_line.strip()
        if not line:
            if body_paragraph:
                break
            continue
        if line.startswith("#") and not heading:
            candidate_heading = compact_text(line.lstrip("#").strip())
            if candidate_heading and candidate_heading.lower() not in (
                "preamble (run first)",
                "preamble",
            ):
                heading = candidate_heading
            continue
        if not description and ":" in line:
            key, value = line.split(":", 1)
            normalized_key = key.strip().lower()
            normalized_value = compact_text(value)
            if normalized_key in ("author", "owner") and not author:
                author = normalized_value
                continue
            if normalized_key in ("version", "ver") and not version:
                version = normalized_value
                continue
            if normalized_key in ("repo", "repository", "origin_repo", "source_repo") and not origin_repo:
                origin_repo = normalized_value
                continue
        if line.startswith(">") or line.startswith("- ") or line.startswith("* "):
            continue
        if not description:
            body_paragraph.append(line)

    if not description and body_paragraph:
        description = compact_text(" ".join(body_paragraph))

    if heading:
        metadata["heading"] = heading
    if name:
        metadata["name"] = name
    if description:
        metadata["description"] = description
    if author:
        metadata["author"] = author
    if version:
        metadata["version"] = version
    if origin_repo:
        metadata["origin_repo"] = origin_repo
    return metadata


def normalize_github_repo(value):
    if not value:
        return None
    candidate = compact_text(str(value))
    if not candidate:
        return None

    patterns = [
        r"github\.com[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$",
        r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, candidate)
        if match:
            owner = match.group("owner")
            repo = match.group("repo")
            return f"{owner}/{repo}"
    return None


def extract_github_repo_from_text(value):
    if not value:
        return None

    matches = re.findall(
        r"github\.com[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?:[/?#\s]|$)",
        str(value),
        flags=re.IGNORECASE,
    )
    for owner, repo in matches:
        normalized = normalize_github_repo(f"{owner}/{repo}")
        if normalized:
            return normalized
    return None


def infer_origin_repo_from_git(path):
    candidates = []
    base_path = os.path.abspath(path)
    real_path = os.path.realpath(path)
    for candidate in (base_path, real_path):
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        base_dir = os.path.dirname(candidate)
        try:
            top_level = subprocess.check_output(
                ["git", "-C", base_dir, "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            continue

        if not top_level:
            continue

        try:
            remote_url = subprocess.check_output(
                ["git", "-C", top_level, "config", "--get", "remote.origin.url"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            continue

        normalized = normalize_github_repo(remote_url)
        if normalized:
            return normalized
    return None


def infer_origin_repo_from_skill_file(path):
    inferred_origin_repo = infer_origin_repo_from_git(path)
    if inferred_origin_repo:
        return inferred_origin_repo

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return extract_github_repo_from_text(handle.read())
    except Exception:
        return None


def load_inventory_cache():
    try:
        with open(INVENTORY_CACHE_PATH, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
            if not isinstance(cached, dict):
                return {}
            entries = cached.get("entries")
            uploads = cached.get("uploads")
            return {
                "entries": entries if isinstance(entries, dict) else {},
                "uploads": uploads if isinstance(uploads, dict) else {},
            }
    except Exception:
        return {"entries": {}, "uploads": {}}


def save_inventory_cache(cache):
    try:
        os.makedirs(AGENTBOARD_DIR, exist_ok=True)
        now = time.time()
        entry_cache = {
            key: value
            for key, value in cache.get("entries", {}).items()
            if isinstance(value, dict)
            and isinstance(value.get("fetched_at"), (int, float))
            and now - float(value["fetched_at"]) < INVENTORY_CACHE_TTL_SECS
        }
        upload_cache = {
            key: value
            for key, value in cache.get("uploads", {}).items()
            if isinstance(value, dict)
            and isinstance(value.get("updated_at"), (int, float))
            and now - float(value["updated_at"]) < 7 * 24 * 60 * 60
        }
        with open(INVENTORY_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(
                {"entries": entry_cache, "uploads": upload_cache},
                handle,
                indent=2,
                sort_keys=True,
            )
    except Exception:
        pass


def scan_skill_manifests_uncached(base_dir, scope, source):
    if not base_dir or not os.path.isdir(base_dir):
        return []

    discovered = []
    for root, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in (".git", "node_modules", "__pycache__") and not d.startswith(".")
        ]
        if "SKILL.md" not in filenames:
            continue

        rel_dir = os.path.relpath(root, base_dir)
        skill_name = rel_dir.replace(os.sep, "/").strip("/")
        if not skill_name or skill_name == ".":
            skill_name = os.path.basename(os.path.normpath(base_dir))

        skill_file = os.path.join(root, "SKILL.md")
        metadata = parse_skill_metadata(skill_file)
        inferred_origin_repo = infer_origin_repo_from_skill_file(skill_file)
        origin_repo = normalize_github_repo(
            metadata.get("origin_repo") or inferred_origin_repo
        )
        discovered.append(
            {
                "source": source,
                "scope": scope,
                "skill_key": f"{source}:{scope}:{skill_name}",
                "skill_name": metadata.get("name") or metadata.get("heading") or skill_name,
                "description": metadata.get("description"),
                "author": metadata.get("author"),
                "version": metadata.get("version"),
                "origin_repo": origin_repo,
                "local_path": os.path.relpath(skill_file, base_dir).replace(os.sep, "/"),
                "fingerprint": file_signature(skill_file),
            }
        )
    return discovered


def scan_skill_manifests(base_dir, scope, source, cache):
    normalized_dir = os.path.abspath(os.path.expanduser(base_dir))
    cache_key = f"{source}:{scope}:{normalized_dir}"
    now = time.time()
    cached = cache.get("entries", {}).get(cache_key)
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("fetched_at"), (int, float))
        and now - float(cached["fetched_at"]) < INVENTORY_CACHE_TTL_SECS
        and isinstance(cached.get("entries"), list)
    ):
        return cached["entries"]

    entries = scan_skill_manifests_uncached(normalized_dir, scope, source)
    cache.setdefault("entries", {})[cache_key] = {"fetched_at": now, "entries": entries}
    return entries


def build_inventory_fingerprint(entries):
    payload = json.dumps(
        [
            {
                "source": entry.get("source"),
                "scope": entry.get("scope"),
                "skill_key": entry.get("skill_key"),
                "skill_name": entry.get("skill_name"),
                "description": entry.get("description"),
                "author": entry.get("author"),
                "version": entry.get("version"),
                "origin_repo": entry.get("origin_repo"),
                "local_path": entry.get("local_path"),
                "fingerprint": entry.get("fingerprint"),
            }
            for entry in sorted(
                entries,
                key=lambda item: (
                    item.get("source", ""),
                    item.get("scope", ""),
                    item.get("skill_key", ""),
                ),
            )
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_inventory_upload_payload(entries, project_roots=None):
    cache = load_inventory_cache()
    normalized_roots = [
        os.path.abspath(os.path.expanduser(root))
        for root in (project_roots or [])
        if isinstance(root, str) and root
    ]
    upload_key = "|".join(sorted(normalized_roots)) or "__global__"
    fingerprint = build_inventory_fingerprint(entries)
    previous = cache.get("uploads", {}).get(upload_key)
    if (
        isinstance(previous, dict)
        and previous.get("fingerprint") == fingerprint
        and previous.get("collector_version") == __version__
    ):
        return None
    cache.setdefault("uploads", {})[upload_key] = {
        "fingerprint": fingerprint,
        "collector_version": __version__,
        "updated_at": time.time(),
    }
    save_inventory_cache(cache)
    return entries


def build_installed_skill_snapshots(entries):
    grouped = {}
    for entry in entries or []:
        source = entry.get("source")
        scope = entry.get("scope")
        skill_key = entry.get("skill_key")
        if not isinstance(source, str) or not source:
            continue
        if scope not in ("user", "project"):
            continue
        if not isinstance(skill_key, str) or not skill_key:
            continue
        grouped.setdefault((source, scope), set()).add(skill_key)

    snapshots = []
    for (source, scope), skill_keys in sorted(grouped.items()):
        snapshots.append(
            {
                "source": source,
                "scope": scope,
                "skill_keys": sorted(skill_keys),
            }
        )
    return snapshots


def collect_installed_skills(project_roots=None):
    cache = load_inventory_cache()
    entries = []
    home_dir = os.path.expanduser("~")
    entries.extend(
        scan_skill_manifests(
            os.path.expanduser("~/.claude/skills"), "user", "claude_code", cache
        )
    )
    entries.extend(
        scan_skill_manifests(os.path.expanduser("~/.codex/skills"), "user", "codex", cache)
    )

    if project_roots is None:
        project_roots = []
    elif isinstance(project_roots, str):
        project_roots = [project_roots]

    for project_root in project_roots:
        if not project_root or not os.path.isdir(project_root):
            continue
        if os.path.abspath(project_root) == os.path.abspath(home_dir):
            continue
        entries.extend(
            scan_skill_manifests(
                os.path.join(project_root, ".claude", "skills"),
                "project",
                "claude_code",
                cache,
            )
        )
        entries.extend(
            scan_skill_manifests(
                os.path.join(project_root, ".codex", "skills"),
                "project",
                "codex",
                cache,
            )
        )

    deduped = {}
    for entry in entries:
        deduped[(entry["source"], entry["skill_key"])] = entry

    save_inventory_cache(cache)
    return list(deduped.values())


def make_day_bucket():
    return {
        "events": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "user_msgs": 0,
        "assistant_msgs": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "projects": set(),
        "tool_calls": 0,
        "tool_counts": defaultdict(int),
        "skill_counts": defaultdict(int),
        "skill_last_used": {},
        "files_touched": set(),
    }


def iter_tool_use_blocks(message):
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def iter_message_text_fragments(message):
    if not isinstance(message, dict):
        return

    content = message.get("content")
    if isinstance(content, str):
        yield content
        return

    if not isinstance(content, list):
        return

    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                yield text


def accumulate_command_skill_usage(day, ts, message):
    for fragment in iter_message_text_fragments(message):
        for raw_name in re.findall(r"<command-name>/([^<\s]+)</command-name>", fragment):
            skill_name = raw_name.strip()
            if not skill_name:
                continue
            day["skill_counts"][skill_name] += 1
            previous = day["skill_last_used"].get(skill_name)
            if previous is None or ts > previous:
                day["skill_last_used"][skill_name] = ts


def iter_assistant_messages(entry):
    if not isinstance(entry, dict):
        return

    msg_type = entry.get("type", "")
    if msg_type == "assistant":
        message = entry.get("message")
        if isinstance(message, dict):
            yield message

    data = entry.get("data")
    if not isinstance(data, dict):
        return

    for key in ("message", "assistant_message"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            yield candidate

    candidates = data.get("messages")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                yield candidate


def should_skip_message(message, seen_msg_ids):
    if seen_msg_ids is None or not isinstance(message, dict):
        return False

    message_id = message.get("id")
    if not message_id:
        return False

    if message_id in seen_msg_ids:
        return True

    seen_msg_ids.add(message_id)
    return False


def tool_use_dedupe_key(message, block):
    block_id = block.get("id") if isinstance(block, dict) else None
    if block_id:
        return f"id:{block_id}"

    message_id = message.get("id") if isinstance(message, dict) else ""
    try:
        input_key = json.dumps(
            block.get("input", {}) if isinstance(block, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        input_key = str(block.get("input", {}) if isinstance(block, dict) else {})
    return f"fallback:{message_id}:{block.get('name', '')}:{input_key}"


def should_skip_tool_use(message, block, seen_tool_use_ids):
    if seen_tool_use_ids is None:
        return False

    key = tool_use_dedupe_key(message, block)
    if key in seen_tool_use_ids:
        return True

    seen_tool_use_ids.add(key)
    return False


def accumulate_day_stats(
    day,
    project_dir,
    ts,
    entry,
    seen_msg_ids=None,
    seen_tool_use_ids=None,
):
    msg_type = entry.get("type", "")

    if msg_type == "user":
        day["events"].append(("user_message", ts))
        day["user_msgs"] += 1
        message = entry.get("message")
        if isinstance(message, dict):
            accumulate_command_skill_usage(day, ts, message)
        if project_dir:
            day["projects"].add(project_dir)
        return

    if msg_type == "progress":
        day["events"].append(("progress_activity", ts))

    for message in iter_assistant_messages(entry):
        if not should_skip_message(message, seen_msg_ids):
            day["events"].append(("assistant_message", ts))
            day["assistant_msgs"] += 1

            usage = message.get("usage", {}) if isinstance(message, dict) else {}
            day["input_tokens"] += usage.get("input_tokens", 0)
            day["output_tokens"] += usage.get("output_tokens", 0)
            day["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            day["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)

        for block in iter_tool_use_blocks(message):
            if should_skip_tool_use(message, block, seen_tool_use_ids):
                continue

            name = block.get("name", "")
            inp = block.get("input", {})
            day["events"].append(("tool_call", ts))
            day["tool_calls"] += 1
            if name:
                day["tool_counts"][name] += 1

            if name == "Skill":
                skill_name = inp.get("skill", "")
                if skill_name:
                    day["skill_counts"][skill_name] += 1
                    previous = day["skill_last_used"].get(skill_name)
                    if previous is None or ts > previous:
                        day["skill_last_used"][skill_name] = ts

            file_path = inp.get("file_path", "")
            if file_path:
                day["files_touched"].add(file_path)

            if name == "Edit":
                old = inp.get("old_string", "")
                new = inp.get("new_string", "")
                day["lines_removed"] += len(old.splitlines()) if old else 0
                day["lines_added"] += len(new.splitlines()) if new else 0
            elif name == "Write":
                content = inp.get("content", "")
                day["lines_added"] += len(content.splitlines()) if content else 0


def build_stats_from_day(data, max_minutes):
    events = sorted(data["events"], key=lambda item: item[1])
    if not events:
        return None

    windows = build_engaged_windows(events, CLAUDE_IDLE_GAP_SECS, SESSION_TAIL_SECS)
    active_seconds = int(sum(end - start for start, end in windows))
    coding_time_mins = clamp_minutes(active_seconds, max_minutes)
    ai_time_mins = (
        max(1, int(data["output_tokens"] / HUMAN_TOKENS_PER_MIN))
        if data["output_tokens"] > 0
        else 0
    )

    return {
        "coding_time_mins": coding_time_mins,
        "ai_time_mins": ai_time_mins,
        "tokens_used": data["input_tokens"] + data["output_tokens"],
        "provider_total_tokens": data["input_tokens"]
        + data["output_tokens"]
        + data["cache_read_tokens"]
        + data["cache_creation_tokens"],
        "input_tokens": data["input_tokens"],
        "output_tokens": data["output_tokens"],
        "cache_read_tokens": data["cache_read_tokens"],
        "cache_creation_tokens": data["cache_creation_tokens"],
        "lines_changed": data["lines_added"] - data["lines_removed"],
        "lines_added": data["lines_added"],
        "lines_removed": data["lines_removed"],
        "messages": data["user_msgs"] + data["assistant_msgs"],
        "assistant_messages": data["assistant_msgs"],
        "projects": len(data["projects"]),
        "tool_calls": data["tool_calls"],
        "tool_breakdown": build_tool_breakdown(data["tool_counts"]),
        "skill_breakdown": build_skill_breakdown(
            data["skill_counts"], data["skill_last_used"]
        ),
        "files_touched": len(data["files_touched"]),
        "first_event_at": events[0][1].isoformat(),
        "last_event_at": events[-1][1].isoformat(),
        "engaged_windows": windows_to_payload(windows),
    }


def post_checkin(api, payload):
    req = urllib.request.Request(
        api,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AgentBoard-CLI/1.0"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        message = f"HTTP {error.code}: {error.reason}"
        if body:
            message += f" body={body}"
        raise RuntimeError(message) from error


def recover_token_from_claim(config_path, config):
    claim_code = config.get("claim_code", "")
    api = config.get("api", "")
    if not claim_code or not api:
        return config

    api_base = api.replace("/api/checkin", "")
    try:
        req = urllib.request.Request(
            f"{api_base}/api/claim/{claim_code}/status",
            headers={"User-Agent": "AgentBoard-CLI/1.0"},
        )
        response = urllib.request.urlopen(req, timeout=5, context=SSL_CONTEXT)
        data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return config

    token = data.get("token", "") if data.get("status") == "claimed" else ""
    if not token:
        return config

    config["token"] = token
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass
    return config


def load_config():
    config_path = os.path.join(AGENTBOARD_DIR, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return None

    api = config.get("api", "")
    if not api:
        return None

    token = config.get("token", "")
    if not token:
        config = recover_token_from_claim(config_path, config)
        token = config.get("token", "")
    if not token:
        return None

    return {
        "token": token,
        "api": api,
        "device_name": config.get("device_name") or detect_device_name(),
        "platform": config.get("platform") or detect_platform_name(),
        "logs_dir": config.get("logs_dir", LOG_DIR),
    }


def namespaced_session_id(session_id):
    if session_id.startswith("claude:"):
        return session_id
    return f"claude:{session_id}"


def post_session(
    config, session_id, date_str, stats, installed_skills=None, full_rescan=False
):
    installed_skill_snapshots = build_installed_skill_snapshots(installed_skills or [])
    post_checkin(
        config["api"],
        {
            "token": config["token"],
            "device_name": config.get("device_name", ""),
            "platform": config.get("platform", ""),
            "session_id": namespaced_session_id(session_id),
            "date": date_str,
            "coding_time_mins": stats["coding_time_mins"],
            "ai_time_mins": stats["ai_time_mins"],
            "tokens_used": stats["tokens_used"],
            "provider_total_tokens": stats["provider_total_tokens"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "cache_read_tokens": stats["cache_read_tokens"],
            "cache_creation_tokens": stats["cache_creation_tokens"],
            "thoughts_tokens": 0,
            "tool_tokens": 0,
            "lines_changed": stats["lines_changed"],
            "lines_added": stats["lines_added"],
            "lines_removed": stats["lines_removed"],
            "sessions": 1,
            "messages": stats["messages"],
            "assistant_messages": stats["assistant_messages"],
            "projects": stats["projects"],
            "tool_calls": stats["tool_calls"],
            "tool_breakdown": stats.get("tool_breakdown", []),
            "skill_breakdown": stats.get("skill_breakdown", []),
            "installed_skills": installed_skills or [],
            "installed_skill_snapshots": installed_skill_snapshots,
            "collector_version": __version__,
            "full_rescan": full_rescan,
            "files_touched": stats["files_touched"],
            "first_event_at": stats["first_event_at"],
            "last_event_at": stats["last_event_at"],
            "engaged_windows": stats["engaged_windows"],
        },
    )


def post_inventory(config, session_ids, full_rescan=False):
    post_checkin(
        config["api"],
        {
            "token": config["token"],
            "device_name": config.get("device_name", ""),
            "platform": config.get("platform", ""),
            "mode": "inventory",
            "source": "claude_code",
            "session_ids": sorted(
                {namespaced_session_id(session_id) for session_id in session_ids if session_id}
            ),
            "collector_version": __version__,
            "full_rescan": full_rescan,
        },
    )


def default_claude_project_roots():
    roots = []
    claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if claude_config_dir:
        roots.append(os.path.join(claude_config_dir, "projects"))
    roots.append(os.path.expanduser("~/.claude/projects"))
    return roots


def resolve_transcript_roots(root_dir=""):
    candidates = [root_dir] if root_dir else default_claude_project_roots()
    roots = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if not os.path.isdir(normalized):
            continue
        real = os.path.realpath(normalized)
        if real in seen:
            continue
        seen.add(real)
        roots.append(normalized)
    return roots


def infer_project_dir(transcript_path, root_dir=""):
    proj_dir = os.path.dirname(transcript_path)
    for root in resolve_transcript_roots(root_dir):
        prefix = os.path.join(root, "")
        if proj_dir.startswith(prefix):
            return proj_dir[len(prefix):].lstrip("-").replace("-", "/")
    return ""


def infer_project_root(transcript_path, root_dir=""):
    proj_dir = os.path.dirname(transcript_path)
    for root in resolve_transcript_roots(root_dir):
        prefix = os.path.join(root, "")
        if not proj_dir.startswith(prefix):
            continue

        encoded = proj_dir[len(prefix):].lstrip("-")
        if not encoded:
            continue

        candidate = os.path.sep + encoded.replace("-", os.path.sep)
        if os.path.isdir(candidate):
            return candidate
    return ""


def iter_transcript_files(root_dir=""):
    seen = set()
    for root in resolve_transcript_roots(root_dir):
        pattern = os.path.join(root, "**", "*.jsonl")
        for path in glob.glob(pattern, recursive=True):
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            yield path


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"claude-sync-state.{HOST_ID}.json")
    try:
        with open(state_path) as f:
            raw_state = json.load(f)
    except Exception:
        return state_path, {}, False

    if not isinstance(raw_state, dict):
        return state_path, {}, False

    files = raw_state.get("files")
    if isinstance(files, dict):
        if raw_state.get("_collector_version") != __version__:
            if __version__ in REPARSE_ON_UPGRADE_RELEASES:
                log_sync(f"collector upgraded to {__version__}; invalidating sync cache")
                return state_path, {}, True
            log_sync(f"collector upgraded to {__version__}; preserving sync cache")
        return state_path, files, False
    legacy_files = {
        key: value
        for key, value in raw_state.items()
        if isinstance(key, str) and key != "_collector_version"
    }
    if legacy_files:
        if __version__ in REPARSE_ON_UPGRADE_RELEASES:
            log_sync(f"collector upgraded to {__version__}; invalidating legacy sync cache")
            return state_path, {}, True
        log_sync(f"collector upgraded to {__version__}; preserving legacy sync cache")
        return state_path, legacy_files, False
    return state_path, {}, False


def save_sync_state(state_path, state):
    state_dir = os.path.dirname(state_path)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(
            {"_collector_version": __version__, "files": state},
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


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def parse_transcript(transcript, project_dir=""):
    """Parse a transcript file and return per-day stats."""
    days = parse_transcript_events(transcript, project_dir)
    results = {}
    for date_str, data in days.items():
        stats = build_stats_from_day(data, MAX_MINS_PER_DAY)
        if stats:
            results[date_str] = stats

    return results


def parse_transcript_events(transcript, project_dir=""):
    """Parse a transcript and return raw per-day events + additive stats."""
    days = defaultdict(make_day_bucket)
    seen_msg_ids = set()
    seen_tool_use_ids = set()

    with open(transcript, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = entry.get("timestamp")
            if not ts_str:
                continue

            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            date_key = ts.astimezone().strftime("%Y-%m-%d")
            day = days[date_key]
            accumulate_day_stats(
                day,
                project_dir,
                ts,
                entry,
                seen_msg_ids,
                seen_tool_use_ids,
            )

    return days


def build_summary(transcript_dir, target_dates=None):
    """Scan transcripts and return combined summary/session data."""
    all_sessions = []
    target_dates = set(target_dates) if target_dates else None
    merged_days = defaultdict(
        lambda: {
            "events": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "user_msgs": 0,
            "assistant_msgs": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "projects": set(),
            "tool_calls": 0,
            "tool_counts": defaultdict(int),
            "skill_counts": defaultdict(int),
            "skill_last_used": {},
            "files_touched": set(),
            "session_count": 0,
        }
    )

    for filepath in iter_transcript_files(transcript_dir):
        try:
            line_count = sum(1 for _ in open(filepath, encoding="utf-8"))
            if line_count <= 2:
                continue
        except Exception:
            continue

        session_id = os.path.splitext(os.path.basename(filepath))[0]

        proj_dir = infer_project_dir(filepath, transcript_dir)

        try:
            day_data = parse_transcript_events(filepath, proj_dir)
        except Exception:
            continue

        for date_str, data in day_data.items():
            if target_dates is not None and date_str not in target_dates:
                continue

            events = sorted(data["events"], key=lambda e: e[1])
            if not events:
                continue

            session_windows = build_engaged_windows(
                events, CLAUDE_IDLE_GAP_SECS, SESSION_TAIL_SECS
            )
            session_active = int(sum(end - start for start, end in session_windows))
            session_entry = build_stats_from_day(data, MAX_MINS_PER_SESSION)
            if not session_entry:
                continue
            session_entry = {
                "date": date_str,
                "session_id": namespaced_session_id(session_id),
                **session_entry,
            }
            all_sessions.append(session_entry)

            merged = merged_days[date_str]
            merged["events"].extend(events)
            merged["input_tokens"] += data["input_tokens"]
            merged["output_tokens"] += data["output_tokens"]
            merged["cache_read_tokens"] += data["cache_read_tokens"]
            merged["cache_creation_tokens"] += data["cache_creation_tokens"]
            merged["user_msgs"] += data["user_msgs"]
            merged["assistant_msgs"] += data["assistant_msgs"]
            merged["lines_added"] += data["lines_added"]
            merged["lines_removed"] += data["lines_removed"]
            merged["projects"].update(data["projects"])
            merged["tool_calls"] += data["tool_calls"]
            for tool_name, count in data["tool_counts"].items():
                merged["tool_counts"][tool_name] += count
            for skill_name, count in data["skill_counts"].items():
                merged["skill_counts"][skill_name] += count
            for skill_name, last_used in data["skill_last_used"].items():
                previous = merged["skill_last_used"].get(skill_name)
                if previous is None or last_used > previous:
                    merged["skill_last_used"][skill_name] = last_used
            merged["files_touched"].update(data["files_touched"])
            merged["session_count"] += 1

    totals = {
        "total_coding_mins": 0,
        "total_ai_mins": 0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_lines_changed": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "total_sessions": 0,
        "total_messages": 0,
        "total_assistant_messages": 0,
        "total_tool_calls": 0,
        "total_files_touched": 0,
        "total_days": 0,
    }

    daily = {}
    for date_str, merged in merged_days.items():
        merged_events = sorted(merged["events"], key=lambda e: e[1])
        merged_windows = build_engaged_windows(
            merged_events, CLAUDE_IDLE_GAP_SECS, SESSION_TAIL_SECS
        )
        active_secs = int(sum(end - start for start, end in merged_windows))
        coding_mins = clamp_minutes(active_secs, MAX_MINS_PER_DAY)
        ai_mins = (
            max(1, int(merged["output_tokens"] / HUMAN_TOKENS_PER_MIN))
            if merged["output_tokens"] > 0
            else 0
        )

        totals["total_coding_mins"] += coding_mins
        totals["total_ai_mins"] += ai_mins
        totals["total_tokens"] += merged["input_tokens"] + merged["output_tokens"]
        totals["total_input_tokens"] += merged["input_tokens"]
        totals["total_output_tokens"] += merged["output_tokens"]
        totals["total_lines_changed"] += merged["lines_added"] - merged["lines_removed"]
        totals["total_lines_added"] += merged["lines_added"]
        totals["total_lines_removed"] += merged["lines_removed"]
        totals["total_sessions"] += merged["session_count"]
        totals["total_messages"] += merged["user_msgs"] + merged["assistant_msgs"]
        totals["total_assistant_messages"] += merged["assistant_msgs"]
        totals["total_tool_calls"] += merged["tool_calls"]
        totals["total_files_touched"] += len(merged["files_touched"])

        daily[date_str] = {
            "coding_time_mins": coding_mins,
            "ai_time_mins": ai_mins,
            "tokens_used": merged["input_tokens"] + merged["output_tokens"],
            "input_tokens": merged["input_tokens"],
            "output_tokens": merged["output_tokens"],
            "cache_read_tokens": merged["cache_read_tokens"],
            "cache_creation_tokens": merged["cache_creation_tokens"],
            "lines_changed": merged["lines_added"] - merged["lines_removed"],
            "lines_added": merged["lines_added"],
            "lines_removed": merged["lines_removed"],
            "sessions": merged["session_count"],
            "messages": merged["user_msgs"] + merged["assistant_msgs"],
            "assistant_messages": merged["assistant_msgs"],
            "projects": len(merged["projects"]),
            "tool_calls": merged["tool_calls"],
            "tool_breakdown": build_tool_breakdown(merged["tool_counts"]),
            "skill_breakdown": build_skill_breakdown(
                merged["skill_counts"], merged["skill_last_used"]
            ),
            "files_touched": len(merged["files_touched"]),
        }

    totals["total_days"] = len(merged_days)

    # Scale session coding_time_mins so per-day sums match daily totals.
    # Sessions computed independently can exceed the merged-daily figure
    # because overlapping activity windows are only deduplicated at the
    # daily level.
    from collections import defaultdict as _dd

    day_session_raw = _dd(int)
    for s in all_sessions:
        day_session_raw[s["date"]] += s["coding_time_mins"]

    for s in all_sessions:
        raw_day_total = day_session_raw[s["date"]]
        if raw_day_total > 0:
            daily_target = daily.get(s["date"], {}).get("coding_time_mins", 0)
            s["coding_time_mins"] = int(
                round(s["coding_time_mins"] * daily_target / raw_day_total)
            )
        # Same treatment for ai_time_mins
    day_session_ai_raw = _dd(int)
    for s in all_sessions:
        day_session_ai_raw[s["date"]] += s["ai_time_mins"]
    for s in all_sessions:
        raw_ai = day_session_ai_raw[s["date"]]
        if raw_ai > 0:
            daily_ai = daily.get(s["date"], {}).get("ai_time_mins", 0)
            s["ai_time_mins"] = int(
                round(s["ai_time_mins"] * daily_ai / raw_ai)
            )

    return {"summary": totals, "sessions": all_sessions, "daily": daily}


def summary_mode(transcript_dir, verbose=False):
    """Scan all transcripts and output combined summary + sessions JSON to stdout."""
    payload = build_summary(transcript_dir)
    if verbose:
        log_sync(
            f"summary complete: sessions={payload['summary'].get('total_sessions', 0)} days={payload['summary'].get('total_days', 0)}"
        )
    emit_json(payload)


def sync_mode(transcript_dir, verbose=False, force_rescan=False):
    config = load_config()
    if not config:
        if verbose:
            log_sync("missing config/token/api; aborting sync")
        return {
            "status": "config_error",
            "message": "Missing config/token/api",
            "transcript_dir": "",
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 1,
        }
    transcript_roots = resolve_transcript_roots(transcript_dir)
    transcript_dir_label = ", ".join(transcript_roots)
    if not transcript_roots:
        if verbose:
            log_sync(f"transcript directory not found: {transcript_dir}")
        return {
            "status": "no_session_dir",
            "message": "No Claude transcript directory found",
            "transcript_dir": transcript_dir,
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 0,
        }

    sync_lock = acquire_sync_lock()
    if sync_lock is False:
        if verbose:
            log_sync("another Claude sync is already running; skipping")
        return {
            "status": "skipped",
            "message": "Another Claude sync is already running",
            "transcript_dir": transcript_dir_label,
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 0,
        }

    state_path, state, state_invalidated = load_sync_state()
    next_state = {}
    scanned = 0
    skipped = 0
    synced = 0
    errors = 0
    full_rescan_mode = bool(force_rescan or state_invalidated)
    current_session_ids = set()

    if verbose:
        log_sync(f"collector version {__version__}")
        if full_rescan_mode:
            if force_rescan:
                log_sync("force rescan enabled; ignoring cached transcript signatures")
            else:
                log_sync("full rescan enabled after collector upgrade")

    for transcript in iter_transcript_files(transcript_dir):
        scanned += 1
        try:
            signature = file_signature(transcript)
        except OSError as error:
            if verbose:
                log_sync(f"failed to stat {transcript}: {error}")
            continue

        if not full_rescan_mode and state.get(transcript) == signature:
            next_state[transcript] = signature
            skipped += 1
            continue

        try:
            session_id = os.path.splitext(os.path.basename(transcript))[0]
            current_session_ids.add(session_id)
            project_dir = infer_project_dir(transcript, transcript_dir)
            raw_days = parse_transcript_events(transcript, project_dir)
            project_roots = {
                project
                for data in raw_days.values()
                for project in data.get("projects", set())
                if project and os.path.isdir(project)
            }
            if not project_roots:
                inferred_root = infer_project_root(transcript, transcript_dir)
                if inferred_root:
                    project_roots.add(inferred_root)
            installed_skills = collect_installed_skills(sorted(project_roots))
            installed_skills_payload = get_inventory_upload_payload(
                installed_skills, sorted(project_roots)
            )
            day_stats = {}
            for date_str, data in raw_days.items():
                stats = build_stats_from_day(data, MAX_MINS_PER_DAY)
                if stats:
                    day_stats[date_str] = stats
            for date_str, stats in day_stats.items():
                if verbose:
                    log_sync(
                        "posting "
                        f"{transcript} date={date_str} "
                        f"coding={stats.get('coding_time_mins', 0)} "
                        f"messages={stats.get('messages', 0)} "
                        f"tool_calls={stats.get('tool_calls', 0)} "
                        f"files_touched={stats.get('files_touched', 0)} "
                        f"tokens={stats.get('tokens_used', 0)} "
                        f"first={stats.get('first_event_at')} "
                        f"last={stats.get('last_event_at')} "
                        f"windows={len(stats.get('engaged_windows', []) or [])}"
                    )
                post_session(
                    config,
                    session_id,
                    date_str,
                    stats,
                    installed_skills=installed_skills_payload,
                    full_rescan=full_rescan_mode,
                )
                synced += 1
        except Exception as error:
            log_sync_error(f"failed to sync {transcript}", error)
            errors += 1
            continue
        next_state[transcript] = signature

    if full_rescan_mode:
        try:
            if verbose:
                log_sync(
                    f"posting inventory for {len(current_session_ids)} Claude sessions"
                )
            post_inventory(
                config,
                current_session_ids,
                full_rescan=True,
            )
        except Exception as error:
            log_sync_error("failed to post inventory", error)
            errors += 1

    try:
        save_sync_state(state_path, next_state)
    except Exception as error:
        if verbose:
            log_sync(f"failed to save sync state {state_path}: {error}")
        return {
            "status": "error",
            "message": f"Failed to save sync state: {error}",
            "transcript_dir": transcript_dir_label,
            "scanned": scanned,
            "skipped": skipped,
            "synced": synced,
            "errors": max(1, errors),
        }

    if verbose:
        log_sync(
            f"scan complete: scanned={scanned} skipped={skipped} synced={synced} state={state_path}"
        )
    if errors == 0:
        mark_last_success()

    status = "success"
    message = "Sync complete"
    if scanned == 0:
        status = "no_sessions"
        message = "No Claude transcripts found"
    elif errors > 0 and synced == 0:
        status = "error"
        message = "All sync attempts failed"
    elif errors > 0:
        status = "partial"
        message = "Sync completed with some errors"

    return {
        "status": status,
        "message": message,
        "transcript_dir": transcript_dir_label,
        "scanned": scanned,
        "skipped": skipped,
        "synced": synced,
        "errors": errors,
    }


def daemon_mode(transcript_dir):
    log_sync(f"collector version {__version__}")
    while True:
        try:
            sync_mode(transcript_dir)
        except Exception as error:
            log_sync_error("daemon sync iteration failed", error)
        time.sleep(SYNC_INTERVAL_SECS)


def main():
    args = sys.argv[1:]
    json_output = False
    force_rescan = False
    filtered_args = []
    for value in args:
        if value == "--json":
            json_output = True
        elif value == "--force-rescan":
            force_rescan = True
        else:
            filtered_args.append(value)
    args = filtered_args

    if len(args) >= 1 and args[0] == "--summary":
        summary_mode(args[1] if len(args) >= 2 else "", verbose=True)
        return 0

    if len(args) >= 1 and args[0] == "--sync":
        result = sync_mode(args[1] if len(args) >= 2 else "", verbose=True, force_rescan=force_rescan)
        if json_output:
            emit_json(result)
        if result["status"] in ("success", "partial", "no_sessions", "no_session_dir", "skipped"):
            return 0
        return 1

    if len(args) >= 1 and args[0] == "--daemon":
        if force_rescan:
            log_sync("ignoring --force-rescan in daemon mode")
        daemon_mode(args[1] if len(args) >= 2 else "")
        return 0

    if len(args) < 2:
        return 0

    session_id = args[0]
    transcript = args[1]

    config = load_config()
    if not config:
        return 0

    if not transcript or not os.path.exists(transcript):
        return 0

    project_dir = args[2] if len(args) > 2 and args[2] else ""
    project_roots = [project_dir] if project_dir else []
    log_sync(f"collector version {__version__}")
    installed_skills = collect_installed_skills(project_roots)
    installed_skills_payload = get_inventory_upload_payload(installed_skills, project_roots)
    day_stats = parse_transcript(transcript, project_dir)

    for date_str, stats in day_stats.items():
        try:
            post_session(
                config,
                session_id,
                date_str,
                stats,
                installed_skills=installed_skills_payload,
                full_rescan=False,
            )
        except Exception as error:
            log_sync_error(f"failed to post session {session_id}:{date_str}", error)

    return 0



if __name__ == "__main__":
    sys.exit(main())
