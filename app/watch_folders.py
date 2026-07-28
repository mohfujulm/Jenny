from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import json
import logging
import re
import threading
from typing import Any, Callable
from uuid import uuid4

from app.config import Settings
from app.datastore import (
    DocumentRecord,
    build_folder_auto_tags,
    load_json_documents,
    normalize_folder_path,
    normalize_tag_values,
)
from app.ingestion import (
    PDF_UPLOAD_SUFFIXES,
    SPREADSHEET_UPLOAD_SUFFIXES,
    SUPPORTED_UPLOAD_SUFFIXES,
    WORD_UPLOAD_SUFFIXES,
    DocumentIngestionService,
)
from app.path_tags import infer_watched_path_tags


logger = logging.getLogger("app.pdf_ingestion")


BINARY_WATCH_SUFFIXES = (
    WORD_UPLOAD_SUFFIXES | SPREADSHEET_UPLOAD_SUFFIXES | PDF_UPLOAD_SUFFIXES
)
WINDOWS_FILE_ATTRIBUTE_OFFLINE = 0x1000
WINDOWS_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
WINDOWS_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
WINDOWS_CLOUD_PLACEHOLDER_ATTRIBUTES = (
    WINDOWS_FILE_ATTRIBUTE_OFFLINE
    | WINDOWS_FILE_ATTRIBUTE_RECALL_ON_OPEN
    | WINDOWS_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


LibraryChangedCallback = Callable[[], None]


@dataclass
class WatchedFolderRecord:
    watch_id: str
    alias: str | None
    display_name: str
    root_path: str
    include_subfolder: str | None
    library_folder: str
    category: str
    tags: list[str]
    recursive: bool
    enabled: bool
    interval_minutes: int
    created_at: str
    last_sync_at: str | None = None
    last_status: str | None = None
    last_message: str | None = None
    last_scanned_count: int = 0
    last_imported_count: int = 0
    last_created_count: int = 0
    last_updated_count: int = 0
    last_unchanged_count: int = 0
    last_skipped_count: int = 0
    last_error_count: int = 0


@dataclass
class WatchSyncResult:
    watch_id: str
    display_name: str
    source_path: str
    status: str
    message: str
    scanned_count: int
    imported_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    error_count: int
    semantic_index_rebuilt: bool
    synced_at: str


@dataclass
class PendingWatchedFile:
    path: Path
    relative_path: str
    upload_key_base: str
    target_document_id: str | None = None
    existing_manual_tags: list[str] = field(default_factory=list)


class WatchedFolderService:
    def __init__(
        self,
        settings: Settings,
        ingestion_service: DocumentIngestionService,
        on_library_changed: LibraryChangedCallback | None = None,
    ) -> None:
        self._settings = settings
        self._ingestion_service = ingestion_service
        self._on_library_changed = on_library_changed
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def set_library_changed_callback(self, callback: LibraryChangedCallback) -> None:
        self._on_library_changed = callback

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_scheduler,
            name="watched-folder-sync",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def list_watchers(self) -> list[dict[str, Any]]:
        return [self._record_to_payload(record) for record in self._load_records()]

    def resolve_watcher_source_path(self, watch_id: str) -> Path:
        normalized_watch_id = str(watch_id or "").strip()
        if not normalized_watch_id:
            raise ValueError("Watch ID is required.")

        with self._sync_lock:
            record = next(
                (
                    item
                    for item in self._load_records()
                    if item.watch_id == normalized_watch_id
                ),
                None,
            )
            if record is None:
                raise ValueError("Watched folder not found.")

            source_path = self._record_source_path(record)
            if source_path.exists() and source_path.is_dir():
                return source_path.resolve()

            relocated_source_path = self._find_relocated_source_path(source_path)
            if relocated_source_path is not None:
                self._persist_source_path(
                    record.watch_id,
                    root_path=str(relocated_source_path),
                    include_subfolder=None,
                )
                return relocated_source_path.resolve()

            raise ValueError(
                f"Synchronized source folder does not exist: {source_path}"
            )

    def create_watcher(
        self,
        *,
        root_path: str,
        include_subfolder: str | None,
        display_name: str | None,
        alias: str | None,
        library_folder: str | None,
        category: str | None,
        tags: list[str],
        recursive: bool,
        enabled: bool,
        interval_minutes: int,
    ) -> dict[str, Any]:
        root = self._normalize_root_path(root_path)
        include = self._normalize_relative_folder(include_subfolder)
        source_path = self._source_path(root, include)
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Watched folder does not exist: {source_path}")

        default_library_folder = self._default_library_folder(root, include)
        normalized_alias = self._normalize_text(alias) or self._normalize_text(display_name)
        now = datetime.now(timezone.utc).isoformat()
        record = WatchedFolderRecord(
            watch_id=uuid4().hex,
            alias=normalized_alias,
            display_name=normalized_alias or default_library_folder,
            root_path=str(root),
            include_subfolder=include,
            library_folder=normalize_folder_path(library_folder or default_library_folder),
            category=self._normalize_text(category) or "watched",
            tags=self._normalize_tags(tags),
            recursive=bool(recursive),
            enabled=bool(enabled),
            interval_minutes=max(1, min(int(interval_minutes), 1440)),
            created_at=now,
        )

        with self._lock:
            records = self._load_records_locked()
            records.append(record)
            self._write_records_locked(records)

        return self._record_to_payload(record)

    def update_watcher_alias(self, watch_id: str, alias: str | None) -> dict[str, Any]:
        return self.update_watcher(watch_id, alias=alias)

    def update_watcher(self, watch_id: str, **updates: Any) -> dict[str, Any]:
        normalized_watch_id = str(watch_id or "").strip()
        if not normalized_watch_id:
            raise ValueError("Watch ID is required.")

        with self._lock:
            records = self._load_records_locked()
            updated_records: list[WatchedFolderRecord] = []
            updated_record: WatchedFolderRecord | None = None
            for record in records:
                if record.watch_id != normalized_watch_id:
                    updated_records.append(record)
                    continue
                normalized_alias = (
                    self._normalize_text(updates.get("alias"))
                    if "alias" in updates
                    else record.alias
                )
                normalized_category = (
                    self._normalize_text(updates.get("category")) or "watched"
                    if "category" in updates
                    else record.category
                )
                normalized_tags = (
                    self._normalize_tags(updates.get("tags") or [])
                    if "tags" in updates
                    else record.tags
                )
                interval_minutes = (
                    max(1, min(int(updates["interval_minutes"]), 1440))
                    if updates.get("interval_minutes") is not None
                    else record.interval_minutes
                )
                updated_record = replace(
                    record,
                    alias=normalized_alias,
                    display_name=normalized_alias or self._default_library_folder(
                        Path(record.root_path),
                        record.include_subfolder,
                    ),
                    category=normalized_category,
                    tags=normalized_tags,
                    recursive=(
                        bool(updates["recursive"])
                        if updates.get("recursive") is not None
                        else record.recursive
                    ),
                    enabled=(
                        bool(updates["enabled"])
                        if updates.get("enabled") is not None
                        else record.enabled
                    ),
                    interval_minutes=interval_minutes,
                )
                updated_records.append(updated_record)

            if updated_record is None:
                raise ValueError("Watched folder not found.")

            self._write_records_locked(updated_records)
            return self._record_to_payload(updated_record)

    def relocate_library_folder(self, folder_id: str, relocated_folder_id: str) -> list[str]:
        normalized_folder_id = normalize_folder_path(folder_id)
        normalized_relocated_folder_id = normalize_folder_path(relocated_folder_id)
        if not normalized_folder_id or not normalized_relocated_folder_id:
            return []

        updated_watch_ids: list[str] = []
        with self._sync_lock:
            with self._lock:
                records = self._load_records_locked()
                updated_records: list[WatchedFolderRecord] = []
                for record in records:
                    current_folder = normalize_folder_path(record.library_folder)
                    if not self._folder_is_within(current_folder, normalized_folder_id):
                        updated_records.append(record)
                        continue
                    suffix = current_folder[len(normalized_folder_id):].lstrip("/")
                    relocated_folder = normalize_folder_path(
                        f"{normalized_relocated_folder_id}/{suffix}"
                        if suffix
                        else normalized_relocated_folder_id
                    )
                    updated_records.append(replace(record, library_folder=relocated_folder))
                    updated_watch_ids.append(record.watch_id)

                if updated_watch_ids:
                    self._write_records_locked(updated_records)
        return updated_watch_ids

    def delete_watcher(self, watch_id: str) -> bool:
        normalized_watch_id = str(watch_id or "").strip()
        if not normalized_watch_id:
            return False

        with self._sync_lock:
            with self._lock:
                records = self._load_records_locked()
                next_records = [
                    record for record in records if record.watch_id != normalized_watch_id
                ]
                deleted = len(next_records) != len(records)
                if deleted:
                    self._write_records_locked(next_records)
        return deleted

    def unsynchronize_library_folder(self, folder_id: str) -> list[dict[str, Any]]:
        normalized_folder_id = normalize_folder_path(folder_id)
        if not normalized_folder_id:
            return []

        with self._sync_lock:
            matching_records = self._find_watchers_for_library_folder_locked(
                normalized_folder_id
            )
            removed_records = self._delete_watchers_by_id_locked(
                {record.watch_id for record in matching_records}
            )

        return [self._record_to_payload(record) for record in removed_records]

    def delete_library_folder_and_unsynchronize(
        self,
        folder_id: str,
    ) -> tuple[Any, list[dict[str, Any]]]:
        normalized_folder_id = normalize_folder_path(folder_id)
        with self._sync_lock:
            matching_records = self._find_watchers_for_library_folder_locked(
                normalized_folder_id
            )
            outcome = self._ingestion_service.delete_folder(
                folder_id=normalized_folder_id,
            )
            removed_records = self._delete_watchers_by_id_locked(
                {record.watch_id for record in matching_records}
            )
        return outcome, [
            self._record_to_payload(record) for record in removed_records
        ]

    def _find_watchers_for_library_folder_locked(
        self,
        normalized_folder_id: str,
    ) -> list[WatchedFolderRecord]:
        with self._lock:
            records = self._load_records_locked()
        return [
            record
            for record in records
            if self._folder_is_within(record.library_folder, normalized_folder_id)
            or self._folder_is_within(
                self._resolve_library_folder(record),
                normalized_folder_id,
            )
        ]

    def _delete_watchers_by_id_locked(
        self,
        watch_ids: set[str],
    ) -> list[WatchedFolderRecord]:
        if not watch_ids:
            return []
        with self._lock:
            records = self._load_records_locked()
            removed_records = [
                record for record in records if record.watch_id in watch_ids
            ]
            if removed_records:
                self._write_records_locked(
                    [record for record in records if record.watch_id not in watch_ids]
                )
        return removed_records

    def sync_watcher(self, watch_id: str) -> WatchSyncResult:
        normalized_watch_id = str(watch_id or "").strip()
        if not normalized_watch_id:
            raise ValueError("Watch ID is required.")

        records = self._load_records()
        record = next((item for item in records if item.watch_id == normalized_watch_id), None)
        if record is None:
            raise ValueError("Watched folder not found.")
        return self._sync_record(record)

    def sync_all(self, *, force: bool = False) -> list[WatchSyncResult]:
        records = self._load_records()
        now = datetime.now(timezone.utc)
        results: list[WatchSyncResult] = []
        for record in records:
            if not record.enabled:
                continue
            if not force and not self._record_is_due(record, now):
                continue
            results.append(self._sync_record(record))
        return results

    def _run_scheduler(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sync_all(force=False)
            except Exception:
                # Individual sync failures are persisted per watcher; keep the scheduler alive.
                pass
            self._stop_event.wait(max(10, self._settings.watched_folder_poll_seconds))

    def _sync_record(self, record: WatchedFolderRecord) -> WatchSyncResult:
        with self._sync_lock:
            result = self._sync_record_locked(record)
            self._persist_sync_result(result)
            if result.semantic_index_rebuilt and self._on_library_changed is not None:
                self._on_library_changed()
            return result

    def _sync_record_locked(self, record: WatchedFolderRecord) -> WatchSyncResult:
        synced_at = datetime.now(timezone.utc).isoformat()
        source_path = self._record_source_path(record)
        relocated_from: Path | None = None
        try:
            if not source_path.exists() or not source_path.is_dir():
                relocated_source_path = self._find_relocated_source_path(source_path)
                if relocated_source_path is None:
                    raise ValueError(f"Watched folder does not exist: {source_path}")
                relocated_from = source_path
                source_path = relocated_source_path
                record = replace(
                    record,
                    root_path=str(relocated_source_path),
                    include_subfolder=None,
                )
                self._persist_source_path(
                    record.watch_id,
                    root_path=record.root_path,
                    include_subfolder=record.include_subfolder,
                )

            resolved_library_folder = self._resolve_library_folder(record)
            if resolved_library_folder != normalize_folder_path(record.library_folder):
                record = replace(record, library_folder=resolved_library_folder)
                self._persist_library_folder(record.watch_id, resolved_library_folder)

            pending_files, scanned_count, skipped_count = self._collect_pending_files(record, source_path)
            if not pending_files:
                return WatchSyncResult(
                    watch_id=record.watch_id,
                    display_name=record.display_name,
                    source_path=str(source_path),
                    status="success",
                    message=self._append_relocation_message(
                        f"Scanned {scanned_count} file(s). No new or changed documents found.",
                        relocated_from=relocated_from,
                        relocated_to=source_path,
                    ),
                    scanned_count=scanned_count,
                    imported_count=0,
                    created_count=0,
                    updated_count=0,
                    unchanged_count=0,
                    skipped_count=skipped_count,
                    error_count=0,
                    semantic_index_rebuilt=False,
                    synced_at=synced_at,
                )

            uploads: list[dict[str, Any]] = []
            file_errors: list[str] = []
            for pending_file in pending_files:
                try:
                    uploads.append(self._build_upload(record, source_path, pending_file))
                except (OSError, RuntimeError, ValueError) as exc:
                    if pending_file.path.suffix.lower() in PDF_UPLOAD_SUFFIXES:
                        logger.error(
                            "PDF ingestion failed before watched-folder upload: path=%s; error=%s",
                            pending_file.relative_path,
                            exc,
                        )
                    file_errors.append(self._format_file_error(pending_file, exc))

            if uploads:
                outcome = self._ingestion_service.ingest_upload_batch(
                    uploads=uploads,
                    continue_on_error=True,
                )
                file_errors.extend(outcome.failed_uploads)
            else:
                outcome = None

            imported_count = 0 if outcome is None else len(outcome.uploaded_documents)
            error_count = len(file_errors)
            if outcome is None:
                outcome_message = "No documents were imported."
            else:
                outcome_message = outcome.message
            message = self._append_file_errors(outcome_message, file_errors)
            message = self._append_relocation_message(
                message,
                relocated_from=relocated_from,
                relocated_to=source_path,
            )
            return WatchSyncResult(
                watch_id=record.watch_id,
                display_name=record.display_name,
                source_path=str(source_path),
                status=(
                    "success"
                    if error_count == 0
                    else "partial"
                    if imported_count > 0
                    else "error"
                ),
                message=message,
                scanned_count=scanned_count,
                imported_count=imported_count,
                created_count=0 if outcome is None else outcome.created_count,
                updated_count=0 if outcome is None else outcome.updated_count,
                unchanged_count=0 if outcome is None else outcome.unchanged_count,
                skipped_count=skipped_count,
                error_count=error_count,
                semantic_index_rebuilt=(
                    False if outcome is None else outcome.semantic_index_rebuilt
                ),
                synced_at=synced_at,
            )
        except Exception as exc:
            return WatchSyncResult(
                watch_id=record.watch_id,
                display_name=record.display_name,
                source_path=str(source_path),
                status="error",
                message=str(exc),
                scanned_count=0,
                imported_count=0,
                created_count=0,
                updated_count=0,
                unchanged_count=0,
                skipped_count=0,
                error_count=1,
                semantic_index_rebuilt=False,
                synced_at=synced_at,
            )

    def _collect_pending_files(
        self,
        record: WatchedFolderRecord,
        source_path: Path,
    ) -> tuple[list[PendingWatchedFile], int, int]:
        existing_documents = load_json_documents(self._settings.docstore_json_path)
        existing_upload_keys = {
            document.upload_key
            for document in existing_documents
            if document.upload_key
        }
        existing_document_by_watch_path: dict[str, DocumentRecord] = {}
        for document in existing_documents:
            if not document.upload_key:
                continue
            watch_path_key = self._watch_path_key_from_upload_key(document.upload_key)
            if watch_path_key:
                existing_document_by_watch_path[watch_path_key] = document

        pending_files: list[PendingWatchedFile] = []
        scanned_count = 0
        skipped_count = 0
        for path in self._iter_supported_files(source_path, recursive=record.recursive):
            scanned_count += 1
            relative_path = path.relative_to(source_path).as_posix()
            upload_key_base = self._build_watch_upload_key_base(
                watch_id=record.watch_id,
                relative_path=relative_path,
                modified_ms=int(path.stat().st_mtime * 1000),
            )
            upload_key = f"upload:{upload_key_base}"
            watch_path_key = self._build_watch_path_key(
                watch_id=record.watch_id,
                relative_path=relative_path,
            )
            existing_document = existing_document_by_watch_path.get(watch_path_key)
            expected_source_auto_tags = self._build_watched_auto_tags(record, path)
            source_tags_are_current = (
                existing_document is not None
                and self._tags_match(
                    self._source_auto_tags(existing_document),
                    expected_source_auto_tags,
                )
            )
            if upload_key in existing_upload_keys and source_tags_are_current:
                skipped_count += 1
                continue

            pending_files.append(
                PendingWatchedFile(
                    path=path,
                    relative_path=relative_path,
                    upload_key_base=upload_key_base,
                    target_document_id=(
                        existing_document.document_id
                        if existing_document is not None
                        else None
                    ),
                    existing_manual_tags=self._existing_manual_tags(
                        existing_document,
                        watcher_tags=record.tags,
                    ),
                )
            )

        return pending_files, scanned_count, skipped_count

    def _build_upload(
        self,
        record: WatchedFolderRecord,
        source_path: Path,
        pending_file: PendingWatchedFile,
    ) -> dict[str, Any]:
        path = pending_file.path
        suffix = path.suffix.lower()
        relative_directory = str(Path(pending_file.relative_path).parent).replace("\\", "/")
        if relative_directory == ".":
            relative_directory = ""
        target_folder = self._join_library_folder(record.library_folder, relative_directory)
        stat = path.stat()

        upload: dict[str, Any] = {
            "filename": path.name,
            "client_path": pending_file.relative_path,
            "client_modified_ms": int(stat.st_mtime * 1000),
            "upload_key_base": pending_file.upload_key_base,
            "source_url": path.resolve().as_uri(),
            "category": record.category,
            "folder": target_folder,
            "tags": pending_file.existing_manual_tags,
            "source_auto_tags": self._build_watched_auto_tags(record, path),
            "similarity_policy": "replace",
        }
        if pending_file.target_document_id:
            upload["similarity_target_document_id"] = pending_file.target_document_id

        if suffix in BINARY_WATCH_SUFFIXES:
            upload["content_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            upload["content_text"] = path.read_text(encoding="utf-8", errors="ignore")
        return upload

    def _build_watched_auto_tags(
        self,
        record: WatchedFolderRecord,
        path: Path,
    ) -> list[str]:
        return normalize_tag_values([*record.tags, *infer_watched_path_tags(path)])

    def _format_file_error(
        self,
        pending_file: PendingWatchedFile,
        error: Exception,
    ) -> str:
        if self._is_offline_cloud_file(pending_file.path):
            return (
                f"{pending_file.relative_path}: online-only cloud file is unavailable. "
                "Start the sync client and make the file available offline, then sync again."
            )
        return f"{pending_file.relative_path}: {error}"

    def _is_offline_cloud_file(self, path: Path) -> bool:
        try:
            file_attributes = int(getattr(path.stat(), "st_file_attributes", 0))
        except OSError:
            return False
        return bool(file_attributes & WINDOWS_CLOUD_PLACEHOLDER_ATTRIBUTES)

    def _append_file_errors(self, message: str, file_errors: list[str]) -> str:
        if not file_errors:
            return message
        visible_errors = "; ".join(file_errors[:3])
        hidden_count = len(file_errors) - 3
        if hidden_count > 0:
            visible_errors = f"{visible_errors}; and {hidden_count} more"
        return (
            f"{message} {len(file_errors)} file(s) could not be imported: "
            f"{visible_errors}"
        )

    def _append_relocation_message(
        self,
        message: str,
        *,
        relocated_from: Path | None,
        relocated_to: Path,
    ) -> str:
        if relocated_from is None:
            return message
        return (
            f"Automatically repaired the synchronized path after its project folder moved "
            f"from `{relocated_from}` to `{relocated_to}`. {message}"
        )

    def _source_auto_tags(self, document: DocumentRecord) -> list[str]:
        folder_auto_keys = {
            tag.lower() for tag in build_folder_auto_tags(document.folder)
        }
        return [
            tag
            for tag in normalize_tag_values(document.auto_tags)
            if tag.lower() not in folder_auto_keys
        ]

    def _existing_manual_tags(
        self,
        document: DocumentRecord | None,
        *,
        watcher_tags: list[str],
    ) -> list[str]:
        if document is None:
            return []
        auto_tag_keys = {tag.lower() for tag in document.auto_tags}
        watcher_tag_keys = {tag.lower() for tag in watcher_tags}
        return [
            tag
            for tag in normalize_tag_values(document.tags)
            if tag.lower() not in auto_tag_keys
            and tag.lower() not in watcher_tag_keys
        ]

    def _tags_match(self, left: list[str], right: list[str]) -> bool:
        return {tag.lower() for tag in left} == {tag.lower() for tag in right}

    def _persist_sync_result(self, result: WatchSyncResult) -> None:
        with self._lock:
            records = self._load_records_locked()
            updated_records: list[WatchedFolderRecord] = []
            for record in records:
                if record.watch_id != result.watch_id:
                    updated_records.append(record)
                    continue
                updated_records.append(
                    WatchedFolderRecord(
                        watch_id=record.watch_id,
                        alias=record.alias,
                        display_name=record.display_name,
                        root_path=record.root_path,
                        include_subfolder=record.include_subfolder,
                        library_folder=record.library_folder,
                        category=record.category,
                        tags=record.tags,
                        recursive=record.recursive,
                        enabled=record.enabled,
                        interval_minutes=record.interval_minutes,
                        created_at=record.created_at,
                        last_sync_at=result.synced_at,
                        last_status=result.status,
                        last_message=result.message,
                        last_scanned_count=result.scanned_count,
                        last_imported_count=result.imported_count,
                        last_created_count=result.created_count,
                        last_updated_count=result.updated_count,
                        last_unchanged_count=result.unchanged_count,
                        last_skipped_count=result.skipped_count,
                        last_error_count=result.error_count,
                    )
                )
            self._write_records_locked(updated_records)

    def _persist_library_folder(self, watch_id: str, library_folder: str) -> None:
        with self._lock:
            records = self._load_records_locked()
            updated_records = [
                replace(record, library_folder=library_folder)
                if record.watch_id == watch_id
                else record
                for record in records
            ]
            self._write_records_locked(updated_records)

    def _persist_source_path(
        self,
        watch_id: str,
        *,
        root_path: str,
        include_subfolder: str | None,
    ) -> None:
        with self._lock:
            records = self._load_records_locked()
            updated_records = [
                replace(
                    record,
                    root_path=root_path,
                    include_subfolder=include_subfolder,
                )
                if record.watch_id == watch_id
                else record
                for record in records
            ]
            self._write_records_locked(updated_records)

    def _resolve_library_folder(self, record: WatchedFolderRecord) -> str:
        configured_folder = normalize_folder_path(record.library_folder)
        inferred_folders: dict[str, int] = {}
        for document in load_json_documents(self._settings.docstore_json_path):
            relative_path = self._watch_relative_path_from_upload_key(
                document.upload_key,
                watch_id=record.watch_id,
            )
            if relative_path is None:
                continue

            document_folder = normalize_folder_path(document.folder)
            relative_parent = normalize_folder_path(
                relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
            )
            inferred_folder = document_folder
            if relative_parent:
                suffix = f"/{relative_parent}"
                if document_folder.lower().endswith(suffix.lower()):
                    inferred_folder = document_folder[:-len(suffix)]
            inferred_folder = normalize_folder_path(inferred_folder)
            if inferred_folder:
                inferred_folders[inferred_folder] = inferred_folders.get(inferred_folder, 0) + 1

        if not inferred_folders:
            return configured_folder
        return max(
            inferred_folders,
            key=lambda folder: (inferred_folders[folder], folder == configured_folder),
        )

    def _record_is_due(self, record: WatchedFolderRecord, now: datetime) -> bool:
        if not record.last_sync_at:
            return True
        try:
            last_sync = datetime.fromisoformat(record.last_sync_at)
        except ValueError:
            return True
        return now >= last_sync + timedelta(minutes=record.interval_minutes)

    def _record_to_payload(self, record: WatchedFolderRecord) -> dict[str, Any]:
        next_sync_at: str | None = None
        if record.enabled:
            if record.last_sync_at:
                try:
                    next_sync_at = (
                        datetime.fromisoformat(record.last_sync_at)
                        + timedelta(minutes=record.interval_minutes)
                    ).isoformat()
                except ValueError:
                    next_sync_at = None
            else:
                next_sync_at = datetime.now(timezone.utc).isoformat()

        source_path = self._record_source_path(record)
        return {
            "watch_id": record.watch_id,
            "alias": record.alias,
            "display_name": self._record_display_name(record),
            "root_path": record.root_path,
            "include_subfolder": record.include_subfolder,
            "source_path": str(source_path),
            "library_folder": record.library_folder,
            "category": record.category,
            "tags": record.tags,
            "recursive": record.recursive,
            "enabled": record.enabled,
            "interval_minutes": record.interval_minutes,
            "last_sync_at": record.last_sync_at,
            "last_status": record.last_status,
            "last_message": record.last_message,
            "last_scanned_count": record.last_scanned_count,
            "last_imported_count": record.last_imported_count,
            "last_created_count": record.last_created_count,
            "last_updated_count": record.last_updated_count,
            "last_unchanged_count": record.last_unchanged_count,
            "last_skipped_count": record.last_skipped_count,
            "last_error_count": record.last_error_count,
            "next_sync_at": next_sync_at,
        }

    def _iter_supported_files(self, source_path: Path, *, recursive: bool) -> list[Path]:
        iterator = source_path.rglob("*") if recursive else source_path.iterdir()
        files: list[Path] = []
        for path in iterator:
            if not path.is_file():
                continue
            if path.name.startswith("~$") or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUPPORTED_UPLOAD_SUFFIXES:
                continue
            files.append(path)
        return sorted(files, key=lambda item: item.as_posix().lower())

    def _load_records(self) -> list[WatchedFolderRecord]:
        with self._lock:
            return self._load_records_locked()

    def _load_records_locked(self) -> list[WatchedFolderRecord]:
        if not self._settings.watched_folders_path.exists():
            return []
        payload = json.loads(self._settings.watched_folders_path.read_text(encoding="utf-8"))
        items = payload.get("watched_folders", []) if isinstance(payload, dict) else []
        return [
            WatchedFolderRecord(
                watch_id=str(item.get("watch_id") or uuid4().hex),
                alias=self._normalize_text(item.get("alias")),
                display_name=str(item.get("display_name") or "Watched folder"),
                root_path=str(item.get("root_path") or ""),
                include_subfolder=self._normalize_relative_folder(item.get("include_subfolder")),
                library_folder=normalize_folder_path(item.get("library_folder") or "watched"),
                category=str(item.get("category") or "watched"),
                tags=self._normalize_tags(item.get("tags") or []),
                recursive=bool(item.get("recursive", True)),
                enabled=bool(item.get("enabled", True)),
                interval_minutes=max(1, min(int(item.get("interval_minutes") or 30), 1440)),
                created_at=str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
                last_sync_at=item.get("last_sync_at"),
                last_status=item.get("last_status"),
                last_message=item.get("last_message"),
                last_scanned_count=int(item.get("last_scanned_count") or 0),
                last_imported_count=int(item.get("last_imported_count") or 0),
                last_created_count=int(item.get("last_created_count") or 0),
                last_updated_count=int(item.get("last_updated_count") or 0),
                last_unchanged_count=int(item.get("last_unchanged_count") or 0),
                last_skipped_count=int(item.get("last_skipped_count") or 0),
                last_error_count=int(item.get("last_error_count") or 0),
            )
            for item in items
        ]

    def _write_records_locked(self, records: list[WatchedFolderRecord]) -> None:
        self._settings.watched_folders_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "watched_folders": [
                self._record_to_json(record)
                for record in records
            ]
        }
        self._settings.watched_folders_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _record_to_json(self, record: WatchedFolderRecord) -> dict[str, Any]:
        return {
            "watch_id": record.watch_id,
            "alias": record.alias,
            "display_name": record.display_name,
            "root_path": record.root_path,
            "include_subfolder": record.include_subfolder,
            "library_folder": record.library_folder,
            "category": record.category,
            "tags": record.tags,
            "recursive": record.recursive,
            "enabled": record.enabled,
            "interval_minutes": record.interval_minutes,
            "created_at": record.created_at,
            "last_sync_at": record.last_sync_at,
            "last_status": record.last_status,
            "last_message": record.last_message,
            "last_scanned_count": record.last_scanned_count,
            "last_imported_count": record.last_imported_count,
            "last_created_count": record.last_created_count,
            "last_updated_count": record.last_updated_count,
            "last_unchanged_count": record.last_unchanged_count,
            "last_skipped_count": record.last_skipped_count,
            "last_error_count": record.last_error_count,
        }

    def _normalize_root_path(self, value: str) -> Path:
        normalized = str(value or "").strip().strip('"')
        if not normalized:
            raise ValueError("Local folder path is required.")
        return Path(normalized).expanduser().resolve()

    def _normalize_relative_folder(self, value: Any) -> str | None:
        normalized = normalize_folder_path(str(value or ""))
        return normalized or None

    def _source_path(self, root_path: Path, include_subfolder: str | None) -> Path:
        if not include_subfolder:
            return root_path
        return (root_path / Path(include_subfolder)).resolve()

    def _record_source_path(self, record: WatchedFolderRecord) -> Path:
        return self._source_path(Path(record.root_path), record.include_subfolder)

    def _find_relocated_source_path(self, missing_source_path: Path) -> Path | None:
        missing_path = missing_source_path.resolve()
        existing_ancestor = missing_path
        missing_parts: list[str] = []
        while not existing_ancestor.exists():
            parent = existing_ancestor.parent
            if parent == existing_ancestor:
                return None
            missing_parts.insert(0, existing_ancestor.name)
            existing_ancestor = parent

        if not existing_ancestor.is_dir() or not missing_parts:
            return None

        project_number = self._project_number_from_folder_name(missing_parts[0])
        if project_number is None:
            return None

        relocated_candidates: list[Path] = []
        try:
            sibling_candidates = existing_ancestor.iterdir()
            for candidate in sibling_candidates:
                if not candidate.is_dir():
                    continue
                if self._project_number_from_folder_name(candidate.name) != project_number:
                    continue
                relocated_path = candidate.joinpath(*missing_parts[1:]).resolve()
                if relocated_path.is_dir():
                    relocated_candidates.append(relocated_path)
        except OSError:
            return None

        if len(relocated_candidates) != 1:
            return None
        return relocated_candidates[0]

    def _project_number_from_folder_name(self, folder_name: str) -> str | None:
        match = re.match(r"^\s*(\d+)\s*[.\-]", str(folder_name or ""))
        if match is None:
            return None
        return match.group(1).lstrip("0") or "0"

    def _record_display_name(self, record: WatchedFolderRecord) -> str:
        return (
            self._normalize_text(record.alias)
            or self._normalize_text(record.display_name)
            or self._default_library_folder(Path(record.root_path), record.include_subfolder)
        )

    def _default_library_folder(self, root_path: Path, include_subfolder: str | None) -> str:
        project_name = root_path.name.strip() or "Dropbox Project"
        if include_subfolder:
            return normalize_folder_path(f"{project_name}/{include_subfolder}")
        return normalize_folder_path(project_name)

    def _join_library_folder(self, base_folder: str, relative_directory: str) -> str:
        normalized_base = normalize_folder_path(base_folder)
        normalized_relative = normalize_folder_path(relative_directory)
        if normalized_base and normalized_relative:
            return normalize_folder_path(f"{normalized_base}/{normalized_relative}")
        return normalized_base or normalized_relative or "watched"

    def _build_watch_upload_key_base(
        self,
        *,
        watch_id: str,
        relative_path: str,
        modified_ms: int,
    ) -> str:
        return f"{self._build_watch_path_key(watch_id=watch_id, relative_path=relative_path)}::mtime:{modified_ms}"

    def _build_watch_path_key(self, *, watch_id: str, relative_path: str) -> str:
        normalized_relative_path = "/".join(
            segment.strip()
            for segment in str(relative_path or "").replace("\\", "/").split("/")
            if segment.strip()
        )
        return f"watch:{watch_id}:path:{normalized_relative_path}"

    def _watch_path_key_from_upload_key(self, upload_key: str) -> str | None:
        if not upload_key.startswith("upload:watch:"):
            return None
        payload = upload_key.removeprefix("upload:")
        payload = payload.split("::item:", 1)[0]
        payload = payload.split("::mtime:", 1)[0]
        return payload or None

    def _watch_relative_path_from_upload_key(
        self,
        upload_key: str | None,
        *,
        watch_id: str,
    ) -> str | None:
        if not upload_key:
            return None
        watch_path_key = self._watch_path_key_from_upload_key(upload_key)
        prefix = f"watch:{watch_id}:path:"
        if not watch_path_key or not watch_path_key.startswith(prefix):
            return None
        return watch_path_key.removeprefix(prefix)

    def _folder_is_within(self, folder_id: str, parent_folder_id: str) -> bool:
        normalized_folder = normalize_folder_path(folder_id).lower()
        normalized_parent = normalize_folder_path(parent_folder_id).lower()
        return normalized_folder == normalized_parent or normalized_folder.startswith(
            f"{normalized_parent}/"
        )

    def _normalize_text(self, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _normalize_tags(self, value: Any) -> list[str]:
        items = value if isinstance(value, list) else str(value or "").split(",")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            tag = str(item or "").strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(tag)
        return normalized
