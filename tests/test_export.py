"""Offline export tests using fixtures in temporary databases."""

import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from youtube_collector.export import FIELDS, export_comments, main

from collector_test_support import sample_records


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = sample_records()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "comments.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(f"CREATE TABLE comments ({', '.join(name + ' TEXT' for name in FIELDS)})")
            connection.executemany(
                f"INSERT INTO comments VALUES ({', '.join('?' for _ in FIELDS)})",
                [tuple(record[name] for name in FIELDS) for record in reversed(self.records)],
            )
        self.original_hash = hashlib.sha256(self.database.read_bytes()).digest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), self.original_hash)

    def test_jsonl_count_unique_fields_order_and_valid_json(self):
        output = self.directory / "nested" / "comments.jsonl"
        count = export_comments(output, "jsonl", self.database)
        with output.open(encoding="utf-8") as stream:
            lines = list(stream)
        records = [json.loads(line) for line in lines]
        self.assertGreaterEqual(count, 200)
        self.assertEqual(count, len(self.records))
        self.assertEqual(len(lines), count)
        self.assertEqual(len({row["comment_id"] for row in records}), count)
        self.assertTrue(all(set(row) == set(FIELDS) for row in records))
        self.assertTrue(all(all(name in row for name in ("author", "url", "date", "text"))
                            for row in records))
        self.assertEqual(records, sorted(self.records, key=lambda row: (row["date"], row["comment_id"])))

    def test_csv_headers_count_and_exact_roundtrip(self):
        output = self.directory / "comments.csv"
        count = export_comments(output, "csv", self.database)
        with output.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(FIELDS))
            records = list(reader)
        self.assertEqual(count, len(self.records))
        self.assertEqual(len({row["comment_id"] for row in records}), count)
        self.assertEqual(records, self.records)

    def test_jsonl_keeps_unicode_unescaped_and_multiline_text_intact(self):
        # Encoding/escaping fixture changes ONLY a temporary test database;
        # the real SQLite and committed exports are never modified by tests.
        text = 'Український текст, "лапки"\nДругий рядок 😀\r\nТретій рядок'
        identifier = self.records[0]["comment_id"]
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE comments SET text = ? WHERE comment_id = ?", (text, identifier))
        self.original_hash = hashlib.sha256(self.database.read_bytes()).digest()
        output = self.directory / "comments.jsonl"
        export_comments(output, "jsonl", self.database)
        self.assertIn("Український", output.read_text(encoding="utf-8"))
        with output.open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        self.assertEqual(next(row["text"] for row in records if row["comment_id"] == identifier), text)
        output_csv = self.directory / "comments.csv"
        export_comments(output_csv, "csv", self.database)
        with output_csv.open(encoding="utf-8", newline="") as stream:
            csv_records = list(csv.DictReader(stream))
        self.assertEqual(csv_records, records)

    def test_cli(self):
        output = self.directory / "cli.csv"
        with patch("youtube_collector.export.DEFAULT_DB_PATH", self.database), \
                patch("youtube_collector.export.export_comments", wraps=lambda path, fmt:
                      export_comments(path, fmt, self.database)), \
                patch("sys.stdout", new_callable=io.StringIO) as captured:
            main(["--format", "csv", "--output", str(output)])
        self.assertIn(f"Експортовано записів: {len(self.records)}", captured.getvalue())
        self.assertTrue(output.exists())

    def test_duplicate_id_rejects_export_without_overwriting_output(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO comments SELECT * FROM comments LIMIT 1")
        self.original_hash = hashlib.sha256(self.database.read_bytes()).digest()
        output = self.directory / "comments.jsonl"
        output.write_bytes(b"previous export\n")
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "повторні comment_id"):
            export_comments(output, "jsonl", self.database)
        self.assertEqual(output.read_bytes(), before)

    def test_database_and_env_cannot_be_output(self):
        for output in (self.database, self.directory / ".env", self.directory / ".env.local"):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    export_comments(output, "csv", self.database)

    def test_missing_database_is_not_created(self):
        missing = self.directory / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            export_comments(self.directory / "output.csv", "csv", missing)
        self.assertFalse(missing.exists())

    def test_partial_failure_keeps_previous_export(self):
        output = self.directory / "comments.jsonl"
        output.write_bytes(b"previous export\n")
        before = output.read_bytes()
        with patch("youtube_collector.export.json.dumps", side_effect=ValueError("failure")):
            with self.assertRaises(ValueError):
                export_comments(output, "jsonl", self.database)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])


class MixedOffsetExportTests(unittest.TestCase):
    def test_R7_chronological_sort_preserves_original_offsets(self):
        from youtube_collector.api import Comment
        from youtube_collector.storage import save_comments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "comments.db"
            records = [("a", "2026-10-01T12:00:00+02:00"),
                       ("b", "2026-10-01T10:05:00Z"),
                       ("c", "2026-10-01T05:00:00-05:00")]
            save_comments([Comment(i,"Автор","https://www.youtube.com/watch?v=v",d,"Текст","v") for i,d in records], db)
            for format in ("jsonl", "csv"):
                output = root / ("output." + format)
                export_comments(output, format, db)
                with output.open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream)) if format == "csv" else [json.loads(line) for line in stream]
                self.assertEqual([row["comment_id"] for row in rows], ["a", "c", "b"])
                self.assertEqual({row["comment_id"]: row["date"] for row in rows}, dict(records))


class SQLiteDestinationRegressionTests(unittest.TestCase):
    def setUp(self):
        from youtube_collector.api import Comment
        from youtube_collector.storage import save_comments
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "comments.db"
        save_comments([Comment("baseline", "Автор", "https://www.youtube.com/watch?v=v",
                               "2026-10-20T12:00:00Z", "Temporary fixture", "v")], self.database)
        self.writer = sqlite3.connect(self.database)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("""INSERT INTO comments SELECT source, 'wal-row', video_id,
                               author, url, date, text, collected_at FROM comments""")
        self.writer.commit()
        self.protected = [Path(str(self.database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
        self.assertTrue(self.protected[1].exists())
        self.assertTrue(self.protected[2].exists())

    def assert_rejected(self, output, db_path=None):
        before = {path: path.read_bytes() if path.exists() else None for path in self.protected}
        with patch("youtube_collector.export.tempfile.NamedTemporaryFile") as temporary, \
                patch("youtube_collector.export.os.replace") as replace:
            with self.assertRaisesRegex(ValueError, "WAL/SHM/journal"):
                export_comments(output, "jsonl", db_path or self.database)
            temporary.assert_not_called()
            replace.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() if path.exists() else None for path in self.protected})
        with closing(sqlite3.connect(self.database)) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 2)
            self.assertEqual(reader.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_Q1_active_WAL_and_all_SQLite_paths_rejected_without_changes(self):
        for path in self.protected:
            with self.subTest(path=path.name):
                self.assert_rejected(path)

    def test_SQLite_symlinks_hardlinks_and_directory_aliases_are_rejected(self):
        import os
        directory_alias = self.root / "directory-alias"
        directory_alias.symlink_to(self.root, target_is_directory=True)
        database_alias = self.root / "database-alias"
        database_alias.symlink_to(self.database)
        for index, target in enumerate(self.protected):
            with self.subTest(target=target.name):
                link = self.root / f"symlink-{index}.jsonl"
                link.symlink_to(target)
                self.assert_rejected(link, database_alias)
                self.assert_rejected(directory_alias / target.name)
                if target.exists():
                    hardlink = self.root / f"hardlink-{index}.jsonl"
                    os.link(target, hardlink)
                    self.assert_rejected(hardlink)

    def test_destination_is_checked_again_before_replace(self):
        output = self.root / "output.jsonl"
        original = json.dumps
        def switch_to_wal(*args, **kwargs):
            if not output.is_symlink():
                output.symlink_to(self.protected[1])
            return original(*args, **kwargs)
        before = self.protected[1].read_bytes()
        with patch("youtube_collector.export.json.dumps", side_effect=switch_to_wal), \
                patch("youtube_collector.export.os.replace") as replace:
            with self.assertRaises(ValueError):
                export_comments(output, "jsonl", self.database)
            replace.assert_not_called()
        self.assertEqual(self.protected[1].read_bytes(), before)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_CLI_explains_rejected_SQLite_destination(self):
        with patch("youtube_collector.export.export_comments", side_effect=lambda path, fmt:
                   export_comments(path, fmt, self.database)):
            with self.assertRaisesRegex(SystemExit, "SQLite-базу, її WAL/SHM/journal"):
                main(["--format", "jsonl", "--output", str(self.protected[1])])


if __name__ == "__main__":
    unittest.main()
