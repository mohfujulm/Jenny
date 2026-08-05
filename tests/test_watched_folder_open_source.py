"""Check source-folder opening behavior and its platform/error boundaries."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.watch_folders import WatchedFolderRecord, WatchedFolderService
from tests.main_runtime import main


class WatchedFolderOpenSourceTests(unittest.TestCase):
    def _service(self, temp_root: Path) -> WatchedFolderService:
        settings = SimpleNamespace(
            watched_folders_path=temp_root / "watched-folders.json",
        )
        return WatchedFolderService(settings, Mock())

    def _record(self, source_path: Path) -> WatchedFolderRecord:
        return WatchedFolderRecord(
            watch_id="watch-source",
            alias="Source folder",
            display_name="Source folder",
            root_path=str(source_path),
            include_subfolder=None,
            library_folder="Projects/Source folder",
            category="watched",
            tags=[],
            recursive=True,
            enabled=True,
            interval_minutes=30,
            created_at="2026-07-24T00:00:00+00:00",
        )

    def test_resolves_existing_source_path_from_watch_id(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            source_path = temp_root / "source"
            source_path.mkdir()
            service = self._service(temp_root)
            service._write_records_locked([self._record(source_path)])

            resolved_path = service.resolve_watcher_source_path("watch-source")

            self.assertEqual(resolved_path, source_path.resolve())

    def test_rejects_missing_source_path(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            missing_path = temp_root / "missing"
            service = self._service(temp_root)
            service._write_records_locked([self._record(missing_path)])

            with self.assertRaisesRegex(
                ValueError,
                "Synchronized source folder does not exist",
            ):
                service.resolve_watcher_source_path("watch-source")

    def test_windows_launcher_opens_the_resolved_directory(self) -> None:
        with TemporaryDirectory() as temp_directory:
            source_path = Path(temp_directory)
            with (
                patch.object(main.platform, "system", return_value="Windows"),
                patch.object(main, "_open_windows_source_folder") as open_source,
            ):
                main._open_local_source_folder(source_path)

            open_source.assert_called_once_with(source_path.resolve())

    def test_windows_launcher_starts_hidden_foreground_helper(self) -> None:
        source_path = Path(r"C:\Dropbox\Project's Notes").resolve()
        with patch.object(main.subprocess, "Popen") as popen:
            main._open_windows_source_folder(source_path)

        command = popen.call_args.args[0]
        script = command[-1]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-WindowStyle", command)
        self.assertIn("Hidden", command)
        self.assertIn(str(source_path).replace("'", "''"), script)
        self.assertIn("ShowWindowAsync", script)
        self.assertIn("SetForegroundWindow", script)
        self.assertFalse(popen.call_args.kwargs.get("shell", False))

    def test_endpoint_opens_only_the_watcher_resolved_path(self) -> None:
        with TemporaryDirectory() as temp_directory:
            source_path = Path(temp_directory).resolve()
            with (
                patch.object(
                    main.watch_folder_service,
                    "resolve_watcher_source_path",
                    return_value=source_path,
                ) as resolve_source,
                patch.object(main, "_open_local_source_folder") as open_source,
            ):
                response = main.open_watched_folder_source("watch-source")

            resolve_source.assert_called_once_with("watch-source")
            open_source.assert_called_once_with(source_path)
            self.assertTrue(response.opened)
            self.assertEqual(response.source_path, str(source_path))

    def test_endpoint_reports_unknown_watcher_as_not_found(self) -> None:
        with patch.object(
            main.watch_folder_service,
            "resolve_watcher_source_path",
            side_effect=ValueError("Watched folder not found."),
        ):
            with self.assertRaises(HTTPException) as context:
                main.open_watched_folder_source("unknown")

        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
