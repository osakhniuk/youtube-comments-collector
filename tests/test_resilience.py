"""Resilience audit: mocked HTTP, temporary databases/logs, no real API key."""

from collector_test_support import sample_records

import io
from contextlib import nullcontext
import json
import logging
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from youtube_collector.api import (
    Comment, CommentPage, QuotaExceeded, YouTubeAPIError, _retry_delay, fetch_comment_page,
)
from youtube_collector.collector import CollectionResult, collect_video
from youtube_collector.config import Config, load_config
from youtube_collector.logging_setup import configure_logging, event_context, log_event
from youtube_collector.main import main
from youtube_collector.storage import load_state, save_comments


class HTTPFixtures:
    def setUp(self):
        context = event_context("test")
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.config = Config(youtube_api_key="audit-test-secret", request_delay_seconds=0)
        self.session_patch = patch("youtube_collector.api.requests.Session")
        self.session = self.session_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.session_patch.stop)
        sleep_patch = patch("youtube_collector.api.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        jitter_patch = patch("youtube_collector.api.random.uniform", return_value=0.125)
        self.jitter = jitter_patch.start()
        self.addCleanup(jitter_patch.stop)

    def response(self, status=200, reason=None, headers=None):
        payload = {"items": []} if reason is None else {
            "error": {"message": "audit-test-secret must never be logged",
                      "errors": [{"reason": reason}]}}
        return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=payload))

    def fetch(self, **changes):
        return fetch_comment_page(replace(self.config, **changes), "audit-video")


class HTTPAuditTests(HTTPFixtures, unittest.TestCase):
    def test_429_retry_after_with_jitter(self):
        first = self.response(429, headers={"Retry-After": "7"})
        self.session.get.side_effect = [first, self.response()]
        page = self.fetch()
        self.sleep.assert_called_once_with(7.125)
        first.close.assert_called_once()
        self.assertEqual(page.api_requests, 2)
        self.jitter.assert_called_once_with(0.0, 0.25)

    def test_403_quota_exceeded_stops_without_retry(self):
        self.session.get.return_value = self.response(403, "quotaExceeded")
        with self.assertRaises(QuotaExceeded) as caught:
            self.fetch()
        self.assertEqual(caught.exception.api_requests, 1)
        self.assertEqual(caught.exception.reason, "quotaExceeded")
        self.session.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_500_successful_retry(self):
        self.session.get.side_effect = [self.response(500), self.response()]
        self.assertEqual(self.fetch().api_requests, 2)
        self.sleep.assert_called_once_with(1.125)

    def test_timeout_and_connection_error_recover(self):
        for error in (requests.Timeout("audit-test-secret"), requests.ConnectionError("audit-test-secret")):
            with self.subTest(error=type(error).__name__):
                self.session.get.reset_mock()
                self.sleep.reset_mock()
                self.session.get.side_effect = [error, self.response()]
                self.assertEqual(self.fetch().api_requests, 2)
                self.sleep.assert_called_once_with(1.125)

    def test_timeout_exhaustion_is_bounded_and_hides_exception_text(self):
        self.session.get.side_effect = requests.Timeout("https://example.com/?key=audit-test-secret")
        with self.assertRaises(YouTubeAPIError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.api_requests, 4)
        self.assertEqual(caught.exception.reason, "retries_exhausted")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.125, 2.125, 4.125])
        self.assertNotIn("audit-test-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_permanent_http_errors_never_retry(self):
        for status, reason, category in (
            (400, None, "invalid_parameters"), (401, None, "authorization_error"),
            (403, "commentsDisabled", "commentsDisabled"), (403, "forbidden", "forbidden"),
        ):
            with self.subTest(status=status, reason=reason):
                self.session.get.reset_mock()
                self.sleep.reset_mock()
                response = self.response(status, reason)
                self.session.get.return_value = response
                with self.assertRaises(YouTubeAPIError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.reason, category)
                self.session.get.assert_called_once()
                self.sleep.assert_not_called()
                response.close.assert_called_once()

    def test_zero_retries_and_configured_timeout(self):
        self.session.get.return_value = self.response(500)
        with self.assertRaises(YouTubeAPIError):
            self.fetch(http_max_retries=0, http_connect_timeout_seconds=2, http_read_timeout_seconds=9)
        self.session.get.assert_called_once()
        self.assertEqual(self.session.get.call_args.kwargs["timeout"], (2, 9))
        self.sleep.assert_not_called()

    def test_custom_retry_count(self):
        self.session.get.return_value = self.response(503)
        with self.assertRaises(YouTubeAPIError) as caught:
            self.fetch(http_max_retries=1)
        self.assertEqual(caught.exception.api_requests, 2)
        self.sleep.assert_called_once_with(1.125)

    def test_future_retry_after_date_and_bounded_delay(self):
        fixed = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        header = format_datetime(fixed + timedelta(seconds=30), usegmt=True)
        with patch("youtube_collector.api.datetime") as clock:
            clock.now.return_value = fixed
            self.assertEqual(_retry_delay(header, 0), 30.125)
        self.assertEqual(_retry_delay("120", 0), 120)
        self.assertEqual(_retry_delay(None, 10), 120)
        for header in ("3600", "inf"):
            with self.assertRaises(YouTubeAPIError):
                _retry_delay(header, 0)
        for header in ("nan", "-1", "invalid"):
            self.assertEqual(_retry_delay(header, 0), 1.125)

    def test_excessive_retry_after_does_not_sleep_or_retry(self):
        self.session.get.return_value = self.response(429, headers={"Retry-After": "3600"})
        with self.assertRaises(YouTubeAPIError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.reason, "retry_wait_exceeded")
        self.assertEqual(caught.exception.api_requests, 1)
        self.sleep.assert_not_called()


class LoggingAuditTests(HTTPFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = Path(temporary.name) / "logs" / "collector.log"
        logger = logging.getLogger("youtube_collector")
        original_handlers, original_level, original_propagate = logger.handlers[:], logger.level, logger.propagate
        def restore():
            for handler in logger.handlers[:]:
                if handler not in original_handlers:
                    logger.removeHandler(handler)
                    handler.close()
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
        self.addCleanup(restore)
        configure_logging(self.log, secrets=(self.config.youtube_api_key,))

    def events(self):
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line.split(" ", 3)[3]) for line in lines]

    def test_request_metadata_headers_and_secret_redaction(self):
        self.session.get.side_effect = [self.response(429, headers={
            "Retry-After": "2", "X-RateLimit-Remaining": "0",
            "X-RateLimit-Debug": "https://example.com/?key=audit-test-secret",
            "Authorization": "audit-test-secret", "Set-Cookie": "never-log-cookie",
        }), self.response()]
        self.fetch()
        contents = self.log.read_text(encoding="utf-8")
        self.assertNotIn("audit-test-secret", contents)
        self.assertNotIn("https://", contents)
        self.assertNotIn("never-log-cookie", contents)
        self.assertNotIn("Authorization", contents)
        self.assertRegex(contents, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        events = self.events()
        result = next(event for event in events if event["event"] == "request_result")
        self.assertEqual((result["video_id"], result["status"], result["attempt"]), ("audit-video", 429, 1))
        self.assertEqual(result["rate_limit_headers"]["X-RateLimit-Remaining"], "0")
        self.assertEqual(result["rate_limit_headers"]["Retry-After"], "2")
        delay = next(event for event in events if event["event"] == "retry_wait")
        self.assertEqual(delay["delay_seconds"], 2.125)

    def test_no_raw_exception_payload_or_traceback_in_logs(self):
        self.session.get.side_effect = requests.ConnectionError("audit-test-secret full-url-and-body")
        with self.assertRaises(YouTubeAPIError):
            self.fetch()
        contents = self.log.read_text(encoding="utf-8")
        self.assertNotIn("audit-test-secret", contents)
        self.assertNotIn("full-url-and-body", contents)
        self.assertNotIn("Traceback", contents)
        self.assertIn("connection_error", contents)

    def test_saved_records_logged_after_commit(self):
        row = sample_records()[0]
        comment = Comment(**{name: row[name] for name in Comment.__dataclass_fields__})
        database = self.log.parent / "comments.db"
        with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([comment])):
            collect_video(self.config, comment.video_id, database)
        event = next(event for event in self.events() if event["event"] == "page_committed")
        self.assertEqual((event["saved"], event["received"], event["duplicates"]), (1, 1, 0))

    def test_configuring_logging_twice_does_not_duplicate_records(self):
        configure_logging(self.log, secrets=(self.config.youtube_api_key,))
        log_event(logging.getLogger("youtube_collector.test"), "once")
        self.assertEqual(len(self.events()), 1)


class MainAuditTests(unittest.TestCase):
    def setUp(self):
        context = event_context("test")
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        lock_patch = patch("youtube_collector.main.collector_lock", side_effect=lambda *_: nullcontext())
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.config = Config(video_ids=("first", "second"))
        self.config_patch = patch("youtube_collector.main.load_config", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        setup = patch("youtube_collector.main.configure_logging")
        setup.start()
        self.addCleanup(setup.stop)

    def result(self, video_id, **changes):
        return replace(CollectionResult(video_id, 0, 0, 0, 434, 0, True, False,
                                        stop_reason="end_of_list"), **changes)

    def test_video_api_failure_does_not_stop_other_videos(self):
        failed = self.result("first", complete=False, has_more=True, error="commentsDisabled", stop_reason="api_error")
        with patch("youtube_collector.main.collect_video", side_effect=[failed, self.result("second")]) as collect, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(collect.call_count, 2)
        self.assertIn("Video ID: second", output.getvalue())

    def test_invalid_comment_page_is_reported_and_next_video_runs(self):
        failed = self.result("first", complete=False, has_more=True,
                             error="Invalid page", stop_reason="invalid_comment")
        with patch("youtube_collector.main.collect_video", side_effect=[failed, self.result("second")]) as collect, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit):
                main()
        self.assertEqual(collect.call_count, 2)
        self.assertIn("курсор збережено", output.getvalue())

    def test_video_initialization_failure_does_not_stop_other_videos(self):
        for error in (sqlite3.OperationalError("private details"), ValueError("corrupt state"), PermissionError()):
            with self.subTest(error=type(error).__name__):
                with patch("youtube_collector.main.collect_video", side_effect=[error, self.result("second")]) as collect, \
                        patch("sys.stdout", new_callable=io.StringIO) as output:
                    with self.assertRaises(SystemExit):
                        main()
                self.assertEqual(collect.call_count, 2)
                self.assertIn("Video ID: second", output.getvalue())
                self.assertNotIn("private details", output.getvalue())

    def test_shared_quota_stops_requests_for_remaining_videos(self):
        failed = self.result("first", complete=False, has_more=True, error="quotaExceeded",
                             stop_reason="quota_exceeded")
        with patch("youtube_collector.main.collect_video", return_value=failed) as collect, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit):
                main()
        collect.assert_called_once()
        self.assertIn("Подальші запити", output.getvalue())

    def test_cursor_and_records_preserved_after_quota_then_resume(self):
        rows = sample_records()
        first = rows[0]
        last = next(row for row in reversed(rows) if row["video_id"] == first["video_id"])
        comments = [Comment(**{name: row[name] for name in Comment.__dataclass_fields__})
                    for row in (first, last)]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "comments.db"
            save_comments([comments[0]], database)
            initial = load_state(first["video_id"], database)
            with patch("youtube_collector.collector.fetch_comment_page", side_effect=[
                CommentPage([comments[1]], "resume-page"), QuotaExceeded("quotaExceeded"),
            ]):
                failed = collect_video(self.config, first["video_id"], database)
            self.assertEqual(failed.stop_reason, "quota_exceeded")
            self.assertEqual(failed.saved, 1)
            state = load_state(first["video_id"], database)
            self.assertEqual(state.last_seen_at, initial.last_seen_at)
            self.assertEqual(state.scan_next_page_token, "resume-page")
            with patch("youtube_collector.collector.fetch_comment_page", return_value=CommentPage([comments[1]])) as fetch:
                resumed = collect_video(self.config, first["video_id"], database)
            self.assertEqual(fetch.call_args.args[2], "resume-page")
            self.assertEqual((resumed.saved, resumed.duplicates, resumed.total), (0, 1, 2))
            self.assertTrue(resumed.complete)


class ConfigAuditTests(unittest.TestCase):
    def load(self, contents):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(contents, encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                return load_config(path)

    def test_safe_http_defaults(self):
        config = self.load("")
        self.assertEqual(config.http_max_retries, 3)
        self.assertEqual(config.http_connect_timeout_seconds, 5)
        self.assertEqual(config.http_read_timeout_seconds, 30)
        self.assertEqual(config.http_max_retry_wait_seconds, 120)
        self.assertEqual(config.request_delay_seconds, 1)
        self.assertEqual(config.max_pages_per_run, 5)

    def test_explicit_http_settings_and_zero_retries(self):
        config = self.load("HTTP_MAX_RETRIES=0\nHTTP_CONNECT_TIMEOUT_SECONDS=2\n"
                           "HTTP_READ_TIMEOUT_SECONDS=8\nHTTP_MAX_RETRY_WAIT_SECONDS=15\n"
                           "REQUEST_DELAY_SECONDS=0\nMAX_PAGES_PER_RUN=2\n")
        self.assertEqual(config.http_max_retries, 0)
        self.assertEqual((config.http_connect_timeout_seconds, config.http_read_timeout_seconds), (2, 8))
        self.assertEqual(config.http_max_retry_wait_seconds, 15)

    def test_invalid_settings_fail_without_echoing_values(self):
        values = {
            "HTTP_MAX_RETRIES": ("-1", "11", "nan", "1.5", "private-secret"),
            "HTTP_CONNECT_TIMEOUT_SECONDS": ("0", "-1", "inf", "nan", "3601"),
            "HTTP_READ_TIMEOUT_SECONDS": ("0", "inf", "nan", "private-secret"),
            "HTTP_MAX_RETRY_WAIT_SECONDS": ("0", "nan", "-1"),
            "REQUEST_DELAY_SECONDS": ("-1", "inf", "nan", "private-secret"),
            "MAX_PAGES_PER_RUN": ("0", "-1", "nan", "private-secret"),
        }
        for name, invalid in values.items():
            for value in invalid:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError) as caught:
                        self.load(f"{name}={value}\n")
                    self.assertNotIn("private-secret", str(caught.exception))


class Transient403RegressionTests(HTTPFixtures, unittest.TestCase):
    def test_R5_temporary_403_retries_with_retry_after(self):
        for reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            with self.subTest(reason=reason):
                self.sleep.reset_mock()
                self.session.get.side_effect = [self.response(403, reason, {"Retry-After": "4"}), self.response()]
                self.assertEqual(self.fetch().api_requests, 2)
                self.sleep.assert_called_once_with(4.125)

    def test_temporary_403_exhaustion_is_bounded(self):
        self.session.get.return_value = self.response(403, "rateLimitExceeded")
        with self.assertRaises(YouTubeAPIError) as caught:
            self.fetch(http_max_retries=2)
        self.assertEqual(caught.exception.api_requests, 3)
        self.assertEqual(caught.exception.reason, "retries_exhausted")
        self.assertEqual(self.sleep.call_count, 2)

    def test_permanent_reason_wins_over_temporary_reason(self):
        for reason in ("quotaExceeded", "commentsDisabled", "forbidden"):
            with self.subTest(reason=reason):
                self.session.get.reset_mock()
                self.sleep.reset_mock()
                response = self.response(403)
                response.json.return_value = {"error": {"errors": [{"reason": reason}, {"reason": "rateLimitExceeded"}]}}
                self.session.get.return_value = response
                with self.assertRaises(YouTubeAPIError):
                    self.fetch()
                self.assertEqual(self.session.get.call_count, 1)
                self.sleep.assert_not_called()
