"""Offline storage and CLI tests using temporary databases only."""

from contextlib import closing
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from youtube_collector.api import Comment
from youtube_collector.storage import SaveResult, init_db, save_comments


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "comments.db"
        self.comment = Comment("id-1", "Автор", "https://example.com", "2026-10-20T10:00:00Z",
                               "Текст ' із лапками 😀", "video-1")

    def rows(self, query):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute(query).fetchall()

    def test_schema_and_empty_batch(self):
        self.assertEqual(save_comments([], self.path), SaveResult(0, 0, 0, 0))
        self.assertEqual([row[1] for row in self.rows("PRAGMA table_info(collector_state)")][:5],
                         ["video_id", "last_seen_at", "last_seen_comment_id",
                          "backfill_next_page_token", "updated_at"])
        self.assertEqual(self.rows("SELECT * FROM collector_state"), [])

    def test_duplicates_in_batch_and_repeated_save(self):
        self.assertEqual(save_comments([self.comment] * 2, self.path), SaveResult(2, 1, 1, 1))
        original = self.rows("SELECT * FROM comments")
        self.assertEqual(original[0][:7], ("youtube", "id-1", "video-1", "Автор",
                                          "https://example.com", "2026-10-20T10:00:00Z",
                                          self.comment.text))
        self.assertIsNotNone(datetime.fromisoformat(original[0][7]).tzinfo)
        init_db(self.path)
        changed = replace(self.comment, text="Edited", video_id="other-video")
        self.assertEqual(save_comments([changed], self.path), SaveResult(1, 0, 1, 1))
        self.assertEqual(self.rows("SELECT * FROM comments"), original)

    def test_mixed_new_and_existing_comments(self):
        save_comments([self.comment], self.path)
        second = replace(self.comment, comment_id="id-2")
        self.assertEqual(save_comments(iter([self.comment, second]), self.path), SaveResult(2, 1, 1, 2))
        self.assertEqual(save_comments([], self.path), SaveResult(0, 0, 0, 2))

    def test_unique_key_includes_source(self):
        save_comments([self.comment], self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""INSERT INTO comments
                SELECT 'other', comment_id, video_id, author, url, date, text, collected_at
                FROM comments WHERE source = 'youtube'""")
        self.assertEqual(save_comments([self.comment], self.path), SaveResult(1, 0, 1, 2))



if __name__ == "__main__":
    unittest.main()
