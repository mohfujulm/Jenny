"""Load and validate application configuration.

Settings come from process environment variables, with a project-level ``.env``
file supplying only values that are not already present.  ``get_settings`` is
cached so every service observes one consistent configuration snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_file() -> None:
    """Apply simple ``KEY=VALUE`` entries from ``.env`` without overriding the OS."""
    dotenv_path = ROOT_DIR / ".env"
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _to_bool(value: str | None, default: bool) -> bool:
    """Convert common truthy strings while preserving a caller-supplied default."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    """Parse an integer or use ``default`` when the variable is absent/invalid."""
    if value is None:
        return default
    return int(value)


def _to_optional_int(value: str | None) -> int | None:
    """Parse an optional integer; blank and invalid values mean "not configured"."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return int(normalized)


_load_dotenv_file()


@dataclass(frozen=True)
class Settings:
    """Immutable-in-practice configuration shared by all application services."""
    app_title: str
    openai_api_key: str | None
    openai_standard_model: str
    openai_maximum_model: str
    openai_store_responses: bool
    openai_standard_reasoning_effort: str
    openai_maximum_reasoning_effort: str
    openai_text_verbosity: str
    session_ttl_minutes: int
    application_database_path: Path
    auth_session_ttl_hours: int
    default_admin_username: str
    default_admin_display_name: str
    default_admin_password: str
    saved_conversations_database_path: Path
    saved_conversations_path: Path
    docstore_backend: str
    docstore_json_path: Path
    docstore_folders_path: Path
    watched_folders_path: Path
    watched_folder_poll_seconds: int
    docstore_base_url: str
    docstore_api_key: str | None
    docstore_timeout_seconds: int
    semantic_index_path: Path
    semantic_search_embedding_model: str
    semantic_search_embedding_dimensions: int | None
    semantic_answer_embedding_model: str
    semantic_answer_embedding_dimensions: int | None
    semantic_chunk_size_words: int
    semantic_chunk_overlap_words: int
    semantic_embedding_batch_size: int
    chat_request_timeout_seconds: int
    chat_max_tool_rounds: int
    chat_history_max_messages: int
    chat_history_max_chars: int
    chat_memory_enabled: bool
    chat_memory_max_chars: int
    chat_memory_max_turns: int
    chat_tool_document_max_chars: int
    chat_max_input_budget: int
    chat_image_budget_units: int
    chat_max_output_tokens: int
    document_generation_max_output_tokens: int
    openai_request_timeout_seconds: int
    source_download_timeout_seconds: int
    source_download_max_attempts: int
    source_download_max_bytes: int
    pdf_ocr_enabled: bool = True
    pdf_ocr_engine: str = "tesseract"
    pdf_ocr_language: str = "eng"
    pdf_ocr_dpi: int = 300
    pdf_ocr_min_native_text_chars: int = 40
    pdf_ocr_timeout_seconds: int = 60
    pdf_ocr_tesseract_cmd: str = "tesseract"
    pdf_max_pages: int = 500
    pdf_image_ocr_enabled: bool = True
    pdf_image_ocr_max_pages: int = 100
    pdf_vision_enabled: bool = True
    pdf_vision_model: str = "gpt-5.6-luna"
    pdf_vision_max_pages: int = 12
    pdf_vision_batch_size: int = 3
    pdf_vision_dpi: int = 144
    pdf_vision_max_dimension: int = 1800
    pdf_vision_timeout_seconds: int = 60
    pdf_extraction_cache_enabled: bool = True
    pdf_extraction_cache_path: Path = ROOT_DIR / "app/data/pdf_extraction_cache"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build the process-wide settings object on first use."""
    return Settings(
        app_title=os.getenv("APP_TITLE", "Team Knowledge Agent"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_standard_model=os.getenv("OPENAI_STANDARD_MODEL", "gpt-5.6-luna"),
        openai_maximum_model=os.getenv("OPENAI_MAXIMUM_MODEL", "gpt-5.6-terra"),
        openai_store_responses=_to_bool(os.getenv("OPENAI_STORE_RESPONSES"), False),
        openai_standard_reasoning_effort=os.getenv(
            "OPENAI_STANDARD_REASONING_EFFORT",
            "medium",
        ),
        openai_maximum_reasoning_effort=os.getenv(
            "OPENAI_MAXIMUM_REASONING_EFFORT",
            "max",
        ),
        openai_text_verbosity=os.getenv("OPENAI_TEXT_VERBOSITY", "medium"),
        session_ttl_minutes=_to_int(os.getenv("SESSION_TTL_MINUTES"), 60),
        application_database_path=ROOT_DIR / os.getenv(
            "APPLICATION_DATABASE_PATH",
            "app/data/application.sqlite",
        ),
        auth_session_ttl_hours=_to_int(os.getenv("AUTH_SESSION_TTL_HOURS"), 168),
        default_admin_username=os.getenv(
            "DEFAULT_ADMIN_USERNAME",
            "admin",
        ),
        default_admin_display_name=os.getenv(
            "DEFAULT_ADMIN_DISPLAY_NAME",
            "Administrator",
        ),
        default_admin_password=os.getenv(
            "DEFAULT_ADMIN_PASSWORD",
            "Administrator!1",
        ),
        saved_conversations_database_path=ROOT_DIR / os.getenv(
            "SAVED_CONVERSATIONS_DATABASE_PATH",
            "app/data/saved_conversations.sqlite",
        ),
        saved_conversations_path=ROOT_DIR / os.getenv(
            "SAVED_CONVERSATIONS_PATH",
            "app/data/saved_conversations.json",
        ),
        docstore_backend=os.getenv("DOCSTORE_BACKEND", "json").strip().lower(),
        docstore_json_path=ROOT_DIR / os.getenv("DOCSTORE_JSON_PATH", "app/data/sample_documents.json"),
        docstore_folders_path=ROOT_DIR / os.getenv("DOCSTORE_FOLDERS_PATH", "app/data/library_folders.json"),
        watched_folders_path=ROOT_DIR / os.getenv("WATCHED_FOLDERS_PATH", "app/data/watched_folders.json"),
        watched_folder_poll_seconds=_to_int(os.getenv("WATCHED_FOLDER_POLL_SECONDS"), 60),
        pdf_ocr_enabled=_to_bool(os.getenv("PDF_OCR_ENABLED"), True),
        pdf_ocr_engine=os.getenv("PDF_OCR_ENGINE", "tesseract").strip().lower() or "tesseract",
        pdf_ocr_language=os.getenv("PDF_OCR_LANGUAGE", "eng").strip() or "eng",
        pdf_ocr_dpi=_to_int(os.getenv("PDF_OCR_DPI"), 300),
        pdf_ocr_min_native_text_chars=_to_int(
            os.getenv("PDF_OCR_MIN_NATIVE_TEXT_CHARS"),
            40,
        ),
        pdf_ocr_timeout_seconds=_to_int(os.getenv("PDF_OCR_TIMEOUT_SECONDS"), 60),
        pdf_ocr_tesseract_cmd=os.getenv("PDF_OCR_TESSERACT_CMD", "tesseract").strip() or "tesseract",
        pdf_max_pages=_to_int(os.getenv("PDF_MAX_PAGES"), 500),
        pdf_image_ocr_enabled=_to_bool(os.getenv("PDF_IMAGE_OCR_ENABLED"), True),
        pdf_image_ocr_max_pages=_to_int(os.getenv("PDF_IMAGE_OCR_MAX_PAGES"), 100),
        pdf_vision_enabled=_to_bool(os.getenv("PDF_VISION_ENABLED"), True),
        pdf_vision_model=os.getenv(
            "PDF_VISION_MODEL",
            os.getenv("OPENAI_STANDARD_MODEL", "gpt-5.6-luna"),
        ).strip() or "gpt-5.6-luna",
        pdf_vision_max_pages=_to_int(os.getenv("PDF_VISION_MAX_PAGES"), 12),
        pdf_vision_batch_size=_to_int(os.getenv("PDF_VISION_BATCH_SIZE"), 3),
        pdf_vision_dpi=_to_int(os.getenv("PDF_VISION_DPI"), 144),
        pdf_vision_max_dimension=_to_int(os.getenv("PDF_VISION_MAX_DIMENSION"), 1800),
        pdf_vision_timeout_seconds=_to_int(os.getenv("PDF_VISION_TIMEOUT_SECONDS"), 60),
        pdf_extraction_cache_enabled=_to_bool(
            os.getenv("PDF_EXTRACTION_CACHE_ENABLED"),
            True,
        ),
        pdf_extraction_cache_path=ROOT_DIR / os.getenv(
            "PDF_EXTRACTION_CACHE_PATH",
            "app/data/pdf_extraction_cache",
        ),
        docstore_base_url=os.getenv("DOCSTORE_BASE_URL", "http://localhost:8081").rstrip("/"),
        docstore_api_key=os.getenv("DOCSTORE_API_KEY"),
        docstore_timeout_seconds=_to_int(os.getenv("DOCSTORE_TIMEOUT_SECONDS"), 15),
        semantic_index_path=ROOT_DIR / os.getenv("SEMANTIC_INDEX_PATH", "app/data/semantic_documents.sqlite"),
        semantic_search_embedding_model=os.getenv(
            "SEMANTIC_SEARCH_EMBEDDING_MODEL",
            os.getenv("SEMANTIC_EMBEDDING_MODEL", "text-embedding-3-small"),
        ),
        semantic_search_embedding_dimensions=_to_optional_int(
            os.getenv("SEMANTIC_SEARCH_EMBEDDING_DIMENSIONS", os.getenv("SEMANTIC_EMBEDDING_DIMENSIONS", ""))
        ),
        semantic_answer_embedding_model=os.getenv("SEMANTIC_ANSWER_EMBEDDING_MODEL", "text-embedding-3-large"),
        semantic_answer_embedding_dimensions=_to_optional_int(os.getenv("SEMANTIC_ANSWER_EMBEDDING_DIMENSIONS")),
        semantic_chunk_size_words=_to_int(os.getenv("SEMANTIC_CHUNK_SIZE_WORDS"), 220),
        semantic_chunk_overlap_words=_to_int(os.getenv("SEMANTIC_CHUNK_OVERLAP_WORDS"), 40),
        semantic_embedding_batch_size=_to_int(os.getenv("SEMANTIC_EMBEDDING_BATCH_SIZE"), 32),
        chat_request_timeout_seconds=_to_int(
            os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS"),
            120,
        ),
        chat_max_tool_rounds=_to_int(os.getenv("CHAT_MAX_TOOL_ROUNDS"), 3),
        chat_history_max_messages=_to_int(
            os.getenv("CHAT_HISTORY_MAX_MESSAGES"),
            8,
        ),
        chat_history_max_chars=_to_int(
            os.getenv("CHAT_HISTORY_MAX_CHARS"),
            16_000,
        ),
        chat_memory_enabled=_to_bool(os.getenv("CHAT_MEMORY_ENABLED"), True),
        chat_memory_max_chars=_to_int(
            os.getenv("CHAT_MEMORY_MAX_CHARS"),
            4_000,
        ),
        chat_memory_max_turns=_to_int(
            os.getenv("CHAT_MEMORY_MAX_TURNS"),
            4,
        ),
        chat_tool_document_max_chars=_to_int(
            os.getenv("CHAT_TOOL_DOCUMENT_MAX_CHARS"),
            6_000,
        ),
        chat_max_input_budget=_to_int(
            os.getenv("CHAT_MAX_INPUT_BUDGET"),
            48_000,
        ),
        chat_image_budget_units=_to_int(
            os.getenv("CHAT_IMAGE_BUDGET_UNITS"),
            4_096,
        ),
        chat_max_output_tokens=_to_int(
            os.getenv("CHAT_MAX_OUTPUT_TOKENS"),
            3_000,
        ),
        document_generation_max_output_tokens=_to_int(
            os.getenv("DOCUMENT_GENERATION_MAX_OUTPUT_TOKENS"),
            6_000,
        ),
        openai_request_timeout_seconds=_to_int(
            os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS"),
            60,
        ),
        source_download_timeout_seconds=_to_int(
            os.getenv("SOURCE_DOWNLOAD_TIMEOUT_SECONDS"),
            20,
        ),
        source_download_max_attempts=_to_int(
            os.getenv("SOURCE_DOWNLOAD_MAX_ATTEMPTS"),
            1,
        ),
        source_download_max_bytes=_to_int(
            os.getenv("SOURCE_DOWNLOAD_MAX_BYTES"),
            20 * 1024 * 1024,
        ),
    )
