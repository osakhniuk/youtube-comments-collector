"""SQLite persistence; each page and its cursor share one transaction."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .api import Comment, validate_comment
from .config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "comments.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Дата коментаря повинна містити часовий пояс.")
    return result


@dataclass(frozen=True)
class SaveResult:
    received: int
    saved: int
    duplicates: int
    total: int


@dataclass(frozen=True)
class CollectorState:
    video_id: str
    last_seen_at: str | None = None
    last_seen_comment_id: str | None = None
    # Independent historical cursor; incremental collection never advances it.
    backfill_next_page_token: str | None = None
    updated_at: str | None = None
    scan_next_page_token: str | None = None
    scan_boundary_at: str | None = None
    scan_newest_at: str | None = None
    scan_newest_comment_id: str | None = None
    scan_old_pages: int = 0
    scan_known_ids: str = "[]"
    backfill_complete: int = 0
    scan_pending_page: int = 0
    backfill_pending_page: int = 0


_STATE_COLUMNS = ", ".join(field.name for field in fields(CollectorState))


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create tables and add incremental state columns without deleting data."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                source TEXT NOT NULL DEFAULT 'youtube',
                comment_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                author TEXT NOT NULL,
                url TEXT NOT NULL,
                date TEXT NOT NULL,
                text TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(source, comment_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS collector_state (
                video_id TEXT PRIMARY KEY,
                last_seen_at TEXT,
                last_seen_comment_id TEXT,
                backfill_next_page_token TEXT,
                updated_at TEXT
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(collector_state)")}
        for name, declaration in (
            ("scan_next_page_token", "TEXT"), ("scan_boundary_at", "TEXT"),
            ("scan_newest_at", "TEXT"), ("scan_newest_comment_id", "TEXT"),
            ("scan_old_pages", "INTEGER NOT NULL DEFAULT 0"),
            ("scan_known_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("backfill_complete", "INTEGER NOT NULL DEFAULT 0"),
            ("scan_pending_page", "INTEGER NOT NULL DEFAULT 0"),
            ("backfill_pending_page", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE collector_state ADD COLUMN {name} {declaration}")
        connection.execute("""CREATE INDEX IF NOT EXISTS comments_source_video
                              ON comments(source, video_id)""")


def _write_state(connection: sqlite3.Connection, state: CollectorState) -> None:
    names = [field.name for field in fields(state)]
    updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != "video_id")
    connection.execute(
        f"INSERT INTO collector_state ({_STATE_COLUMNS}) VALUES ({', '.join('?' for _ in names)}) "
        f"ON CONFLICT(video_id) DO UPDATE SET {updates}",
        tuple(getattr(state, name) for name in names),
    )


def load_state(video_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> CollectorState:
    """Seed an absent/empty checkpoint from existing YouTube comments once."""
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        row = connection.execute(
            f"SELECT {_STATE_COLUMNS} FROM collector_state WHERE video_id = ?", (video_id,),
        ).fetchone()
        state = CollectorState(*row) if row else CollectorState(video_id)
        if (state.last_seen_at or state.scan_next_page_token or state.scan_newest_at
                or state.backfill_next_page_token or state.backfill_complete
                or state.scan_pending_page or state.backfill_pending_page):
            return state
        newest = None
        for comment_id, date in connection.execute(
            "SELECT comment_id, date FROM comments WHERE source = 'youtube' AND video_id = ?",
            (video_id,),
        ):
            # IDs are opaque: only timestamps determine the newest comment.
            if newest is None or parse_date(date) > parse_date(newest[1]):
                newest = (comment_id, date)
        if newest:
            state = replace(state, last_seen_at=newest[1], last_seen_comment_id=newest[0],
                            updated_at=utc_now())
        _write_state(connection, state)
        return state


def count_comments(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0]


def save_comments(
    comments: Iterable[Comment], db_path: str | Path = DEFAULT_DB_PATH,
    *, state: CollectorState | None = None,
) -> SaveResult:
    """Commit comments and corresponding state atomically, or roll both back."""
    init_db(db_path)
    received = saved = 0
    collected_at = utc_now()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for comment in comments:
            validate_comment(comment)
            cursor = connection.execute("""
                INSERT INTO comments
                    (source, comment_id, video_id, author, url, date, text, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, comment_id) DO NOTHING
            """, (
                "youtube", comment.comment_id, comment.video_id, comment.author,
                comment.url, comment.date, comment.text, collected_at,
            ))
            received += 1
            saved += cursor.rowcount
        if state is not None:
            _write_state(connection, state)
        total = connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    return SaveResult(received, saved, received - saved, total)


def known_comment_ids(
    video_id: str, comment_ids: Iterable[str], db_path: str | Path = DEFAULT_DB_PATH,
) -> set[str]:
    """Check only this page's IDs using the unique index; never load the archive."""
    identifiers = list(set(comment_ids))
    known = set()
    with closing(sqlite3.connect(db_path)) as connection:
        for start in range(0, len(identifiers), 100):
            batch = identifiers[start:start + 100]
            placeholders = ", ".join("?" for _ in batch)
            known.update(row[0] for row in connection.execute(
                f"SELECT comment_id FROM comments WHERE source = 'youtube' "
                f"AND video_id = ? AND comment_id IN ({placeholders})",
                (video_id, *batch),
            ))
    return known


@dataclass(frozen=True)
class PageSaveResult:
    saved: int
    duplicates: int
    total: int
    processed: int
    limit_reached: bool
    state: CollectorState


def save_page(
    comments: list[Comment], db_path: str | Path, max_total_comments: int,
    state_builder: Callable[[list[Comment], bool, bool], CollectorState],
) -> PageSaveResult:
    """Enforce the global cap and commit only the processed prefix and its cursor.

    BEGIN IMMEDIATE serializes capacity checks with other SQLite writers.
    Unprocessed rows stay on the CURRENT page token for deduplicated replay.
    """
    if max_total_comments <= 0:
        raise ValueError("MAX_TOTAL_COMMENTS має бути > 0.")
    for comment in comments:
        validate_comment(comment)
    init_db(db_path)
    saved = duplicates = processed = 0
    collected_at = utc_now()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        total = connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        for comment in comments:
            if total >= max_total_comments:
                break
            cursor = connection.execute("""
                INSERT INTO comments
                    (source, comment_id, video_id, author, url, date, text, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, comment_id) DO NOTHING
            """, ("youtube", comment.comment_id, comment.video_id, comment.author,
                  comment.url, comment.date, comment.text, collected_at))
            saved += cursor.rowcount
            duplicates += 1 - cursor.rowcount
            total += cursor.rowcount
            processed += 1
        limit_reached = total >= max_total_comments
        state = state_builder(comments[:processed], limit_reached, processed < len(comments))
        _write_state(connection, state)
    return PageSaveResult(saved, duplicates, total, processed, limit_reached, state)
