"""Read-only dashboard data, filtering and conservative log attribution."""

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "comments.db"
LOG_PATH = ROOT / "logs" / "collector.log"
COMMENT_FIELDS = ("author", "date", "text", "url", "video_id", "comment_id", "collected_at")


@dataclass
class Snapshot:
    comments: list[dict] = field(default_factory=list)
    states: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def parse_date(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def read_limit(env_path: Path = ROOT / ".env") -> tuple[int | None, str | None]:
    """Match collector precedence, returning only the numeric public limit."""
    try:
        raw = os.environ.get("MAX_TOTAL_COMMENTS")
        if raw is None:
            raw = dotenv_values(env_path, interpolate=False).get("MAX_TOTAL_COMMENTS", "200")
        limit = int(raw)
        if limit <= 0:
            raise ValueError
        return limit, None
    except (ValueError, TypeError, OSError):
        return None, "Не вдалося прочитати коректний MAX_TOTAL_COMMENTS."


def read_snapshot(db_path: str | Path = DB_PATH) -> Snapshot:
    result = Snapshot()
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(comments)")}
            if not columns:
                result.error = "У базі відсутня таблиця comments."
                return result
            missing = set(COMMENT_FIELDS) - columns
            if missing:
                result.warnings.append("Деякі поля comments відсутні; показано доступні дані.")
            selection = ", ".join(name if name in columns else f"NULL AS {name}" for name in COMMENT_FIELDS)
            result.comments = [dict(row) for row in connection.execute(f"SELECT {selection} FROM comments")]
            state_columns = {row[1] for row in connection.execute("PRAGMA table_info(collector_state)")}
            if not state_columns:
                result.warnings.append("Таблиця collector_state відсутня — стан збору невідомий.")
            else:
                # Actual page tokens are never read or returned to the browser.
                direct = ("video_id", "last_seen_at", "backfill_complete")
                expressions = [name if name in state_columns else f"NULL AS {name}" for name in direct]
                for name in ("scan_next_page_token", "backfill_next_page_token",
                             "scan_pending_page", "backfill_pending_page", "scan_newest_at"):
                    expressions.append(f"CASE WHEN {name} IS NOT NULL AND {name} != '' AND {name} != 0 "
                                       f"THEN 1 ELSE 0 END AS has_{name}" if name in state_columns
                                       else f"0 AS has_{name}")
                result.states = [dict(row) for row in connection.execute(
                    f"SELECT {', '.join(expressions)} FROM collector_state")]
        return result
    except (sqlite3.Error, OSError, ValueError):
        return Snapshot(error="SQLite недоступна або має некоректний формат. Перевірте data/comments.db і права читання.")


def statistics(snapshot: Snapshot) -> dict:
    rows = snapshot.comments
    dates = [parsed for row in rows if (parsed := parse_date(row.get("date"))) is not None]
    return {"total": len(rows), "unique": len({row.get("comment_id") for row in rows if row.get("comment_id")}),
            "videos": len({row.get("video_id") for row in rows if row.get("video_id")}),
            "oldest": min(dates) if dates else None, "newest": max(dates) if dates else None,
            "missing_dates": len(rows) - len(dates)}


def filter_comments(rows, *, text="", author="", videos=None, start: date | None = None,
                    end: date | None = None, ascending=False, include_undated=False) -> list[dict]:
    result = []
    for row in rows:
        timestamp = parse_date(row.get("date"))
        if text.casefold() not in str(row.get("text") or "").casefold():
            continue
        if author.casefold() not in str(row.get("author") or "").casefold():
            continue
        if videos is not None and row.get("video_id") not in videos:
            continue
        if timestamp is None:
            if (start or end) and not include_undated:
                continue
        elif (start and timestamp.date() < start) or (end and timestamp.date() > end):
            continue
        result.append(dict(row))
    dated = [row for row in result if parse_date(row.get("date")) is not None]
    undated = [row for row in result if parse_date(row.get("date")) is None]
    dated.sort(key=lambda row: (parse_date(row.get("date")), str(row.get("comment_id") or "")),
               reverse=not ascending)
    return dated + undated


def chart_counts(rows) -> dict:
    dates, hours, videos = Counter(), Counter(), Counter()
    for row in rows:
        if parsed := parse_date(row.get("date")):
            dates[parsed.date().isoformat()] += 1
            hours[parsed.hour] += 1
        if row.get("video_id"):
            videos[row["video_id"]] += 1
    return {"dates": sorted(dates.items()), "hours": sorted(hours.items()), "videos": sorted(videos.items())}


def collector_status(snapshot: Snapshot, limit: int | None) -> str:
    if limit is not None and len(snapshot.comments) >= limit:
        return "Призупинено: ліміт досягнуто"
    if not snapshot.states:
        return "Стан невідомий"
    if any(row.get("has_scan_next_page_token") or row.get("has_scan_pending_page")
           or row.get("has_scan_newest_at") for row in snapshot.states):
        return "Збережено прогрес для продовження"
    return "Останній прохід завершено" if all(row.get("last_seen_at") for row in snapshot.states) else "Ще не розпочато"


def safe_url(value) -> str | None:
    try:
        parsed = urlparse(str(value))
        if parsed.scheme == "https" and parsed.netloc in ("www.youtube.com", "youtube.com", "youtu.be"):
            return str(value)
    except ValueError:
        pass
    return None


def read_api_statistics(snapshot: Snapshot, log_path: str | Path = LOG_PATH) -> dict:
    """Count only explicitly real events. Legacy/test events never prove HTTP usage."""
    empty = {"requests": None, "http_errors": None, "retries": None, "rate_limits": None,
             "last_success": None, "unknown_events": 0, "note": "Дані відсутні"}
    try:
        stream = Path(log_path).open(encoding="utf-8", errors="replace")
    except OSError:
        return empty
    trusted = []
    unknown = 0
    videos = {row.get("video_id") for row in snapshot.comments if isinstance(row.get("video_id"), str)}
    with stream:
        for line in stream:
            try:
                prefix, payload = line.split(" {", 1)
                parts = prefix.split()
                if len(parts) != 3 or not parts[2].startswith("youtube_collector."):
                    continue
                timestamp = parse_date(parts[0])
                event = json.loads("{" + payload)
                if timestamp is None or not isinstance(event, dict) or not isinstance(event.get("event"), str):
                    continue
                if event["event"] not in {"request_started", "request_result", "request_failed", "retry_wait", "page_committed", "database_reset"}:
                    continue
                if event["event"] != "database_reset":
                    if not isinstance(event.get("video_id"), str) or event["video_id"] not in videos:
                        continue
                # Keep malformed starts as observations, but mark retry totals incomplete.
                if any(name in event and not (name == "attempt" and event["event"] == "request_started")
                       and (type(event[name]) is not int or event[name] < minimum)
                       for name, minimum in (("attempt", 1), ("saved", 0))):
                    continue
                status = event.get("status")
                if status is not None and (type(status) is not int or not 100 <= status <= 599):
                    continue
                if event["event"] == "request_result" and status is None:
                    continue
                if event.get("reason") is not None and not isinstance(event["reason"], str):
                    continue
                markers = {str(event.get(name, "")).casefold() for name in ("execution", "environment")}
                if event.get("origin") == "test" or markers & {"test", "mock", "mock_test", "testing"} or any(
                    event.get(name) for name in ("mock", "is_mock", "test", "is_test")
                ):
                    continue
                if event.get("origin") != "real" or not isinstance(event.get("run_id"), str) or not event["run_id"].strip():
                    unknown += 1
                    continue
                if event["event"] == "database_reset":
                    trusted.clear()
                else:
                    trusted.append((timestamp, event))
            except (ValueError, TypeError, KeyError):
                continue
    attempts = [item for _, item in trusted if item["event"] == "request_started"]
    results = [item for _, item in trusted if item["event"] == "request_result"]
    successes = [timestamp for timestamp, event in trusted if event["event"] == "page_committed"
                 and type(event.get("saved")) is int and event["saved"] > 0]
    attempts_complete = bool(attempts) and all(
        type(item.get("attempt")) is int and item["attempt"] >= 1 for item in attempts
    )
    retry_note = " Retry-метрика неповна: відсутній або некоректний attempt." if attempts and not attempts_complete else ""
    return {**empty, "unknown_events": unknown,
            "requests": len(attempts) if attempts else len(results) if results else None,
            "http_errors": sum(item["status"] >= 400 for item in results) if results else None,
            "retries": sum(item["attempt"] > 1 for item in attempts) if attempts_complete else None,
            "rate_limits": sum(item["status"] == 429 or item.get("reason") in ("rate_limit", "rateLimitExceeded", "userRateLimitExceeded") for item in results) if results else None,
            "last_success": max(successes) if successes else None,
            "note": f"Лише origin=real з run_id у поточному журналі. Події unknown: {unknown}; тестові події виключено. Ротації не враховано." + retry_note}
