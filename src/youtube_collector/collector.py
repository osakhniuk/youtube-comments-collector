"""Resumable incremental scanning and explicitly selected historical backfill."""

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from .api import InvalidComment, InvalidPageToken, QuotaExceeded, YouTubeAPIError, fetch_comment_page
from .logging_setup import log_event
from .locking import collector_lock
from .config import Config
from .storage import (
    DEFAULT_DB_PATH, count_comments, load_state, parse_date,
    CollectorState, init_db, save_comments, save_page, utc_now,
)

LOOKBACK_PAGES = 2
LOOKBACK_WINDOW = timedelta(minutes=5)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionResult:
    video_id: str
    received: int
    saved: int
    duplicates: int
    total: int
    pages: int
    complete: bool
    has_more: bool
    error: str | None = None
    token_restarted: bool = False
    api_requests: int = 0
    stop_reason: str = "page_limit"
    mode: str = "incremental"


def collect_video(
    config: Config, video_id: str, db_path: str | Path = DEFAULT_DB_PATH,
    *, mode: str = "incremental",
) -> CollectionResult:
    with collector_lock(db_path):
        return _collect_video(config, video_id, db_path, mode=mode)


def _collect_video(
    config: Config, video_id: str, db_path: str | Path = DEFAULT_DB_PATH,
    *, mode: str = "incremental",
) -> CollectionResult:
    """Scan the head until a temporal old-page overlap is established.

    Stop only at the API end or after two whole pages strictly older than
    the stable watermark minus five minutes. IDs never determine completion.
    Equal timestamps and pinned/newer items reset the old-page streak.
    Backfill uses an independent cursor and never advances the watermark.
    """
    if mode not in ("incremental", "backfill"):
        raise ValueError("Невідомий режим збору.")
    init_db(db_path)
    total = count_comments(db_path)
    if total >= config.max_total_comments:
        log_event(LOGGER, "video_finished", video_id=video_id, mode=mode, saved=0,
                  duplicates=0, api_requests=0, pages=0, reason="total_limit")
        return CollectionResult(video_id, 0, 0, 0, total, 0, False, True,
                                stop_reason="total_limit", mode=mode)
    state = load_state(video_id, db_path)
    backfill = mode == "backfill"
    received = saved = duplicates = pages = api_requests = attempts = 0
    restarted = complete = False
    error = None
    reason = "page_limit"
    total = count_comments(db_path)
    if backfill and state.backfill_complete:
        return CollectionResult(video_id, 0, 0, 0, total, 0, True, False,
                                stop_reason="backfill_already_complete", mode=mode)
    while attempts < config.max_pages_per_run:
        token = state.backfill_next_page_token if backfill else state.scan_next_page_token
        attempts += 1
        try:
            page = fetch_comment_page(config, video_id, token)
            api_requests += page.api_requests
            pages += 1  # Parsed pages, even when the subsequent SQLite write fails.
        except InvalidPageToken as exc:
            api_requests += exc.api_requests
            if not token or restarted:
                error, reason = str(exc), "api_error"
                break
            reset = (replace(state, backfill_next_page_token=None, backfill_pending_page=1, updated_at=utc_now()) if backfill
                     else replace(state, scan_next_page_token=None, scan_old_pages=0,
                                  scan_known_ids="[]", scan_pending_page=1, updated_at=utc_now()))
            try:
                save_comments([], db_path, state=reset)
            except (sqlite3.Error, OSError):
                error, reason = "Не вдалося скинути курсор у SQLite.", "storage_error"
                break
            state = reset
            log_event(LOGGER, "cursor_reset", video_id=video_id, mode=mode, reason="invalidPageToken")
            restarted = True
            continue
        except YouTubeAPIError as exc:
            api_requests += exc.api_requests
            error = str(exc)
            reason = "quota_exceeded" if isinstance(exc, QuotaExceeded) else "api_error"
            break

        received += len(page.comments)
        try:
            def build_state(processed, limited, partial):
                nonlocal complete, reason
                next_state, complete, reason = _page_state(
                    state, processed, page.next_page_token, token, mode, limited, partial,
                )
                return next_state
            result = save_page(page.comments, db_path, config.max_total_comments, build_state)
            next_state = result.state
        except InvalidComment:
            complete = False
            error, reason = "Некоректна сторінка коментарів; курсор не просунуто.", "invalid_comment"
            break
        except (sqlite3.Error, OSError):
            complete = False
            error, reason = "Не вдалося зберегти сторінку у SQLite; курсор не просунуто.", "storage_error"
            break
        log_event(LOGGER, "page_committed", video_id=video_id, mode=mode,
                  received=len(page.comments), processed=result.processed,
                  saved=result.saved, duplicates=result.duplicates,
                  total=result.total, scan_complete=complete, limit_reached=result.limit_reached)
        state = next_state  # Advance only after the transaction commits.
        saved += result.saved
        duplicates += result.duplicates
        total = result.total
        if complete or result.limit_reached:
            break
    log_event(LOGGER, "video_finished", video_id=video_id, mode=mode, saved=saved,
              duplicates=duplicates, api_requests=api_requests, pages=pages, reason=reason)
    return CollectionResult(video_id, received, saved, duplicates, total, pages,
                            complete, not complete, error, restarted, api_requests, reason, mode)


def _page_state(
    state: CollectorState, comments, next_token, current_token, mode,
    limited: bool, partial: bool,
) -> tuple[CollectorState, bool, str]:
    """Build progress only from rows processed within the page transaction."""
    if mode == "backfill":
        complete = not limited and next_token is None
        return replace(
            state, backfill_next_page_token=current_token if partial else next_token,
            backfill_pending_page=int(partial or (limited and next_token is None)),
            backfill_complete=int(complete), updated_at=utc_now(),
        ), complete, "total_limit" if limited else ("end_of_list" if complete else "page_limit")
    newest_at = state.scan_newest_at or state.last_seen_at
    newest_id = state.scan_newest_comment_id or state.last_seen_comment_id
    for comment in comments:
        if newest_at is None or parse_date(comment.date) > parse_date(newest_at):
            newest_at, newest_id = comment.date, comment.comment_id
    boundary = state.scan_boundary_at or state.last_seen_at or newest_at
    if limited:
        # No final watermark advancement at a cap, even on the final API page.
        return replace(
            state, scan_next_page_token=current_token if partial else next_token,
            scan_pending_page=int(partial or next_token is None), scan_boundary_at=boundary,
            scan_newest_at=newest_at, scan_newest_comment_id=newest_id, updated_at=utc_now(),
        ), False, "total_limit"
    cutoff = parse_date(boundary) - LOOKBACK_WINDOW if boundary else None
    old_page = bool(comments) and state.last_seen_at is not None and cutoff is not None and all(
        parse_date(comment.date) < cutoff for comment in comments
    )
    old_pages = state.scan_old_pages + 1 if old_page else 0
    if next_token is None:
        reason = "end_of_list"
    elif old_pages >= LOOKBACK_PAGES and not state.scan_pending_page:
        reason = "old_page_overlap"
    else:
        reason = "page_limit"
    complete = reason != "page_limit"
    if complete:
        return replace(
            state, last_seen_at=newest_at, last_seen_comment_id=newest_id,
            scan_next_page_token=None, scan_boundary_at=None, scan_newest_at=None,
            scan_newest_comment_id=None, scan_old_pages=0, scan_known_ids="[]",
            scan_pending_page=0, updated_at=utc_now(),
        ), True, reason
    return replace(
        state, scan_next_page_token=next_token, scan_boundary_at=boundary,
        scan_newest_at=newest_at, scan_newest_comment_id=newest_id,
        scan_old_pages=old_pages, scan_known_ids="[]",
        scan_pending_page=0, updated_at=utc_now(),
    ), False, reason
