"""Verify importing the FastAPI composition root has no user-database effects."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class MainImportSafetyTests(unittest.TestCase):
    @staticmethod
    def _isolated_environment(root: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "OPENAI_API_KEY": "",
                "APPLICATION_DATABASE_PATH": str(root / "application.sqlite"),
                "SAVED_CONVERSATIONS_DATABASE_PATH": str(
                    root / "saved-conversations.sqlite"
                ),
                "SAVED_CONVERSATIONS_PATH": str(
                    root / "legacy-conversations.json"
                ),
                "DOCSTORE_BACKEND": "json",
                "DOCSTORE_JSON_PATH": str(root / "documents.json"),
                "DOCSTORE_FOLDERS_PATH": str(root / "folders.json"),
                "WATCHED_FOLDERS_PATH": str(root / "watched-folders.json"),
                "SEMANTIC_INDEX_PATH": str(root / "semantic.sqlite"),
                "OPENAI_USAGE_LOG_PATH": str(root / "usage.jsonl"),
                "PDF_EXTRACTION_CACHE_PATH": str(root / "pdf-cache"),
                "DEFAULT_ADMIN_USERNAME": "restart-admin",
                "DEFAULT_ADMIN_DISPLAY_NAME": "Configured Administrator",
                "DEFAULT_ADMIN_PASSWORD": "RestartAdministrator1!",
                "PDF_VISION_ENABLED": "false",
            }
        )
        return environment

    def test_import_does_not_create_or_open_the_user_database(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            database_path = root / "application.sqlite"
            environment = self._isolated_environment(root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import app.main; "
                        f"print(Path({str(database_path)!r}).exists())"
                    ),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "False")
            self.assertFalse(database_path.exists())

    def test_restart_preserves_intentional_demotion_and_deactivation(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            database_path = root / "application.sqlite"
            environment = self._isolated_environment(root)
            startup_command = (
                "import app.main as main; "
                "main.initialize_user_runtime_services()"
            )
            first_start = subprocess.run(
                [sys.executable, "-c", startup_command],
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(first_start.returncode, 0, first_start.stderr)

            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE users
                    SET display_name = 'Intentionally Demoted',
                        role = 'member', is_active = 0,
                        updated_at = '2026-08-01T00:00:00+00:00'
                    WHERE username = 'restart-admin'
                    """
                )
                connection.commit()

            second_start = subprocess.run(
                [sys.executable, "-c", startup_command],
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(second_start.returncode, 0, second_start.stderr)
            with sqlite3.connect(database_path) as connection:
                account = connection.execute(
                    """
                    SELECT display_name, role, is_active, updated_at
                    FROM users WHERE username = 'restart-admin'
                    """
                ).fetchone()

            self.assertEqual(
                account,
                (
                    "Intentionally Demoted",
                    "member",
                    0,
                    "2026-08-01T00:00:00+00:00",
                ),
            )


if __name__ == "__main__":
    unittest.main()
