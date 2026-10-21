"""YouTube pages with bounded retries, jitter and credential-safe event logs."""

import logging
import math
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse

import requests

from .config import Config
from .logging_setup import log_event

ENDPOINT = "https://www.googleapis.com/youtube/v3/commentThreads"
MAX_RETRY_WAIT = 120.0
LOGGER = logging.getLogger(__name__)


class YouTubeAPIError(Exception):
    """Only safe messages and categories, without server text or credentials."""
    api_requests: int = 1
    reason: str = "api_error"

    def __init__(self, message: str, *, reason: str = "api_error") -> None:
        super().__init__(message)
        self.reason = reason


class InvalidPageToken(YouTubeAPIError):
    """The stored pagination token is no longer usable."""


class QuotaExceeded(YouTubeAPIError):
    """Shared project quota exhausted: stop this run, not just one video."""


@dataclass(frozen=True)
class Comment:
    comment_id: str
    author: str
    url: str
    date: str
    text: str
    video_id: str


class InvalidComment(ValueError):
    """Invalid page data: reject the whole page without advancing its cursor."""


def validate_comment(comment: Comment) -> None:
    if not isinstance(comment, Comment):
        raise InvalidComment("Некоректний тип коментаря.")
    for field in ("comment_id", "video_id", "url", "date"):
        value = getattr(comment, field, None)
        if not isinstance(value, str) or not value.strip():
            raise InvalidComment(f"Некоректне обов'язкове поле: {field}.")
    # Blank display names/text are retained verbatim; never invent replacements.
    if not isinstance(comment.author, str) or not isinstance(comment.text, str):
        raise InvalidComment("author та text повинні бути рядками.")
    try:
        published = datetime.fromisoformat(comment.date.replace("Z", "+00:00"))
        url = urlparse(comment.url)
        if published.tzinfo is None or url.scheme != "https" or not url.netloc:
            raise ValueError
    except ValueError:
        raise InvalidComment("Некоректна дата або URL коментаря.") from None


@dataclass(frozen=True)
class CommentPage:
    comments: list[Comment]
    next_page_token: str | None = None
    api_requests: int = 1


def _retry_delay(value: str | None, attempt: int, *, max_wait: float = MAX_RETRY_WAIT) -> float:
    base = min(2.0 ** attempt, max_wait)
    delay = base
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0.0
        if seconds > max_wait:
            raise YouTubeAPIError("Сервер просить тривале очікування. Повторіть збір пізніше.",
                                  reason="retry_wait_exceeded")
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    return min(max_wait, delay + random.uniform(0.0, min(1.0, base * 0.25)))


def _wait_before_retry(config: Config, video_id: str, attempt: int, reason: str,
                       status: int | None, retry_after: str | None = None) -> None:
    if attempt >= config.http_max_retries:
        raise YouTubeAPIError("Ліміт повторів YouTube API вичерпано.", reason="retries_exhausted")
    delay = _retry_delay(retry_after, attempt, max_wait=config.http_max_retry_wait_seconds)
    log_event(LOGGER, "retry_wait", video_id=video_id, attempt=attempt + 1,
              max_attempts=config.http_max_retries + 1, status=status, reason=reason,
              delay_seconds=delay)
    time.sleep(delay)


def _fetch_comment_page(
    config: Config, video_id: str, page_token: str | None, counter: list[int],
) -> CommentPage:
    if not config.youtube_api_key:
        raise YouTubeAPIError("У .env не задано YOUTUBE_API_KEY.", reason="missing_key")
    params = {"part": "snippet", "videoId": video_id, "maxResults": 100,
              "order": "time", "textFormat": "plainText"}
    if page_token is not None:
        params["pageToken"] = page_token
    with requests.Session() as session:
        for attempt in range(config.http_max_retries + 1):
            if config.request_delay_seconds:
                log_event(LOGGER, "request_wait", video_id=video_id, attempt=attempt + 1,
                          delay_seconds=config.request_delay_seconds)
                time.sleep(config.request_delay_seconds)
            log_event(LOGGER, "request_started", video_id=video_id, attempt=attempt + 1,
                      max_attempts=config.http_max_retries + 1)
            started = time.monotonic()
            network_reason = None
            try:
                counter[0] += 1
                response = session.get(
                    ENDPOINT, params=params, headers={"X-Goog-Api-Key": config.youtube_api_key},
                    timeout=(config.http_connect_timeout_seconds, config.http_read_timeout_seconds),
                    allow_redirects=False,
                )
            except requests.Timeout:
                network_reason = "timeout"
            except requests.ConnectionError:
                network_reason = "connection_error"
            except requests.RequestException:
                raise YouTubeAPIError("Не вдалося виконати HTTP-запит до YouTube API.",
                                      reason="request_error") from None
            if network_reason:
                log_event(LOGGER, "request_failed", level=logging.WARNING, video_id=video_id,
                          attempt=attempt + 1, status=None, reason=network_reason,
                          elapsed_seconds=round(time.monotonic() - started, 3))
                _wait_before_retry(config, video_id, attempt, network_reason, None)
                continue
            try:
                status = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                reasons = set()
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    errors = payload["error"].get("errors", [])
                    for error in errors if isinstance(errors, list) else []:
                        if isinstance(error, dict) and isinstance(error.get("reason"), str):
                            reasons.add(error["reason"])
                allowed = {"quotaExceeded", "dailyLimitExceeded", "invalidPageToken",
                           "commentsDisabled", "forbidden", "rateLimitExceeded", "userRateLimitExceeded"}
                reason = next((item for item in sorted(reasons) if item in allowed), None)
                if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                    reason = "quotaExceeded"
                reason = reason or ({200: "ok", 400: "invalid_parameters", 401: "authorization_error",
                                     403: "forbidden", 429: "rate_limit"}.get(status, "http_error"))
                headers = {str(name).replace(config.youtube_api_key, "[REDACTED]"):
                           str(value).replace(config.youtube_api_key, "[REDACTED]")
                           for name, value in response.headers.items()
                           if name.lower() == "retry-after" or name.lower().startswith("x-ratelimit-")}
                log_event(LOGGER, "request_result", video_id=video_id, status=status,
                          attempt=attempt + 1, reason=reason, rate_limit_headers=headers,
                          elapsed_seconds=round(time.monotonic() - started, 3))
                retry_after = next((value for name, value in headers.items()
                                    if name.lower() == "retry-after"), None)
                if reason == "quotaExceeded":
                    raise QuotaExceeded("Квоту YouTube API вичерпано (quotaExceeded). Збір зупинено без повторів.",
                                        reason="quotaExceeded")
                if status == 400 and page_token and "invalidPageToken" in reasons:
                    raise InvalidPageToken("YouTube API: збережений pageToken недійсний.",
                                           reason="invalidPageToken")
                if status == 200:
                    return _parse_page(payload, video_id)
                if not (status in (429, 500, 502, 503, 504) or (status == 403
                        and reasons & {"rateLimitExceeded", "userRateLimitExceeded"}
                        and not reasons & {"forbidden", "commentsDisabled", "authError"})):
                    explanation = {
                        400: "Некоректні параметри запиту.",
                        401: "Помилка авторизації. Перевірте API-ключ.",
                        403: "Доступ заборонено. Перевірте дозволи API та доступність коментарів.",
                        404: "Відео не знайдено.",
                    }.get(status, "Неочікувана відповідь сервера.")
                    if "commentsDisabled" in reasons:
                        explanation = "Коментарі до цього відео вимкнено."
                    raise YouTubeAPIError(f"YouTube API: HTTP {status}. {explanation}", reason=reason)
            finally:
                response.close()
            _wait_before_retry(config, video_id, attempt, reason, status, retry_after)
    raise YouTubeAPIError("Не вдалося отримати коментарі.")


def _parse_page(payload, video_id: str) -> CommentPage:
    try:
        comments = []
        items = payload["items"]
        if not isinstance(items, list):
            raise TypeError
        next_token = payload.get("nextPageToken")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise TypeError
        for item in items:
            top = item["snippet"]["topLevelComment"]
            snippet = top["snippet"]
            comment_id = top["id"]
            if not all(isinstance(value, str) for value in (
                comment_id, snippet["authorDisplayName"],
                snippet["publishedAt"], snippet["textDisplay"],
            )):
                raise TypeError
            published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError
            comments.append(Comment(
                comment_id=comment_id, author=snippet["authorDisplayName"],
                url="https://www.youtube.com/watch?" + urlencode({"v": video_id, "lc": comment_id}),
                date=snippet["publishedAt"], text=snippet["textDisplay"], video_id=video_id,
            ))
        for comment in comments:
            validate_comment(comment)
        return CommentPage(comments, next_token)
    except (KeyError, TypeError, IndexError, ValueError):
        raise YouTubeAPIError("YouTube API повернув некоректну структуру коментарів.", reason="invalid_response") from None


def fetch_comment_page(
    config: Config, video_id: str, page_token: str | None = None,
) -> CommentPage:
    """Count actual HTTP attempts, including retries and failed requests."""
    counter = [0]
    try:
        page = _fetch_comment_page(config, video_id, page_token, counter)
    except YouTubeAPIError as exc:
        exc.api_requests = counter[0]
        log_event(LOGGER, "page_failed", level=logging.WARNING, video_id=video_id,
                  api_requests=counter[0], reason=exc.reason)
        raise exc from None
    return replace(page, api_requests=counter[0])


def fetch_comments(config: Config, video_id: str) -> list[Comment]:
    """Compatibility helper returning the first page without pagination."""
    return fetch_comment_page(config, video_id).comments
