import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collectors" / "collect_kimi.py"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "kimi-code"
PRIVATE_MARKERS = (
    "synthetic-secret-never-read",
    "synthetic-server-token-never-read",
    "/Users/synthetic/private-project",
    "private prompt",
    "private source code",
    "private response",
)


def load_collector():
    module_name = "collect_kimi_under_test"
    spec = importlib.util.spec_from_file_location(module_name, str(COLLECTOR))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_session(root, session_id, entries, agent_id="main"):
    wire_path = (
        Path(root)
        / "sessions"
        / "wd_fixture_hash"
        / f"session_{session_id}"
        / "agents"
        / agent_id
        / "wire.jsonl"
    )
    wire_path.parent.mkdir(parents=True, exist_ok=True)
    wire_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return wire_path


def user_prompt(time="2026-08-20T09:59:00.000Z"):
    return {
        "type": "turn.prompt",
        "time": time,
        "origin": {"kind": "user"},
        "input": "synthetic private prompt must never be uploaded",
    }


def usage_record(time="2026-08-20T10:00:00.000Z"):
    return {
        "type": "usage.record",
        "usageScope": "turn",
        "time": time,
        "model": "kimi-code/k3",
        "usage": {
            "inputOther": 100,
            "output": 20,
            "inputCacheRead": 900,
            "inputCacheCreation": 30,
        },
    }


def tool_call(call_id, name, time="2026-08-20T10:00:30.000Z"):
    return {
        "type": "context.append_loop_event",
        "time": time,
        "event": {
            "type": "tool.call",
            "toolCallId": call_id,
            "uuid": f"uuid-{call_id}",
            "name": name,
            "args": {"content": "synthetic private tool input"},
        },
    }


class FakeResponse:
    def read(self):
        return b"{}"

    def close(self):
        pass


class KimiCollectorTest(unittest.TestCase):
    def run_summary(self):
        completed = subprocess.run(
            [sys.executable, str(COLLECTOR), "--summary", str(FIXTURE_ROOT)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_summary_deduplicates_usage_and_counts_safe_metadata(self):
        result = self.run_summary()
        self.assertEqual(result["summary"]["total_sessions"], 1)
        self.assertEqual(result["summary"]["total_tokens"], 1050)
        self.assertEqual(result["summary"]["total_input_tokens"], 100)
        self.assertEqual(result["summary"]["total_output_tokens"], 20)
        self.assertEqual(result["summary"]["total_messages"], 2)
        self.assertEqual(result["summary"]["total_assistant_messages"], 1)
        self.assertEqual(result["summary"]["total_tool_calls"], 2)
        self.assertEqual(result["summary"]["total_coding_mins"], 3)
        self.assertEqual(result["diagnostics"]["unsupported_usage_scopes"], 1)

        session = result["sessions"][0]
        self.assertEqual(session["session_id"], "kimi:main:abc-123")
        self.assertEqual(session["tokens_used"], 120)
        self.assertEqual(session["provider_total_tokens"], 1050)
        self.assertEqual(session["cache_read_tokens"], 900)
        self.assertEqual(session["cache_creation_tokens"], 30)
        self.assertEqual(session["messages"], 2)
        self.assertEqual(session["assistant_messages"], 1)
        self.assertEqual(session["collector_version"], "2026-08-25")
        self.assertEqual(session["projects"], 0)
        self.assertEqual(session["tool_calls"], 2)
        self.assertEqual(
            session["tool_breakdown"],
            [
                {"tool": "Bash", "count": 1, "percentage": 50},
                {"tool": "Edit", "count": 1, "percentage": 50},
            ],
        )
        self.assertEqual(session["lines_changed"], 0)
        self.assertEqual(session["files_touched"], 0)
        self.assertEqual(
            result["daily"]["2026-08-20"]["tool_breakdown"],
            session["tool_breakdown"],
        )

        encoded = json.dumps(result)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, encoded)

    def test_summary_keeps_canceled_zero_token_activity(self):
        with tempfile.TemporaryDirectory() as root:
            write_session(root, "cancelled", [user_prompt()])

            completed = subprocess.run(
                [sys.executable, str(COLLECTOR), "--summary", root],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)

        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["session_id"], "kimi:main:cancelled")
        self.assertEqual(result["sessions"][0]["coding_time_mins"], 2)
        self.assertEqual(result["sessions"][0]["provider_total_tokens"], 0)
        self.assertEqual(result["sessions"][0]["messages"], 1)
        self.assertEqual(result["sessions"][0]["projects"], 0)

    def test_summary_aggregates_daily_tools_across_sessions(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as root:
            write_session(
                root,
                "tools-a",
                [user_prompt(), tool_call("bash-1", "Bash")],
            )
            write_session(
                root,
                "tools-b",
                [
                    user_prompt(),
                    tool_call("edit-1", "Edit"),
                    tool_call("edit-2", "Edit", "2026-08-20T10:01:00.000Z"),
                ],
            )
            with mock.patch("sys.stdout", new=io.StringIO()) as stdout:
                collector.summary_mode(root)
                result = json.loads(stdout.getvalue())

        self.assertEqual(
            result["daily"]["2026-08-20"]["tool_breakdown"],
            [
                {"tool": "Edit", "count": 2, "percentage": 67},
                {"tool": "Bash", "count": 1, "percentage": 33},
            ],
        )

    def test_summary_never_opens_kimi_secrets_or_session_index(self):
        collector = load_collector()
        protected_names = {"credentials", "server.token", "session_index.jsonl"}
        opened_paths = []
        real_open = builtins.open

        def guarded_open(path, *args, **kwargs):
            opened_paths.append(os.fspath(path))
            if Path(path).name in protected_names:
                raise AssertionError("collector attempted to open protected Kimi data")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=guarded_open):
            with mock.patch("sys.stdout", new=io.StringIO()):
                collector.summary_mode(str(FIXTURE_ROOT))

        self.assertTrue(any(path.endswith("wire.jsonl") for path in opened_paths))
        self.assertFalse(any(Path(path).name in protected_names for path in opened_paths))

    def test_force_rescan_uploads_session_without_account_inventory(self):
        collector = load_collector()
        payloads = []

        def fake_urlopen(request, *args, **kwargs):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as state_dir:
            collector.AGENTBOARD_DIR = state_dir
            collector.HOST_ID = "synthetic-host"
            collector.LOG_DIR = os.path.join(state_dir, "logs")
            collector.SYNC_LOG_PATH = os.path.join(collector.LOG_DIR, "kimi-sync.log")
            collector.SYNC_LOCK_PATH = os.path.join(state_dir, "kimi-sync.synthetic-host.lock")
            config = {
                "api": "https://example.invalid/api/checkin",
                "token": "synthetic-agentboard-token",
                "device_name": "Synthetic Mac",
                "platform": "macos",
            }
            with mock.patch.object(collector, "load_config", return_value=config):
                with mock.patch.object(collector, "acquire_sync_lock", return_value=None):
                    with mock.patch.object(collector.urllib.request, "urlopen", side_effect=fake_urlopen):
                        collector.sync_mode(str(FIXTURE_ROOT), force_rescan=True)

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["source"], "kimi_code")
        self.assertEqual(payload["session_id"], "kimi:main:abc-123")
        self.assertTrue(payload["full_rescan"])
        self.assertEqual(payload["collector_version"], "2026-08-25")
        self.assertEqual(payload["provider_total_tokens"], 1050)
        self.assertEqual(payload["messages"], 2)
        self.assertEqual(payload["tool_calls"], 2)
        self.assertNotEqual(payload.get("mode"), "inventory")

        encoded = json.dumps(payload)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, encoded)

    def test_incremental_sync_uploads_a_smaller_authoritative_snapshot(self):
        collector = load_collector()
        payloads = []

        def fake_urlopen(request, *args, **kwargs):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state_dir:
            write_session(
                root,
                "shrunk",
                [
                    user_prompt(),
                    usage_record(),
                    usage_record("2026-08-20T11:00:00.000Z"),
                ],
            )
            collector.AGENTBOARD_DIR = state_dir
            collector.HOST_ID = "synthetic-host"
            collector.LOG_DIR = os.path.join(state_dir, "logs")
            collector.SYNC_LOG_PATH = os.path.join(collector.LOG_DIR, "kimi-sync.log")
            collector.SYNC_LOCK_PATH = os.path.join(state_dir, "kimi-sync.synthetic-host.lock")
            config = {
                "api": "https://example.invalid/api/checkin",
                "token": "synthetic-agentboard-token",
                "device_name": "Synthetic Mac",
                "platform": "macos",
            }
            with mock.patch.object(collector, "load_config", return_value=config):
                with mock.patch.object(collector, "acquire_sync_lock", return_value=None):
                    with mock.patch.object(collector.urllib.request, "urlopen", side_effect=fake_urlopen):
                        collector.sync_mode(root)
                        write_session(root, "shrunk", [user_prompt(), usage_record()])
                        collector.sync_mode(root)

        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["provider_total_tokens"], 2100)
        self.assertTrue(payloads[0]["full_rescan"])
        self.assertEqual(payloads[1]["provider_total_tokens"], 1050)
        self.assertFalse(payloads[1]["full_rescan"])
        self.assertEqual(payloads[1]["collector_version"], "2026-08-25")


if __name__ == "__main__":
    unittest.main()
