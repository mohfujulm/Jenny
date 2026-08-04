"""Regression coverage for watched-document automatic tag preservation."""

from __future__ import annotations

from dataclasses import replace
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


class IngestionAutoTagRepairTests(unittest.TestCase):
    def _settings(self, temp_root: Path, *, backend: str = "json") -> SimpleNamespace:
        settings = SimpleNamespace(
            docstore_backend=backend,
            docstore_json_path=temp_root / "documents.json",
            docstore_folders_path=temp_root / "folders.json",
            semantic_index_path=temp_root / "semantic.sqlite3",
            openai_api_key="test-key" if backend == "semantic" else None,
        )
        settings.docstore_json_path.write_text("[]\n", encoding="utf-8")
        settings.docstore_folders_path.write_text("[]\n", encoding="utf-8")
        return settings

    def _write_document_with_missing_source_metadata(
        self,
        settings: SimpleNamespace,
    ) -> tuple[DocumentRecord, list[str], list[str]]:
        source_auto_tags = ["department:delivery", "owner:Moh"]
        tags, expected_auto_tags = build_effective_document_tags(
            ["priority:high"],
            "Projects/EWR",
            source_auto_tags,
        )
        document = DocumentRecord(
            document_id="DOC-REPAIR",
            title="Coordination note",
            category="project-delivery",
            folder="Projects/EWR",
            tags=tags,
            summary="Coordination summary",
            text="Keep this extracted source text unchanged.",
            source_url="file:///watched/coordination-note.txt",
            updated_at="2026-07-30",
            upload_key="upload:watch:watch-1:coordination-note.txt:123",
            content_hash="preserve-this-hash",
            auto_tags=[],
        )
        write_json_documents(settings.docstore_json_path, [document])
        return document, source_auto_tags, expected_auto_tags

    def test_watched_replacement_preserves_source_tags_and_next_scan_skips_file(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            source_path = temp_root / "watched"
            source_path.mkdir()
            note_path = source_path / "coordination-note.txt"
            note_path.write_text("Coordinate the VMSS controls package.", encoding="utf-8")

            settings = self._settings(temp_root)
            ingestion_service = DocumentIngestionService(settings)
            watcher_service = WatchedFolderService(settings, ingestion_service)
            record = WatchedFolderRecord(
                watch_id="watch-1",
                alias="EWR notes",
                display_name="EWR notes",
                root_path=str(source_path),
                include_subfolder=None,
                library_folder="PANYNJ EWR/Project Notes",
                category="project-delivery",
                tags=["department:delivery"],
                recursive=True,
                enabled=True,
                interval_minutes=30,
                created_at="2026-07-30T00:00:00+00:00",
            )

            pending, _, _ = watcher_service._collect_pending_files(record, source_path)
            first_upload = watcher_service._build_upload(record, source_path, pending[0])
            first_outcome = ingestion_service.ingest_upload_batch(uploads=[first_upload])
            self.assertEqual(first_outcome.created_count, 1)

            updated_record = replace(record, tags=["department:operations"])
            pending, _, skipped_count = watcher_service._collect_pending_files(
                updated_record,
                source_path,
            )
            self.assertEqual((len(pending), skipped_count), (1, 0))

            replacement_upload = watcher_service._build_upload(
                updated_record,
                source_path,
                pending[0],
            )
            replacement_outcome = ingestion_service.ingest_upload_batch(
                uploads=[replacement_upload]
            )
            self.assertEqual(replacement_outcome.updated_count, 1)

            replaced_document = load_json_documents(settings.docstore_json_path)[0]
            expected_source_tags = watcher_service._build_watched_auto_tags(
                updated_record,
                note_path,
            )
            self.assertEqual(
                replacement_outcome.uploaded_documents[0].auto_tags,
                replaced_document.auto_tags,
            )
            for expected_tag in expected_source_tags:
                self.assertIn(expected_tag, replaced_document.auto_tags)
                self.assertIn(expected_tag, replaced_document.tags)
            self.assertNotIn("department:delivery", replaced_document.auto_tags)

            pending, _, skipped_count = watcher_service._collect_pending_files(
                updated_record,
                source_path,
            )
            self.assertEqual((len(pending), skipped_count), (0, 1))

    def test_repair_updates_only_tags_with_strict_metadata_sync(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            settings = self._settings(temp_root, backend="semantic")
            original, source_auto_tags, expected_auto_tags = (
                self._write_document_with_missing_source_metadata(settings)
            )
            service = DocumentIngestionService(settings)

            with (
                patch(
                    "app.ingestion.sync_semantic_metadata_only",
                    return_value={"full_rebuild": False, "embedded_documents": 0},
                ) as metadata_sync,
                patch("app.ingestion.sync_semantic_index") as embedding_sync,
            ):
                repaired_ids = service.repair_document_source_auto_tags(
                    {original.document_id: source_auto_tags}
                )
                second_repair_ids = service.repair_document_source_auto_tags(
                    {original.document_id: source_auto_tags}
                )

            self.assertEqual(repaired_ids, [original.document_id])
            self.assertEqual(second_repair_ids, [])
            metadata_sync.assert_called_once()
            embedding_sync.assert_not_called()

            repaired = load_json_documents(settings.docstore_json_path)[0]
            self.assertEqual(repaired.tags, original.tags)
            self.assertEqual(repaired.auto_tags, expected_auto_tags)
            self.assertEqual(repaired.text, original.text)
            self.assertEqual(repaired.upload_key, original.upload_key)
            self.assertEqual(repaired.content_hash, original.content_hash)
            self.assertEqual(repaired.updated_at, original.updated_at)

    def test_repair_never_falls_back_to_embeddings_when_metadata_sync_fails(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            settings = self._settings(temp_root, backend="semantic")
            original, source_auto_tags, _ = self._write_document_with_missing_source_metadata(
                settings
            )
            service = DocumentIngestionService(settings)

            with (
                patch(
                    "app.ingestion.sync_semantic_metadata_only",
                    side_effect=RuntimeError("semantic metadata unavailable"),
                ),
                patch("app.ingestion.sync_semantic_index") as embedding_sync,
            ):
                with self.assertRaisesRegex(RuntimeError, "semantic metadata unavailable"):
                    service.repair_document_source_auto_tags(
                        {original.document_id: source_auto_tags}
                    )

            embedding_sync.assert_not_called()
            rolled_back = load_json_documents(settings.docstore_json_path)[0]
            for source_tag in source_auto_tags:
                self.assertNotIn(source_tag, rolled_back.auto_tags)
            self.assertEqual(rolled_back.content_hash, original.content_hash)


if __name__ == "__main__":
    unittest.main()
