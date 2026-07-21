from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from app.datastore import load_json_documents
from app.ingestion import DocumentIngestionService
from app.watch_folders import WatchedFolderRecord, WatchedFolderService


class WatchedFolderAutoTagTests(unittest.TestCase):
    def test_sync_adds_path_tags_refreshes_them_and_preserves_manual_tags(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            project_notes = (
                temp_root
                / "Vasquez Integrators Dropbox"
                / "01. Project Delivery"
                / "00. Projects"
                / "43. PANYNJ - EWR Innomotics VMSS"
                / "Working Moh"
                / "Project Notes"
            )
            project_notes.mkdir(parents=True)
            note_path = project_notes / "coordination-note.txt"
            note_path.write_text("Coordinate the VMSS controls package.", encoding="utf-8")

            settings = SimpleNamespace(
                docstore_backend="json",
                docstore_json_path=temp_root / "documents.json",
                docstore_folders_path=temp_root / "folders.json",
                openai_api_key=None,
            )
            settings.docstore_json_path.write_text("[]\n", encoding="utf-8")
            settings.docstore_folders_path.write_text("[]\n", encoding="utf-8")

            ingestion_service = DocumentIngestionService(settings)
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = WatchedFolderRecord(
                watch_id="watch-1",
                alias="EWR notes",
                display_name="EWR notes",
                root_path=str(temp_root / "Vasquez Integrators Dropbox"),
                include_subfolder=str(project_notes.relative_to(temp_root / "Vasquez Integrators Dropbox")),
                library_folder="PANYNJ EWR/Project Notes",
                category="project-delivery",
                tags=["department:delivery"],
                recursive=True,
                enabled=True,
                interval_minutes=30,
                created_at="2026-07-21T00:00:00+00:00",
            )

            pending, scanned_count, skipped_count = watcher_service._collect_pending_files(
                record,
                project_notes,
            )
            self.assertEqual((scanned_count, skipped_count, len(pending)), (1, 0, 1))

            first_upload = watcher_service._build_upload(record, project_notes, pending[0])
            self.assertEqual(first_upload["tags"], [])
            self.assertEqual(
                first_upload["source_auto_tags"],
                [
                    "department:delivery",
                    "workflow:project",
                    "project-number:43",
                    "project:PANYNJ - EWR Innomotics VMSS",
                    "client:PANYNJ",
                    "site:EWR",
                    "owner:Moh",
                    "workstream:Project Notes",
                ],
            )
            ingestion_service.ingest_upload_batch(uploads=[first_upload])

            document = load_json_documents(settings.docstore_json_path)[0]
            self.assertIn("project:PANYNJ - EWR Innomotics VMSS", document.tags)
            self.assertIn("owner:Moh", document.auto_tags)

            ingestion_service.update_document_tags(
                document_id=document.document_id,
                tags=[*document.tags, "priority:high"],
            )

            pending, _, skipped_count = watcher_service._collect_pending_files(
                record,
                project_notes,
            )
            self.assertEqual((len(pending), skipped_count), (0, 1))

            updated_record = replace(record, tags=["department:operations"])
            pending, _, skipped_count = watcher_service._collect_pending_files(
                updated_record,
                project_notes,
            )
            self.assertEqual((len(pending), skipped_count), (1, 0))
            replacement_upload = watcher_service._build_upload(
                updated_record,
                project_notes,
                pending[0],
            )
            self.assertEqual(replacement_upload["tags"], ["priority:high"])
            ingestion_service.ingest_upload_batch(uploads=[replacement_upload])

            updated_document = load_json_documents(settings.docstore_json_path)[0]
            self.assertIn("priority:high", updated_document.tags)
            self.assertIn("department:operations", updated_document.tags)
            self.assertNotIn("department:delivery", updated_document.tags)


if __name__ == "__main__":
    unittest.main()
