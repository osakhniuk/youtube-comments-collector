"""Offline tests: never read the real .env or contact Google."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from youtube_collector.api import InvalidPageToken, YouTubeAPIError, _retry_delay, fetch_comments, fetch_comment_page
from youtube_collector.config import Config, load_config
from youtube_collector.logging_setup import event_context


class APITests(unittest.TestCase):
    def setUp(self):
        context = event_context("test")
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.jitter_patch = patch("youtube_collector.api.random.uniform", return_value=0.0)
        self.jitter = self.jitter_patch.start()
        self.addCleanup(self.jitter_patch.stop)
        self.config = Config(youtube_api_key="test-secret", video_ids=("mVOq9iKH818",), request_delay_seconds=0)
        self.session_patch = patch("youtube_collector.api.requests.Session")
        self.session = self.session_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.session_patch.stop)
        self.sleep_patch = patch("youtube_collector.api.time.sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def response(self, status=200, payload=None, headers=None):
        return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=payload or {"items": []}))

    def fetch(self):
        return fetch_comments(self.config, self.config.video_ids[0])

    def test_comment_fields_and_request(self):
        self.session.get.return_value = self.response(payload={"items": [{"id": "thread-id", "snippet": {
            "topLevelComment": {"id": "comment-id", "snippet": {
                "authorDisplayName": "Автор", "publishedAt": "2026-10-20T10:00:00Z", "textDisplay": "Текст"}}}}]})
        comment, = self.fetch()
        self.assertEqual(comment.comment_id, "comment-id")
        self.assertEqual(comment.author, "Автор")
        self.assertEqual(comment.date, "2026-10-20T10:00:00Z")
        self.assertEqual(comment.text, "Текст")
        self.assertEqual(comment.video_id, "mVOq9iKH818")
        self.assertEqual(comment.url, "https://www.youtube.com/watch?v=mVOq9iKH818&lc=comment-id")
        args, kwargs = self.session.get.call_args
        self.assertNotIn("test-secret", args[0])
        self.assertEqual(kwargs["params"], {"part": "snippet", "videoId": "mVOq9iKH818", "maxResults": 100, "order": "time", "textFormat": "plainText"})
        self.assertEqual(kwargs["headers"], {"X-Goog-Api-Key": "test-secret"})
        self.assertEqual(kwargs["timeout"], (5, 30))

    def test_retries_are_bounded(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.sleep.reset_mock()
                self.session.get.return_value = self.response(status)
                with self.assertRaises(YouTubeAPIError):
                    self.fetch()
                self.assertEqual(self.session.get.call_count, 4)
                self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2, 4])

    def test_retry_after_and_recovery(self):
        self.session.get.side_effect = [self.response(429, headers={"Retry-After": "7"}), self.response()]
        self.assertEqual(self.fetch(), [])
        self.sleep.assert_called_once_with(7)

    def test_quota_and_permanent_errors_never_retry(self):
        for status, payload in ((400, {}), (403, {}), (429, {"error": {"errors": [{"reason": "quotaExceeded"}]}})):
            self.session.get.reset_mock()
            self.session.get.return_value = self.response(status, payload)
            with self.assertRaises(YouTubeAPIError):
                self.fetch()
            self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_network_errors_hide_credentials(self):
        for error in (requests.ConnectionError("test-secret"), requests.Timeout("test-secret")):
            self.session.get.side_effect = error
            with self.assertRaises(YouTubeAPIError) as caught:
                self.fetch()
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_json(self):
        response = self.response()
        response.json.side_effect = ValueError("test-secret")
        self.session.get.return_value = response
        with self.assertRaises(YouTubeAPIError):
            self.fetch()

    def test_invalid_response_structure(self):
        for payload in ({"items": ""}, {"items": {}}, {"items": [None]},
                        {"items": [{"snippet": {"topLevelComment": {
                            "id": None, "snippet": {"authorDisplayName": "Author",
                            "publishedAt": "date", "textDisplay": "text"}}}}]}):
            with self.subTest(payload=payload):
                self.session.get.return_value = self.response(payload=payload)
                with self.assertRaises(YouTubeAPIError):
                    self.fetch()

    def test_retry_after_http_date(self):
        self.assertEqual(_retry_delay("Wed, 21 Oct 2015 07:28:00 GMT", 2), 4)
        self.assertEqual(_retry_delay("invalid", 1), 2)
        with self.assertRaises(YouTubeAPIError):
            _retry_delay("3600", 0)

    def test_page_token_and_100_comment_page(self):
        item = {"snippet": {"topLevelComment": {"id": "opaque", "snippet": {
            "authorDisplayName": "Author", "publishedAt": "2026-10-20T10:00:00Z",
            "textDisplay": "Text"}}}}
        self.session.get.return_value = self.response(payload={"items": [item] * 100, "nextPageToken": "next"})
        page = fetch_comment_page(self.config, "video", "previous")
        self.assertEqual(len(page.comments), 100)
        self.assertEqual(page.next_page_token, "next")
        self.assertEqual(self.session.get.call_args.kwargs["params"]["pageToken"], "previous")

    def test_invalid_token_is_specific_and_not_retried(self):
        self.session.get.return_value = self.response(400, {
            "error": {"errors": [{"reason": "invalidPageToken"}]}})
        with self.assertRaises(InvalidPageToken):
            fetch_comment_page(self.config, "video", "expired")
        self.session.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_request_delay_applies_to_pages_and_retries(self):
        from dataclasses import replace
        config = replace(self.config, request_delay_seconds=0.5)
        self.session.get.side_effect = [self.response(503), self.response(), self.response()]
        fetch_comment_page(config, "video")
        fetch_comment_page(config, "video", "p2")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.5, 1, 0.5, 0.5])

    def test_invalid_date_or_next_token_rejects_whole_page(self):
        item = {"snippet": {"topLevelComment": {"id": "opaque", "snippet": {
            "authorDisplayName": "Author", "publishedAt": "invalid",
            "textDisplay": "Text"}}}}
        for payload in ({"items": [item]}, {"items": [], "nextPageToken": 4},
                        {"items": [], "nextPageToken": ""}):
            self.session.get.return_value = self.response(payload=payload)
            with self.assertRaises(YouTubeAPIError):
                self.fetch()

    def test_api_request_counts_include_retries(self):
        self.session.get.side_effect = [self.response(503), self.response(429), self.response()]
        page = fetch_comment_page(self.config, "video")
        self.assertEqual(page.api_requests, 3)
        self.session.get.side_effect = None
        self.session.get.return_value = self.response(503)
        with self.assertRaises(YouTubeAPIError) as caught:
            fetch_comment_page(self.config, "video")
        self.assertEqual(caught.exception.api_requests, 4)

    def test_missing_key_makes_zero_requests(self):
        from dataclasses import replace
        with self.assertRaises(YouTubeAPIError) as caught:
            fetch_comment_page(replace(self.config, youtube_api_key=""), "video")
        self.assertEqual(caught.exception.api_requests, 0)
        self.session.get.assert_not_called()

    def test_credentials_and_video_ids_only_from_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("YOUTUBE_API_KEY=file-key\nVIDEO_IDS=mVOq9iKH818\n")
            with patch.dict("os.environ", {"YOUTUBE_API_KEY": "environment-key", "VIDEO_IDS": "other"}):
                config = load_config(path)
            self.assertEqual(config.youtube_api_key, "file-key")
            self.assertEqual(config.video_ids, ("mVOq9iKH818",))
            self.assertNotIn("file-key", repr(config))


if __name__ == "__main__":
    unittest.main()
