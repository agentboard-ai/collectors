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
        self.assertEqual(result["diagnostics"]["unsupported_usage_scopes"], 1)

        session = result["sessions"][0]
        self.assertEqual(session["session_id"], "kimi:main:abc-123")
        self.assertEqual(session["tokens_used"], 120)
        self.assertEqual(session["provider_total_tokens"], 1050)
        self.assertEqual(session["cache_read_tokens"], 900)
        self.assertEqual(session["cache_creation_tokens"], 30)
        self.assertEqual(session["messages"], 2)
        self.assertEqual(session["assistant_messages"], 1)
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

        encoded = json.dumps(result)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, encoded)

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
        self.assertEqual(payload["provider_total_tokens"], 1050)
        self.assertEqual(payload["messages"], 2)
        self.assertEqual(payload["tool_calls"], 2)
        self.assertNotEqual(payload.get("mode"), "inventory")

        encoded = json.dumps(payload)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, encoded)


if __name__ == "__main__":
    unittest.main()
