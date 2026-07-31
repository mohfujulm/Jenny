from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import io
from pathlib import Path
import hashlib
import json
import logging
import posixpath
import re
import sqlite3
import threading
import uuid
from typing import Any, Callable, Literal
from xml.etree import ElementTree as ET
import zipfile

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.config import Settings
from app.ocr import OcrProvider, create_ocr_provider
from app.pdf_vision import PdfVisionAnalyzer, PdfVisionPage
from app.datastore import (
    DocumentRecord,
    build_effective_document_tags,
    delete_documents_from_semantic_index,
    iter_folder_lineage,
    load_folder_registry,
    load_json_documents,
    normalize_tag_values,
    strip_folder_auto_tags,
    sync_semantic_metadata_only,
    sync_semantic_index,
    write_folder_registry,
    write_json_documents,
)


logger = logging.getLogger("app.pdf_ingestion")


TEXT_UPLOAD_SUFFIXES = {
    ".csv",
    ".htm",
    ".html",
    ".json",
    ".log",
    ".markdown",
    ".md",
    ".rst",
    ".text",
    ".txt",
}
WORD_UPLOAD_SUFFIXES = {
    ".docm",
    ".docx",
}
SPREADSHEET_UPLOAD_SUFFIXES = {
    ".xlsm",
    ".xlsx",
    ".xltm",
    ".xltx",
}
PDF_UPLOAD_SUFFIXES = {".pdf"}
SUPPORTED_UPLOAD_SUFFIXES = (
    TEXT_UPLOAD_SUFFIXES
    | WORD_UPLOAD_SUFFIXES
    | SPREADSHEET_UPLOAD_SUFFIXES
    | PDF_UPLOAD_SUFFIXES
)
STRUCTURED_JSON_FIELDS = {"title", "category", "summary", "text"}
WHITESPACE_RE = re.compile(r"\s+")
WORDPROCESSING_MAIN_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SPREADSHEET_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass
class UploadOutcome:
    uploaded_documents: list[DocumentRecord]
    semantic_index_rebuilt: bool
    message: str
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    ignored_count: int = 0
    failed_uploads: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SimilarDocumentConflictCandidate:
    document_id: str
    title: str
    folder: str
    updated_at: str | None


@dataclass(frozen=True)
class SimilarDocumentConflictItem:
    upload_key: str | None
    upload_name: str
    incoming_title: str
    existing_document_id: str
    existing_title: str
    existing_folder: str
    existing_updated_at: str | None
    match_count: int = 1
    candidates: list[SimilarDocumentConflictCandidate] | None = None


class SimilarDocumentConflictError(RuntimeError):
    def __init__(self, conflicts: list[SimilarDocumentConflictItem]) -> None:
        self.conflicts = conflicts
        super().__init__("Similar documents already exist in the library.")


UploadSimilarityPolicy = Literal["warn", "replace", "ignore"]


@dataclass
class DeleteOutcome:
    deleted_document_ids: list[str]
    semantic_index_rebuilt: bool
    message: str


@dataclass
class TagUpdateOutcome:
    updated_document: DocumentRecord
    semantic_index_rebuilt: bool
    message: str


@dataclass
class MetadataUpdateOutcome:
    updated_document: DocumentRecord
    semantic_index_rebuilt: bool
    message: str


@dataclass
class FolderCreateOutcome:
    folder_id: str
    parent_folder_id: str | None
    created: bool
    semantic_index_rebuilt: bool
    message: str


@dataclass
class FolderDeleteOutcome:
    folder_id: str
    deleted_document_ids: list[str]
    removed_folder_ids: list[str]
    semantic_index_rebuilt: bool
    message: str


@dataclass
class FolderMoveOutcome:
    folder_id: str
    moved_folder_id: str
    updated_document_ids: list[str]
    semantic_index_rebuilt: bool
    message: str


@dataclass
class FolderRenameOutcome:
    folder_id: str
    renamed_folder_id: str
    updated_document_ids: list[str]
    semantic_index_rebuilt: bool
    message: str


@dataclass(frozen=True)
class LibraryBackupPayload:
    document_payload: str
    folder_payload: str


class DocumentIngestionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._ocr_provider: OcrProvider | None = None
        self._pdf_vision_analyzer: PdfVisionAnalyzer | None = None

    def ingest_upload(
        self,
        *,
        filename: str,
        content_text: str | None = None,
        content_base64: str | None = None,
        client_path: str | None = None,
        client_modified_ms: int | None = None,
        similarity_policy: UploadSimilarityPolicy = "warn",
        similarity_target_document_id: str | None = None,
        source_url: str | None = None,
        upload_key_base: str | None = None,
        title: str | None = None,
        category: str | None = None,
        folder: str | None = None,
        tags: list[str] | None = None,
    ) -> UploadOutcome:
        if not filename.strip():
            raise ValueError("Uploaded file must have a filename.")

        self._require_local_mutation_backend("Upload")

        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_UPLOAD_SUFFIXES))
            raise ValueError(f"Unsupported file type `{suffix or 'unknown'}`. Supported types: {supported}")

        if self._settings.docstore_backend == "semantic" and not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required to upload and embed documents in semantic mode.")

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            existing_ids = {document.document_id for document in existing_documents}

            parsed_documents = self._parse_upload(
                filename=filename,
                content_text=content_text,
                content_base64=content_base64,
                existing_ids=existing_ids,
                client_path=client_path,
                client_modified_ms=client_modified_ms,
                source_url=source_url,
                upload_key_base=upload_key_base,
                title=title,
                category=category,
                folder=folder,
                tags=tags or [],
                source_auto_tags=[],
            )
            if not parsed_documents:
                raise ValueError("The uploaded file did not produce any documents.")

            merge_result = self._merge_uploaded_documents(
                existing_documents=existing_documents,
                parsed_documents=parsed_documents,
                default_similarity_policy=similarity_policy,
                similarity_policies_by_upload_key=self._build_similarity_policy_map(
                    parsed_documents=parsed_documents,
                    similarity_policy=similarity_policy,
                ),
                similarity_target_document_ids_by_upload_key=self._build_similarity_target_map(
                    parsed_documents=parsed_documents,
                    target_document_id=similarity_target_document_id,
                ),
            )
            if merge_result["changed"]:
                backup_payload = self._backup_payload()
                semantic_index_rebuilt = self._persist_documents(
                    documents=merge_result["documents"],
                    backup_payload=backup_payload,
                )
            else:
                semantic_index_rebuilt = False

        total_uploaded = len(parsed_documents)
        message = self._build_upload_message(
            total_uploaded=total_uploaded,
            created_count=int(merge_result["created_count"]),
            updated_count=int(merge_result["updated_count"]),
            unchanged_count=int(merge_result["unchanged_count"]),
            ignored_count=int(merge_result["ignored_count"]),
            semantic_index_rebuilt=semantic_index_rebuilt,
        )

        return UploadOutcome(
            uploaded_documents=merge_result["uploaded_documents"],
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
            created_count=int(merge_result["created_count"]),
            updated_count=int(merge_result["updated_count"]),
            unchanged_count=int(merge_result["unchanged_count"]),
            ignored_count=int(merge_result["ignored_count"]),
        )

    def ingest_upload_batch(
        self,
        *,
        uploads: list[dict[str, Any]],
        continue_on_error: bool = False,
    ) -> UploadOutcome:
        if not uploads:
            raise ValueError("Select at least one document to upload.")

        self._require_local_mutation_backend("Upload")

        if self._settings.docstore_backend == "semantic" and not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required to upload and embed documents in semantic mode.")

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            existing_ids = {document.document_id for document in existing_documents}
            parsed_documents: list[DocumentRecord] = []
            similarity_policies_by_upload_key: dict[str, UploadSimilarityPolicy] = {}
            similarity_target_document_ids_by_upload_key: dict[str, str] = {}
            failed_uploads: list[str] = []

            for upload in uploads:
                filename = str(upload.get("filename") or "").strip()
                if not filename:
                    if not continue_on_error:
                        raise ValueError("Uploaded file must have a filename.")
                    failed_uploads.append("Unnamed file: uploaded file must have a filename.")
                    continue

                try:
                    next_documents = self._parse_upload(
                        filename=filename,
                        content_text=upload.get("content_text"),
                        content_base64=upload.get("content_base64"),
                        existing_ids=existing_ids,
                        client_path=upload.get("client_path"),
                        client_modified_ms=upload.get("client_modified_ms"),
                        source_url=upload.get("source_url"),
                        upload_key_base=upload.get("upload_key_base"),
                        title=upload.get("title"),
                        category=upload.get("category"),
                        folder=upload.get("folder"),
                        tags=upload.get("tags") or [],
                        source_auto_tags=upload.get("source_auto_tags") or [],
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    if not continue_on_error:
                        raise
                    failed_uploads.append(f"{filename}: {exc}")
                    continue
                if not next_documents:
                    continue

                parsed_documents.extend(next_documents)
                existing_ids.update(document.document_id for document in next_documents)
                normalized_similarity_policy = self._normalize_similarity_policy(
                    upload.get("similarity_policy")
                )
                for document in next_documents:
                    if document.upload_key:
                        similarity_policies_by_upload_key[document.upload_key] = normalized_similarity_policy
                normalized_target_document_id = self._normalize_similarity_target_document_id(
                    upload.get("similarity_target_document_id")
                )
                if normalized_target_document_id:
                    for document in next_documents:
                        if document.upload_key:
                            similarity_target_document_ids_by_upload_key[document.upload_key] = normalized_target_document_id

            if not parsed_documents and not continue_on_error:
                raise ValueError("The selected files did not produce any documents.")

            if not parsed_documents:
                return UploadOutcome(
                    uploaded_documents=[],
                    semantic_index_rebuilt=False,
                    message="No documents were imported.",
                    failed_uploads=failed_uploads,
                )

            merge_result = self._merge_uploaded_documents(
                existing_documents=existing_documents,
                parsed_documents=parsed_documents,
                default_similarity_policy="warn",
                similarity_policies_by_upload_key=similarity_policies_by_upload_key,
                similarity_target_document_ids_by_upload_key=similarity_target_document_ids_by_upload_key,
            )
            if merge_result["changed"]:
                backup_payload = self._backup_payload()
                semantic_index_rebuilt = self._persist_documents(
                    documents=merge_result["documents"],
                    backup_payload=backup_payload,
                )
            else:
                semantic_index_rebuilt = False

        total_uploaded = len(parsed_documents)
        message = self._build_upload_message(
            total_uploaded=total_uploaded,
            created_count=int(merge_result["created_count"]),
            updated_count=int(merge_result["updated_count"]),
            unchanged_count=int(merge_result["unchanged_count"]),
            ignored_count=int(merge_result["ignored_count"]),
            semantic_index_rebuilt=semantic_index_rebuilt,
        )
        return UploadOutcome(
            uploaded_documents=merge_result["uploaded_documents"],
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
            created_count=int(merge_result["created_count"]),
            updated_count=int(merge_result["updated_count"]),
            unchanged_count=int(merge_result["unchanged_count"]),
            ignored_count=int(merge_result["ignored_count"]),
            failed_uploads=failed_uploads,
        )

    def delete_documents(
        self,
        *,
        document_ids: list[str],
        progress_callback: Callable[[str, int, str], None] | None = None,
    ) -> DeleteOutcome:
        self._require_local_mutation_backend("Delete")

        def report(phase: str, percent: int, detail: str) -> None:
            if progress_callback is not None:
                progress_callback(phase, percent, detail)

        report("validating", 5, "Validating selected documents...")
        normalized_document_ids = []
        seen_ids: set[str] = set()
        for item in document_ids:
            document_id = str(item).strip()
            if not document_id:
                continue
            if document_id in seen_ids:
                continue
            seen_ids.add(document_id)
            normalized_document_ids.append(document_id)

        if not normalized_document_ids:
            raise ValueError("Select at least one document to delete.")

        with self._lock:
            report("loading_library", 12, "Loading the current library...")
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            existing_ids = {document.document_id for document in existing_documents}
            missing_ids = [document_id for document_id in normalized_document_ids if document_id not in existing_ids]
            if missing_ids:
                missing_list = ", ".join(missing_ids[:8])
                if len(missing_ids) > 8:
                    missing_list += ", ..."
                raise ValueError(f"Document IDs not found: {missing_list}")

            backup_payload = self._backup_payload()
            delete_id_set = set(normalized_document_ids)
            remaining_documents = [
                document
                for document in existing_documents
                if document.document_id not in delete_id_set
            ]
            report("writing_library", 22, "Updating the library document file...")
            semantic_index_rebuilt = self._persist_documents(
                documents=remaining_documents,
                backup_payload=backup_payload,
                semantic_sync_mode="delete_only",
                deleted_document_ids=normalized_document_ids,
                progress_callback=progress_callback,
            )

        total_deleted = len(normalized_document_ids)
        noun = "document" if total_deleted == 1 else "documents"
        if semantic_index_rebuilt:
            message = f"Deleted {total_deleted} {noun} and rebuilt embeddings."
        elif self._settings.docstore_backend == "semantic":
            message = f"Deleted {total_deleted} {noun} and updated the semantic index."
        else:
            message = f"Deleted {total_deleted} {noun}."
        report("complete", 100, message)

        return DeleteOutcome(
            deleted_document_ids=normalized_document_ids,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def update_document_tags(
        self,
        *,
        document_id: str,
        tags: list[str] | None = None,
    ) -> TagUpdateOutcome:
        self._require_local_mutation_backend("Tag updates")

        normalized_document_id = str(document_id).strip()
        if not normalized_document_id:
            raise ValueError("Document ID is required to update tags.")

        normalized_tags = self._normalize_tags(tags or [])

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            target_index = next(
                (
                    index
                    for index, document in enumerate(existing_documents)
                    if document.document_id == normalized_document_id
                ),
                None,
            )
            if target_index is None:
                raise ValueError(f"Document ID `{normalized_document_id}` was not found.")

            current_document = existing_documents[target_index]
            normalized_tags = self._strip_document_auto_tags(
                normalized_tags,
                current_document,
            )
            next_tags, next_auto_tags = self._build_document_tags(
                tags=normalized_tags,
                folder=current_document.folder,
                source_auto_tags=self._source_auto_tags_from_document(current_document),
            )
            if current_document.tags == next_tags:
                return TagUpdateOutcome(
                    updated_document=current_document,
                    semantic_index_rebuilt=False,
                    message=f"Tags for {normalized_document_id} were already up to date.",
                )

            updated_document = DocumentRecord(
                document_id=current_document.document_id,
                title=current_document.title,
                category=current_document.category,
                folder=current_document.folder,
                tags=next_tags,
                summary=current_document.summary,
                text=current_document.text,
                source_url=current_document.source_url,
                updated_at=datetime.now(timezone.utc).date().isoformat(),
                upload_key=current_document.upload_key,
                content_hash=self._build_content_hash(
                    title=current_document.title,
                    category=current_document.category,
                    folder=current_document.folder,
                    tags=next_tags,
                    summary=current_document.summary,
                    text=current_document.text,
                    source_url=current_document.source_url,
                ),
                auto_tags=next_auto_tags,
            )
            updated_documents = list(existing_documents)
            updated_documents[target_index] = updated_document

            semantic_index_rebuilt = self._persist_documents(
                documents=updated_documents,
                backup_payload=self._backup_payload(),
            )

        message = (
            f"Updated tags for {normalized_document_id} and rebuilt embeddings."
            if semantic_index_rebuilt
            else f"Updated tags for {normalized_document_id}."
        )
        return TagUpdateOutcome(
            updated_document=updated_document,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def update_document_metadata(
        self,
        *,
        document_id: str,
        title: str,
        category: str,
        folder: str,
        tags: list[str] | None = None,
    ) -> MetadataUpdateOutcome:
        self._require_local_mutation_backend("Document updates")

        normalized_document_id = str(document_id).strip()
        if not normalized_document_id:
            raise ValueError("Document ID is required to update library metadata.")

        normalized_title = self._require_text(title, field_name="Title")
        normalized_category = self._require_text(category, field_name="Category")
        normalized_folder = self._normalize_folder(folder, fallback=normalized_category)

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            target_index = next(
                (
                    index
                    for index, document in enumerate(existing_documents)
                    if document.document_id == normalized_document_id
                ),
                None,
            )
            if target_index is None:
                raise ValueError(f"Document ID `{normalized_document_id}` was not found.")

            current_document = existing_documents[target_index]
            normalized_tags = self._strip_document_auto_tags(
                tags or [],
                current_document,
            )
            next_tags, next_auto_tags = self._build_document_tags(
                tags=normalized_tags,
                folder=normalized_folder,
                source_auto_tags=self._source_auto_tags_from_document(current_document),
            )
            if (
                current_document.title == normalized_title
                and current_document.category == normalized_category
                and self._normalize_folder(current_document.folder, fallback=current_document.category) == normalized_folder
                and current_document.tags == next_tags
            ):
                return MetadataUpdateOutcome(
                    updated_document=current_document,
                    semantic_index_rebuilt=False,
                    message=f"Metadata for {normalized_document_id} was already up to date.",
                )

            updated_document = DocumentRecord(
                document_id=current_document.document_id,
                title=normalized_title,
                category=normalized_category,
                folder=normalized_folder,
                tags=next_tags,
                summary=current_document.summary,
                text=current_document.text,
                source_url=current_document.source_url,
                updated_at=datetime.now(timezone.utc).date().isoformat(),
                upload_key=current_document.upload_key,
                content_hash=self._build_content_hash(
                    title=normalized_title,
                    category=normalized_category,
                    folder=normalized_folder,
                    tags=next_tags,
                    summary=current_document.summary,
                    text=current_document.text,
                    source_url=current_document.source_url,
                ),
                auto_tags=next_auto_tags,
            )
            updated_documents = list(existing_documents)
            updated_documents[target_index] = updated_document
            metadata_only_folder_move = (
                current_document.title == normalized_title
                and current_document.category == normalized_category
                and self._manual_tags_from_document(current_document) == normalized_tags
                and self._normalize_folder(current_document.folder, fallback=current_document.category) != normalized_folder
            )

            semantic_index_rebuilt = self._persist_documents(
                documents=updated_documents,
                backup_payload=self._backup_payload(),
                semantic_sync_mode="metadata_only" if metadata_only_folder_move else "full",
            )

        message = (
            f"Updated metadata for {normalized_document_id} and rebuilt embeddings."
            if semantic_index_rebuilt
            else f"Updated metadata for {normalized_document_id}."
        )
        return MetadataUpdateOutcome(
            updated_document=updated_document,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def rename_folder(
        self,
        *,
        folder_id: str,
        new_name: str,
    ) -> FolderRenameOutcome:
        self._require_local_mutation_backend("Folder rename")

        normalized_folder_id = self._normalize_folder(folder_id, fallback="")
        if not normalized_folder_id:
            raise ValueError("Folder ID is required to rename a folder.")

        normalized_new_name = self._normalize_folder_name(new_name)
        renamed_folder_id = self._build_renamed_folder_id(
            folder_id=normalized_folder_id,
            new_name=normalized_new_name,
        )
        if renamed_folder_id == normalized_folder_id:
            return FolderRenameOutcome(
                folder_id=normalized_folder_id,
                renamed_folder_id=renamed_folder_id,
                updated_document_ids=[],
                semantic_index_rebuilt=False,
                message=f"Folder {normalized_folder_id} was already named {normalized_new_name}.",
            )

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            registered_folder_ids = self._load_registered_folder_ids()
            existing_folder_ids = self._collect_existing_folder_ids(
                existing_documents,
                registered_folder_ids,
            )
            if normalized_folder_id not in existing_folder_ids:
                raise ValueError(f"Folder `{normalized_folder_id}` was not found.")

            subtree_folder_ids = {
                current_folder_id
                for current_folder_id in existing_folder_ids
                if self._folder_is_within_scope(current_folder_id, normalized_folder_id)
            }
            renamed_subtree_folder_ids = {
                self._replace_folder_prefix(
                    current_folder_id,
                    old_prefix=normalized_folder_id,
                    new_prefix=renamed_folder_id,
                )
                for current_folder_id in subtree_folder_ids
            }
            conflicting_folder_ids = sorted(
                renamed_subtree_folder_ids.intersection(existing_folder_ids.difference(subtree_folder_ids))
            )
            if conflicting_folder_ids:
                raise ValueError(
                    f"Folder rename would collide with existing folder `{conflicting_folder_ids[0]}`."
                )

            matching_indexes = [
                index
                for index, document in enumerate(existing_documents)
                if self._folder_is_within_scope(document.folder, normalized_folder_id)
            ]

            updated_document_ids: list[str] = []
            updated_documents = list(existing_documents)
            for index in matching_indexes:
                current_document = existing_documents[index]
                next_folder = self._replace_folder_prefix(
                    current_document.folder,
                    old_prefix=normalized_folder_id,
                    new_prefix=renamed_folder_id,
                )
                next_tags, next_auto_tags = self._build_document_tags(
                    tags=self._manual_tags_from_document(current_document),
                    folder=next_folder,
                    source_auto_tags=self._source_auto_tags_from_document(current_document),
                )
                updated_documents[index] = DocumentRecord(
                    document_id=current_document.document_id,
                    title=current_document.title,
                    category=current_document.category,
                    folder=next_folder,
                    tags=next_tags,
                    summary=current_document.summary,
                    text=current_document.text,
                    source_url=current_document.source_url,
                    updated_at=datetime.now(timezone.utc).date().isoformat(),
                    upload_key=current_document.upload_key,
                    content_hash=self._build_content_hash(
                        title=current_document.title,
                        category=current_document.category,
                        folder=next_folder,
                        tags=next_tags,
                        summary=current_document.summary,
                        text=current_document.text,
                        source_url=current_document.source_url,
                    ),
                    auto_tags=next_auto_tags,
                )
                updated_document_ids.append(current_document.document_id)

            updated_registered_folder_ids = [
                self._replace_folder_prefix(
                    current_folder_id,
                    old_prefix=normalized_folder_id,
                    new_prefix=renamed_folder_id,
                )
                if self._folder_is_within_scope(current_folder_id, normalized_folder_id)
                else current_folder_id
                for current_folder_id in registered_folder_ids
            ]

            if matching_indexes:
                semantic_index_rebuilt = self._persist_documents(
                    documents=updated_documents,
                    backup_payload=self._backup_payload(),
                    folder_ids=updated_registered_folder_ids,
                    semantic_sync_mode="metadata_only",
                )
            else:
                self._persist_folder_registry(
                    folder_ids=updated_registered_folder_ids,
                    backup_payload=self._backup_payload(),
                )
                semantic_index_rebuilt = False

        message = (
            f"Renamed folder {normalized_folder_id} to {renamed_folder_id} and rebuilt embeddings."
            if semantic_index_rebuilt
            else f"Renamed folder {normalized_folder_id} to {renamed_folder_id}."
        )
        return FolderRenameOutcome(
            folder_id=normalized_folder_id,
            renamed_folder_id=renamed_folder_id,
            updated_document_ids=updated_document_ids,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def create_folder(
        self,
        *,
        folder_name: str,
        parent_folder_id: str | None = None,
    ) -> FolderCreateOutcome:
        self._require_local_mutation_backend("Folder creation")

        normalized_parent_folder_id = self._normalize_optional_folder(parent_folder_id)
        normalized_folder_name = self._normalize_folder_name(folder_name)
        created_folder_id = self._build_created_folder_id(
            folder_name=normalized_folder_name,
            parent_folder_id=normalized_parent_folder_id,
        )

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            registered_folder_ids = self._load_registered_folder_ids()
            existing_folder_ids = self._collect_existing_folder_ids(
                existing_documents,
                registered_folder_ids,
            )

            if normalized_parent_folder_id and normalized_parent_folder_id not in existing_folder_ids:
                raise ValueError(f"Parent folder `{normalized_parent_folder_id}` was not found.")
            if created_folder_id in existing_folder_ids:
                raise ValueError(f"Folder `{created_folder_id}` already exists.")

            updated_registered_folder_ids = [*registered_folder_ids, created_folder_id]
            self._persist_folder_registry(
                folder_ids=updated_registered_folder_ids,
                backup_payload=self._backup_payload(),
            )

        message = (
            f"Created folder {created_folder_id} inside {normalized_parent_folder_id}."
            if normalized_parent_folder_id
            else f"Created folder {created_folder_id}."
        )
        return FolderCreateOutcome(
            folder_id=created_folder_id,
            parent_folder_id=normalized_parent_folder_id,
            created=True,
            semantic_index_rebuilt=False,
            message=message,
        )

    def delete_folder(
        self,
        *,
        folder_id: str,
    ) -> FolderDeleteOutcome:
        self._require_local_mutation_backend("Folder deletion")

        normalized_folder_id = self._normalize_folder(folder_id, fallback="")
        if not normalized_folder_id:
            raise ValueError("Folder ID is required to delete a folder.")

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            registered_folder_ids = self._load_registered_folder_ids()
            existing_folder_ids = self._collect_existing_folder_ids(
                existing_documents,
                registered_folder_ids,
            )
            if normalized_folder_id not in existing_folder_ids:
                raise ValueError(f"Folder `{normalized_folder_id}` was not found.")

            deleted_document_ids = [
                document.document_id
                for document in existing_documents
                if self._folder_is_within_scope(document.folder, normalized_folder_id)
            ]
            remaining_documents = [
                document
                for document in existing_documents
                if not self._folder_is_within_scope(document.folder, normalized_folder_id)
            ]
            removed_folder_ids = sorted(
                [
                    current_folder_id
                    for current_folder_id in existing_folder_ids
                    if self._folder_is_within_scope(current_folder_id, normalized_folder_id)
                ]
            )
            updated_registered_folder_ids = [
                current_folder_id
                for current_folder_id in registered_folder_ids
                if not self._folder_is_within_scope(current_folder_id, normalized_folder_id)
            ]

            if deleted_document_ids:
                semantic_index_rebuilt = self._persist_documents(
                    documents=remaining_documents,
                    backup_payload=self._backup_payload(),
                    folder_ids=updated_registered_folder_ids,
                    semantic_sync_mode="delete_only",
                    deleted_document_ids=deleted_document_ids,
                )
            else:
                self._persist_folder_registry(
                    folder_ids=updated_registered_folder_ids,
                    backup_payload=self._backup_payload(),
                )
                semantic_index_rebuilt = False

        deleted_document_count = len(deleted_document_ids)
        if deleted_document_count:
            message = (
                f"Deleted folder {normalized_folder_id}, removed {deleted_document_count} document"
                f"{'' if deleted_document_count == 1 else 's'}, and rebuilt embeddings."
                if semantic_index_rebuilt
                else f"Deleted folder {normalized_folder_id} and removed {deleted_document_count} document"
                f"{'' if deleted_document_count == 1 else 's'}."
            )
        else:
            message = f"Deleted empty folder {normalized_folder_id}."

        return FolderDeleteOutcome(
            folder_id=normalized_folder_id,
            deleted_document_ids=deleted_document_ids,
            removed_folder_ids=removed_folder_ids,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def move_folder(
        self,
        *,
        folder_id: str,
        new_parent_folder_id: str | None = None,
    ) -> FolderMoveOutcome:
        self._require_local_mutation_backend("Folder move")

        normalized_folder_id = self._normalize_folder(folder_id, fallback="")
        if not normalized_folder_id:
            raise ValueError("Folder ID is required to move a folder.")

        normalized_parent_folder_id = self._normalize_optional_folder(new_parent_folder_id)
        current_folder_name = normalized_folder_id.split("/")[-1]
        moved_folder_id = self._build_created_folder_id(
            folder_name=current_folder_name,
            parent_folder_id=normalized_parent_folder_id,
        )
        if moved_folder_id == normalized_folder_id:
            return FolderMoveOutcome(
                folder_id=normalized_folder_id,
                moved_folder_id=moved_folder_id,
                updated_document_ids=[],
                semantic_index_rebuilt=False,
                message=f"Folder {normalized_folder_id} is already in that location.",
            )

        with self._lock:
            existing_documents = load_json_documents(self._settings.docstore_json_path)
            registered_folder_ids = self._load_registered_folder_ids()
            existing_folder_ids = self._collect_existing_folder_ids(
                existing_documents,
                registered_folder_ids,
            )
            if normalized_folder_id not in existing_folder_ids:
                raise ValueError(f"Folder `{normalized_folder_id}` was not found.")

            if normalized_parent_folder_id:
                if normalized_parent_folder_id not in existing_folder_ids:
                    raise ValueError(f"Parent folder `{normalized_parent_folder_id}` was not found.")
                if self._folder_is_within_scope(normalized_parent_folder_id, normalized_folder_id):
                    raise ValueError("A folder cannot be moved into itself or one of its descendants.")

            subtree_folder_ids = {
                current_folder_id
                for current_folder_id in existing_folder_ids
                if self._folder_is_within_scope(current_folder_id, normalized_folder_id)
            }
            moved_subtree_folder_ids = {
                self._replace_folder_prefix(
                    current_folder_id,
                    old_prefix=normalized_folder_id,
                    new_prefix=moved_folder_id,
                )
                for current_folder_id in subtree_folder_ids
            }
            conflicting_folder_ids = sorted(
                moved_subtree_folder_ids.intersection(existing_folder_ids.difference(subtree_folder_ids))
            )
            if conflicting_folder_ids:
                raise ValueError(
                    f"Folder move would collide with existing folder `{conflicting_folder_ids[0]}`."
                )

            matching_indexes = [
                index
                for index, document in enumerate(existing_documents)
                if self._folder_is_within_scope(document.folder, normalized_folder_id)
            ]

            updated_document_ids: list[str] = []
            updated_documents = list(existing_documents)
            for index in matching_indexes:
                current_document = existing_documents[index]
                next_folder = self._replace_folder_prefix(
                    current_document.folder,
                    old_prefix=normalized_folder_id,
                    new_prefix=moved_folder_id,
                )
                next_tags, next_auto_tags = self._build_document_tags(
                    tags=self._manual_tags_from_document(current_document),
                    folder=next_folder,
                    source_auto_tags=self._source_auto_tags_from_document(current_document),
                )
                updated_documents[index] = DocumentRecord(
                    document_id=current_document.document_id,
                    title=current_document.title,
                    category=current_document.category,
                    folder=next_folder,
                    tags=next_tags,
                    summary=current_document.summary,
                    text=current_document.text,
                    source_url=current_document.source_url,
                    updated_at=datetime.now(timezone.utc).date().isoformat(),
                    upload_key=current_document.upload_key,
                    content_hash=self._build_content_hash(
                        title=current_document.title,
                        category=current_document.category,
                        folder=next_folder,
                        tags=next_tags,
                        summary=current_document.summary,
                        text=current_document.text,
                        source_url=current_document.source_url,
                    ),
                    auto_tags=next_auto_tags,
                )
                updated_document_ids.append(current_document.document_id)

            updated_registered_folder_ids = [
                self._replace_folder_prefix(
                    current_folder_id,
                    old_prefix=normalized_folder_id,
                    new_prefix=moved_folder_id,
                )
                if self._folder_is_within_scope(current_folder_id, normalized_folder_id)
                else current_folder_id
                for current_folder_id in registered_folder_ids
            ]

            if matching_indexes:
                semantic_index_rebuilt = self._persist_documents(
                    documents=updated_documents,
                    backup_payload=self._backup_payload(),
                    folder_ids=updated_registered_folder_ids,
                    semantic_sync_mode="metadata_only",
                )
            else:
                self._persist_folder_registry(
                    folder_ids=updated_registered_folder_ids,
                    backup_payload=self._backup_payload(),
                )
                semantic_index_rebuilt = False

        message = (
            f"Moved folder {normalized_folder_id} to {moved_folder_id} and rebuilt embeddings."
            if semantic_index_rebuilt
            else f"Moved folder {normalized_folder_id} to {moved_folder_id}."
        )
        return FolderMoveOutcome(
            folder_id=normalized_folder_id,
            moved_folder_id=moved_folder_id,
            updated_document_ids=updated_document_ids,
            semantic_index_rebuilt=semantic_index_rebuilt,
            message=message,
        )

    def _parse_upload(
        self,
        *,
        filename: str,
        content_text: str | None,
        content_base64: str | None,
        existing_ids: set[str],
        client_path: str | None,
        client_modified_ms: int | None,
        source_url: str | None,
        upload_key_base: str | None,
        title: str | None,
        category: str | None,
        folder: str | None,
        tags: list[str],
        source_auto_tags: list[str],
    ) -> list[DocumentRecord]:
        suffix = Path(filename).suffix.lower()
        decoded_text = self._extract_upload_text(
            filename=filename,
            suffix=suffix,
            content_text=content_text,
            content_base64=content_base64,
        )
        if not decoded_text:
            raise ValueError("Uploaded file is empty.")

        if suffix == ".json":
            resolved_upload_key_base = upload_key_base or self._build_upload_key_base(
                filename=filename,
                client_path=client_path,
                client_modified_ms=client_modified_ms,
            )
            structured_documents = self._parse_structured_json(
                raw_text=decoded_text,
                existing_ids=existing_ids,
                upload_key_base=resolved_upload_key_base,
                source_auto_tags=source_auto_tags,
            )
            if structured_documents is not None:
                return structured_documents

        normalized_title = title.strip() if title and title.strip() else self._display_title_from_filename(filename)
        normalized_category = self._normalize_field(category, fallback="uploaded")
        normalized_folder = self._normalize_folder(folder, fallback=normalized_category)
        normalized_tags = self._normalize_tags(
            self._manual_tags_for_folder(
                raw_tags=tags,
                folder=normalized_folder,
            )
        )
        effective_tags, auto_tags = self._build_document_tags(
            tags=normalized_tags,
            folder=normalized_folder,
            source_auto_tags=source_auto_tags,
        )
        summary = self._summarize(decoded_text)
        upload_key = self._build_upload_key(
            filename=filename,
            client_path=client_path,
            client_modified_ms=client_modified_ms,
            upload_key_base=upload_key_base,
        )
        normalized_source_url = self._optional_text(source_url)

        document = DocumentRecord(
            document_id=self._build_document_id(existing_ids),
            title=normalized_title,
            category=normalized_category,
            folder=normalized_folder,
            tags=effective_tags,
            summary=summary,
            text=decoded_text,
            source_url=normalized_source_url,
            updated_at=datetime.now(timezone.utc).date().isoformat(),
            upload_key=upload_key,
            content_hash=self._build_content_hash(
                title=normalized_title,
                category=normalized_category,
                folder=normalized_folder,
                tags=effective_tags,
                summary=summary,
                text=decoded_text,
                source_url=normalized_source_url,
            ),
            auto_tags=auto_tags,
        )
        return [document]

    def _extract_upload_text(
        self,
        *,
        filename: str,
        suffix: str,
        content_text: str | None,
        content_base64: str | None,
    ) -> str:
        if suffix in WORD_UPLOAD_SUFFIXES:
            if content_text and content_text.strip():
                return content_text.lstrip("\ufeff").strip()
            return self._extract_word_document_text(
                filename=filename,
                content_bytes=self._decode_base64_content(content_base64, filename=filename),
            )

        if suffix in SPREADSHEET_UPLOAD_SUFFIXES:
            if content_text and content_text.strip():
                return content_text.lstrip("\ufeff").strip()
            return self._extract_spreadsheet_text(
                filename=filename,
                content_bytes=self._decode_base64_content(content_base64, filename=filename),
            )

        if suffix in PDF_UPLOAD_SUFFIXES:
            return self._extract_pdf_document_text(
                filename=filename,
                content_bytes=self._decode_base64_content(content_base64, filename=filename),
            )

        if content_text is None:
            raise ValueError(f"Uploaded file `{filename}` did not include text content.")
        return content_text.lstrip("\ufeff").strip()

    def _decode_base64_content(self, content_base64: str | None, *, filename: str) -> bytes:
        normalized = str(content_base64 or "").strip()
        if not normalized:
            if Path(filename).suffix.lower() in PDF_UPLOAD_SUFFIXES:
                logger.error("PDF ingestion failed: filename=%s; binary content was missing.", filename)
            raise ValueError(f"Uploaded file `{filename}` did not include binary content.")

        try:
            return base64.b64decode(normalized, validate=True)
        except (ValueError, binascii.Error) as exc:
            if Path(filename).suffix.lower() in PDF_UPLOAD_SUFFIXES:
                logger.error(
                    "PDF ingestion failed: filename=%s; binary content was not valid base64.",
                    filename,
                )
            raise ValueError(f"Uploaded file `{filename}` contained invalid binary content.") from exc

    def _extract_pdf_document_text(self, *, filename: str, content_bytes: bytes) -> str:
        try:
            return self._extract_pdf_document_text_impl(
                filename=filename,
                content_bytes=content_bytes,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error(
                "PDF ingestion failed: filename=%s; byte_count=%d; error=%s",
                filename,
                len(content_bytes),
                exc,
            )
            raise
        except Exception:
            logger.exception(
                "Unexpected PDF ingestion failure: filename=%s; byte_count=%d.",
                filename,
                len(content_bytes),
            )
            raise

    def _extract_pdf_document_text_impl(self, *, filename: str, content_bytes: bytes) -> str:
        logger.info(
            "PDF ingestion started: filename=%s; byte_count=%d.",
            filename,
            len(content_bytes),
        )
        try:
            reader = PdfReader(io.BytesIO(content_bytes))
        except (PyPdfError, EOFError, OSError, ValueError) as exc:
            raise ValueError(f"Uploaded file `{filename}` is not a valid PDF document.") from exc

        if reader.is_encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except (PyPdfError, NotImplementedError, ValueError):
                unlocked = False
            if not unlocked:
                raise ValueError(
                    f"Uploaded PDF `{filename}` is password-protected. Remove the password before uploading."
                )

        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError(f"Uploaded PDF `{filename}` does not contain any pages.")
        maximum_pages = max(1, int(self._pdf_ocr_setting("pdf_max_pages", 500)))
        if page_count > maximum_pages:
            raise ValueError(
                f"Uploaded PDF `{filename}` has {page_count} pages; the configured limit is "
                f"{maximum_pages} pages."
            )

        native_text_by_number: dict[int, str] = {}
        pages_requiring_ocr: list[int] = []
        pages_with_embedded_images: list[int] = []
        pages_with_vector_graphics: list[int] = []
        minimum_native_chars = max(
            0,
            int(self._pdf_ocr_setting("pdf_ocr_min_native_text_chars", 40)),
        )
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = (page.extract_text() or "").strip()
            except (PyPdfError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Could not extract text from page {page_number} of `{filename}`."
                ) from exc

            native_text_by_number[page_number] = page_text
            native_character_count = len(WHITESPACE_RE.sub("", page_text))
            if native_character_count < minimum_native_chars:
                pages_requiring_ocr.append(page_number)
            if self._page_has_embedded_images(page):
                pages_with_embedded_images.append(page_number)
            elif self._page_has_vector_graphics(page):
                pages_with_vector_graphics.append(page_number)

        extra_image_ocr_pages: list[int] = []
        if bool(self._pdf_ocr_setting("pdf_image_ocr_enabled", True)):
            maximum_image_ocr_pages = max(
                0,
                int(self._pdf_ocr_setting("pdf_image_ocr_max_pages", 100)),
            )
            extra_image_ocr_pages = [
                page_number
                for page_number in pages_with_embedded_images
                if page_number not in pages_requiring_ocr
            ][:maximum_image_ocr_pages]
        ocr_page_numbers = sorted(
            set(pages_requiring_ocr).union(extra_image_ocr_pages)
        )
        ocr_text_by_number: dict[int, str] = {}
        if ocr_page_numbers and bool(self._pdf_ocr_setting("pdf_ocr_enabled", True)):
            logger.warning(
                "PDF OCR required: filename=%s; page_count=%d; pages=%s; mixed_image_pages=%s.",
                filename,
                page_count,
                ",".join(str(page_number) for page_number in ocr_page_numbers),
                ",".join(str(page_number) for page_number in extra_image_ocr_pages) or "none",
            )
            ocr_text_by_number = self._ocr_pdf_pages(
                filename=filename,
                content_bytes=content_bytes,
                page_numbers=ocr_page_numbers,
            )

        vision_text_by_number: dict[int, str] = {}
        vision_page_numbers = self._select_pdf_vision_pages(
            pages_requiring_ocr=pages_requiring_ocr,
            pages_with_embedded_images=pages_with_embedded_images,
            pages_with_vector_graphics=pages_with_vector_graphics,
        )
        if vision_page_numbers:
            try:
                vision_text_by_number = self._analyze_pdf_visual_pages(
                    filename=filename,
                    content_bytes=content_bytes,
                    page_numbers=vision_page_numbers,
                    native_text_by_number=native_text_by_number,
                    ocr_text_by_number=ocr_text_by_number,
                )
            except Exception as exc:
                logger.exception(
                    "PDF vision analysis failed; continuing with extracted text: "
                    "filename=%s; pages=%s; error=%s",
                    filename,
                    ",".join(str(page_number) for page_number in vision_page_numbers),
                    exc,
                )

        page_text_by_number = {
            page_number: self._merge_pdf_page_content(
                native_text=native_text_by_number.get(page_number, ""),
                ocr_text=ocr_text_by_number.get(page_number, ""),
                vision_text=vision_text_by_number.get(page_number, ""),
            )
            for page_number in range(1, page_count + 1)
        }
        sections = [
            f"Page {page_number}\n{page_text}"
            for page_number, page_text in page_text_by_number.items()
            if page_text
        ]

        combined = "\n\n".join(sections).strip()
        if not combined:
            if not bool(self._pdf_ocr_setting("pdf_ocr_enabled", True)):
                raise ValueError(
                    f"Uploaded PDF `{filename}` contains no extractable text. "
                    "Image-only PDFs require OCR, but PDF OCR is disabled."
                )
            raise ValueError(
                f"Uploaded PDF `{filename}` contains no extractable text. "
                "OCR ran but did not recognize any text, and image interpretation "
                "did not identify searchable visual information."
            )
        logger.info(
            "PDF ingestion completed: filename=%s; page_count=%d; ocr_page_count=%d; "
            "vision_page_count=%d; extracted_character_count=%d.",
            filename,
            page_count,
            len(ocr_page_numbers),
            len(vision_text_by_number),
            len(combined),
        )
        return combined

    def _select_pdf_vision_pages(
        self,
        *,
        pages_requiring_ocr: list[int],
        pages_with_embedded_images: list[int],
        pages_with_vector_graphics: list[int],
    ) -> list[int]:
        if not bool(self._pdf_ocr_setting("pdf_vision_enabled", False)):
            return []
        if not str(self._pdf_ocr_setting("openai_api_key", "") or "").strip():
            logger.warning("PDF vision skipped because OPENAI_API_KEY is not configured.")
            return []

        maximum_pages = max(
            0,
            int(self._pdf_ocr_setting("pdf_vision_max_pages", 12)),
        )
        if maximum_pages == 0:
            return []
        priority_order = [
            *pages_with_embedded_images,
            *pages_requiring_ocr,
            *pages_with_vector_graphics,
        ]
        selected: list[int] = []
        seen: set[int] = set()
        for page_number in priority_order:
            if page_number in seen:
                continue
            seen.add(page_number)
            selected.append(page_number)
            if len(selected) >= maximum_pages:
                break
        return sorted(selected)

    def _analyze_pdf_visual_pages(
        self,
        *,
        filename: str,
        content_bytes: bytes,
        page_numbers: list[int],
        native_text_by_number: dict[int, str],
        ocr_text_by_number: dict[int, str],
    ) -> dict[int, str]:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError(
                "PDF image interpretation requires the `pypdfium2` package."
            ) from exc
        from PIL import Image

        dpi = min(
            300,
            max(72, int(self._pdf_ocr_setting("pdf_vision_dpi", 144))),
        )
        maximum_dimension = min(
            3000,
            max(512, int(self._pdf_ocr_setting("pdf_vision_max_dimension", 1800))),
        )
        logger.info(
            "PDF vision started: filename=%s; pages=%s; model=%s; dpi=%d.",
            filename,
            ",".join(str(page_number) for page_number in page_numbers),
            self._pdf_ocr_setting("pdf_vision_model", "gpt-5.6-luna"),
            dpi,
        )
        try:
            document = pdfium.PdfDocument(content_bytes)
        except Exception as exc:
            raise ValueError(
                f"Uploaded file `{filename}` could not be rendered for image interpretation."
            ) from exc

        vision_pages: list[PdfVisionPage] = []
        try:
            for page_number in page_numbers:
                page = None
                bitmap = None
                image = None
                try:
                    page = document[page_number - 1]
                    bitmap = page.render(scale=dpi / 72)
                    image = bitmap.to_pil().convert("RGB")
                    image.thumbnail(
                        (maximum_dimension, maximum_dimension),
                        Image.Resampling.LANCZOS,
                    )
                    image_buffer = io.BytesIO()
                    image.save(
                        image_buffer,
                        format="JPEG",
                        quality=82,
                        optimize=True,
                    )
                    vision_pages.append(
                        PdfVisionPage(
                            page_number=page_number,
                            image_bytes=image_buffer.getvalue(),
                            native_text=native_text_by_number.get(page_number, ""),
                            ocr_text=ocr_text_by_number.get(page_number, ""),
                        )
                    )
                finally:
                    if image is not None:
                        image.close()
                    if bitmap is not None:
                        bitmap.close()
                    if page is not None:
                        page.close()
        finally:
            document.close()

        results = self._get_pdf_vision_analyzer().analyze_pages(vision_pages)
        logger.info(
            "PDF vision completed: filename=%s; analyzed_page_count=%d; "
            "described_page_count=%d.",
            filename,
            len(vision_pages),
            len(results),
        )
        return results

    def _page_has_embedded_images(self, page: Any) -> bool:
        try:
            return len(page.images) > 0
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _page_has_vector_graphics(self, page: Any) -> bool:
        try:
            contents = page.get_contents()
            content_bytes = contents.get_data() if contents is not None else b""
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        graphics_operators = re.findall(
            rb"(?:^|\s)(?:m|l|c|v|y|re|S|s|f|f\*|B|B\*)(?=\s|$)",
            content_bytes,
        )
        return len(graphics_operators) >= 16

    def _merge_pdf_page_content(
        self,
        *,
        native_text: str,
        ocr_text: str,
        vision_text: str,
    ) -> str:
        native = str(native_text or "").strip()
        ocr = str(ocr_text or "").strip()
        vision = str(vision_text or "").strip()
        sections = [native] if native else []
        novel_ocr = self._deduplicate_pdf_ocr_text(native, ocr)
        if novel_ocr:
            if native:
                sections.append(f"Image text (OCR)\n{novel_ocr}")
            else:
                sections.append(novel_ocr)
        if vision:
            sections.append(f"Visual content\n{vision}")
        return "\n\n".join(section for section in sections if section).strip()

    def _deduplicate_pdf_ocr_text(self, native_text: str, ocr_text: str) -> str:
        native_lines = {
            self._normalize_pdf_dedupe_line(line)
            for line in str(native_text or "").splitlines()
            if self._normalize_pdf_dedupe_line(line)
        }
        novel_lines: list[str] = []
        seen_lines = set(native_lines)
        for line in str(ocr_text or "").splitlines():
            normalized = self._normalize_pdf_dedupe_line(line)
            if not normalized or normalized in seen_lines:
                continue
            seen_lines.add(normalized)
            novel_lines.append(line.strip())
        return "\n".join(novel_lines).strip()

    def _normalize_pdf_dedupe_line(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _ocr_pdf_pages(
        self,
        *,
        filename: str,
        content_bytes: bytes,
        page_numbers: list[int],
    ) -> dict[int, str]:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError(
                "PDF OCR requires the `pypdfium2` package. Install the project requirements and retry."
            ) from exc

        dpi = min(600, max(72, int(self._pdf_ocr_setting("pdf_ocr_dpi", 300))))
        provider = self._get_ocr_provider()
        logger.info(
            "PDF OCR started: filename=%s; pages=%s; engine=%s; dpi=%d.",
            filename,
            ",".join(str(page_number) for page_number in page_numbers),
            getattr(provider, "name", type(provider).__name__),
            dpi,
        )

        try:
            document = pdfium.PdfDocument(content_bytes)
        except Exception as exc:
            raise ValueError(f"Uploaded file `{filename}` could not be rendered for OCR.") from exc

        extracted: dict[int, str] = {}
        try:
            for page_number in page_numbers:
                page = None
                bitmap = None
                image = None
                try:
                    page = document[page_number - 1]
                    bitmap = page.render(scale=dpi / 72)
                    image = bitmap.to_pil()
                    image.info["dpi"] = (dpi, dpi)
                    extracted[page_number] = provider.recognize(
                        image,
                        filename=filename,
                        page_number=page_number,
                    )
                except RuntimeError:
                    raise
                except Exception as exc:
                    raise ValueError(
                        f"Could not render page {page_number} of `{filename}` for OCR."
                    ) from exc
                finally:
                    if image is not None:
                        image.close()
                    if bitmap is not None:
                        bitmap.close()
                    if page is not None:
                        page.close()
        finally:
            document.close()

        empty_pages = [
            str(page_number)
            for page_number, text in extracted.items()
            if not text.strip()
        ]
        if empty_pages:
            logger.warning(
                "PDF OCR returned no text: filename=%s; pages=%s.",
                filename,
                ",".join(empty_pages),
            )
        logger.info(
            "PDF OCR completed: filename=%s; recognized_page_count=%d.",
            filename,
            sum(bool(text.strip()) for text in extracted.values()),
        )
        return extracted

    def _get_ocr_provider(self) -> OcrProvider:
        provider = getattr(self, "_ocr_provider", None)
        if provider is None:
            provider = create_ocr_provider(self._settings)
            self._ocr_provider = provider
        return provider

    def _get_pdf_vision_analyzer(self) -> PdfVisionAnalyzer:
        analyzer = getattr(self, "_pdf_vision_analyzer", None)
        if analyzer is None:
            analyzer = PdfVisionAnalyzer(self._settings)
            self._pdf_vision_analyzer = analyzer
        return analyzer

    def _pdf_ocr_setting(self, name: str, default: Any) -> Any:
        settings = getattr(self, "_settings", None)
        return getattr(settings, name, default)

    def _extract_word_document_text(self, *, filename: str, content_bytes: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as archive:
                archive_names = set(archive.namelist())
                part_names = [
                    "word/document.xml",
                    *sorted(
                        name
                        for name in archive_names
                        if name.startswith("word/header") and name.endswith(".xml")
                    ),
                    *sorted(
                        name
                        for name in archive_names
                        if name.startswith("word/footer") and name.endswith(".xml")
                    ),
                    "word/footnotes.xml",
                    "word/endnotes.xml",
                ]

                sections: list[str] = []
                for part_name in part_names:
                    if part_name not in archive_names:
                        continue
                    extracted = self._extract_wordprocessingml_text(archive.read(part_name))
                    if extracted:
                        sections.append(extracted)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Uploaded file `{filename}` is not a valid Word document.") from exc

        combined = "\n\n".join(section for section in sections if section.strip()).strip()
        if not combined:
            raise ValueError(f"Uploaded file `{filename}` did not contain readable Word text.")
        return combined

    def _extract_wordprocessingml_text(self, xml_payload: bytes) -> str:
        try:
            root = ET.fromstring(xml_payload)
        except ET.ParseError:
            return ""

        paragraph_texts: list[str] = []
        text_tag = f"{{{WORDPROCESSING_MAIN_NS}}}t"
        tab_tag = f"{{{WORDPROCESSING_MAIN_NS}}}tab"
        break_tags = {
            f"{{{WORDPROCESSING_MAIN_NS}}}br",
            f"{{{WORDPROCESSING_MAIN_NS}}}cr",
        }
        paragraph_tag = f"{{{WORDPROCESSING_MAIN_NS}}}p"

        for paragraph in root.iter(paragraph_tag):
            pieces: list[str] = []
            for node in paragraph.iter():
                if node.tag == text_tag:
                    pieces.append(node.text or "")
                elif node.tag == tab_tag:
                    pieces.append("\t")
                elif node.tag in break_tags:
                    pieces.append("\n")

            paragraph_text = "".join(pieces).strip()
            if paragraph_text:
                paragraph_texts.append(paragraph_text)

        return "\n".join(paragraph_texts).strip()

    def _extract_spreadsheet_text(self, *, filename: str, content_bytes: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as archive:
                shared_strings = self._read_spreadsheet_shared_strings(archive)
                sheet_entries = self._read_spreadsheet_sheet_entries(archive)
                sections = [
                    extracted
                    for extracted in (
                        self._extract_spreadsheet_sheet_text(
                            archive,
                            sheet_name=sheet_name,
                            sheet_path=sheet_path,
                            shared_strings=shared_strings,
                        )
                        for sheet_name, sheet_path in sheet_entries
                    )
                    if extracted
                ]
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Uploaded file `{filename}` is not a valid Excel workbook.") from exc

        combined = "\n\n".join(section for section in sections if section.strip()).strip()
        if not combined:
            raise ValueError(f"Uploaded file `{filename}` did not contain readable spreadsheet data.")
        return combined

    def _read_spreadsheet_shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        shared_strings_path = "xl/sharedStrings.xml"
        if shared_strings_path not in archive.namelist():
            return []

        try:
            root = ET.fromstring(archive.read(shared_strings_path))
        except ET.ParseError:
            return []

        namespace = {"main": SPREADSHEET_MAIN_NS}
        return [
            "".join(text for text in item.itertext()).strip()
            for item in root.findall("main:si", namespace)
        ]

    def _read_spreadsheet_sheet_entries(self, archive: zipfile.ZipFile) -> list[tuple[str, str]]:
        workbook_path = "xl/workbook.xml"
        if workbook_path not in archive.namelist():
            worksheet_paths = sorted(
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            )
            return [(Path(path).stem, path) for path in worksheet_paths]

        try:
            workbook_root = ET.fromstring(archive.read(workbook_path))
        except ET.ParseError:
            return []

        rel_path = "xl/_rels/workbook.xml.rels"
        rel_targets: dict[str, str] = {}
        if rel_path in archive.namelist():
            try:
                rel_root = ET.fromstring(archive.read(rel_path))
            except ET.ParseError:
                rel_root = None
            if rel_root is not None:
                for relationship in rel_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
                    rel_id = relationship.attrib.get("Id")
                    target = relationship.attrib.get("Target")
                    if not rel_id or not target:
                        continue
                    rel_targets[rel_id] = self._normalize_spreadsheet_target_path(target)

        namespace = {"main": SPREADSHEET_MAIN_NS, "rel": OFFICE_DOCUMENT_REL_NS}
        sheets: list[tuple[str, str]] = []
        for sheet in workbook_root.findall("main:sheets/main:sheet", namespace):
            sheet_name = str(sheet.attrib.get("name") or "Sheet").strip() or "Sheet"
            rel_id = sheet.attrib.get(f"{{{OFFICE_DOCUMENT_REL_NS}}}id")
            sheet_path = rel_targets.get(rel_id or "")
            if sheet_path and sheet_path in archive.namelist():
                sheets.append((sheet_name, sheet_path))

        if sheets:
            return sheets

        worksheet_paths = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        return [(Path(path).stem, path) for path in worksheet_paths]

    def _normalize_spreadsheet_target_path(self, target: str) -> str:
        normalized_target = str(target or "").replace("\\", "/").strip()
        if not normalized_target:
            return ""
        if normalized_target.startswith("/"):
            return normalized_target.lstrip("/")
        return posixpath.normpath(posixpath.join("xl", normalized_target))

    def _extract_spreadsheet_sheet_text(
        self,
        archive: zipfile.ZipFile,
        *,
        sheet_name: str,
        sheet_path: str,
        shared_strings: list[str],
    ) -> str:
        try:
            root = ET.fromstring(archive.read(sheet_path))
        except ET.ParseError:
            return ""

        namespace = {"main": SPREADSHEET_MAIN_NS}
        lines = [f"Sheet: {sheet_name}"]
        for row in root.findall("main:sheetData/main:row", namespace):
            row_number = str(row.attrib.get("r") or "").strip() or str(len(lines))
            values: list[str] = []
            for cell in row.findall("main:c", namespace):
                cell_text = self._extract_spreadsheet_cell_text(cell, shared_strings)
                if not cell_text:
                    continue
                cell_ref = str(cell.attrib.get("r") or "").strip()
                column_label = re.sub(r"\d+", "", cell_ref) or "?"
                values.append(f"[{column_label}] {cell_text}")
            if values:
                lines.append(f"Row {row_number}: {' | '.join(values)}")

        return "\n".join(lines) if len(lines) > 1 else ""

    def _extract_spreadsheet_cell_text(self, cell: ET.Element, shared_strings: list[str]) -> str:
        cell_type = str(cell.attrib.get("t") or "").strip()
        raw_value = self._find_xml_text(cell, f"{{{SPREADSHEET_MAIN_NS}}}v")
        formula_text = self._find_xml_text(cell, f"{{{SPREADSHEET_MAIN_NS}}}f")

        if cell_type == "s":
            try:
                shared_index = int(raw_value or "")
            except ValueError:
                return ""
            if 0 <= shared_index < len(shared_strings):
                return shared_strings[shared_index]
            return ""

        if cell_type == "inlineStr":
            inline_strings = [
                (node.text or "")
                for node in cell.iter(f"{{{SPREADSHEET_MAIN_NS}}}t")
            ]
            return "".join(inline_strings).strip()

        if cell_type == "b":
            return "TRUE" if raw_value == "1" else "FALSE" if raw_value == "0" else ""

        value = raw_value or (f"={formula_text}" if formula_text else "")
        return WHITESPACE_RE.sub(" ", value).strip()

    def _find_xml_text(self, root: ET.Element, tag_name: str) -> str | None:
        node = root.find(tag_name)
        if node is None or node.text is None:
            return None
        return node.text.strip()

    def _parse_structured_json(
        self,
        *,
        raw_text: str,
        existing_ids: set[str],
        upload_key_base: str | None,
        source_auto_tags: list[str],
    ) -> list[DocumentRecord] | None:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None

        items = payload if isinstance(payload, list) else [payload]
        if not items or not all(isinstance(item, dict) for item in items):
            return None
        if not all(STRUCTURED_JSON_FIELDS.issubset(item.keys()) for item in items):
            return None

        used_ids = set(existing_ids)
        payload_document_ids: set[str] = set()
        documents: list[DocumentRecord] = []
        for index, item in enumerate(items):
            normalized_text = str(item["text"]).strip()
            if not normalized_text:
                raise ValueError("Structured JSON documents must include non-empty `text` values.")

            explicit_document_id = str(item.get("document_id") or "").strip()
            if explicit_document_id:
                if explicit_document_id in payload_document_ids:
                    raise ValueError(f"Document ID `{explicit_document_id}` is duplicated in the uploaded JSON.")
                payload_document_ids.add(explicit_document_id)
                document_id = explicit_document_id
                used_ids.add(explicit_document_id)
            else:
                document_id = self._build_document_id(used_ids)
                used_ids.add(document_id)

            title = str(item["title"]).strip()
            if not title:
                raise ValueError("Structured JSON documents must include non-empty `title` values.")

            category = self._normalize_field(item.get("category"), fallback="uploaded")
            folder = self._normalize_folder(item.get("folder"), fallback=category)
            tags = self._normalize_tags(
                self._manual_tags_for_folder(
                    raw_tags=item.get("tags", []),
                    folder=folder,
                )
            )
            effective_tags, auto_tags = self._build_document_tags(
                tags=tags,
                folder=folder,
                source_auto_tags=source_auto_tags,
            )
            summary = str(item.get("summary") or "").strip() or self._summarize(normalized_text)
            upload_key = self._build_structured_upload_key(
                upload_key_base=upload_key_base,
                item_index=index,
                title=title,
            )
            source_url = self._optional_text(item.get("source_url"))

            documents.append(
                DocumentRecord(
                    document_id=document_id,
                    title=title,
                    category=category,
                    folder=folder,
                    tags=effective_tags,
                    summary=summary,
                    text=normalized_text,
                    source_url=source_url,
                    updated_at=self._optional_text(item.get("updated_at"))
                    or datetime.now(timezone.utc).date().isoformat(),
                    upload_key=upload_key,
                    content_hash=self._build_content_hash(
                        title=title,
                        category=category,
                        folder=folder,
                        tags=effective_tags,
                        summary=summary,
                        text=normalized_text,
                        source_url=source_url,
                    ),
                    auto_tags=auto_tags,
                )
            )

        return documents

    def _merge_uploaded_documents(
        self,
        *,
        existing_documents: list[DocumentRecord],
        parsed_documents: list[DocumentRecord],
        default_similarity_policy: UploadSimilarityPolicy,
        similarity_policies_by_upload_key: dict[str, UploadSimilarityPolicy],
        similarity_target_document_ids_by_upload_key: dict[str, str],
    ) -> dict[str, Any]:
        merged_documents = list(existing_documents)
        document_index_by_id = {
            document.document_id: index
            for index, document in enumerate(merged_documents)
        }
        document_id_by_upload_key: dict[str, str] = {}
        document_ids_by_similarity_name: dict[str, list[str]] = {}
        for document in merged_documents:
            for upload_key in self._document_upload_key_candidates(document):
                document_id_by_upload_key[upload_key] = document.document_id
            similarity_name = self._document_similarity_name(document)
            if similarity_name:
                document_ids_by_similarity_name.setdefault(similarity_name, []).append(document.document_id)
        uploaded_documents: list[DocumentRecord] = []
        created_count = 0
        updated_count = 0
        unchanged_count = 0
        ignored_count = 0
        changed = False
        conflicts: list[SimilarDocumentConflictItem] = []

        for candidate in parsed_documents:
            matched_document_id: str | None = None
            if candidate.document_id in document_index_by_id:
                matched_document_id = candidate.document_id
            elif candidate.upload_key and candidate.upload_key in document_id_by_upload_key:
                matched_document_id = document_id_by_upload_key[candidate.upload_key]

            if matched_document_id is None:
                similarity_name = self._document_similarity_name(candidate)
                similar_matches = [
                    merged_documents[document_index_by_id[document_id]]
                    for document_id in document_ids_by_similarity_name.get(similarity_name or "", [])
                    if document_id in document_index_by_id
                ]
                if similar_matches:
                    selected_match = self._select_similar_document_match(similar_matches)
                    candidate_similarity_policy = (
                        similarity_policies_by_upload_key.get(candidate.upload_key or "", default_similarity_policy)
                        if candidate.upload_key
                        else default_similarity_policy
                    )
                    explicit_target_document_id = (
                        similarity_target_document_ids_by_upload_key.get(candidate.upload_key or "")
                        if candidate.upload_key
                        else None
                    )
                    if candidate_similarity_policy == "warn":
                        conflicts.append(
                            SimilarDocumentConflictItem(
                                upload_key=candidate.upload_key,
                                upload_name=similarity_name or self._display_title_from_filename(candidate.title),
                                incoming_title=candidate.title,
                                existing_document_id=selected_match.document_id,
                                existing_title=selected_match.title,
                                existing_folder=selected_match.folder,
                                existing_updated_at=selected_match.updated_at,
                                match_count=len(similar_matches),
                                candidates=[
                                    SimilarDocumentConflictCandidate(
                                        document_id=item.document_id,
                                        title=item.title,
                                        folder=item.folder,
                                        updated_at=item.updated_at,
                                    )
                                    for item in self._sort_similar_document_matches(similar_matches)
                                ],
                            )
                        )
                        continue
                    if candidate_similarity_policy == "ignore":
                        ignored_count += 1
                        continue
                    if explicit_target_document_id:
                        explicit_match = next(
                            (
                                item
                                for item in similar_matches
                                if item.document_id == explicit_target_document_id
                            ),
                            None,
                        )
                        if explicit_match is not None:
                            matched_document_id = explicit_match.document_id
                            selected_match = explicit_match
                    if matched_document_id is None:
                        if len(similar_matches) == 1:
                            matched_document_id = selected_match.document_id
                        else:
                            conflicts.append(
                                SimilarDocumentConflictItem(
                                    upload_key=candidate.upload_key,
                                    upload_name=similarity_name or self._display_title_from_filename(candidate.title),
                                    incoming_title=candidate.title,
                                    existing_document_id=selected_match.document_id,
                                    existing_title=selected_match.title,
                                    existing_folder=selected_match.folder,
                                    existing_updated_at=selected_match.updated_at,
                                    match_count=len(similar_matches),
                                    candidates=[
                                        SimilarDocumentConflictCandidate(
                                            document_id=item.document_id,
                                            title=item.title,
                                            folder=item.folder,
                                            updated_at=item.updated_at,
                                        )
                                        for item in self._sort_similar_document_matches(similar_matches)
                                    ],
                                )
                            )
                            continue

            if matched_document_id is None:
                merged_documents.append(candidate)
                document_index_by_id[candidate.document_id] = len(merged_documents) - 1
                if candidate.upload_key:
                    document_id_by_upload_key[candidate.upload_key] = candidate.document_id
                similarity_name = self._document_similarity_name(candidate)
                if similarity_name:
                    document_ids_by_similarity_name.setdefault(similarity_name, []).append(candidate.document_id)
                uploaded_documents.append(candidate)
                created_count += 1
                changed = True
                continue

            current_document = merged_documents[document_index_by_id[matched_document_id]]
            if self._uploaded_document_matches(current_document, candidate):
                if (
                    candidate.upload_key
                    and (current_document.upload_key != candidate.upload_key or current_document.content_hash != candidate.content_hash)
                ):
                    replacement_document = DocumentRecord(
                        document_id=current_document.document_id,
                        title=current_document.title,
                        category=current_document.category,
                        folder=current_document.folder,
                        tags=current_document.tags,
                        summary=current_document.summary,
                        text=current_document.text,
                        source_url=current_document.source_url,
                        updated_at=current_document.updated_at,
                        upload_key=candidate.upload_key,
                        content_hash=candidate.content_hash or current_document.content_hash,
                    )
                    merged_documents[document_index_by_id[matched_document_id]] = replacement_document
                    for upload_key in self._document_upload_key_candidates(replacement_document):
                        document_id_by_upload_key[upload_key] = replacement_document.document_id
                    uploaded_documents.append(replacement_document)
                    changed = True
                else:
                    uploaded_documents.append(current_document)
                unchanged_count += 1
                continue

            replacement_document = DocumentRecord(
                document_id=current_document.document_id,
                title=candidate.title,
                category=candidate.category,
                folder=candidate.folder,
                tags=candidate.tags,
                summary=candidate.summary,
                text=candidate.text,
                source_url=candidate.source_url if candidate.source_url is not None else current_document.source_url,
                updated_at=datetime.now(timezone.utc).date().isoformat(),
                upload_key=candidate.upload_key or current_document.upload_key,
                content_hash=candidate.content_hash
                or current_document.content_hash
                or self._build_content_hash(
                    title=candidate.title,
                    category=candidate.category,
                    folder=candidate.folder,
                    tags=candidate.tags,
                    summary=candidate.summary,
                    text=candidate.text,
                    source_url=candidate.source_url if candidate.source_url is not None else current_document.source_url,
                ),
            )
            merged_documents[document_index_by_id[matched_document_id]] = replacement_document
            if replacement_document.upload_key:
                document_id_by_upload_key[replacement_document.upload_key] = replacement_document.document_id
            uploaded_documents.append(replacement_document)
            updated_count += 1
            changed = True

        if conflicts:
            raise SimilarDocumentConflictError(conflicts)

        return {
            "documents": merged_documents,
            "uploaded_documents": uploaded_documents,
            "created_count": created_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "ignored_count": ignored_count,
            "changed": changed,
        }

    def _uploaded_document_matches(
        self,
        left: DocumentRecord,
        right: DocumentRecord,
    ) -> bool:
        if left.content_hash and right.content_hash:
            return left.content_hash == right.content_hash
        return (
            left.title == right.title
            and left.category == right.category
            and left.folder == right.folder
            and left.tags == right.tags
            and left.summary == right.summary
            and left.text == right.text
            and left.source_url == right.source_url
        )

    def _document_upload_key_candidates(self, document: DocumentRecord) -> list[str]:
        candidates: list[str] = []
        if document.upload_key:
            candidates.append(document.upload_key)
        legacy_title = Path(str(document.title or "").strip()).name.strip()
        if legacy_title:
            legacy_keys = [
                f"upload:{legacy_title}",
                f"upload:path:{legacy_title}",
            ]
            for legacy_key in legacy_keys:
                if legacy_key not in candidates:
                    candidates.append(legacy_key)
        return candidates

    def _document_similarity_name(self, document: DocumentRecord) -> str | None:
        if document.upload_key:
            if document.upload_key.startswith("upload:watch:"):
                payload = document.upload_key.removeprefix("upload:")
                payload = payload.split("::item:", 1)[0]
                payload = payload.split("::mtime:", 1)[0]
                normalized = payload.strip().lower()
                return normalized or None
            if document.upload_key.startswith("upload:name:"):
                payload = document.upload_key.removeprefix("upload:name:")
                name_part, _, _ = payload.partition("::mtime:")
                normalized = Path(name_part).name.strip().lower()
                return normalized or None
            if document.upload_key.startswith("upload:path:"):
                payload = document.upload_key.removeprefix("upload:path:")
                payload = payload.split("::item:", 1)[0]
                normalized = Path(payload).name.strip().lower()
                return normalized or None

        legacy_title = Path(str(document.title or "").strip()).name.strip().lower()
        return legacy_title or None

    def _select_similar_document_match(
        self,
        matches: list[DocumentRecord],
    ) -> DocumentRecord:
        return self._sort_similar_document_matches(matches)[0]

    def _sort_similar_document_matches(
        self,
        matches: list[DocumentRecord],
    ) -> list[DocumentRecord]:
        return sorted(
            matches,
            key=lambda item: (
                str(item.updated_at or ""),
                item.document_id,
            ),
            reverse=True,
        )

    def _build_similarity_target_map(
        self,
        *,
        parsed_documents: list[DocumentRecord],
        target_document_id: str | None,
    ) -> dict[str, str]:
        normalized_target_document_id = self._normalize_similarity_target_document_id(target_document_id)
        if not normalized_target_document_id:
            return {}
        return {
            document.upload_key: normalized_target_document_id
            for document in parsed_documents
            if document.upload_key
        }

    def _build_similarity_policy_map(
        self,
        *,
        parsed_documents: list[DocumentRecord],
        similarity_policy: UploadSimilarityPolicy,
    ) -> dict[str, UploadSimilarityPolicy]:
        return {
            document.upload_key: similarity_policy
            for document in parsed_documents
            if document.upload_key
        }

    def _build_upload_message(
        self,
        *,
        total_uploaded: int,
        created_count: int,
        updated_count: int,
        unchanged_count: int,
        ignored_count: int,
        semantic_index_rebuilt: bool,
    ) -> str:
        noun = "document" if total_uploaded == 1 else "documents"
        if ignored_count == total_uploaded and created_count == 0 and updated_count == 0 and unchanged_count == 0:
            return f"Ignored {ignored_count} similar {noun}. No embedding refresh was needed."
        if unchanged_count == total_uploaded and ignored_count == 0:
            return f"Matched {total_uploaded} existing {noun}. No embedding refresh was needed."

        parts: list[str] = []
        if created_count:
            parts.append(f"{created_count} new")
        if updated_count:
            parts.append(f"{updated_count} updated")
        if unchanged_count:
            parts.append(f"{unchanged_count} unchanged")
        if ignored_count:
            parts.append(f"{ignored_count} ignored")

        summary = ", ".join(parts) if parts else f"{total_uploaded} imported"
        if semantic_index_rebuilt:
            return f"Imported {total_uploaded} {noun}: {summary}. Embeddings refreshed only for changed documents."
        return f"Imported {total_uploaded} {noun}: {summary}."

    def _normalize_similarity_policy(self, value: Any) -> UploadSimilarityPolicy:
        normalized = str(value or "warn").strip().lower()
        if normalized not in {"warn", "replace", "ignore"}:
            return "warn"
        return normalized  # type: ignore[return-value]

    def _normalize_similarity_target_document_id(self, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _build_upload_key(
        self,
        *,
        filename: str,
        client_path: str | None,
        client_modified_ms: int | None,
        upload_key_base: str | None = None,
    ) -> str | None:
        base = upload_key_base or self._build_upload_key_base(
                filename=filename,
                client_path=client_path,
                client_modified_ms=client_modified_ms,
            )
        return None if base is None else f"upload:{base}"

    def _build_upload_key_base(
        self,
        *,
        filename: str,
        client_path: str | None,
        client_modified_ms: int | None,
    ) -> str | None:
        normalized_filename = Path(str(filename or "").strip()).name.strip()
        if normalized_filename and client_modified_ms is not None:
            return f"name:{normalized_filename}::mtime:{int(client_modified_ms)}"

        raw_value = str(client_path or normalized_filename or "").strip().replace("\\", "/")
        parts = [segment.strip() for segment in raw_value.split("/") if segment.strip()]
        if not parts:
            return None
        return f"path:{'/'.join(parts)}"

    def _build_structured_upload_key(
        self,
        *,
        upload_key_base: str | None,
        item_index: int,
        title: str,
    ) -> str | None:
        if not upload_key_base:
            return None
        normalized_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        title_key = normalized_title or f"item-{item_index + 1}"
        return f"upload:{upload_key_base}::item:{item_index}:{title_key}"

    def _build_content_hash(
        self,
        *,
        title: str,
        category: str,
        folder: str,
        tags: list[str],
        summary: str,
        text: str,
        source_url: str | None = None,
    ) -> str:
        payload = {
            "title": title,
            "category": category,
            "folder": folder,
            "tags": tags,
            "summary": summary,
            "text": text,
            "source_url": source_url,
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _build_document_id(self, existing_ids: set[str]) -> str:
        while True:
            candidate = f"UPL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
            if candidate not in existing_ids:
                return candidate

    def _display_title_from_filename(self, filename: str) -> str:
        original_name = Path(filename).name.strip()
        if not original_name:
            return "Uploaded Document"
        return original_name

    def _summarize(self, text: str, max_length: int = 220) -> str:
        normalized = WHITESPACE_RE.sub(" ", text).strip()
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 1].rstrip() + "..."

    def _normalize_field(self, value: Any, *, fallback: str) -> str:
        if value is None:
            return fallback
        normalized = str(value).strip()
        return normalized or fallback

    def _normalize_folder(self, value: Any, *, fallback: str) -> str:
        normalized = self._normalize_field(value, fallback=fallback)
        parts = [segment.strip() for segment in re.split(r"[\\/]+", normalized) if segment.strip()]
        if not parts:
            return fallback
        return "/".join(parts)

    def _normalize_folder_name(self, value: Any) -> str:
        normalized = self._require_text(value, field_name="Folder name")
        if "/" in normalized or "\\" in normalized:
            raise ValueError("Folder rename accepts a single folder name, not a path.")
        return normalized

    def _normalize_optional_folder(self, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        return self._normalize_folder(normalized, fallback="")

    def _build_created_folder_id(self, *, folder_name: str, parent_folder_id: str | None) -> str:
        if not parent_folder_id:
            return folder_name
        return f"{parent_folder_id}/{folder_name}"

    def _build_renamed_folder_id(self, *, folder_id: str, new_name: str) -> str:
        parent_parts = self._normalize_folder(folder_id, fallback=folder_id).split("/")[:-1]
        return "/".join([*parent_parts, new_name]) if parent_parts else new_name

    def _collect_existing_folder_ids(
        self,
        documents: list[DocumentRecord],
        registered_folder_ids: list[str],
    ) -> set[str]:
        existing_folder_ids: set[str] = set()
        for document in documents:
            existing_folder_ids.update(
                iter_folder_lineage(
                    self._normalize_folder(document.folder, fallback=document.category)
                )
            )
        for folder_id in registered_folder_ids:
            existing_folder_ids.update(iter_folder_lineage(folder_id))
        return existing_folder_ids

    def _load_registered_folder_ids(self) -> list[str]:
        return load_folder_registry(self._settings.docstore_folders_path)

    def _folder_is_within_scope(self, folder_value: str, folder_prefix: str) -> bool:
        normalized_folder_value = self._normalize_folder(folder_value, fallback=folder_value)
        return (
            normalized_folder_value == folder_prefix
            or normalized_folder_value.startswith(f"{folder_prefix}/")
        )

    def _replace_folder_prefix(self, folder_value: str, *, old_prefix: str, new_prefix: str) -> str:
        normalized_folder_value = self._normalize_folder(folder_value, fallback=folder_value)
        if normalized_folder_value == old_prefix:
            return new_prefix
        if normalized_folder_value.startswith(f"{old_prefix}/"):
            return f"{new_prefix}{normalized_folder_value[len(old_prefix):]}"
        return normalized_folder_value

    def _require_text(self, value: Any, *, field_name: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field_name} is required.")
        return normalized

    def _normalize_tags(self, raw_tags: Any) -> list[str]:
        return normalize_tag_values(raw_tags)

    def _manual_tags_for_folder(self, *, raw_tags: Any, folder: str) -> list[str]:
        return strip_folder_auto_tags(raw_tags, folder)

    def _manual_tags_from_document(self, document: DocumentRecord) -> list[str]:
        return self._strip_document_auto_tags(document.tags, document)

    def _source_auto_tags_from_document(self, document: DocumentRecord) -> list[str]:
        folder_auto_tags = build_effective_document_tags([], document.folder)[1]
        folder_auto_keys = {tag.lower() for tag in folder_auto_tags}
        return [
            tag
            for tag in normalize_tag_values(document.auto_tags)
            if tag.lower() not in folder_auto_keys
        ]

    def _strip_document_auto_tags(
        self,
        raw_tags: Any,
        document: DocumentRecord,
    ) -> list[str]:
        auto_tag_keys = {tag.lower() for tag in document.auto_tags}
        return [
            tag
            for tag in normalize_tag_values(raw_tags)
            if tag.lower() not in auto_tag_keys
        ]

    def _build_document_tags(
        self,
        *,
        tags: Any,
        folder: str,
        source_auto_tags: Any = None,
    ) -> tuple[list[str], list[str]]:
        return build_effective_document_tags(tags, folder, source_auto_tags)

    def _optional_text(self, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _backup_payload(self) -> LibraryBackupPayload:
        document_payload = "[]\n"
        if self._settings.docstore_json_path.exists():
            document_payload = self._settings.docstore_json_path.read_text(encoding="utf-8")

        folder_payload = "[]\n"
        if self._settings.docstore_folders_path.exists():
            folder_payload = self._settings.docstore_folders_path.read_text(encoding="utf-8")

        return LibraryBackupPayload(
            document_payload=document_payload,
            folder_payload=folder_payload,
        )

    def _persist_documents(
        self,
        *,
        documents: list[DocumentRecord],
        backup_payload: LibraryBackupPayload,
        folder_ids: list[str] | None = None,
        semantic_sync_mode: str = "full",
        deleted_document_ids: list[str] | None = None,
        progress_callback: Callable[[str, int, str], None] | None = None,
    ) -> bool:
        try:
            write_json_documents(self._settings.docstore_json_path, documents)
            if folder_ids is not None:
                write_folder_registry(self._settings.docstore_folders_path, folder_ids)

            semantic_index_rebuilt = False
            if self._settings.docstore_backend == "semantic":
                if semantic_sync_mode == "delete_only" and deleted_document_ids:
                    try:
                        sync_result = delete_documents_from_semantic_index(
                            index_path=self._settings.semantic_index_path,
                            document_ids=deleted_document_ids,
                            progress_callback=progress_callback,
                        )
                    except (RuntimeError, sqlite3.DatabaseError):
                        if progress_callback is not None:
                            progress_callback(
                                "rebuilding_index",
                                35,
                                "Index requires repair; rebuilding semantic data...",
                            )
                        sync_result = sync_semantic_index(
                            source_path=self._settings.docstore_json_path,
                            index_path=self._settings.semantic_index_path,
                            openai_api_key=self._settings.openai_api_key,
                            search_embedding_model=self._settings.semantic_search_embedding_model,
                            search_embedding_dimensions=self._settings.semantic_search_embedding_dimensions,
                            answer_embedding_model=self._settings.semantic_answer_embedding_model,
                            answer_embedding_dimensions=self._settings.semantic_answer_embedding_dimensions,
                            chunk_size_words=self._settings.semantic_chunk_size_words,
                            chunk_overlap_words=self._settings.semantic_chunk_overlap_words,
                            batch_size=self._settings.semantic_embedding_batch_size,
                        )
                elif semantic_sync_mode == "metadata_only":
                    try:
                        sync_result = sync_semantic_metadata_only(
                            index_path=self._settings.semantic_index_path,
                            documents=documents,
                        )
                    except Exception:
                        sync_result = sync_semantic_index(
                            source_path=self._settings.docstore_json_path,
                            index_path=self._settings.semantic_index_path,
                            openai_api_key=self._settings.openai_api_key,
                            search_embedding_model=self._settings.semantic_search_embedding_model,
                            search_embedding_dimensions=self._settings.semantic_search_embedding_dimensions,
                            answer_embedding_model=self._settings.semantic_answer_embedding_model,
                            answer_embedding_dimensions=self._settings.semantic_answer_embedding_dimensions,
                            chunk_size_words=self._settings.semantic_chunk_size_words,
                            chunk_overlap_words=self._settings.semantic_chunk_overlap_words,
                            batch_size=self._settings.semantic_embedding_batch_size,
                        )
                else:
                    sync_result = sync_semantic_index(
                        source_path=self._settings.docstore_json_path,
                        index_path=self._settings.semantic_index_path,
                        openai_api_key=self._settings.openai_api_key,
                        search_embedding_model=self._settings.semantic_search_embedding_model,
                        search_embedding_dimensions=self._settings.semantic_search_embedding_dimensions,
                        answer_embedding_model=self._settings.semantic_answer_embedding_model,
                        answer_embedding_dimensions=self._settings.semantic_answer_embedding_dimensions,
                        chunk_size_words=self._settings.semantic_chunk_size_words,
                        chunk_overlap_words=self._settings.semantic_chunk_overlap_words,
                        batch_size=self._settings.semantic_embedding_batch_size,
                    )
                semantic_index_rebuilt = bool(
                    sync_result.get("full_rebuild")
                    or int(sync_result.get("embedded_documents", 0)) > 0
                )
            return semantic_index_rebuilt
        except Exception:
            self._restore_library_backup(backup_payload)
            raise

    def _persist_folder_registry(
        self,
        *,
        folder_ids: list[str],
        backup_payload: LibraryBackupPayload,
    ) -> None:
        try:
            write_folder_registry(self._settings.docstore_folders_path, folder_ids)
        except Exception:
            self._restore_library_backup(backup_payload)
            raise

    def _restore_library_backup(self, backup_payload: LibraryBackupPayload) -> None:
        self._settings.docstore_json_path.write_text(
            backup_payload.document_payload,
            encoding="utf-8",
        )
        self._settings.docstore_folders_path.write_text(
            backup_payload.folder_payload,
            encoding="utf-8",
        )

    def _require_local_mutation_backend(self, operation_name: str) -> None:
        if self._settings.docstore_backend not in {"json", "semantic"}:
            raise RuntimeError(
                f"{operation_name} is only supported for local json and semantic datastores."
            )
