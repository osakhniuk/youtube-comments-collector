"""Regression reproductions from AUDIT_FINAL, isolated from working data."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from youtube_collector.api import Comment, CommentPage, YouTubeAPIError
from youtube_collector.collector import collect_video
from youtube_collector.config import Config
from youtube_collector.storage import load_state, save_comments


def comment(identifier, date="2026-10-20T12:00:00Z"):
    return Comment(identifier, "Fixture author", "https://www.youtube.com/watch?v=v&lc=" + identifier,
                   date, "Isolated regression fixture", "v")


class IncrementalRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "comments.db"
        self.config = Config(request_delay_seconds=0, max_total_comments=1000)

    def test_R1_equal_timestamp_on_next_page_is_not_skipped(self):
        history = [comment("watermark")] + [comment(f"known-{i}", "2026-10-20T11:59:59Z") for i in range(99)]
        save_comments(history, self.path)
        with patch("youtube_collector.collector.fetch_comment_page", side_effect=[
            CommentPage(history, "next"), CommentPage([comment("missed-equal-time")]),
        ]) as fetch:
            result = collect_video(self.config, "v", self.path)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(result.complete)
        self.assertEqual(result.saved, 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments WHERE comment_id = 'missed-equal-time'").fetchone()[0], 1)

    def test_old_pages_streak_resumes_after_budget_and_failure(self):
        save_comments([comment("watermark")], self.path)
        old1 = comment("old1", "2026-10-20T11:00:00Z")
        old2 = comment("old2", "2026-10-20T10:00:00Z")
        with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([old1], "p2")):
            first = collect_video(replace(self.config, max_pages_per_run=1), "v", self.path)
        self.assertFalse(first.complete)
        self.assertEqual(load_state("v", self.path).scan_old_pages, 1)
        with patch("youtube_collector.collector.fetch_comment_page", side_effect=YouTubeAPIError("failure")):
            failed = collect_video(self.config, "v", self.path)
        self.assertFalse(failed.complete)
        with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([old2], "history")) as fetch:
            resumed = collect_video(self.config, "v", self.path)
        self.assertEqual(fetch.call_args.args[2], "p2")
        self.assertEqual(resumed.stop_reason, "old_page_overlap")
        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.pages, 1)


class ValidationRegressionTests(unittest.TestCase):
    setUp = IncrementalRegressionTests.setUp

    def test_R4_empty_identifier_rejects_entire_page_and_preserves_cursor(self):
        save_comments([comment("baseline")], self.path)
        with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([comment("first")], "p2")):
            collect_video(replace(self.config, max_pages_per_run=1), "v", self.path)
        before = load_state("v", self.path)
        with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([
            comment("valid-but-rolled-back"), comment("")], "p3")):
            result = collect_video(self.config, "v", self.path)
        self.assertEqual(result.stop_reason, "invalid_comment")
        self.assertEqual(load_state("v", self.path), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 2)

    def test_invalid_bulk_save_rolls_back_and_blank_display_fields_are_preserved(self):
        from youtube_collector.api import InvalidComment
        with self.assertRaises(InvalidComment):
            save_comments([comment("valid"), comment(" ")], self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)
        blank = replace(comment("blank-display"), author="", text="")
        save_comments([blank], self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT author,text FROM comments").fetchone(), ("", ""))

    def test_API_parser_rejects_empty_id(self):
        from youtube_collector.api import _parse_page
        payload = {"items": [{"snippet": {"topLevelComment": {"id": "", "snippet": {
            "authorDisplayName": "", "textDisplay": "", "publishedAt": "2026-10-20T12:00:00Z"}}}}]}
        with self.assertRaises(YouTubeAPIError) as caught:
            _parse_page(payload, "v")
        self.assertEqual(caught.exception.reason, "invalid_response")
