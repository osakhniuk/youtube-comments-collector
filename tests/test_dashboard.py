"""Dashboard tests: temporary SQLite/logs, no network or collector invocation."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from youtube_collector.dashboard_data import (
    Snapshot, chart_counts, collector_status, filter_comments, read_api_statistics,
    read_limit, read_snapshot, safe_url, statistics,
)


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "comments.db"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TABLE comments (author TEXT, date TEXT, text TEXT,
                url TEXT, video_id TEXT, comment_id TEXT, collected_at TEXT)""")
            connection.execute("""CREATE TABLE collector_state (video_id TEXT, last_seen_at TEXT,
                scan_next_page_token TEXT, backfill_next_page_token TEXT, backfill_complete INTEGER,
                scan_pending_page INTEGER, scan_newest_at TEXT)""")
            connection.executemany("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?)", [
                ("Автор", "2026-10-01T12:00:00Z", "Привіт, Україно!\nДругий рядок", "https://www.youtube.com/watch?v=v1&lc=a", "v1", "a", "2026-10-20T10:00:00Z"),
                ("Інший", "2026-10-02T12:00:00Z", "Відгук %", "https://www.youtube.com/watch?v=v1&lc=b", "v1", "b", "2026-10-20T10:00:00Z"),
                (None, None, None, None, "v2", "c", "2026-10-20T10:00:00Z"),
            ])
            connection.execute("INSERT INTO collector_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                               ("v1", None, "secret-cursor-token", None, 0, 0, "2026-10-02T12:00:00Z"))
        self.digest = hashlib.sha256(self.path.read_bytes()).digest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), self.digest)

    def test_statistics_and_readonly_database(self):
        snapshot = read_snapshot(self.path)
        self.assertIsNone(snapshot.error)
        stats = statistics(snapshot)
        self.assertEqual((stats["total"], stats["unique"], stats["videos"]), (3, 3, 2))
        self.assertEqual(stats["oldest"].date(), date(2026, 10, 1))
        self.assertEqual(stats["newest"].date(), date(2026, 10, 2))
        self.assertEqual(stats["missing_dates"], 1)

    def test_tokens_are_never_returned(self):
        snapshot = read_snapshot(self.path)
        self.assertNotIn("secret-cursor-token", str(snapshot))
        self.assertEqual(snapshot.states[0]["has_scan_next_page_token"], 1)
        self.assertEqual(collector_status(snapshot, 3), "Призупинено: ліміт досягнуто")
        self.assertEqual(collector_status(snapshot, 10), "Збережено прогрес для продовження")

    def test_filter_text_author_and_literal_search(self):
        rows = read_snapshot(self.path).comments
        self.assertEqual([row["comment_id"] for row in filter_comments(rows, text="УКРАЇНО", author="авт")], ["a"])
        self.assertEqual([row["comment_id"] for row in filter_comments(rows, text="%")], ["b"])
        self.assertEqual(filter_comments(rows, text="does not exist"), [])

    def test_filter_dates_videos_and_sorting(self):
        rows = read_snapshot(self.path).comments
        self.assertEqual([row["comment_id"] for row in filter_comments(rows, videos=["v1"], ascending=True)], ["a", "b"])
        self.assertEqual([row["comment_id"] for row in filter_comments(rows, start=date(2026, 10, 2), end=date(2026, 10, 2))], ["b"])
        self.assertEqual(filter_comments(rows, videos=[]), [])
        self.assertEqual(len(filter_comments(rows, start=date(2026, 10, 2), include_undated=True)), 2)

    def test_chart_counts_use_actual_rows(self):
        counts = chart_counts(read_snapshot(self.path).comments)
        self.assertEqual(counts["dates"], [("2026-10-01", 1), ("2026-10-02", 1)])
        self.assertEqual(counts["videos"], [("v1", 2), ("v2", 1)])
        self.assertEqual(counts["hours"], [(12, 2)])

    def test_empty_database(self):
        path = self.path.parent / "empty.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE comments (comment_id TEXT)")
        snapshot = read_snapshot(path)
        self.assertIsNone(snapshot.error)
        self.assertEqual(statistics(snapshot)["total"], 0)
        self.assertTrue(snapshot.warnings)

    def test_missing_database_is_not_created(self):
        missing = self.path.parent / "missing.db"
        self.assertIsNotNone(read_snapshot(missing).error)
        self.assertFalse(missing.exists())

    def test_invalid_database_and_missing_comments_table(self):
        invalid = self.path.parent / "invalid.db"
        invalid.write_text("not sqlite")
        self.assertIsNotNone(read_snapshot(invalid).error)
        blank = self.path.parent / "blank.db"
        with closing(sqlite3.connect(blank)):
            pass
        self.assertIn("comments", read_snapshot(blank).error)

    def test_permission_error_is_safe(self):
        with patch("youtube_collector.dashboard_data.sqlite3.connect", side_effect=sqlite3.OperationalError("private")):
            snapshot = read_snapshot(self.path)
        self.assertIsNotNone(snapshot.error)
        self.assertNotIn("private", snapshot.error)

    def test_limit_reading_exposes_only_numeric_value(self):
        path = self.path.parent / ".env"
        path.write_text("YOUTUBE_API_KEY=must-not-be-displayed\nMAX_TOTAL_COMMENTS=200\n")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_limit(path), (200, None))
        path.write_text("MAX_TOTAL_COMMENTS=invalid\n")
        with patch.dict("os.environ", {}, clear=True):
            value, warning = read_limit(path)
        self.assertIsNone(value)
        self.assertIsNotNone(warning)

    def test_safe_links(self):
        self.assertIsNone(safe_url("javascript:alert(1)"))
        self.assertIsNone(safe_url("https://example.com"))
        self.assertEqual(safe_url("https://www.youtube.com/watch?v=abc"), "https://www.youtube.com/watch?v=abc")


class DashboardLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "collector.log"
        self.snapshot = Snapshot(comments=[{"video_id": "v1", "collected_at": "2026-10-20T10:00:00Z"}])

    def write_events(self, events):
        self.path.write_text("\n".join("2026-10-20T10:00:00Z INFO youtube_collector.api " + json.dumps(event)
                                       for event in events), encoding="utf-8")

    def test_missing_or_unstructured_logs_show_unknown(self):
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["requests"])
        self.path.write_text("unstructured text\n{invalid json}")
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["http_errors"])

    def test_marked_mock_events_not_counted(self):
        self.write_events([{"event": "request_result", "video_id": "v1", "status": 429, "execution": "test"},
                           {"event": "request_started", "video_id": "v1", "attempt": 2, "execution": "mock"}])
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["requests"])

    def test_legacy_events_require_matching_real_commit(self):
        events = [{"event": "request_started", "video_id": "v1", "attempt": 1},
                  {"event": "request_result", "video_id": "v1", "status": 200, "attempt": 1},
                  {"event": "page_committed", "video_id": "v1", "saved": 1}]
        self.write_events(events)
        stats = read_api_statistics(self.snapshot, self.path)
        self.assertIsNone(stats["requests"])
        self.assertIsNone(stats["last_success"])
        self.assertEqual(stats["unknown_events"], 3)

    def test_runtime_errors_retries_and_rate_limits(self):
        events = [{"event": "request_started", "attempt": 1}, {"event": "request_result", "status": 429},
                  {"event": "request_started", "attempt": 2}, {"event": "request_result", "status": 200}]
        self.write_events([{**event, "video_id": "v1", "origin": "real", "run_id": "fixture-run"} for event in events])
        stats = read_api_statistics(self.snapshot, self.path)
        self.assertEqual((stats["requests"], stats["http_errors"], stats["retries"], stats["rate_limits"]), (2, 1, 1, 1))

    def test_audit_summary_is_not_http_evidence(self):
        self.write_events([{"event": "offline_audit_completed", "requests": 100, "status": 429}])
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["requests"])


class DashboardUITests(unittest.TestCase):
    def setUp(self):
        from collector_test_support import sample_records
        from youtube_collector.api import Comment
        from youtube_collector.storage import save_comments
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "comments.db"
        save_comments([Comment(**{k: row[k] for k in Comment.__dataclass_fields__})
                       for row in sample_records()], self.database)
        for target, value in (("read_snapshot", lambda: read_snapshot(self.database)),
                              ("read_limit", lambda: (200, None)),
                              ("read_api_statistics", lambda snapshot: read_api_statistics(snapshot, self.database.parent / "absent.log"))):
            patcher = patch("src.youtube_collector.dashboard_data." + target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @classmethod
    def tearDownClass(cls):
        # Streamlit 1.66 keeps a module-global test directory until interpreter
        # shutdown. Close it explicitly so ResourceWarning checks remain useful.
        from streamlit.testing.v1.app_test import TMP_DIR
        TMP_DIR.cleanup()

    def test_R9_render_filters_refresh_without_working_files_or_network(self):
        from streamlit.testing.v1 import AppTest
        root = Path(__file__).resolve().parents[1]
        tracked = [self.database]
        before = [hashlib.sha256(path.read_bytes()).digest() for path in tracked]
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Dashboard must not use HTTP")):
            app = AppTest.from_file(str(root / "dashboard.py")).run(timeout=30)
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.metric[0].value, "200")
            app.text_input[0].set_value("__NO_MATCH_FOR_TEST__").run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("немає" in message.value for message in app.info))
            app.button[0].click().run()
            self.assertEqual(list(app.exception), [])
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in tracked])

    def test_Q5_table_dates_are_displayed_in_UTC_without_mutating_snapshot(self):
        from streamlit.testing.v1 import AppTest
        from src.youtube_collector.dashboard_data import Snapshot as UISnapshot
        snapshot = UISnapshot(comments=[dict(author="Автор", date="2026-10-01T12:00:00+02:00",
            text="Temporary UI fixture", url="https://www.youtube.com/watch?v=v&lc=id",
            video_id="v", comment_id="id", collected_at="2026-10-20T12:00:00Z")])
        original = [dict(row) for row in snapshot.comments]
        with patch("src.youtube_collector.dashboard_data.read_snapshot", return_value=snapshot), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden")):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run(timeout=30)
        self.assertEqual(list(app.exception), [])
        table = next(item.value for item in app.dataframe if "date" in item.value.columns)
        self.assertEqual(table.iloc[0]["date"].isoformat(), "2026-10-01T10:00:00+00:00")
        self.assertEqual(app.metric[3].value, "01.10.2026 10:00")
        self.assertEqual(snapshot.comments, original)

    def test_incomplete_retry_metric_is_displayed_as_unknown(self):
        from streamlit.testing.v1 import AppTest
        log = self.database.parent / "absent.log"
        log.write_text('2026-10-20T12:00:00Z INFO youtube_collector.api ' + json.dumps({
            "event": "request_started", "video_id": "fixture-video",
            "origin": "real", "run_id": "isolated-input-fixture"}) + '\n', encoding="utf-8")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden")):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run(timeout=30)
        self.assertEqual(list(app.exception), [])
        retry = next(metric for metric in app.metric if metric.label == "Повторних спроб")
        self.assertEqual(retry.value, "Дані відсутні")
        self.assertTrue(any("неповна" in caption.value for caption in app.caption))

    def test_render_missing_database_without_crashing(self):
        from streamlit.testing.v1 import AppTest
        from src.youtube_collector.dashboard_data import Snapshot as UISnapshot
        root = Path(__file__).resolve().parents[1]
        with patch("src.youtube_collector.dashboard_data.read_snapshot", return_value=UISnapshot(error="SQLite недоступна")):
            app = AppTest.from_file(str(root / "dashboard.py")).run(timeout=30)
        self.assertEqual(list(app.exception), [])
        self.assertTrue(app.error)

    def test_render_empty_database_without_crashing(self):
        from streamlit.testing.v1 import AppTest
        from src.youtube_collector.dashboard_data import Snapshot as UISnapshot
        root = Path(__file__).resolve().parents[1]
        with patch("src.youtube_collector.dashboard_data.read_snapshot", return_value=UISnapshot()):
            app = AppTest.from_file(str(root / "dashboard.py")).run(timeout=30)
        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.metric[0].value, "0")
        self.assertTrue(any("порожня" in message.value for message in app.info))


class MalformedLogRegressionTests(unittest.TestCase):
    setUp = DashboardLogTests.setUp
    write_events = DashboardLogTests.write_events

    def test_Q2_missing_or_invalid_attempt_makes_retry_metric_unknown(self):
        for changes in ({}, {"attempt": None}, {"attempt": True}, {"attempt": "2"},
                        {"attempt": 0}, {"attempt": -1}, {"attempt": []}):
            for mixed in (False, True):
                with self.subTest(changes=changes, mixed=mixed):
                    event = {"event": "request_started", "video_id": "v1", "origin": "real", "run_id": "fixture"}
                    events = [{**event, **changes}]
                    if mixed:
                        events.append({**event, "attempt": 2})
                    self.write_events(events)
                    stats = read_api_statistics(self.snapshot, self.path)
                    self.assertEqual(stats["requests"], len(events))
                    self.assertIsNone(stats["retries"])
                    self.assertIn("неповна", stats["note"])

    def test_valid_attempts_still_report_proven_zero_or_retry(self):
        event = {"event": "request_started", "video_id": "v1", "origin": "real", "run_id": "fixture"}
        self.write_events([{**event, "attempt": 1}])
        self.assertEqual(read_api_statistics(self.snapshot, self.path)["retries"], 0)
        self.write_events([{**event, "attempt": 1}, {**event, "attempt": 2}])
        self.assertEqual(read_api_statistics(self.snapshot, self.path)["retries"], 1)

    def test_R6_malformed_prefix_and_types_do_not_crash(self):
        self.path.write_text(' {"event":"request_result"}\n', encoding="utf-8")
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["requests"])
        malformed = [{"video_id": []}, {"status": True}, {"attempt": "2"}, {"saved": []}, {"reason": {}}]
        self.write_events([{"event": "request_result", "video_id": "v1", "status": 200,
                            "origin": "real", "run_id": "run", **changes} for changes in malformed])
        self.assertIsNone(read_api_statistics(self.snapshot, self.path)["requests"])

    def test_R8_legacy_matching_commit_is_unknown_and_test_origin_excluded(self):
        self.write_events([
            {"event": "request_result", "video_id": "v1", "status": 429, "execution": "runtime"},
            {"event": "page_committed", "video_id": "v1", "saved": 1},
            {"event": "request_result", "video_id": "v1", "status": 500, "origin": "test", "run_id": "mock"},
            {"event": "request_result", "video_id": "v1", "status": 200, "origin": "real", "run_id": "real-run"}])
        stats = read_api_statistics(self.snapshot, self.path)
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["http_errors"], 0)
        self.assertEqual(stats["unknown_events"], 2)
        self.assertIsNone(stats["last_success"])
