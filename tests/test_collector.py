"""Incremental collection tests: temporary SQLite, mocked API, no credentials."""

import io
from contextlib import closing, nullcontext
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from youtube_collector.api import Comment, CommentPage, InvalidPageToken, YouTubeAPIError
from youtube_collector.collector import collect_video
from youtube_collector.config import Config
from youtube_collector.logging_setup import event_context
from youtube_collector.main import main
from youtube_collector.storage import init_db, load_state, save_comments


def comment(identifier, minute=20, video_id="video"):
    return Comment(identifier, "Автор", "https://example.com", f"2026-10-20T10:{minute:02d}:00Z",
                   "Текст", video_id)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        context = event_context("test")
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        lock_patch = patch("youtube_collector.main.collector_lock", side_effect=lambda *_: nullcontext())
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        logging_patch = patch("youtube_collector.main.configure_logging")
        logging_patch.start()
        self.addCleanup(logging_patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "comments.db"
        self.config = Config(video_ids=("video",), request_delay_seconds=0, max_pages_per_run=5)
        self.api_patch = patch("youtube_collector.collector.fetch_comment_page")
        self.fetch = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def run_collector(self, **changes):
        return collect_video(replace(self.config, **changes), "video", self.path)

    def state(self):
        return load_state("video", self.path)

    def seed(self):
        save_comments([comment("baseline")], self.path)
        return self.state()

    def ids(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return {row[0] for row in connection.execute("SELECT comment_id FROM comments")}

    def test_first_and_repeated_run(self):
        self.fetch.return_value = CommentPage([comment("opaque-Z"), comment("opaque-A")])
        first = self.run_collector()
        self.assertEqual((first.received, first.saved, first.duplicates, first.total), (2, 2, 0, 2))
        self.assertTrue(first.complete)
        self.assertFalse(first.has_more)
        self.assertEqual(self.state().last_seen_comment_id, "opaque-Z")
        second = self.run_collector()
        self.assertEqual((second.saved, second.duplicates, second.total), (0, 2, 2))
        self.assertTrue(second.complete)

    def test_checkpoint_seeded_from_existing_data_and_empty_state(self):
        init_db(self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO collector_state(video_id) VALUES ('video')")
        save_comments([comment("old", 10), comment("z", 30), comment("a", 30),
                       comment("unrelated", 59, "other")], self.path)
        state = self.state()
        self.assertEqual(state.last_seen_at, comment("z", 30).date)
        self.assertIn(state.last_seen_comment_id, {"z", "a"})
        self.assertEqual(len(self.ids()), 4)

    def test_new_comments_pinned_duplicate_and_ties_across_pages(self):
        self.seed()
        self.fetch.side_effect = [
            CommentPage([comment("baseline"), comment("new", 25)], "p2"),
            CommentPage([comment("same-time-z"), comment("old", 10)], "p3"),
            CommentPage([comment("same-time-a"), comment("same-time-z")], "p4"),
            CommentPage([comment("older", 9)], "p5"),
            CommentPage([comment("oldest", 8)], "historical"),
        ]
        result = self.run_collector()
        self.assertEqual((result.saved, result.duplicates), (6, 2))
        self.assertTrue(result.complete)
        self.assertIsNone(self.state().scan_next_page_token)
        self.assertEqual(self.state().last_seen_comment_id, "new")
        self.assertIn("same-time-a", self.ids())
        self.assertEqual(self.fetch.call_args_list[-1].args[2], "p5")

    def test_time_lookback_does_not_stop_on_nearby_old_comments(self):
        self.seed()
        self.fetch.side_effect = [CommentPage([comment("near", 18)], "p2"),
                                  CommentPage([comment("late", 21)])]
        result = self.run_collector()
        self.assertTrue(result.complete)
        self.assertIn("late", self.ids())
        self.assertEqual(result.pages, 2)

    def test_page_limit_resume_and_checkpoint_only_at_completion(self):
        initial = self.seed()
        self.fetch.side_effect = [CommentPage([comment("new", 30)], "p2")]
        result = self.run_collector(max_pages_per_run=1)
        pending = self.state()
        self.assertFalse(result.complete)
        self.assertTrue(result.has_more)
        self.assertEqual(pending.last_seen_at, initial.last_seen_at)
        self.assertEqual(pending.scan_next_page_token, "p2")
        self.assertEqual(pending.scan_newest_comment_id, "new")
        self.fetch.side_effect = [CommentPage([comment("middle", 25)], "p3"),
                                  CommentPage([comment("old", 10)], "p4"),
                                  CommentPage([comment("older", 9)], "history")]
        resumed = self.run_collector()
        self.assertEqual(self.fetch.call_args_list[1].args[2], "p2")
        self.assertTrue(resumed.complete)
        self.assertEqual(self.state().last_seen_comment_id, "new")
        self.assertNotIn("history", [call.args[2] for call in self.fetch.call_args_list])

    def test_api_failure_mid_scan_and_restart(self):
        initial = self.seed()
        self.fetch.side_effect = [CommentPage([comment("new", 30)], "p2"),
                                  YouTubeAPIError("quotaExceeded")]
        failed = self.run_collector()
        self.assertIn("quotaExceeded", failed.error)
        self.assertEqual((failed.saved, failed.pages), (1, 1))
        self.assertFalse(failed.complete)
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.assertEqual(self.state().scan_next_page_token, "p2")
        self.fetch.side_effect = [CommentPage([comment("new", 30), comment("next", 25)])]
        resumed = self.run_collector()
        self.assertTrue(resumed.complete)
        self.assertEqual((resumed.saved, resumed.duplicates), (1, 1))
        self.assertEqual(self.fetch.call_args.args[2], "p2")
        self.assertEqual(self.state().last_seen_comment_id, "new")

    def test_sqlite_failure_rolls_back_comments_and_cursor(self):
        initial = self.seed()
        # Abort the state write after INSERTs have executed, exercising SQLite rollback.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_state BEFORE UPDATE ON collector_state
                                  BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        self.fetch.return_value = CommentPage([comment("new", 30)], "p2")
        failed = self.run_collector()
        self.assertIn("SQLite", failed.error)
        self.assertFalse(failed.complete)
        self.assertEqual((failed.received, failed.saved), (1, 0))
        self.assertEqual(self.ids(), {"baseline"})
        self.assertEqual(self.state(), initial)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_state")
        self.fetch.return_value = CommentPage([comment("new", 30)])
        self.assertTrue(self.run_collector().complete)
        self.assertEqual(self.ids(), {"baseline", "new"})
        self.assertIsNone(self.fetch.call_args.args[2])

    def test_invalid_token_restarts_safely_and_counts_against_budget(self):
        initial = self.seed()
        self.fetch.return_value = CommentPage([comment("new", 30)], "expired")
        self.run_collector(max_pages_per_run=1)
        self.fetch.side_effect = [InvalidPageToken("expired")]
        result = self.run_collector(max_pages_per_run=1)
        self.assertTrue(result.token_restarted)
        self.assertFalse(result.complete)
        self.assertIsNone(self.state().scan_next_page_token)
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.fetch.side_effect = [CommentPage([comment("new", 30), comment("missed", 25)])]
        result = self.run_collector()
        self.assertIsNone(self.fetch.call_args.args[2])
        self.assertTrue(result.complete)
        self.assertEqual(result.saved, 1)
        self.assertIn("missed", self.ids())

    def test_invalid_token_restart_in_same_run(self):
        self.seed()
        self.fetch.return_value = CommentPage([comment("new", 30)], "expired")
        self.run_collector(max_pages_per_run=1)
        self.fetch.side_effect = [InvalidPageToken("expired"), CommentPage([comment("new", 30)])]
        result = self.run_collector()
        self.assertTrue(result.complete)
        self.assertTrue(result.token_restarted)
        self.assertEqual(result.duplicates, 1)
        self.assertIsNone(self.fetch.call_args.args[2])

    def test_bootstrap_limit_does_not_seed_final_checkpoint_from_partial_data(self):
        self.fetch.return_value = CommentPage([comment("first", 30)], "expired")
        self.run_collector(max_pages_per_run=1)
        self.assertIsNone(self.state().last_seen_at)
        self.fetch.side_effect = [InvalidPageToken("expired")]
        self.run_collector(max_pages_per_run=1)
        self.assertIsNone(self.state().last_seen_at)
        self.fetch.side_effect = [CommentPage([comment("first", 30), comment("second", 29)])]
        self.assertTrue(self.run_collector().complete)
        self.assertEqual(self.state().last_seen_comment_id, "first")

    def test_old_page_overlap_persists_across_runs_and_no_backfill(self):
        self.seed()
        self.fetch.return_value = CommentPage([comment("older", 10)], "p2")
        self.assertFalse(self.run_collector(max_pages_per_run=1).complete)
        self.assertEqual(self.state().scan_old_pages, 1)
        self.fetch.return_value = CommentPage([comment("oldest", 9)], "history")
        self.assertTrue(self.run_collector(max_pages_per_run=1).complete)
        self.assertIsNone(self.state().backfill_next_page_token)
        self.fetch.return_value = CommentPage([])
        self.run_collector()
        self.assertIsNone(self.fetch.call_args.args[2])

    def test_legacy_schema_migration_preserves_records_and_state(self):
        init_db(self.path)
        save_comments([comment("baseline")], self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE collector_state")
            connection.execute("""CREATE TABLE collector_state (
                video_id TEXT PRIMARY KEY, last_seen_at TEXT, last_seen_comment_id TEXT,
                backfill_next_page_token TEXT, updated_at TEXT)""")
            connection.execute("INSERT INTO collector_state VALUES (?, ?, ?, ?, ?)",
                               ("video", comment("baseline").date, "baseline", "reserved", "old"))
        state = self.state()
        self.assertEqual(state.last_seen_comment_id, "baseline")
        self.assertEqual(state.backfill_next_page_token, "reserved")
        self.assertEqual(self.ids(), {"baseline"})
        init_db(self.path)
        self.assertEqual(self.state(), state)

    def test_checkpoint_uses_instants_not_timestamp_string_order(self):
        older = replace(comment("older"), date="2026-10-20T12:00:00+02:00")
        newer = replace(comment("newer"), date="2026-10-20T10:05:00Z")
        save_comments([older, newer], self.path)
        self.assertEqual(self.state().last_seen_comment_id, "newer")

    def test_empty_page_with_token_does_not_complete(self):
        self.fetch.return_value = CommentPage([], "p2")
        self.assertFalse(self.run_collector(max_pages_per_run=1).complete)
        self.assertEqual(self.state().scan_next_page_token, "p2")

    def history(self, count=134):
        from datetime import datetime, timedelta, timezone
        newest = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        return [replace(comment(f"id-{i}"), date=(newest - timedelta(seconds=i)).isoformat())
                for i in range(count)]

    def test_repeat_134_comments_inside_lookback_reads_two_pages(self):
        history = self.history()
        save_comments(history, self.path)
        initial = self.state()
        self.fetch.side_effect = [CommentPage(history[:100], "p2"), CommentPage(history[100:])]
        result = self.run_collector()
        self.assertEqual((result.api_requests, result.pages, result.received), (2, 2, 134))
        self.assertEqual((result.saved, result.duplicates, result.total), (0, 134, 134))
        self.assertEqual(result.stop_reason, "end_of_list")
        self.assertTrue(result.complete)
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.assertEqual(self.state().last_seen_comment_id, initial.last_seen_comment_id)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertIsNone(self.fetch.call_args_list[0].args[2])

    def test_new_comment_and_old_pin_inside_temporal_lookback(self):
        history = self.history()
        save_comments(history, self.path)
        new = replace(comment("new"), date="2026-10-20T13:00:00Z")
        self.fetch.side_effect = [CommentPage([history[-1], new, *history[:98]], "history"), CommentPage(history[98:])]
        result = self.run_collector()
        self.assertEqual((result.api_requests, result.saved, result.duplicates), (2, 1, 135))
        self.assertEqual(result.stop_reason, "end_of_list")
        self.assertEqual(self.state().last_seen_comment_id, "new")

    def test_new_item_after_overlap_in_same_page_prevents_early_stop(self):
        history = self.history()
        save_comments(history, self.path)
        new = replace(comment("new"), date="2026-10-20T13:00:00Z")
        later = replace(new, comment_id="later")
        self.fetch.side_effect = [CommentPage([history[-1], *history[:98], new], "p2"),
                                  CommentPage([later, *history[100:]])]
        result = self.run_collector()
        self.assertEqual((result.api_requests, result.pages, result.saved), (2, 2, 2))
        self.assertIn("later", self.ids())
        self.assertEqual(result.stop_reason, "end_of_list")

    def test_watermark_timestamp_ties_extend_beyond_first_page(self):
        history = self.history()
        tied = [replace(c, date=history[0].date) for c in history[:101]]
        older = history[101:]
        save_comments([*tied, *older], self.path)
        new_tie = replace(tied[0], comment_id="new-same-time")
        self.fetch.side_effect = [CommentPage(tied[:100], "p2"),
                                  CommentPage([tied[100], new_tie, *older])]
        result = self.run_collector()
        self.assertEqual((result.api_requests, result.saved), (2, 1))
        self.assertIn("new-same-time", self.ids())
        self.assertEqual(result.stop_reason, "end_of_list")

    def test_one_repeated_known_id_does_not_count_as_overlap(self):
        history = self.history()
        save_comments(history, self.path)
        self.fetch.side_effect = [CommentPage([history[-1]] * 100, "p2"),
                                  CommentPage([replace(history[0], comment_id="new")])]
        result = self.run_collector()
        self.assertEqual(result.pages, 2)
        self.assertIn("new", self.ids())

    def test_cursor_and_candidate_survive_failure(self):
        history = self.history()
        save_comments(history, self.path)
        initial = self.state()
        new = replace(history[0], comment_id="new", date="2026-10-20T13:00:00Z")
        self.fetch.side_effect = [CommentPage([new, *history[1:11]], "p2"),
                                  YouTubeAPIError("quotaExceeded")]
        failed = self.run_collector()
        self.assertEqual((failed.api_requests, failed.pages), (2, 1))
        self.assertEqual(failed.stop_reason, "api_error")
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.fetch.side_effect = [CommentPage(history[11:21])]
        resumed = self.run_collector()
        self.assertEqual((resumed.api_requests, resumed.pages), (1, 1))
        self.assertEqual(resumed.stop_reason, "end_of_list")
        self.assertEqual(self.state().last_seen_comment_id, "new")
        self.assertEqual(self.fetch.call_args.args[2], "p2")

    def test_request_stats_include_retries_and_failed_pages(self):
        # Exception counters are provided by the HTTP client, independently of pages.
        failure = YouTubeAPIError("failed")
        failure.api_requests = 4
        self.fetch.side_effect = [CommentPage([comment("x")], "p2", api_requests=3), failure]
        result = self.run_collector()
        self.assertEqual((result.api_requests, result.pages), (7, 1))
        self.assertEqual(result.stop_reason, "api_error")

    def test_backfill_is_separate_and_resumable(self):
        history = self.history()
        save_comments(history, self.path)
        initial = self.state()
        self.fetch.return_value = CommentPage(history[:100], "backfill-p2")
        first = collect_video(replace(self.config, max_pages_per_run=1), "video", self.path,
                              mode="backfill")
        self.assertFalse(first.complete)
        self.assertEqual(first.stop_reason, "page_limit")
        self.assertEqual(self.state().backfill_next_page_token, "backfill-p2")
        self.assertIsNone(self.state().scan_next_page_token)
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.fetch.return_value = CommentPage(history[100:])
        second = collect_video(self.config, "video", self.path, mode="backfill")
        self.assertTrue(second.complete)
        self.assertEqual(self.fetch.call_args.args[2], "backfill-p2")
        self.assertEqual(self.state().last_seen_at, initial.last_seen_at)
        self.assertEqual(self.state().backfill_complete, 1)
        self.fetch.reset_mock()
        third = collect_video(self.config, "video", self.path, mode="backfill")
        self.fetch.assert_not_called()
        self.assertEqual(third.stop_reason, "backfill_already_complete")
        self.assertEqual(third.api_requests, 0)
        self.fetch.return_value = CommentPage(history[:100])
        self.assertEqual(self.run_collector().stop_reason, "end_of_list")

    def test_main_two_runs_statistics(self):
        self.fetch.return_value = CommentPage([comment("x")])
        real_collect = collect_video
        with patch("youtube_collector.main.load_config", return_value=self.config), \
                patch("youtube_collector.main.collect_video",
                      side_effect=lambda config, video: real_collect(config, video, self.path)):
            for saved, duplicates in ((1, 0), (0, 1)):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    main()
                for line in ("Video ID: video", "Отримано коментарів: 1",
                             f"Нових збережено: {saved}", f"Дублікатів пропущено: {duplicates}",
                             "Загалом записів у базі: 1", "Виконано API-запитів: 1",
                             "Прочитано сторінок: 1", "Причина завершення: кінець списку API",
                             "Стан інкрементного збору: завершено",
                             "Чи залишилися сторінки для продовження: ні"):
                    self.assertIn(line, output.getvalue())

    def test_main_error_reports_stats_and_exits_nonzero(self):
        self.fetch.side_effect = [CommentPage([comment("x")], "p2"), YouTubeAPIError("quotaExceeded")]
        real_collect = collect_video
        with patch("youtube_collector.main.load_config", return_value=self.config), \
                patch("youtube_collector.main.collect_video",
                      side_effect=lambda config, video: real_collect(config, video, self.path)), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Нових збережено: 1", output.getvalue())
        self.assertIn("quotaExceeded", output.getvalue())
        self.assertIn("перервано", output.getvalue())


if __name__ == "__main__":
    unittest.main()
