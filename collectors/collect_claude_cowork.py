#!/usr/bin/env python3
"""
AgentBoard Claude Cowork session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from local Claude
Cowork transcript files. It NEVER reads, stores, or transmits conversation
content, code, prompts, or responses.
"""

import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import collect as claude_collect
except Exception as error:  # pragma: no cover - only hit on broken installs
    print(f"[agentboard-cowork] failed to import collect.py: {error}", file=sys.stderr)
    sys.exit(0)


__version__ = "1.3.4-cowork.2"

AGENTBOARD_DIR = os.path.expanduser("~/.agentboard")
HOST_ID = getattr(claude_collect, "HOST_ID", "unknown")
LOG_DIR = os.path.join(AGENTBOARD_DIR, "logs")
SYNC_LOG_PATH = os.path.join(LOG_DIR, "cowork-sync.log")
LAST_SUCCESS_PATH = os.path.join(LOG_DIR, "last-success-cowork.txt")
DEFAULT_COWORK_DIR = os.path.expanduser(
    "~/Library/Application Support/Claude/local-agent-mode-sessions"
)
STATE_PATH = os.path.join(AGENTBOARD_DIR, f"claude-cowork-sync-state.{HOST_ID}.json")
SYNC_INTERVAL_SECS = 300
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
    line = f"[agentboard-cowork] {message}"
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
    log_sync(f"{context}: {error}")


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


def resolve_cowork_dir(cowork_dir=""):
    path = cowork_dir or DEFAULT_COWORK_DIR
    path = os.path.expanduser(path)
    return path if os.path.isdir(path) else ""


def load_sync_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            raw_state = json.load(f)
    except Exception:
        return {}, False

    if not isinstance(raw_state, dict):
        return {}, False

    if raw_state.get("_collector_version") != __version__:
        log_sync(f"collector upgraded to {__version__}; invalidating sync cache")
        return {}, True

    files = raw_state.get("files")
    if isinstance(files, dict):
        return files, False
    return {}, False


def save_sync_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"_collector_version": __version__, "files": state},
            f,
            indent=2,
            sort_keys=True,
        )


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def combined_signature(*paths):
    parts = []
    for path in paths:
        if not path:
            continue
        try:
            parts.append(f"{path}:{file_signature(path)}")
        except OSError:
            parts.append(f"{path}:missing")
    return "|".join(parts)


def path_has_segment(path, segment):
    return segment in path.split(os.sep)


def read_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_local_metadata(cowork_dir):
    pattern = os.path.join(cowork_dir, "**", "local_*.json")
    for path in glob.glob(pattern, recursive=True):
        if os.path.isfile(path):
            yield path


def iter_transcripts_for_local(local_json_path):
    sibling_session_dir = os.path.splitext(local_json_path)[0]
    session_dir = (
        sibling_session_dir
        if os.path.isdir(sibling_session_dir)
        else os.path.dirname(local_json_path)
    )
    projects_dir = os.path.join(session_dir, ".claude", "projects")
    if not os.path.isdir(projects_dir):
        return

    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        if not os.path.isfile(path):
            continue
        if path_has_segment(path, "subagents"):
            continue
        yield path


def infer_project_dir(transcript_path, selected_folders):
    selected = [
        os.path.abspath(os.path.expanduser(path))
        for path in selected_folders
        if isinstance(path, str) and path
    ]

    parts = transcript_path.split(os.sep)
    try:
        projects_index = parts.index("projects")
    except ValueError:
        projects_index = -1

    if projects_index >= 0 and projects_index + 1 < len(parts):
        encoded = parts[projects_index + 1].lstrip("-")
        if encoded:
            candidate = os.path.sep + encoded.replace("-", os.path.sep)
            if os.path.isdir(candidate):
                return candidate

    for path in selected:
        if os.path.isdir(path):
            return path
    return ""


def discover_sessions(cowork_dir):
    discovered = []
    seen_transcripts = set()

    for local_json_path in iter_local_metadata(cowork_dir):
        try:
            metadata = read_json_file(local_json_path)
        except Exception as error:
            log_sync_error(f"failed to read {local_json_path}", error)
            continue

        selected_folders = metadata.get("userSelectedFolders")
        if not isinstance(selected_folders, list):
            selected_folders = []

        cli_session_id = metadata.get("cliSessionId")
        local_session_id = metadata.get("sessionId") or os.path.splitext(
            os.path.basename(local_json_path)
        )[0]

        for transcript in iter_transcripts_for_local(local_json_path) or []:
            if transcript in seen_transcripts:
                continue
            seen_transcripts.add(transcript)

            transcript_session_id = os.path.splitext(os.path.basename(transcript))[0]
            session_id = transcript_session_id or cli_session_id or local_session_id
            if not session_id:
                continue

            discovered.append(
                {
                    "local_json_path": local_json_path,
                    "local_session_id": str(local_session_id or ""),
                    "cli_session_id": str(cli_session_id or ""),
                    "session_id": str(session_id),
                    "transcript": transcript,
                    "project_dir": infer_project_dir(transcript, selected_folders),
                    "selected_folders": selected_folders,
                }
            )

    return discovered


def namespaced_session_id(session_id):
    if session_id.startswith("cowork:"):
        return session_id
    return f"cowork:{session_id}"


def post_session(config, session_id, date_str, stats, installed_skills=None, full_rescan=False):
    payload = {
        "token": config["token"],
        "device_name": config.get("device_name", ""),
        "platform": config.get("platform", ""),
        "source": "claude_cowork",
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
        "installed_skill_snapshots": [],
        "collector_version": __version__,
        "full_rescan": full_rescan,
        "files_touched": stats["files_touched"],
        "first_event_at": stats["first_event_at"],
        "last_event_at": stats["last_event_at"],
        "engaged_windows": stats["engaged_windows"],
    }
    claude_collect.post_checkin(config["api"], payload)


def build_stats_from_day(data, max_minutes):
    events = sorted(data.get("events", []), key=lambda item: item[1])
    if not events:
        return None

    windows = claude_collect.build_engaged_windows(
        events, claude_collect.CLAUDE_IDLE_GAP_SECS, claude_collect.SESSION_TAIL_SECS
    )
    active_seconds = int(sum(end - start for start, end in windows))
    coding_time_mins = claude_collect.clamp_minutes(active_seconds, max_minutes)
    output_tokens = data.get("output_tokens", 0)
    ai_time_mins = (
        max(1, int(output_tokens / claude_collect.HUMAN_TOKENS_PER_MIN))
        if output_tokens > 0
        else 0
    )

    skill_counts = data.get("skill_counts", {})
    skill_last_used = data.get("skill_last_used", {})
    if hasattr(claude_collect, "build_skill_breakdown"):
        skill_breakdown = claude_collect.build_skill_breakdown(
            skill_counts, skill_last_used
        )
    else:
        skill_breakdown = []

    return {
        "coding_time_mins": coding_time_mins,
        "ai_time_mins": ai_time_mins,
        "tokens_used": data.get("input_tokens", 0) + output_tokens,
        "provider_total_tokens": data.get("input_tokens", 0)
        + output_tokens
        + data.get("cache_read_tokens", 0)
        + data.get("cache_creation_tokens", 0),
        "input_tokens": data.get("input_tokens", 0),
        "output_tokens": output_tokens,
        "cache_read_tokens": data.get("cache_read_tokens", 0),
        "cache_creation_tokens": data.get("cache_creation_tokens", 0),
        "lines_changed": data.get("lines_added", 0) - data.get("lines_removed", 0),
        "lines_added": data.get("lines_added", 0),
        "lines_removed": data.get("lines_removed", 0),
        "messages": data.get("user_msgs", 0) + data.get("assistant_msgs", 0),
        "assistant_messages": data.get("assistant_msgs", 0),
        "projects": len(data.get("projects", [])),
        "tool_calls": data.get("tool_calls", 0),
        "tool_breakdown": claude_collect.build_tool_breakdown(
            data.get("tool_counts", {})
        ),
        "skill_breakdown": skill_breakdown,
        "files_touched": len(data.get("files_touched", [])),
        "first_event_at": events[0][1].isoformat(),
        "last_event_at": events[-1][1].isoformat(),
        "engaged_windows": claude_collect.windows_to_payload(windows),
    }


def collect_installed_skills_payload(project_roots):
    if not (
        hasattr(claude_collect, "collect_installed_skills")
        and hasattr(claude_collect, "get_inventory_upload_payload")
    ):
        return []
    installed_skills = claude_collect.collect_installed_skills(sorted(project_roots))
    return claude_collect.get_inventory_upload_payload(
        installed_skills, sorted(project_roots)
    )


def parse_session(entry):
    raw_days = claude_collect.parse_transcript_events(
        entry["transcript"], entry.get("project_dir", "")
    )
    day_stats = {}
    for date_str, data in raw_days.items():
        stats = build_stats_from_day(data, claude_collect.MAX_MINS_PER_DAY)
        if stats:
            day_stats[date_str] = stats
    return day_stats, raw_days


def build_summary(cowork_dir):
    sessions = []
    merged_days = defaultdict(
        lambda: {
            "coding_time_mins": 0,
            "ai_time_mins": 0,
            "tokens_used": 0,
            "provider_total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "messages": 0,
            "assistant_messages": 0,
            "projects": 0,
            "tool_calls": 0,
            "files_touched": 0,
            "sessions": 0,
        }
    )

    discovered = discover_sessions(cowork_dir)
    for entry in discovered:
        try:
            day_stats, _raw_days = parse_session(entry)
        except Exception as error:
            log_sync_error(f"failed to parse {entry['transcript']}", error)
            continue

        for date_str, stats in day_stats.items():
            session_entry = {
                "date": date_str,
                "session_id": namespaced_session_id(entry["session_id"]),
                "source": "claude_cowork",
                **stats,
            }
            sessions.append(session_entry)
            merged = merged_days[date_str]
            for field in (
                "coding_time_mins",
                "ai_time_mins",
                "tokens_used",
                "provider_total_tokens",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "messages",
                "assistant_messages",
                "projects",
                "tool_calls",
                "files_touched",
            ):
                merged[field] += stats.get(field, 0)
            merged["sessions"] += 1

    summary = {
        "total_sessions": len(sessions),
        "total_days": len(merged_days),
        "coding_time_mins": sum(day["coding_time_mins"] for day in merged_days.values()),
        "ai_time_mins": sum(day["ai_time_mins"] for day in merged_days.values()),
        "tokens_used": sum(day["tokens_used"] for day in merged_days.values()),
        "provider_total_tokens": sum(
            day["provider_total_tokens"] for day in merged_days.values()
        ),
    }

    return {"summary": summary, "sessions": sessions, "daily": dict(merged_days)}


def summary_mode(cowork_dir, verbose=False):
    resolved_dir = resolve_cowork_dir(cowork_dir)
    if not resolved_dir:
        if verbose:
            log_sync("summary aborted: no Claude Cowork sessions directory found")
        emit_json({"summary": {}, "sessions": [], "daily": {}})
        return
    payload = build_summary(resolved_dir)
    if verbose:
        log_sync(
            f"summary complete: sessions={payload['summary'].get('total_sessions', 0)} days={payload['summary'].get('total_days', 0)}"
        )
    emit_json(payload)


def sync_mode(cowork_dir="", verbose=False, force_rescan=False):
    config = claude_collect.load_config()
    if not config:
        if verbose:
            log_sync("missing config/token/api; aborting sync")
        return {
            "status": "config_error",
            "message": "Missing config/token/api",
            "session_dir": "",
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 1,
        }

    resolved_dir = resolve_cowork_dir(cowork_dir)
    if not resolved_dir:
        if verbose:
            log_sync("Claude Cowork sessions directory not found")
        return {
            "status": "no_session_dir",
            "message": "No Claude Cowork sessions directory found",
            "session_dir": cowork_dir or DEFAULT_COWORK_DIR,
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 0,
        }

    state, state_invalidated = load_sync_state()
    next_state = {}
    scanned = 0
    skipped = 0
    synced = 0
    errors = 0
    full_rescan_mode = bool(force_rescan or state_invalidated)

    if verbose:
        log_sync(f"collector version {__version__}")
        if full_rescan_mode:
            log_sync("full rescan enabled; ignoring cached transcript signatures")

    for entry in discover_sessions(resolved_dir):
        transcript = entry["transcript"]
        scanned += 1
        signature = combined_signature(transcript, entry.get("local_json_path"))
        if not full_rescan_mode and state.get(transcript) == signature:
            next_state[transcript] = signature
            skipped += 1
            continue

        try:
            day_stats, raw_days = parse_session(entry)
            project_roots = {
                project
                for data in raw_days.values()
                for project in data.get("projects", set())
                if project and os.path.isdir(project)
            }
            project_roots.update(
                os.path.abspath(os.path.expanduser(path))
                for path in entry.get("selected_folders", [])
                if isinstance(path, str)
                and path
                and os.path.isdir(os.path.abspath(os.path.expanduser(path)))
            )
            installed_skills_payload = collect_installed_skills_payload(project_roots)

            for date_str, stats in day_stats.items():
                if verbose:
                    log_sync(
                        "posting "
                        f"{transcript} date={date_str} "
                        f"coding={stats.get('coding_time_mins', 0)} "
                        f"messages={stats.get('messages', 0)} "
                        f"tool_calls={stats.get('tool_calls', 0)} "
                        f"tokens={stats.get('tokens_used', 0)} "
                        f"first={stats.get('first_event_at')} "
                        f"last={stats.get('last_event_at')}"
                    )
                post_session(
                    config,
                    entry["session_id"],
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

    try:
        save_sync_state(next_state)
    except Exception as error:
        log_sync_error("failed to save sync state", error)
        errors += 1

    if errors == 0:
        mark_last_success()

    status = "success"
    message = "Sync complete"
    if scanned == 0:
        status = "no_sessions"
        message = "No Claude Cowork sessions found"
    elif errors > 0 and synced == 0:
        status = "error"
        message = "All sync attempts failed"
    elif errors > 0:
        status = "partial"
        message = "Sync completed with some errors"

    return {
        "status": status,
        "message": message,
        "session_dir": resolved_dir,
        "scanned": scanned,
        "skipped": skipped,
        "synced": synced,
        "errors": errors,
    }


def daemon_mode(cowork_dir=""):
    log_sync(f"collector version {__version__}")
    while True:
        try:
            sync_mode(cowork_dir, verbose=False)
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

    cowork_dir = ""
    if len(args) >= 2:
        cowork_dir = args[1]

    if len(args) >= 1 and args[0] == "--summary":
        summary_mode(cowork_dir, verbose=True)
        return 0

    if len(args) >= 1 and args[0] == "--sync":
        result = sync_mode(cowork_dir, verbose=True, force_rescan=force_rescan)
        if json_output:
            emit_json(result)
        if result["status"] in ("success", "partial", "no_sessions", "no_session_dir"):
            return 0
        return 1

    if len(args) >= 1 and args[0] == "--daemon":
        if force_rescan:
            log_sync("ignoring --force-rescan in daemon mode")
        daemon_mode(cowork_dir)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
