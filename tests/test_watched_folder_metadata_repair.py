"""Regression tests for no-ingestion watched-file metadata repairs."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.datastore import (
    DocumentRecord,
    build_effective_document_tags,
    load_json_documents,
    write_json_documents,
)
from app.ingestion import DocumentIngestionService
from app.watch_folders import WatchedFolderRecord, WatchedFolderService


class WatchedFolderMetadataRepairTests(unittest.TestCase):
    def test_unchanged_file_repairs_auto_tags_without_reading_or_ingesting_source(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            source_path = temp_root / "watched"
            source_path.mkdir()
            pdf_path = source_path / "datasheet.pdf"
            pdf_path.write_bytes(b"source bytes must not be read during metadata repair")

            settings = self._settings(temp_root)
            ingestion_service = DocumentIngestionService(settings)
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = self._record(source_path)
            expected_source_tags = ["department:delivery"]
            effective_tags, _ = build_effective_document_tags(
                ["priority:high"],
                record.library_folder,
                expected_source_tags,
            )
            upload_key = self._upload_key(watcher_service, record, pdf_path)
            original_document = DocumentRecord(
                document_id="doc-1",
                title=pdf_path.name,
                category=record.category,
                folder=record.library_folder,
                tags=effective_tags,
                summary="Existing summary",
                text="Existing extracted PDF text",
                source_url=pdf_path.resolve().as_uri(),
                updated_at="2026-07-01",
                upload_key=upload_key,
                content_hash="existing-content-hash",
                auto_tags=[],
            )
            write_json_documents(settings.docstore_json_path, [original_document])

            with (
                patch.object(
                    watcher_service,
                    "_build_upload",
                    side_effect=AssertionError("unchanged source must not be uploaded"),
                ),
                patch.object(
                    ingestion_service,
                    "ingest_upload_batch",
                    side_effect=AssertionError("unchanged source must not be ingested"),
                ),
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("unchanged source bytes must not be read"),
                ),
            ):
                result = watcher_service._sync_record_locked(record)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.scanned_count, 1)
            self.assertEqual(result.skipped_count, 1)
            self.assertEqual(result.imported_count, 0)
            self.assertEqual(result.updated_count, 1)
            self.assertFalse(result.semantic_index_rebuilt)
            self.assertIn("without reading or re-embedding", result.message)

            repaired_document = load_json_documents(settings.docstore_json_path)[0]
            self.assertIn("department:delivery", repaired_document.auto_tags)
            self.assertIn("priority:high", repaired_document.tags)
            self.assertEqual(repaired_document.text, original_document.text)
            self.assertEqual(repaired_document.upload_key, original_document.upload_key)
            self.assertEqual(repaired_document.content_hash, original_document.content_hash)

            with patch.object(
                ingestion_service,
                "repair_document_source_auto_tags",
                wraps=ingestion_service.repair_document_source_auto_tags,
            ) as repair:
                second_result = watcher_service._sync_record_locked(record)
            repair.assert_not_called()
            self.assertEqual(second_result.updated_count, 0)
            self.assertEqual(second_result.skipped_count, 1)

    def test_changed_file_version_remains_pending_for_normal_ingestion(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            source_path = temp_root / "watched"
            source_path.mkdir()
            note_path = source_path / "note.txt"
            note_path.write_text("original", encoding="utf-8")

            settings = self._settings(temp_root)
            ingestion_service = DocumentIngestionService(settings)
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = self._record(source_path)
            old_upload_key = self._upload_key(watcher_service, record, note_path)
            write_json_documents(
                settings.docstore_json_path,
                [
                    DocumentRecord(
                        document_id="doc-1",
                        title=note_path.name,
                        category=record.category,
                        folder=record.library_folder,
                        tags=["department:delivery"],
                        summary="",
                        text="original",
                        upload_key=old_upload_key,
                        auto_tags=[],
                    )
                ],
            )
            next_timestamp = note_path.stat().st_mtime + 5
            os.utime(note_path, (next_timestamp, next_timestamp))

            pending, repairs, scanned_count, skipped_count = (
                watcher_service._collect_sync_actions(record, source_path)
            )

            self.assertEqual(scanned_count, 1)
            self.assertEqual(skipped_count, 0)
            self.assertEqual(len(pending), 1)
            self.assertEqual(repairs, {})

    def test_structured_item_upload_key_uses_parent_file_fingerprint(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            source_path = temp_root / "watched"
            source_path.mkdir()
            workbook_path = source_path / "schedule.xlsx"
            workbook_path.write_bytes(b"placeholder workbook")

            settings = self._settings(temp_root)
            ingestion_service = DocumentIngestionService(settings)
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = self._record(source_path)
            parent_upload_key = self._upload_key(
                watcher_service,
                record,
                workbook_path,
            )
            write_json_documents(
                settings.docstore_json_path,
                [
                    DocumentRecord(
                        document_id="sheet-1",
                        title="Schedule",
                        category=record.category,
                        folder=record.library_folder,
                        tags=["department:delivery"],
                        summary="",
                        text="Existing spreadsheet rows",
                        upload_key=f"{parent_upload_key}::item:0:schedule",
                        auto_tags=[],
                    )
                ],
            )

            pending, repairs, scanned_count, skipped_count = (
                watcher_service._collect_sync_actions(record, source_path)
            )

            self.assertEqual(scanned_count, 1)
            self.assertEqual(skipped_count, 1)
            self.assertEqual(pending, [])
            self.assertEqual(repairs, {"sheet-1": ["department:delivery"]})

    def _settings(self, temp_root: Path) -> SimpleNamespace:
        docstore_json_path = temp_root / "documents.json"
        docstore_folders_path = temp_root / "folders.json"
        docstore_json_path.write_text("[]\n", encoding="utf-8")
        docstore_folders_path.write_text("[]\n", encoding="utf-8")
        return SimpleNamespace(
            docstore_backend="json",
            docstore_json_path=docstore_json_path,
            docstore_folders_path=docstore_folders_path,
            watched_folders_path=temp_root / "watched-folders.json",
            openai_api_key=None,
        )

    def _record(self, source_path: Path) -> WatchedFolderRecord:
        return WatchedFolderRecord(
            watch_id="watch-1",
            alias="Watched",
            display_name="Watched",
            root_path=str(source_path),
            include_subfolder=None,
            library_folder="Shared/Datasheets",
            category="watched",
            tags=["department:delivery"],
            recursive=True,
            enabled=True,
            interval_minutes=30,
            created_at="2026-07-01T00:00:00+00:00",
        )

    def _upload_key(
        self,
        watcher_service: WatchedFolderService,
        record: WatchedFolderRecord,
        path: Path,
    ) -> str:
        upload_key_base = watcher_service._build_watch_upload_key_base(
            watch_id=record.watch_id,
            relative_path=path.name,
            modified_ms=int(path.stat().st_mtime * 1000),
        )
        return f"upload:{upload_key_base}"


if __name__ == "__main__":
    unittest.main()
