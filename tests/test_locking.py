"""Cross-process flock tests: no network and only temporary databases."""

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_collector.collector import collect_video
from youtube_collector.config import Config
from youtube_collector.locking import CollectorAlreadyRunning, collector_lock
from youtube_collector.storage import load_state

CHILD = '''
import sys
from unittest.mock import patch
from youtube_collector.api import Comment,CommentPage
from youtube_collector.collector import collect_video
from youtube_collector.config import Config
from youtube_collector.locking import collector_lock
from youtube_collector.storage import load_state
path=sys.argv[1]
if sys.argv[2]=='hold':
    with collector_lock(path):
        print('READY',flush=True)
        sys.stdin.readline()
else:
    def fetch(config,video,token):
        if token is None:
            print('READY',flush=True)
            sys.stdin.readline()
        comment=Comment('first' if token is None else 'second','Author','https://www.youtube.com/watch?v=v',
                        '2026-10-20T12:00:00Z','Mock only','v')
        return CommentPage([comment],'p2' if token is None else 'p3')
    with patch('youtube_collector.collector.fetch_comment_page',side_effect=fetch):
        collect_video(Config(request_delay_seconds=0,max_pages_per_run=2,max_total_comments=1000),'v',path)
    print(load_state('v',path).scan_next_page_token,flush=True)
'''


class LockingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "comments.db"

    def child(self, mode):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(self.path), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=environment)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], "Child lock startup timed out")
        self.assertEqual(process.stdout.readline().strip(), "READY")
        return process

    def test_R2_second_collector_is_rejected_cursor_stays_p3(self):
        child = self.child("collect")
        with patch("youtube_collector.collector.fetch_comment_page") as fetch:
            with self.assertRaises(CollectorAlreadyRunning):
                collect_video(Config(), "v", self.path)
            fetch.assert_not_called()
        output, error = child.communicate("continue\n", timeout=10)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(output.strip(), "p3")
        self.assertEqual(load_state("v", self.path).scan_next_page_token, "p3")
        with collector_lock(self.path):
            pass

    def test_lock_released_after_sigkill(self):
        child = self.child("hold")
        with self.assertRaises(CollectorAlreadyRunning):
            with collector_lock(self.path):
                pass
        child.kill()
        child.communicate(timeout=10)
        with collector_lock(self.path):
            pass

    def test_nested_run_lock_and_exception_release(self):
        with self.assertRaisesRegex(ValueError, "fixture"):
            with collector_lock(self.path):
                with collector_lock(self.path):
                    raise ValueError("fixture")
        with collector_lock(self.path):
            pass
