"""Import ``app.main`` with all mutable application state isolated for tests."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory


# Keep the owner alive for the test process. Windows services may retain SQLite
# handles briefly at interpreter shutdown, so cleanup deliberately tolerates
# transient handle-release delays.
_RUNTIME_DIRECTORY = TemporaryDirectory(
    prefix="askjenny-main-tests-",
    ignore_cleanup_errors=True,
)
_RUNTIME_ROOT = Path(_RUNTIME_DIRECTORY.name)

os.environ.update(
    {
        "OPENAI_API_KEY": "",
        "APPLICATION_DATABASE_PATH": str(_RUNTIME_ROOT / "application.sqlite"),
        "SAVED_CONVERSATIONS_DATABASE_PATH": str(
            _RUNTIME_ROOT / "saved-conversations.sqlite"
        ),
        "SAVED_CONVERSATIONS_PATH": str(
            _RUNTIME_ROOT / "legacy-conversations.json"
        ),
        "DOCSTORE_BACKEND": "json",
        "DOCSTORE_JSON_PATH": str(_RUNTIME_ROOT / "documents.json"),
        "DOCSTORE_FOLDERS_PATH": str(_RUNTIME_ROOT / "folders.json"),
        "WATCHED_FOLDERS_PATH": str(_RUNTIME_ROOT / "watched-folders.json"),
        "SEMANTIC_INDEX_PATH": str(_RUNTIME_ROOT / "semantic.sqlite"),
        "OPENAI_USAGE_LOG_PATH": str(_RUNTIME_ROOT / "openai-usage.jsonl"),
        "ROUTINES_DATABASE_PATH": str(_RUNTIME_ROOT / "routines.sqlite"),
        "PDF_EXTRACTION_CACHE_PATH": str(_RUNTIME_ROOT / "pdf-cache"),
        "DEFAULT_ADMIN_USERNAME": "test-administrator",
        "DEFAULT_ADMIN_DISPLAY_NAME": "Test Administrator",
        "DEFAULT_ADMIN_PASSWORD": "TestAdministrator1!",
        "PDF_VISION_ENABLED": "false",
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app import main  # noqa: E402


__all__ = ["main"]
