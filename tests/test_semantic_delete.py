from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.datastore import delete_documents_from_semantic_index


class SemanticDeleteTests(unittest.TestCase):
    def test_deletes_only_selected_rows_without_rebuilding_embeddings(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            index_path = Path(temporary_directory) / "semantic.sqlite"
            with closing(sqlite3.connect(index_path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE documents (
                        document_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL
                    );
                    CREATE TABLE chunks (
                        chunk_id TEXT PRIMARY KEY,
                        document_id TEXT NOT NULL,
                        chunk_text TEXT NOT NULL
                    );
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO documents (document_id, title) VALUES (?, ?)",
                    [("DOC-1", "One"), ("DOC-2", "Two"), ("DOC-3", "Three")],
                )
                connection.executemany(
                    "INSERT INTO chunks (chunk_id, document_id, chunk_text) VALUES (?, ?, ?)",
                    [
                        ("DOC-1:0", "DOC-1", "one a"),
                        ("DOC-1:1", "DOC-1", "one b"),
                        ("DOC-2:0", "DOC-2", "two"),
                        ("DOC-3:0", "DOC-3", "three"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    [("documents_indexed", "3"), ("chunks_indexed", "4")],
                )

            progress: list[tuple[str, int, str]] = []
            result = delete_documents_from_semantic_index(
                index_path=index_path,
                document_ids=["DOC-1", "DOC-3"],
                progress_callback=lambda phase, percent, detail: progress.append(
                    (phase, percent, detail)
                ),
            )

            self.assertEqual(result["removed_documents"], 2)
            self.assertEqual(result["removed_chunks"], 3)
            self.assertFalse(result["full_rebuild"])
            self.assertEqual(result["embedded_documents"], 0)
            with closing(sqlite3.connect(index_path)) as connection:
                remaining_documents = connection.execute(
                    "SELECT document_id FROM documents ORDER BY document_id"
                ).fetchall()
                remaining_chunks = connection.execute(
                    "SELECT chunk_id FROM chunks ORDER BY chunk_id"
                ).fetchall()
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            self.assertEqual(remaining_documents, [("DOC-2",)])
            self.assertEqual(remaining_chunks, [("DOC-2:0",)])
            self.assertEqual(metadata["documents_indexed"], "1")
            self.assertEqual(metadata["chunks_indexed"], "1")
            self.assertEqual(
                [phase for phase, _percent, _detail in progress],
                [
                    "opening_index",
                    "removing_chunks",
                    "removing_documents",
                    "updating_metadata",
                    "committing",
                ],
            )


if __name__ == "__main__":
    unittest.main()
