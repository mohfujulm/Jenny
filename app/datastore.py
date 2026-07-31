from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import math
import re
import shutil
import sqlite3
from contextlib import closing
from typing import Any, Callable
from uuid import uuid4

import httpx
from openai import OpenAI

from app.config import Settings


TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
AUTO_TAG_FOLDER_PREFIX = "folder:"
AUTO_TAG_PATH_PREFIX = "folder-path:"


def normalize_folder_path(folder_id: str) -> str:
    return "/".join(
        segment.strip()
        for segment in re.split(r"[\\/]+", str(folder_id or "").strip())
        if segment.strip()
    )


def iter_folder_lineage(folder_id: str) -> list[str]:
    normalized = normalize_folder_path(folder_id)
    if not normalized:
        return []

    lineage: list[str] = []
    parts: list[str] = []
    for segment in normalized.split("/"):
        parts.append(segment)
        lineage.append("/".join(parts))
    return lineage


def normalize_tag_values(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, str):
        items = raw_tags.split(",")
    elif isinstance(raw_tags, list):
        items = raw_tags
    else:
        items = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    return normalized


def build_folder_auto_tags(folder_id: str) -> list[str]:
    normalized_folder_id = normalize_folder_path(folder_id)
    if not normalized_folder_id:
        return []

    auto_tags: list[str] = []
    seen: set[str] = set()
    lineage_parts: list[str] = []
    for segment in normalized_folder_id.split("/"):
        normalized_segment = segment.strip()
        if not normalized_segment:
            continue
        lineage_parts.append(normalized_segment)
        for candidate in (
            f"{AUTO_TAG_FOLDER_PREFIX}{normalized_segment}",
            f"{AUTO_TAG_PATH_PREFIX}{'/'.join(lineage_parts)}",
        ):
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            auto_tags.append(candidate)
    return auto_tags


def strip_folder_auto_tags(raw_tags: Any, folder_id: str) -> list[str]:
    auto_tag_keys = {tag.lower() for tag in build_folder_auto_tags(folder_id)}
    if not auto_tag_keys:
        return normalize_tag_values(raw_tags)

    return [
        tag
        for tag in normalize_tag_values(raw_tags)
        if tag.lower() not in auto_tag_keys
    ]


def build_effective_document_tags(
    raw_tags: Any,
    folder_id: str,
    additional_auto_tags: Any = None,
) -> tuple[list[str], list[str]]:
    folder_auto_tags = build_folder_auto_tags(folder_id)
    auto_tags = normalize_tag_values(
        [*folder_auto_tags, *normalize_tag_values(additional_auto_tags)]
    )
    auto_tag_keys = {tag.lower() for tag in auto_tags}
    manual_tags = [
        tag
        for tag in normalize_tag_values(raw_tags)
        if tag.lower() not in auto_tag_keys
    ]
    return normalize_tag_values([*manual_tags, *auto_tags]), auto_tags


def load_folder_registry(path: Path) -> list[str]:
    if not path.exists():
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Folder registry must be a JSON array.")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in payload:
        folder_id = normalize_folder_path(str(item or ""))
        if not folder_id or folder_id in seen:
            continue
        seen.add(folder_id)
        normalized.append(folder_id)

    normalized.sort()
    return normalized


def write_folder_registry(path: Path, folder_ids: list[str]) -> None:
    normalized = sorted(
        {
            normalize_folder_path(folder_id)
            for folder_id in folder_ids
            if normalize_folder_path(folder_id)
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def build_folder_records(
    exact_folder_counts: dict[str, int],
    explicit_folder_ids: list[str] | None = None,
) -> list["FolderRecord"]:
    all_folder_ids: set[str] = set()
    for folder_id in exact_folder_counts:
        all_folder_ids.update(iter_folder_lineage(folder_id))
    for folder_id in explicit_folder_ids or []:
        all_folder_ids.update(iter_folder_lineage(folder_id))

    return [
        FolderRecord(
            folder_id=folder_id,
            display_name=folder_id,
            document_count=exact_folder_counts.get(folder_id, 0),
        )
        for folder_id in sorted(all_folder_ids)
    ]


@dataclass
class DocumentRecord:
    document_id: str
    title: str
    category: str
    folder: str
    tags: list[str]
    summary: str
    text: str
    source_url: str | None = None
    updated_at: str | None = None
    upload_key: str | None = None
    content_hash: str | None = None
    auto_tags: list[str] = field(default_factory=list)

    def to_tool_payload(self, max_chars: int = 12000) -> dict[str, Any]:
        truncated = len(self.text) > max_chars
        return {
            "document_id": self.document_id,
            "title": self.title,
            "category": self.category,
            "folder": self.folder,
            "tags": self.tags,
            "summary": self.summary,
            "text": self.text[:max_chars],
            "source_url": self.source_url,
            "updated_at": self.updated_at,
            "truncated": truncated,
        }


@dataclass
class SearchHit:
    document_id: str
    title: str
    category: str
    folder: str
    summary: str
    excerpt: str
    score: float
    source_url: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["score"] = round(self.score, 3)
        return payload


@dataclass
class SemanticChunk:
    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    search_embedding: list[float]
    search_embedding_norm: float
    answer_embedding: list[float]
    answer_embedding_norm: float


@dataclass(frozen=True)
class RetrievalContext:
    folder_ids: frozenset[str] = field(default_factory=frozenset)
    document_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_lists(
        cls,
        folder_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> RetrievalContext:
        return cls(
            folder_ids=frozenset(item.strip() for item in (folder_ids or []) if item and item.strip()),
            document_ids=frozenset(item.strip() for item in (document_ids or []) if item and item.strip()),
        )

    @property
    def is_active(self) -> bool:
        return bool(self.folder_ids or self.document_ids)

    def allows_document(self, document: DocumentRecord) -> bool:
        if not self.is_active:
            return True
        document_folder = normalize_folder_path(document.folder)
        return (
            any(
                document_folder == normalize_folder_path(folder_id)
                or document_folder.startswith(f"{normalize_folder_path(folder_id)}/")
                for folder_id in self.folder_ids
            )
            or document.document_id in self.document_ids
        )


@dataclass
class FolderRecord:
    folder_id: str
    display_name: str
    document_count: int


@dataclass
class DocumentListRecord:
    document_id: str
    title: str
    category: str
    folder: str
    tags: list[str]
    summary: str
    source_url: str | None
    updated_at: str | None
    chunk_count: int | None
    embedded: bool


@dataclass
class DocumentLibraryRecord:
    backend: str
    total_documents: int
    total_chunks: int | None
    folders: list[FolderRecord]
    documents: list[DocumentListRecord]


class BaseDocumentStore(ABC):
    def invalidate_cache(self) -> None:
        return None

    @abstractmethod
    def search_documents(
        self,
        query: str,
        limit: int = 5,
        context: RetrievalContext | None = None,
        search_profile: str = "search",
    ) -> list[SearchHit]:
        raise NotImplementedError

    @abstractmethod
    def get_document(
        self,
        document_id: str,
        context: RetrievalContext | None = None,
    ) -> DocumentRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_documents(self) -> DocumentLibraryRecord:
        raise NotImplementedError


def _tokenize(value: str) -> list[str]:
    return TOKEN_RE.findall(value.lower())


def _build_excerpt(text: str, query_tokens: list[str], width: int = 320) -> str:
    lowered = text.lower()
    pivot = 0
    for token in query_tokens:
        location = lowered.find(token)
        if location >= 0:
            pivot = location
            break

    start = max(0, pivot - width // 3)
    end = min(len(text), start + width)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return excerpt


def _normalized_folder(item: dict[str, Any]) -> str:
    folder = str(item.get("folder") or item.get("category") or "General").strip()
    return folder or "General"


def _load_json_documents_from_path(path: Path) -> list[DocumentRecord]:
    raw_documents = json.loads(path.read_text(encoding="utf-8"))
    documents: list[DocumentRecord] = []
    for item in raw_documents:
        folder = _normalized_folder(item)
        tags, auto_tags = build_effective_document_tags(
            item.get("tags", []),
            folder,
            item.get("auto_tags", []),
        )
        documents.append(
            DocumentRecord(
                document_id=item["document_id"],
                title=item["title"],
                category=item["category"],
                folder=folder,
                tags=tags,
                summary=item["summary"],
                text=item["text"],
                source_url=item.get("source_url"),
                updated_at=item.get("updated_at"),
                upload_key=item.get("upload_key"),
                content_hash=item.get("content_hash"),
                auto_tags=auto_tags,
            )
        )
    return documents


def _document_record_to_json(document: DocumentRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_id": document.document_id,
        "title": document.title,
        "category": document.category,
        "folder": document.folder,
        "tags": document.tags,
        "summary": document.summary,
        "text": document.text,
    }
    if document.source_url:
        payload["source_url"] = document.source_url
    if document.updated_at:
        payload["updated_at"] = document.updated_at
    if document.upload_key:
        payload["upload_key"] = document.upload_key
    if document.content_hash:
        payload["content_hash"] = document.content_hash
    if document.auto_tags:
        payload["auto_tags"] = document.auto_tags
    return payload


def load_json_documents(path: Path) -> list[DocumentRecord]:
    if not path.exists():
        return []
    return _load_json_documents_from_path(path)


def write_json_documents(path: Path, documents: list[DocumentRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_document_record_to_json(document) for document in documents]
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _vector_norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _cosine_similarity(
    left: list[float],
    left_norm: float,
    right: list[float],
    right_norm: float,
) -> float:
    if left_norm == 0 or right_norm == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    return dot / (left_norm * right_norm)


def _chunk_text(text: str, chunk_size_words: int, chunk_overlap_words: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    overlap = min(chunk_overlap_words, max(0, chunk_size_words - 1))
    step = max(1, chunk_size_words - overlap)
    chunks: list[str] = []

    for start in range(0, len(words), step):
        segment = words[start:start + chunk_size_words]
        if not segment:
            break
        chunks.append(" ".join(segment).strip())
        if start + chunk_size_words >= len(words):
            break

    return chunks


def _prepare_embedding_input(document: DocumentRecord, chunk_text: str) -> str:
    parts = [
        f"Title: {document.title}",
        f"Category: {document.category}",
        f"Folder: {document.folder}",
    ]
    if document.tags:
        parts.append(f"Tags: {', '.join(document.tags)}")
    if document.summary:
        parts.append(f"Summary: {document.summary}")
    parts.append(f"Passage: {chunk_text}")
    return "\n".join(parts)


def _document_content(document: DocumentRecord) -> str:
    return document.text.strip() or document.summary.strip() or document.title.strip()


def _document_chunk_texts(
    document: DocumentRecord,
    *,
    chunk_size_words: int,
    chunk_overlap_words: int,
) -> list[str]:
    content = _document_content(document)
    return _chunk_text(content, chunk_size_words, chunk_overlap_words) or [content]


def _documents_match(left: DocumentRecord | None, right: DocumentRecord) -> bool:
    if left is None:
        return False
    return (
        left.document_id == right.document_id
        and left.title == right.title
        and left.category == right.category
        and left.folder == right.folder
        and left.tags == right.tags
        and left.summary == right.summary
        and left.text == right.text
        and left.source_url == right.source_url
        and left.updated_at == right.updated_at
    )


def _embed_texts(
    client: OpenAI,
    texts: list[str],
    model: str,
    dimensions: int | None,
) -> list[list[float]]:
    request: dict[str, Any] = {
        "input": texts,
        "model": model,
        "encoding_format": "float",
    }
    if dimensions is not None:
        request["dimensions"] = dimensions
    response = client.embeddings.create(**request)
    return [item.embedding for item in response.data]


def _embed_texts_in_batches(
    *,
    client: OpenAI,
    texts: list[str],
    model: str,
    dimensions: int | None,
    batch_size: int,
) -> list[list[float]]:
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        embeddings.extend(
            _embed_texts(
                client=client,
                texts=batch,
                model=model,
                dimensions=dimensions,
            )
        )
    return embeddings


def _build_library_record(
    *,
    backend: str,
    documents: list[DocumentRecord],
    embedded: bool,
    chunk_counts_by_document: dict[str, int] | None = None,
    total_chunks: int | None = None,
) -> DocumentLibraryRecord:
    chunk_counts = chunk_counts_by_document or {}
    folder_counts = Counter(document.folder for document in documents)
    folders = [
        FolderRecord(
            folder_id=folder_id,
            display_name=folder_id,
            document_count=folder_counts[folder_id],
        )
        for folder_id in sorted(folder_counts)
    ]
    document_records = [
        DocumentListRecord(
            document_id=document.document_id,
            title=document.title,
            category=document.category,
            folder=document.folder,
            tags=document.tags,
            summary=document.summary,
            source_url=document.source_url,
            updated_at=document.updated_at,
            chunk_count=chunk_counts.get(document.document_id),
            embedded=embedded,
        )
        for document in sorted(documents, key=lambda item: (item.folder.lower(), item.title.lower()))
    ]
    return DocumentLibraryRecord(
        backend=backend,
        total_documents=len(documents),
        total_chunks=total_chunks,
        folders=folders,
        documents=document_records,
    )


class JsonDocumentStore(BaseDocumentStore):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._loaded_mtime: float | None = None
        self._documents: list[DocumentRecord] = []
        self._by_id: dict[str, DocumentRecord] = {}

    def _ensure_loaded(self) -> None:
        stat = self._path.stat()
        if self._loaded_mtime is not None and math.isclose(stat.st_mtime, self._loaded_mtime):
            return

        documents = _load_json_documents_from_path(self._path)
        self._documents = documents
        self._by_id = {document.document_id: document for document in documents}
        self._loaded_mtime = stat.st_mtime

    def invalidate_cache(self) -> None:
        self._loaded_mtime = None
        self._documents = []
        self._by_id = {}

    def search_documents(
        self,
        query: str,
        limit: int = 5,
        context: RetrievalContext | None = None,
        search_profile: str = "search",
    ) -> list[SearchHit]:
        self._ensure_loaded()
        active_context = context or RetrievalContext()
        tokens = _tokenize(query)
        hits: list[SearchHit] = []

        for document in self._documents:
            if not active_context.allows_document(document):
                continue

            title_tokens = _tokenize(document.title)
            summary_tokens = _tokenize(document.summary)
            text_tokens = _tokenize(document.text)
            tag_tokens = _tokenize(" ".join(document.tags))
            category_tokens = _tokenize(document.category)
            folder_tokens = _tokenize(document.folder)

            score = 0.0
            for token in tokens:
                score += 5.0 * title_tokens.count(token)
                score += 3.0 * summary_tokens.count(token)
                score += 1.0 * text_tokens.count(token)
                score += 2.0 * tag_tokens.count(token)
                score += 2.0 * category_tokens.count(token)
                score += 2.0 * folder_tokens.count(token)

            if query.lower() in document.text.lower():
                score += 6.0
            if query.lower() in document.title.lower():
                score += 8.0

            if score <= 0:
                continue

            hits.append(
                SearchHit(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    folder=document.folder,
                    summary=document.summary,
                    excerpt=_build_excerpt(document.text, tokens),
                    score=score,
                    source_url=document.source_url,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    def get_document(
        self,
        document_id: str,
        context: RetrievalContext | None = None,
    ) -> DocumentRecord | None:
        self._ensure_loaded()
        document = self._by_id.get(document_id)
        active_context = context or RetrievalContext()
        if document is None or not active_context.allows_document(document):
            return None
        return document

    def list_documents(self) -> DocumentLibraryRecord:
        self._ensure_loaded()
        return _build_library_record(
            backend="json",
            documents=self._documents,
            embedded=False,
            total_chunks=None,
        )


class SemanticDocumentStore(BaseDocumentStore):
    def __init__(
        self,
        index_path: Path,
        openai_api_key: str | None,
        search_embedding_model: str,
        search_embedding_dimensions: int | None,
        answer_embedding_model: str,
        answer_embedding_dimensions: int | None,
    ) -> None:
        self._index_path = index_path
        self._openai_api_key = openai_api_key
        self._search_embedding_model = search_embedding_model
        self._search_embedding_dimensions = search_embedding_dimensions
        self._answer_embedding_model = answer_embedding_model
        self._answer_embedding_dimensions = answer_embedding_dimensions
        self._client: OpenAI | None = None
        self._loaded_mtime: float | None = None
        self._documents_by_id: dict[str, DocumentRecord] = {}
        self._chunks: list[SemanticChunk] = []
        self._chunk_counts_by_document: dict[str, int] = {}

    @property
    def client(self) -> OpenAI:
        if not self._openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for semantic retrieval.")
        if self._client is None:
            self._client = OpenAI(api_key=self._openai_api_key)
        return self._client

    def _ensure_loaded(self) -> None:
        if not self._index_path.exists():
            raise RuntimeError(
                "Semantic index not found. Run `python -m app.build_semantic_index` first."
            )

        stat = self._index_path.stat()
        if self._loaded_mtime is not None and math.isclose(stat.st_mtime, self._loaded_mtime):
            return

        documents_by_id: dict[str, DocumentRecord] = {}
        chunks: list[SemanticChunk] = []
        chunk_counts: dict[str, int] = {}

        try:
            with closing(sqlite3.connect(self._index_path)) as connection:
                connection.row_factory = sqlite3.Row

                for row in connection.execute(
                    """
                    SELECT document_id, title, category, folder, tags_json, summary, text, source_url, updated_at
                    FROM documents
                    """
                ):
                    tags, auto_tags = build_effective_document_tags(
                        json.loads(row["tags_json"]),
                        row["folder"],
                    )
                    document = DocumentRecord(
                        document_id=row["document_id"],
                        title=row["title"],
                        category=row["category"],
                        folder=row["folder"],
                        tags=tags,
                        summary=row["summary"],
                        text=row["text"],
                        source_url=row["source_url"],
                        updated_at=row["updated_at"],
                        auto_tags=auto_tags,
                    )
                    documents_by_id[document.document_id] = document

                for row in connection.execute(
                    """
                    SELECT
                        chunk_id,
                        document_id,
                        chunk_index,
                        chunk_text,
                        search_embedding_json,
                        search_embedding_norm,
                        answer_embedding_json,
                        answer_embedding_norm
                    FROM chunks
                    ORDER BY document_id, chunk_index
                    """
                ):
                    chunks.append(
                        SemanticChunk(
                            chunk_id=row["chunk_id"],
                            document_id=row["document_id"],
                            chunk_index=row["chunk_index"],
                            text=row["chunk_text"],
                            search_embedding=json.loads(row["search_embedding_json"]),
                            search_embedding_norm=float(row["search_embedding_norm"]),
                            answer_embedding=json.loads(row["answer_embedding_json"]),
                            answer_embedding_norm=float(row["answer_embedding_norm"]),
                        )
                    )
                    chunk_counts[row["document_id"]] = chunk_counts.get(row["document_id"], 0) + 1
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "Semantic index schema is outdated. Run `python -m app.build_semantic_index` to rebuild it."
            ) from exc

        self._documents_by_id = documents_by_id
        self._chunks = chunks
        self._chunk_counts_by_document = chunk_counts
        self._loaded_mtime = stat.st_mtime

    def invalidate_cache(self) -> None:
        self._loaded_mtime = None
        self._documents_by_id = {}
        self._chunks = []
        self._chunk_counts_by_document = {}

    def search_documents(
        self,
        query: str,
        limit: int = 5,
        context: RetrievalContext | None = None,
        search_profile: str = "search",
    ) -> list[SearchHit]:
        self._ensure_loaded()
        if not query.strip():
            return []

        active_context = context or RetrievalContext()
        query_embedding = _embed_texts(
            client=self.client,
            texts=[query],
            model=self._answer_embedding_model if search_profile == "answer" else self._search_embedding_model,
            dimensions=(
                self._answer_embedding_dimensions
                if search_profile == "answer"
                else self._search_embedding_dimensions
            ),
        )[0]
        query_norm = _vector_norm(query_embedding)
        query_tokens = _tokenize(query)
        best_by_document: dict[str, tuple[SemanticChunk, float]] = {}

        for chunk in self._chunks:
            document = self._documents_by_id[chunk.document_id]
            if not active_context.allows_document(document):
                continue

            chunk_embedding = chunk.answer_embedding if search_profile == "answer" else chunk.search_embedding
            chunk_embedding_norm = (
                chunk.answer_embedding_norm
                if search_profile == "answer"
                else chunk.search_embedding_norm
            )
            score = _cosine_similarity(query_embedding, query_norm, chunk_embedding, chunk_embedding_norm)
            current = best_by_document.get(chunk.document_id)
            if current is None or score > current[1]:
                best_by_document[chunk.document_id] = (chunk, score)

        ranked_hits = sorted(best_by_document.values(), key=lambda item: item[1], reverse=True)
        hits: list[SearchHit] = []

        for chunk, score in ranked_hits[:limit]:
            document = self._documents_by_id[chunk.document_id]
            hits.append(
                SearchHit(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    folder=document.folder,
                    summary=document.summary,
                    excerpt=_build_excerpt(chunk.text, query_tokens),
                    score=score,
                    source_url=document.source_url,
                )
            )

        return hits

    def get_document(
        self,
        document_id: str,
        context: RetrievalContext | None = None,
    ) -> DocumentRecord | None:
        self._ensure_loaded()
        document = self._documents_by_id.get(document_id)
        active_context = context or RetrievalContext()
        if document is None or not active_context.allows_document(document):
            return None
        return document

    def list_documents(self) -> DocumentLibraryRecord:
        self._ensure_loaded()
        return _build_library_record(
            backend="semantic",
            documents=list(self._documents_by_id.values()),
            embedded=True,
            chunk_counts_by_document=self._chunk_counts_by_document,
            total_chunks=len(self._chunks),
        )


class HttpDocumentStore(BaseDocumentStore):
    def __init__(self, base_url: str, api_key: str | None, timeout_seconds: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def search_documents(
        self,
        query: str,
        limit: int = 5,
        context: RetrievalContext | None = None,
        search_profile: str = "search",
    ) -> list[SearchHit]:
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if search_profile != "search":
            payload["search_profile"] = search_profile
        if context and context.is_active:
            payload["context_filter"] = {
                "folder_ids": sorted(context.folder_ids),
                "document_ids": sorted(context.document_ids),
            }

        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._base_url}/documents/search",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        items = data["results"] if isinstance(data, dict) and "results" in data else data
        return [
            SearchHit(
                document_id=item["document_id"],
                title=item["title"],
                category=item["category"],
                folder=item.get("folder", item["category"]),
                summary=item.get("summary", ""),
                excerpt=item.get("excerpt", item.get("summary", "")),
                score=float(item.get("score", 0.0)),
                source_url=item.get("source_url"),
            )
            for item in items
        ]

    def get_document(
        self,
        document_id: str,
        context: RetrievalContext | None = None,
    ) -> DocumentRecord | None:
        params: dict[str, str] = {}
        if context and context.is_active:
            if context.folder_ids:
                params["folder_ids"] = ",".join(sorted(context.folder_ids))
            if context.document_ids:
                params["document_ids"] = ",".join(sorted(context.document_ids))

        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.get(
                f"{self._base_url}/documents/{document_id}",
                headers=self._headers(),
                params=params,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            item = response.json()

        return DocumentRecord(
            document_id=item["document_id"],
            title=item["title"],
            category=item["category"],
            folder=item.get("folder", item["category"]),
            tags=build_effective_document_tags(
                item.get("tags", []),
                item.get("folder", item["category"]),
            )[0],
            summary=item.get("summary", ""),
            text=item["text"],
            source_url=item.get("source_url"),
            updated_at=item.get("updated_at"),
            auto_tags=build_effective_document_tags(
                item.get("tags", []),
                item.get("folder", item["category"]),
            )[1],
        )

    def list_documents(self) -> DocumentLibraryRecord:
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.get(
                f"{self._base_url}/documents",
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()

        folders = [
            FolderRecord(
                folder_id=item["folder_id"],
                display_name=item.get("display_name", item["folder_id"]),
                document_count=int(item["document_count"]),
            )
            for item in payload.get("folders", [])
        ]
        documents = [
            DocumentListRecord(
                document_id=item["document_id"],
                title=item["title"],
                category=item["category"],
                folder=item.get("folder", item["category"]),
                tags=item.get("tags", []),
                summary=item.get("summary", ""),
                source_url=item.get("source_url"),
                updated_at=item.get("updated_at"),
                chunk_count=item.get("chunk_count"),
                embedded=bool(item.get("embedded", True)),
            )
            for item in payload.get("documents", [])
        ]
        return DocumentLibraryRecord(
            backend=payload.get("backend", "http"),
            total_documents=int(payload.get("total_documents", len(documents))),
            total_chunks=payload.get("total_chunks"),
            folders=folders,
            documents=documents,
        )


def rebuild_semantic_index(
    *,
    source_path: Path,
    index_path: Path,
    openai_api_key: str | None,
    search_embedding_model: str,
    search_embedding_dimensions: int | None,
    answer_embedding_model: str,
    answer_embedding_dimensions: int | None,
    chunk_size_words: int,
    chunk_overlap_words: int,
    batch_size: int,
) -> dict[str, Any]:
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required to build the semantic index.")

    documents = _load_json_documents_from_path(source_path)
    client = OpenAI(api_key=openai_api_key)
    document_rows: list[DocumentRecord] = []
    chunk_rows: list[tuple[str, str, int, str]] = []
    embedding_inputs: list[str] = []

    for document in documents:
        content = document.text.strip() or document.summary.strip() or document.title.strip()
        chunks = _chunk_text(content, chunk_size_words, chunk_overlap_words) or [content]
        document_rows.append(document)

        for chunk_index, chunk_text in enumerate(chunks):
            chunk_id = f"{document.document_id}:{chunk_index}"
            chunk_rows.append((chunk_id, document.document_id, chunk_index, chunk_text))
            embedding_inputs.append(_prepare_embedding_input(document, chunk_text))

    search_embeddings = _embed_texts_in_batches(
        client=client,
        texts=embedding_inputs,
        model=search_embedding_model,
        dimensions=search_embedding_dimensions,
        batch_size=batch_size,
    )
    answer_embeddings = _embed_texts_in_batches(
        client=client,
        texts=embedding_inputs,
        model=answer_embedding_model,
        dimensions=answer_embedding_dimensions,
        batch_size=batch_size,
    )

    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_index_path = index_path.with_name(
        f"{index_path.stem}-{uuid4().hex}{index_path.suffix}.tmp"
    )

    try:
        with closing(sqlite3.connect(temporary_index_path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_url TEXT,
                    updated_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    search_embedding_json TEXT NOT NULL,
                    search_embedding_norm REAL NOT NULL,
                    answer_embedding_json TEXT NOT NULL,
                    answer_embedding_norm REAL NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                )
                """
            )
            connection.execute("CREATE INDEX idx_chunks_document_id ON chunks(document_id)")
            connection.execute(
                """
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            connection.executemany(
                """
                INSERT INTO documents (
                    document_id, title, category, folder, tags_json, summary, text, source_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        document.document_id,
                        document.title,
                        document.category,
                        document.folder,
                        json.dumps(document.tags, ensure_ascii=True),
                        document.summary,
                        document.text,
                        document.source_url,
                        document.updated_at,
                    )
                    for document in document_rows
                ],
            )

            connection.executemany(
                """
                INSERT INTO chunks (
                    chunk_id,
                    document_id,
                    chunk_index,
                    chunk_text,
                    search_embedding_json,
                    search_embedding_norm,
                    answer_embedding_json,
                    answer_embedding_norm
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk_id,
                        document_id,
                        chunk_index,
                        chunk_text,
                        json.dumps(search_embedding, ensure_ascii=True),
                        _vector_norm(search_embedding),
                        json.dumps(answer_embedding, ensure_ascii=True),
                        _vector_norm(answer_embedding),
                    )
                    for (
                        chunk_id,
                        document_id,
                        chunk_index,
                        chunk_text,
                    ), search_embedding, answer_embedding in zip(chunk_rows, search_embeddings, answer_embeddings)
                ],
            )

            metadata_items = {
                "search_embedding_model": search_embedding_model,
                "search_embedding_dimensions": "" if search_embedding_dimensions is None else str(search_embedding_dimensions),
                "answer_embedding_model": answer_embedding_model,
                "answer_embedding_dimensions": "" if answer_embedding_dimensions is None else str(answer_embedding_dimensions),
                "chunk_size_words": str(chunk_size_words),
                "chunk_overlap_words": str(chunk_overlap_words),
                "source_path": str(source_path),
                "documents_indexed": str(len(document_rows)),
                "chunks_indexed": str(len(chunk_rows)),
            }
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                list(metadata_items.items()),
            )
            connection.commit()
    except Exception:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise

    try:
        temporary_index_path.replace(index_path)
    except PermissionError as exc:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise RuntimeError(
            f"Semantic index file is locked: {index_path}. Stop any running app process and rebuild the index."
        ) from exc

    return {
        "documents_indexed": len(document_rows),
        "chunks_indexed": len(chunk_rows),
        "embedding_model": search_embedding_model,
        "embedding_dimensions": search_embedding_dimensions,
        "search_embedding_model": search_embedding_model,
        "search_embedding_dimensions": search_embedding_dimensions,
        "answer_embedding_model": answer_embedding_model,
        "answer_embedding_dimensions": answer_embedding_dimensions,
        "index_path": str(index_path),
    }


def sync_semantic_index(
    *,
    source_path: Path,
    index_path: Path,
    openai_api_key: str | None,
    search_embedding_model: str,
    search_embedding_dimensions: int | None,
    answer_embedding_model: str,
    answer_embedding_dimensions: int | None,
    chunk_size_words: int,
    chunk_overlap_words: int,
    batch_size: int,
) -> dict[str, Any]:
    if not index_path.exists():
        result = rebuild_semantic_index(
            source_path=source_path,
            index_path=index_path,
            openai_api_key=openai_api_key,
            search_embedding_model=search_embedding_model,
            search_embedding_dimensions=search_embedding_dimensions,
            answer_embedding_model=answer_embedding_model,
            answer_embedding_dimensions=answer_embedding_dimensions,
            chunk_size_words=chunk_size_words,
            chunk_overlap_words=chunk_overlap_words,
            batch_size=batch_size,
        )
        result["full_rebuild"] = True
        result["embedded_documents"] = result["documents_indexed"]
        result["reused_documents"] = 0
        result["removed_documents"] = 0
        result["changed"] = True
        return result

    documents = _load_json_documents_from_path(source_path)
    desired_by_id = {document.document_id: document for document in documents}

    try:
        with closing(sqlite3.connect(index_path)) as connection:
            connection.row_factory = sqlite3.Row
            metadata_rows = connection.execute("SELECT key, value FROM metadata").fetchall()
            existing_metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
            required_metadata = {
                "search_embedding_model": search_embedding_model,
                "search_embedding_dimensions": "" if search_embedding_dimensions is None else str(search_embedding_dimensions),
                "answer_embedding_model": answer_embedding_model,
                "answer_embedding_dimensions": "" if answer_embedding_dimensions is None else str(answer_embedding_dimensions),
                "chunk_size_words": str(chunk_size_words),
                "chunk_overlap_words": str(chunk_overlap_words),
                "source_path": str(source_path),
            }
            if any(existing_metadata.get(key) != value for key, value in required_metadata.items()):
                raise RuntimeError("Semantic index configuration changed; a full rebuild is required.")

            existing_rows = connection.execute(
                """
                SELECT document_id, title, category, folder, tags_json, summary, text, source_url, updated_at
                FROM documents
                """
            ).fetchall()
    except (sqlite3.DatabaseError, RuntimeError):
        result = rebuild_semantic_index(
            source_path=source_path,
            index_path=index_path,
            openai_api_key=openai_api_key,
            search_embedding_model=search_embedding_model,
            search_embedding_dimensions=search_embedding_dimensions,
            answer_embedding_model=answer_embedding_model,
            answer_embedding_dimensions=answer_embedding_dimensions,
            chunk_size_words=chunk_size_words,
            chunk_overlap_words=chunk_overlap_words,
            batch_size=batch_size,
        )
        result["full_rebuild"] = True
        result["embedded_documents"] = result["documents_indexed"]
        result["reused_documents"] = 0
        result["removed_documents"] = 0
        result["changed"] = True
        return result

    existing_by_id = {
        str(row["document_id"]): DocumentRecord(
            document_id=row["document_id"],
            title=row["title"],
            category=row["category"],
            folder=row["folder"],
            tags=build_effective_document_tags(
                json.loads(row["tags_json"]),
                row["folder"],
            )[0],
            summary=row["summary"],
            text=row["text"],
            source_url=row["source_url"],
            updated_at=row["updated_at"],
            auto_tags=build_effective_document_tags(
                json.loads(row["tags_json"]),
                row["folder"],
            )[1],
        )
        for row in existing_rows
    }

    removed_document_ids = sorted(set(existing_by_id).difference(desired_by_id))
    changed_documents = [
        document
        for document in documents
        if not _documents_match(existing_by_id.get(document.document_id), document)
    ]
    changed_document_ids = [document.document_id for document in changed_documents]
    reused_document_count = len(documents) - len(changed_documents)

    if not removed_document_ids and not changed_document_ids:
        with closing(sqlite3.connect(index_path)) as connection:
            total_chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        return {
            "documents_indexed": len(documents),
            "chunks_indexed": total_chunks,
            "embedding_model": search_embedding_model,
            "embedding_dimensions": search_embedding_dimensions,
            "search_embedding_model": search_embedding_model,
            "search_embedding_dimensions": search_embedding_dimensions,
            "answer_embedding_model": answer_embedding_model,
            "answer_embedding_dimensions": answer_embedding_dimensions,
            "index_path": str(index_path),
            "full_rebuild": False,
            "embedded_documents": 0,
            "reused_documents": reused_document_count,
            "removed_documents": 0,
            "changed": False,
        }

    embedding_inputs: list[str] = []
    chunk_rows: list[tuple[str, str, int, str]] = []
    for document in changed_documents:
        for chunk_index, chunk_text in enumerate(
            _document_chunk_texts(
                document,
                chunk_size_words=chunk_size_words,
                chunk_overlap_words=chunk_overlap_words,
            )
        ):
            chunk_id = f"{document.document_id}:{chunk_index}"
            chunk_rows.append((chunk_id, document.document_id, chunk_index, chunk_text))
            embedding_inputs.append(_prepare_embedding_input(document, chunk_text))

    search_embeddings: list[list[float]] = []
    answer_embeddings: list[list[float]] = []
    if embedding_inputs:
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required to build the semantic index.")
        client = OpenAI(api_key=openai_api_key)
        search_embeddings = _embed_texts_in_batches(
            client=client,
            texts=embedding_inputs,
            model=search_embedding_model,
            dimensions=search_embedding_dimensions,
            batch_size=batch_size,
        )
        answer_embeddings = _embed_texts_in_batches(
            client=client,
            texts=embedding_inputs,
            model=answer_embedding_model,
            dimensions=answer_embedding_dimensions,
            batch_size=batch_size,
        )

    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_index_path = index_path.with_name(
        f"{index_path.stem}-{uuid4().hex}{index_path.suffix}.tmp"
    )
    shutil.copy2(index_path, temporary_index_path)

    try:
        with closing(sqlite3.connect(temporary_index_path)) as connection, connection:
            if removed_document_ids:
                connection.executemany(
                    "DELETE FROM chunks WHERE document_id = ?",
                    [(document_id,) for document_id in removed_document_ids],
                )
                connection.executemany(
                    "DELETE FROM documents WHERE document_id = ?",
                    [(document_id,) for document_id in removed_document_ids],
                )

            if changed_document_ids:
                connection.executemany(
                    "DELETE FROM chunks WHERE document_id = ?",
                    [(document_id,) for document_id in changed_document_ids],
                )
                connection.executemany(
                    "DELETE FROM documents WHERE document_id = ?",
                    [(document_id,) for document_id in changed_document_ids],
                )
                connection.executemany(
                    """
                    INSERT INTO documents (
                        document_id, title, category, folder, tags_json, summary, text, source_url, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document.document_id,
                            document.title,
                            document.category,
                            document.folder,
                            json.dumps(document.tags, ensure_ascii=True),
                            document.summary,
                            document.text,
                            document.source_url,
                            document.updated_at,
                        )
                        for document in changed_documents
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id,
                        document_id,
                        chunk_index,
                        chunk_text,
                        search_embedding_json,
                        search_embedding_norm,
                        answer_embedding_json,
                        answer_embedding_norm
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk_id,
                            document_id,
                            chunk_index,
                            chunk_text,
                            json.dumps(search_embedding, ensure_ascii=True),
                            _vector_norm(search_embedding),
                            json.dumps(answer_embedding, ensure_ascii=True),
                            _vector_norm(answer_embedding),
                        )
                        for (
                            chunk_id,
                            document_id,
                            chunk_index,
                            chunk_text,
                        ), search_embedding, answer_embedding in zip(chunk_rows, search_embeddings, answer_embeddings)
                    ],
                )

            total_chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            metadata_items = {
                "search_embedding_model": search_embedding_model,
                "search_embedding_dimensions": "" if search_embedding_dimensions is None else str(search_embedding_dimensions),
                "answer_embedding_model": answer_embedding_model,
                "answer_embedding_dimensions": "" if answer_embedding_dimensions is None else str(answer_embedding_dimensions),
                "chunk_size_words": str(chunk_size_words),
                "chunk_overlap_words": str(chunk_overlap_words),
                "source_path": str(source_path),
                "documents_indexed": str(len(documents)),
                "chunks_indexed": str(total_chunks),
            }
            connection.execute("DELETE FROM metadata")
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                list(metadata_items.items()),
            )
            connection.commit()
    except Exception:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise

    try:
        temporary_index_path.replace(index_path)
    except PermissionError as exc:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise RuntimeError(
            f"Semantic index file is locked: {index_path}. Stop any running app process and rebuild the index."
        ) from exc

    return {
        "documents_indexed": len(documents),
        "chunks_indexed": total_chunks,
        "embedding_model": search_embedding_model,
        "embedding_dimensions": search_embedding_dimensions,
        "search_embedding_model": search_embedding_model,
        "search_embedding_dimensions": search_embedding_dimensions,
        "answer_embedding_model": answer_embedding_model,
        "answer_embedding_dimensions": answer_embedding_dimensions,
        "index_path": str(index_path),
        "full_rebuild": False,
        "embedded_documents": len(changed_documents),
        "reused_documents": reused_document_count,
        "removed_documents": len(removed_document_ids),
        "changed": True,
    }


def delete_documents_from_semantic_index(
    *,
    index_path: Path,
    document_ids: list[str],
    progress_callback: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    normalized_ids = list(dict.fromkeys(str(item).strip() for item in document_ids if str(item).strip()))
    if not normalized_ids:
        return {
            "removed_documents": 0,
            "removed_chunks": 0,
            "documents_indexed": 0,
            "chunks_indexed": 0,
            "changed": False,
            "full_rebuild": False,
            "embedded_documents": 0,
        }
    if not index_path.exists():
        raise RuntimeError("Semantic index does not exist.")

    def report(phase: str, percent: int, detail: str) -> None:
        if progress_callback is not None:
            progress_callback(phase, percent, detail)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    report("opening_index", 35, "Opening the semantic index...")
    with closing(sqlite3.connect(index_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            report("removing_chunks", 50, "Removing document chunks...")
            before_chunks = connection.total_changes
            connection.executemany(
                "DELETE FROM chunks WHERE document_id = ?",
                [(document_id,) for document_id in normalized_ids],
            )
            removed_chunks = connection.total_changes - before_chunks

            report("removing_documents", 68, "Removing document records...")
            before_documents = connection.total_changes
            connection.executemany(
                "DELETE FROM documents WHERE document_id = ?",
                [(document_id,) for document_id in normalized_ids],
            )
            removed_documents = connection.total_changes - before_documents

            report("updating_metadata", 82, "Updating semantic index metadata...")
            document_count = int(
                connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )
            chunk_count = int(
                connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            )
            connection.executemany(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [
                    ("documents_indexed", str(document_count)),
                    ("chunks_indexed", str(chunk_count)),
                ],
            )
            report("committing", 92, "Committing database changes...")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "removed_documents": removed_documents,
        "removed_chunks": removed_chunks,
        "documents_indexed": document_count,
        "chunks_indexed": chunk_count,
        "changed": bool(removed_documents or removed_chunks),
        "full_rebuild": False,
        "embedded_documents": 0,
    }


def sync_semantic_metadata_only(
    *,
    index_path: Path,
    documents: list[DocumentRecord],
) -> dict[str, Any]:
    if not index_path.exists():
        raise RuntimeError("Semantic index not found.")

    desired_document_ids = {document.document_id for document in documents}

    try:
        with closing(sqlite3.connect(index_path)) as connection:
            connection.row_factory = sqlite3.Row
            existing_rows = connection.execute(
                """
                SELECT document_id
                FROM documents
                """
            ).fetchall()
            existing_document_ids = {
                str(row["document_id"])
                for row in existing_rows
            }
            if existing_document_ids != desired_document_ids:
                raise RuntimeError("Semantic index document set changed; full sync required.")
    except (sqlite3.DatabaseError, RuntimeError):
        raise

    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_index_path = index_path.with_name(
        f"{index_path.stem}-{uuid4().hex}{index_path.suffix}.tmp"
    )
    shutil.copy2(index_path, temporary_index_path)

    try:
        with closing(sqlite3.connect(temporary_index_path)) as connection, connection:
            connection.executemany(
                """
                UPDATE documents
                SET title = ?,
                    category = ?,
                    folder = ?,
                    tags_json = ?,
                    summary = ?,
                    text = ?,
                    source_url = ?,
                    updated_at = ?
                WHERE document_id = ?
                """,
                [
                    (
                        document.title,
                        document.category,
                        document.folder,
                        json.dumps(document.tags, ensure_ascii=True),
                        document.summary,
                        document.text,
                        document.source_url,
                        document.updated_at,
                        document.document_id,
                    )
                    for document in documents
                ],
            )
            total_chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            connection.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("documents_indexed", str(len(documents))),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("chunks_indexed", str(total_chunks)),
            )
            connection.commit()
    except Exception:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise

    try:
        temporary_index_path.replace(index_path)
    except PermissionError as exc:
        try:
            temporary_index_path.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise RuntimeError(
            f"Semantic index file is locked: {index_path}. Stop any running app process and rebuild the index."
        ) from exc

    return {
        "documents_indexed": len(documents),
        "chunks_indexed": total_chunks,
        "index_path": str(index_path),
        "full_rebuild": False,
        "embedded_documents": 0,
        "reused_documents": len(documents),
        "removed_documents": 0,
        "changed": True,
        "metadata_only": True,
    }


def build_document_store(settings: Settings) -> BaseDocumentStore:
    if settings.docstore_backend == "semantic":
        return SemanticDocumentStore(
            index_path=settings.semantic_index_path,
            openai_api_key=settings.openai_api_key,
            search_embedding_model=settings.semantic_search_embedding_model,
            search_embedding_dimensions=settings.semantic_search_embedding_dimensions,
            answer_embedding_model=settings.semantic_answer_embedding_model,
            answer_embedding_dimensions=settings.semantic_answer_embedding_dimensions,
        )
    if settings.docstore_backend == "json":
        return JsonDocumentStore(settings.docstore_json_path)
    if settings.docstore_backend == "http":
        return HttpDocumentStore(
            base_url=settings.docstore_base_url,
            api_key=settings.docstore_api_key,
            timeout_seconds=settings.docstore_timeout_seconds,
        )
    raise ValueError(f"Unsupported DOCSTORE_BACKEND: {settings.docstore_backend}")
