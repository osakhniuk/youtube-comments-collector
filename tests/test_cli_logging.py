"""Exercise the literal python -m entry point with a temporary sitecustomize."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Runs before the module entry point, without adding production test switches.
SHIM = '''
import os,sqlite3
from pathlib import Path
from unittest.mock import Mock,patch
from youtube_collector import collector,config,logging_setup,storage
context=logging_setup.event_context('test')
context.__enter__()
root=Path(__file__).parent
storage.DEFAULT_DB_PATH=root/'comments.db'
original_collect=collector.collect_video
original_logging=logging_setup.configure_logging
scenario=os.environ['CLI_TEST_SCENARIO']
videos=('broken','ok','quota','not-run') if scenario=='errors' else ('ok',)
patch('youtube_collector.config.load_config',return_value=config.Config(
    youtube_api_key='isolated-test-key',video_ids=videos,request_delay_seconds=0)).start()
def collect(cfg,video,**kwargs):
    if video=='broken':raise sqlite3.OperationalError('private error')
    return original_collect(cfg,video,storage.DEFAULT_DB_PATH,**kwargs)
patch('youtube_collector.collector.collect_video',side_effect=collect).start()
patch('youtube_collector.logging_setup.configure_logging',side_effect=lambda **kwargs:
      original_logging(root/'collector.log',**kwargs)).start()
patch('requests.sessions.Session.request',side_effect=AssertionError('real HTTP forbidden')).start()
session=patch('youtube_collector.api.requests.Session').start().return_value.__enter__.return_value
success=Mock(status_code=200,headers={},json=Mock(return_value={'items':[]}))
quota=Mock(status_code=403,headers={},json=Mock(return_value={'error':{'errors':[{'reason':'quotaExceeded'}]}}))
session.get.side_effect=[success,quota] if scenario=='errors' else [success]
'''


class CLILoggingTests(unittest.TestCase):
    def run_cli(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sitecustomize.py").write_text(SHIM, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).resolve().parents[1] / "src"))),
                   "PYTHONDONTWRITEBYTECODE": "1", "CLI_TEST_SCENARIO": scenario}
            result = subprocess.run([sys.executable, "-m", "youtube_collector.main"], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertTrue((root / "collector.log").exists(), result.stderr)
            contents = (root / "collector.log").read_text(encoding="utf-8")
            self.assertNotIn("isolated-test-key", contents)
            self.assertNotIn("private error", contents)
            events = [json.loads(line.split(" ", 3)[3]) for line in contents.splitlines()]
            self.assertTrue(all(event["origin"] == "test" for event in events))
            self.assertEqual(len({event["run_id"] for event in events}), 1)
            return result, events

    def test_R3_module_entrypoint_logs_start_finish_and_http(self):
        result, events = self.run_cli("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [event["event"] for event in events]
        for name in ("run_started", "request_started", "request_result", "run_finished"):
            self.assertIn(name, names)

    def test_module_entrypoint_logs_video_error_and_global_quota_stop(self):
        result, events = self.run_cli("errors")
        self.assertEqual(result.returncode, 1, result.stderr)
        names = [event["event"] for event in events]
        for name in ("run_started", "video_failed", "run_stopped", "run_finished"):
            self.assertIn(name, names)
        self.assertEqual([event["status"] for event in events if event["event"] == "request_result"], [200, 403])
        self.assertNotIn("Video ID: not-run", result.stdout)
