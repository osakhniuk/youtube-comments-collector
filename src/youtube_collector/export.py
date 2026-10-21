"""Export SQLite comments as UTF-8 JSONL or CSV without modifying the database."""

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "comments.db"
FIELDS = ("author", "url", "date", "text", "source", "comment_id", "video_id", "collected_at")


class UnsafeExportPath(ValueError):
    """The output would overwrite protected application data."""


def _utc_sort_key(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Export dates must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_destination(destination: Path, database_paths: tuple[Path, ...]) -> None:
    """Reject SQLite files and their symlink/hard-link aliases before writing."""
    protected = set()
    for database in database_paths:
        for base in (database.absolute(), database.resolve()):
            protected.update(Path(str(base) + suffix).resolve()
                             for suffix in ("", "-wal", "-shm", "-journal"))
    resolved = destination.resolve()
    if (resolved in protected or resolved.name.startswith(".env")
            or resolved.suffix.lower() in (".db", ".sqlite", ".sqlite3")
            or (destination.exists() and any(path.exists() and destination.samefile(path)
                                             for path in protected))):
        raise UnsafeExportPath("Файл експорту не може перезаписувати SQLite-базу, її WAL/SHM/journal або .env.")


def export_comments(
    output: str | Path, format: str, db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    """Export all rows in date/comment_id order; reject duplicate IDs explicitly.

    Write to a temporary file, then replace the destination only on success.
    The SQLite connection uses mode=ro, without schema initialization or migration.
    Only the eight public comment fields are selected; configuration is never read.
    """
    if format not in ("jsonl", "csv"):
        raise ValueError("Формат має бути jsonl або csv.")
    database = Path(db_path).resolve()
    destination = Path(output).absolute()
    database_paths = (Path(db_path), DEFAULT_DB_PATH)
    _validate_destination(destination, database_paths)
    temporary = None
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # Validation and export share one read snapshot.
            duplicate = connection.execute(
                "SELECT 1 FROM comments GROUP BY comment_id HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate:
                raise ValueError("У базі є повторні comment_id; експорт скасовано без втрати записів.")
            connection.create_function("utc_sort_key", 1, _utc_sort_key, deterministic=True)
            rows = connection.execute(
                f"SELECT {', '.join(FIELDS)} FROM comments ORDER BY utc_sort_key(date) ASC, comment_id ASC"
            )
            _validate_destination(destination, database_paths)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=destination.parent,
                prefix=f".{destination.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=FIELDS) if format == "csv" else None
                if writer:
                    writer.writeheader()
                count = 0
                for row in rows:
                    record = dict(row)
                    if writer:
                        writer.writerow(record)
                    else:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
            _validate_destination(destination, database_paths)
            os.replace(temporary, destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Експорт коментарів із SQLite без зміни бази")
    parser.add_argument("--format", required=True, choices=("jsonl", "csv"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        count = export_comments(args.output, args.format)
    except UnsafeExportPath as exc:
        raise SystemExit(f"Помилка експорту: {exc}") from None
    except (sqlite3.Error, OSError, ValueError):
        raise SystemExit("Помилка експорту: перевірте базу, унікальність ID та шлях вихідного файлу.") from None
    print(f"Експортовано записів: {count}\nФормат: {args.format}\nФайл: {args.output}")


if __name__ == "__main__":
    main()
