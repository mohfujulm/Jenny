from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_file() -> None:
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
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _to_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return int(normalized)


_load_dotenv_file()


@dataclass(frozen=True)
class Settings:
    app_title: str
    openai_api_key: str | None
    openai_model: str
    openai_store_responses: bool
    openai_reasoning_effort: str
    openai_text_verbosity: str
    session_ttl_minutes: int
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
    pdf_ocr_enabled: bool = True
    pdf_ocr_engine: str = "tesseract"
    pdf_ocr_language: str = "eng"
    pdf_ocr_dpi: int = 300
    pdf_ocr_min_native_text_chars: int = 40
    pdf_ocr_timeout_seconds: int = 60
    pdf_ocr_tesseract_cmd: str = "tesseract"
    pdf_max_pages: int = 500


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        app_title=os.getenv("APP_TITLE", "Team Knowledge Agent"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        openai_store_responses=_to_bool(os.getenv("OPENAI_STORE_RESPONSES"), False),
        openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
        openai_text_verbosity=os.getenv("OPENAI_TEXT_VERBOSITY", "medium"),
        session_ttl_minutes=_to_int(os.getenv("SESSION_TTL_MINUTES"), 60),
        saved_conversations_path=ROOT_DIR / os.getenv("SAVED_CONVERSATIONS_PATH", "app/data/saved_conversations.json"),
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
    )
