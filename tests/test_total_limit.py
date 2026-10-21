"""Global capacity and page replay tests using isolated fixture rows and mock API."""

from collector_test_support import sample_records

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from youtube_collector.api import Comment, CommentPage, YouTubeAPIError
from youtube_collector.collector import collect_video
from youtube_collector.config import Config, load_config
from youtube_collector.storage import load_state, save_comments


class TotalLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = sample_records()
        cls.comments = [Comment(**{name: row[name] for name in Comment.__dataclass_fields__}) for row in rows]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "comments.db"
        self.video = self.comments[0].video_id
        self.config = Config(request_delay_seconds=0, max_total_comments=200)
        api = patch("youtube_collector.collector.fetch_comment_page")
        self.fetch = api.start()
        self.addCleanup(api.stop)

    def run_collector(self, limit=200, mode="incremental", pages=5):
        return collect_video(replace(self.config, max_total_comments=limit, max_pages_per_run=pages),
                             self.video, self.path, mode=mode)

    def test_clean_start_exactly_200_and_repeat_no_api(self):
        self.assertFalse(self.path.exists())
        self.fetch.side_effect = [CommentPage(self.comments[:100], "p2"),
                                  CommentPage(self.comments[100:200], "p3")]
        first = self.run_collector()
        self.assertEqual((first.total, first.saved, first.api_requests), (200, 200, 2))
        self.assertEqual(first.stop_reason, "total_limit")
        self.assertFalse(first.complete)
        state = load_state(self.video, self.path)
        self.assertEqual(state.scan_next_page_token, "p3")
        self.assertIsNone(state.last_seen_at)
        self.fetch.reset_mock()
        second = self.run_collector()
        self.fetch.assert_not_called()
        self.assertEqual(second.api_requests, 0)
        self.assertEqual(second.total, 200)
        self.assertEqual(load_state(self.video, self.path), state)

    def test_only_20_remaining_does_not_save_100(self):
        save_comments(self.comments[:180], self.path)
        self.fetch.return_value = CommentPage([*self.comments[180:200], *self.comments[:80]], "next")
        result = self.run_collector()
        self.assertEqual((result.saved, result.total, result.received), (20, 200, 100))
        state = load_state(self.video, self.path)
        self.assertIsNone(state.scan_next_page_token)  # Current page was the head.
        self.assertEqual(state.scan_pending_page, 1)

    def test_partial_head_resume_when_limit_increases_without_skips(self):
        page = CommentPage(self.comments[:100], "p2")
        self.fetch.return_value = page
        first = self.run_collector(limit=20)
        self.assertEqual((first.saved, first.total), (20, 20))
        state = load_state(self.video, self.path)
        self.assertIsNone(state.last_seen_at)
        self.assertEqual(state.scan_pending_page, 1)
        second = self.run_collector(limit=50)
        self.assertEqual((second.saved, second.duplicates, second.total), (30, 20, 50))
        self.assertIsNone(self.fetch.call_args.args[2])
        self.assertIsNone(load_state(self.video, self.path).last_seen_at)
        self.fetch.side_effect = [page, CommentPage([])]
        third = self.run_collector(limit=150)
        self.assertEqual((third.saved, third.duplicates, third.total), (50, 50, 100))
        self.assertTrue(third.complete)
        self.assertIsNotNone(load_state(self.video, self.path).last_seen_at)

    def test_partial_non_head_cursor_does_not_advance(self):
        self.fetch.side_effect = [CommentPage(self.comments[:100], "p2"),
                                  CommentPage(self.comments[100:200], "p3")]
        first = self.run_collector(limit=120)
        self.assertEqual(first.total, 120)
        self.assertEqual(load_state(self.video, self.path).scan_next_page_token, "p2")
        self.fetch.side_effect = [CommentPage(self.comments[100:200], "p3")]
        second = self.run_collector(limit=150)
        self.assertEqual((second.saved, second.duplicates, second.total), (30, 20, 150))
        self.assertEqual(self.fetch.call_args.args[2], "p2")
        self.assertEqual(load_state(self.video, self.path).scan_next_page_token, "p2")

    def test_duplicates_do_not_consume_capacity(self):
        page = [*self.comments[:10], *self.comments[:10], *self.comments[10:30]]
        self.fetch.return_value = CommentPage(page, "p2")
        result = self.run_collector(limit=30)
        self.assertEqual((result.saved, result.duplicates, result.total), (30, 10, 30))

    def test_failure_then_resume_to_limit(self):
        self.fetch.side_effect = [CommentPage(self.comments[:100], "p2"), YouTubeAPIError("failed")]
        first = self.run_collector()
        self.assertEqual((first.saved, first.total), (100, 100))
        state = load_state(self.video, self.path)
        self.assertEqual(state.scan_next_page_token, "p2")
        self.assertIsNone(state.last_seen_at)
        self.fetch.side_effect = [CommentPage(self.comments[100:200], "p3")]
        second = self.run_collector()
        self.assertEqual(second.total, 200)
        self.assertEqual(self.fetch.call_args.args[2], "p2")
        self.assertIsNone(load_state(self.video, self.path).last_seen_at)

    def test_page_budget_then_resume(self):
        self.fetch.return_value = CommentPage(self.comments[:100], "p2")
        first = self.run_collector(pages=1)
        self.assertEqual(first.stop_reason, "page_limit")
        self.assertEqual(first.total, 100)
        self.fetch.return_value = CommentPage(self.comments[100:200], "p3")
        self.assertEqual(self.run_collector(pages=1).total, 200)
        self.assertEqual(self.fetch.call_args.args[2], "p2")

    def test_bootstrap_does_not_stop_after_two_old_pages(self):
        self.fetch.side_effect = [CommentPage(self.comments[:50], "p2"),
                                  CommentPage(self.comments[50:100], "p3"),
                                  CommentPage(self.comments[100:150], "p4"),
                                  CommentPage(self.comments[150:200])]
        result = self.run_collector(limit=250)
        self.assertEqual((result.pages, result.total), (4, 200))
        self.assertTrue(result.complete)

    def test_backfill_partial_page_and_increased_limit(self):
        self.fetch.return_value = CommentPage(self.comments[:100], "p2")
        first = self.run_collector(limit=20, mode="backfill")
        self.assertFalse(first.complete)
        state = load_state(self.video, self.path)
        self.assertEqual(state.backfill_complete, 0)
        self.assertEqual(state.backfill_pending_page, 1)
        self.assertIsNone(state.last_seen_at)
        second = self.run_collector(limit=50, mode="backfill")
        self.assertEqual((second.saved, second.duplicates, second.total), (30, 20, 50))
        self.assertIsNone(self.fetch.call_args.args[2])

    def test_limit_is_global_across_videos(self):
        save_comments(self.comments[:200], self.path)
        result = collect_video(self.config, "a-different-video", self.path)
        self.fetch.assert_not_called()
        self.assertEqual(result.total, 200)
        self.assertEqual(result.stop_reason, "total_limit")

    def test_cap_at_end_of_list_keeps_progress_unfinished(self):
        self.fetch.return_value = CommentPage(self.comments[:20])
        result = self.run_collector(limit=20)
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "total_limit")
        self.assertIsNone(load_state(self.video, self.path).last_seen_at)
        self.fetch.return_value = CommentPage(self.comments[:30])
        resumed = self.run_collector(limit=40)
        self.assertEqual((resumed.saved, resumed.total), (10, 30))
        self.assertTrue(resumed.complete)

    def test_pending_newest_uses_only_processed_prefix(self):
        from youtube_collector.storage import parse_date
        prefix = self.comments[:20]
        self.fetch.return_value = CommentPage([*prefix, *self.comments[20:100]], "p2")
        self.run_collector(limit=20)
        state = load_state(self.video, self.path)
        self.assertEqual(parse_date(state.scan_newest_at), max(parse_date(c.date) for c in prefix))
        self.assertIsNone(state.last_seen_at)
        self.assertIsNone(state.scan_next_page_token)
        self.assertEqual(state.scan_pending_page, 1)

    def test_config_total_limit_validation(self):
        for value in ("0", "-1", "nan", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Config(max_total_comments=int(value))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("MAX_TOTAL_COMMENTS=300\n")
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(load_config(path).max_total_comments, 300)
