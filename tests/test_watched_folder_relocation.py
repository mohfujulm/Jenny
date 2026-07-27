from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import base64
import unittest
from unittest.mock import Mock

from app.watch_folders import WatchedFolderRecord, WatchedFolderService


class WatchedFolderRelocationTests(unittest.TestCase):
    def _settings(self, temp_root: Path) -> SimpleNamespace:
        documents_path = temp_root / "documents.json"
        folders_path = temp_root / "folders.json"
        watched_folders_path = temp_root / "watched-folders.json"
        documents_path.write_text("[]\n", encoding="utf-8")
        folders_path.write_text("[]\n", encoding="utf-8")
        return SimpleNamespace(
            docstore_json_path=documents_path,
            docstore_folders_path=folders_path,
            watched_folders_path=watched_folders_path,
        )

    def _record(self, source_path: Path) -> WatchedFolderRecord:
        return WatchedFolderRecord(
            watch_id="watch-pdf",
            alias="OEM Datasheets",
            display_name="OEM Datasheets",
            root_path=str(source_path),
            include_subfolder=None,
            library_folder="PANYNJ EWR/OEM Datasheets",
            category="watched",
            tags=[],
            recursive=True,
            enabled=True,
            interval_minutes=30,
            created_at="2026-07-23T00:00:00+00:00",
        )

    def _ingestion_service(self) -> Mock:
        service = Mock()
        service.ingest_upload_batch.return_value = SimpleNamespace(
            uploaded_documents=[SimpleNamespace(document_id="pdf-1")],
            message="Imported 1 document.",
            created_count=1,
            updated_count=0,
            unchanged_count=0,
            semantic_index_rebuilt=False,
            failed_uploads=[],
        )
        return service

    def test_repairs_unique_renamed_project_folder_and_discovers_pdf(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            projects_root = temp_root / "00. Projects"
            stale_source = (
                projects_root
                / "43. PANYNJ - EWR Innomotics VMSS"
                / "Working Moh"
                / "OEM Datasheets"
            )
            relocated_source = (
                projects_root
                / "43. PANYNJ - EWR VMSS"
                / "Working Moh"
                / "OEM Datasheets"
            )
            relocated_source.mkdir(parents=True)
            pdf_bytes = b"%PDF-1.4 relocated watched PDF"
            (relocated_source / "camera.pdf").write_bytes(pdf_bytes)

            settings = self._settings(temp_root)
            ingestion_service = self._ingestion_service()
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = self._record(stale_source)
            watcher_service._write_records_locked([record])

            result = watcher_service._sync_record(record)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.scanned_count, 1)
            self.assertEqual(result.imported_count, 1)
            self.assertEqual(result.source_path, str(relocated_source.resolve()))
            self.assertIn("Automatically repaired the synchronized path", result.message)
            upload = ingestion_service.ingest_upload_batch.call_args.kwargs["uploads"][0]
            self.assertEqual(upload["filename"], "camera.pdf")
            self.assertEqual(
                base64.b64decode(upload["content_base64"]),
                pdf_bytes,
            )
            persisted_record = watcher_service._load_records()[0]
            self.assertEqual(
                Path(persisted_record.root_path),
                relocated_source.resolve(),
            )
            self.assertIsNone(persisted_record.include_subfolder)

    def test_does_not_guess_when_project_number_match_is_ambiguous(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            projects_root = temp_root / "00. Projects"
            stale_source = (
                projects_root
                / "43. PANYNJ - EWR Innomotics VMSS"
                / "Working Moh"
                / "OEM Datasheets"
            )
            for project_name in (
                "43. PANYNJ - EWR VMSS",
                "43. PANYNJ - EWR Alternate",
            ):
                (
                    projects_root
                    / project_name
                    / "Working Moh"
                    / "OEM Datasheets"
                ).mkdir(parents=True)

            settings = self._settings(temp_root)
            ingestion_service = self._ingestion_service()
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = self._record(stale_source)
            watcher_service._write_records_locked([record])

            result = watcher_service._sync_record(record)

            self.assertEqual(result.status, "error")
            self.assertIn("Watched folder does not exist", result.message)
            ingestion_service.ingest_upload_batch.assert_not_called()
            persisted_record = watcher_service._load_records()[0]
            self.assertEqual(Path(persisted_record.root_path), stale_source)


if __name__ == "__main__":
    unittest.main()
