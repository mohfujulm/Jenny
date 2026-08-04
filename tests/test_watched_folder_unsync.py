"""Verify unsynchronizing watchers preserves or removes library data as requested."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.config import get_settings
from app.watch_folders import WatchedFolderService


class WatchedFolderUnsyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        corpus_path = temp_path / "documents.json"
        corpus_path.write_text("[]", encoding="utf-8")
        settings = replace(
            get_settings(),
            watched_folders_path=temp_path / "watched_folders.json",
            docstore_json_path=corpus_path,
        )
        self.ingestion_service = Mock()
        self.service = WatchedFolderService(settings, self.ingestion_service)
        self.source_root = temp_path / "sources"
        self.source_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_watcher(self, name: str, library_folder: str) -> dict:
        source_path = self.source_root / name
        source_path.mkdir()
        return self.service.create_watcher(
            root_path=str(source_path),
            include_subfolder=None,
            display_name=name,
            alias=None,
            library_folder=library_folder,
            category="watched",
            tags=[],
            recursive=True,
            enabled=True,
            interval_minutes=30,
        )

    def test_unsynchronizing_parent_library_folder_removes_nested_watchers(self) -> None:
        project = self._create_watcher("project", "Projects/PANYNJ")
        datasheets = self._create_watcher(
            "datasheets",
            "Projects/PANYNJ/OEM Datasheets",
        )
        other = self._create_watcher("other", "Projects/Astoria")

        removed = self.service.unsynchronize_library_folder("Projects/PANYNJ")

        self.assertEqual(
            {item["watch_id"] for item in removed},
            {project["watch_id"], datasheets["watch_id"]},
        )
        self.assertEqual(
            {item["watch_id"] for item in self.service.list_watchers()},
            {other["watch_id"]},
        )

    def test_deleting_library_folder_also_unsynchronizes_its_source(self) -> None:
        watched = self._create_watcher("project", "Projects/PANYNJ")
        outcome = SimpleNamespace(folder_id="Projects/PANYNJ")
        self.ingestion_service.delete_folder.return_value = outcome

        returned_outcome, removed = (
            self.service.delete_library_folder_and_unsynchronize("Projects/PANYNJ")
        )

        self.assertIs(returned_outcome, outcome)
        self.ingestion_service.delete_folder.assert_called_once_with(
            folder_id="Projects/PANYNJ"
        )
        self.assertEqual([item["watch_id"] for item in removed], [watched["watch_id"]])
        self.assertEqual(self.service.list_watchers(), [])


if __name__ == "__main__":
    unittest.main()
