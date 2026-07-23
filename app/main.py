from __future__ import annotations

import base64
import platform
from pathlib import Path
import subprocess
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from openai import OpenAIError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.conversation_store import SavedConversationStore
from app.datastore import build_folder_records, build_document_store, load_folder_registry, normalize_folder_path
from app.document_generator import ContextDocumentGenerator
from app.ingestion import DocumentIngestionService, SimilarDocumentConflictError
from app.watch_folders import WatchedFolderService
from app.models import (
    ChatRequest,
    ChatResponse,
    ConversationDeleteResponse,
    ConversationListResponse,
    ConversationSaveRequest,
    ConversationSaveResponse,
    ConversationSettingsRequest,
    ConversationSettingsResponse,
    DocumentDeleteRequest,
    DocumentDeleteResponse,
    DocumentDetailResponse,
    DocumentGenerationRequest,
    DocumentGenerationResponse,
    DocumentUploadBatchRequest,
    FolderCreateRequest,
    FolderCreateResponse,
    FolderDeleteRequest,
    FolderDeleteResponse,
    FolderMoveRequest,
    FolderMoveResponse,
    FolderRenameRequest,
    FolderRenameResponse,
    DocumentLibraryResponse,
    DocumentMetadataUpdateRequest,
    DocumentMetadataUpdateResponse,
    DocumentTagUpdateRequest,
    DocumentTagUpdateResponse,
    DocumentUploadRequest,
    DocumentUploadResponse,
    DocumentSummary,
    FolderSummary,
    LocalFolderBrowseResponse,
    SavedConversationDetail,
    UploadedDocumentSummary,
    WatchedFolderCreateRequest,
    WatchedFolderCreateResponse,
    WatchedFolderDeleteResponse,
    WatchedFolderListResponse,
    WatchedFolderSummary,
    WatchedFolderSyncResponse,
    WatchedFolderSyncResult,
    WatchedFolderUpdateRequest,
    WatchedFolderUpdateResponse,
)
from app.openai_agent import BusinessKnowledgeAgent, SessionManager
from app.ocr import get_ocr_runtime_status


settings = get_settings()
document_store = build_document_store(settings)
ingestion_service = DocumentIngestionService(settings)
watch_folder_service = WatchedFolderService(settings, ingestion_service)
conversation_store = SavedConversationStore(settings.saved_conversations_path)
session_manager = SessionManager(
    settings.session_ttl_minutes,
    saved_conversations=conversation_store,
)
document_generator = ContextDocumentGenerator(settings, document_store)
agent = BusinessKnowledgeAgent(
    settings,
    document_store,
    session_manager,
    document_generator=document_generator,
)

app = FastAPI(title=settings.app_title)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_TEMPLATE_PATH = STATIC_DIR / "index.html"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_url(filename: str) -> str:
    asset_path = STATIC_DIR / filename
    version = int(asset_path.stat().st_mtime) if asset_path.exists() else 0
    return f"/static/{quote(filename)}?v={version}"


def _render_index_html() -> str:
    template = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{APP_CSS_URL}}", _asset_url("app.css"))
        .replace("{{APP_JS_URL}}", _asset_url("app.js"))
    )


def _invalidate_document_store_cache() -> None:
    document_store.invalidate_cache()


watch_folder_service.set_library_changed_callback(_invalidate_document_store_cache)


def _watch_summary_from_payload(payload: dict[str, object]) -> WatchedFolderSummary:
    return WatchedFolderSummary.model_validate(payload)


def _watch_sync_result_from_payload(result: object) -> WatchedFolderSyncResult:
    return WatchedFolderSyncResult.model_validate(result.__dict__)


def _open_local_folder_picker() -> str | None:
    if platform.system() == "Windows":
        return _open_windows_folder_picker()
    return _open_tk_folder_picker()


def _open_windows_folder_picker() -> str | None:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Opacity = 0
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$owner.Activate()

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "Select a folder to synchronize"
$dialog.Filter = "Folders|*.folder"
$dialog.CheckFileExists = $false
$dialog.CheckPathExists = $true
$dialog.ValidateNames = $false
$dialog.DereferenceLinks = $true
$dialog.RestoreDirectory = $true
$dialog.FileName = "Select this folder"
$result = $dialog.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  $selectedPath = [System.IO.Path]::GetDirectoryName($dialog.FileName)
  if ([System.IO.Directory]::Exists($selectedPath)) {
    [Console]::Out.WriteLine($selectedPath)
  }
}
$dialog.Dispose()
$owner.Close()
$owner.Dispose()
"""
    executable_candidates = ["powershell.exe", "powershell", "pwsh.exe", "pwsh"]
    last_error: Exception | None = None
    for executable in executable_candidates:
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-STA",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
            )
        except FileNotFoundError as exc:
            last_error = exc
            continue

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise RuntimeError(
                stderr or "Windows folder picker failed to open."
            )

        selected_path = completed.stdout.strip()
        return selected_path or None

    if last_error is not None:
        return None
    return None


def _open_tk_folder_picker() -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Native folder picker is not available in this Python environment.") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected_path = filedialog.askdirectory(
            parent=root,
            title="Select folder to monitor",
            mustexist=True,
        )
    finally:
        root.destroy()

    normalized_path = str(selected_path or "").strip()
    return normalized_path or None


@app.on_event("startup")
def start_watched_folder_scheduler() -> None:
    watch_folder_service.start()


@app.on_event("shutdown")
def stop_watched_folder_scheduler() -> None:
    watch_folder_service.stop()


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "app_title": settings.app_title,
        "model": settings.openai_model,
        "docstore_backend": settings.docstore_backend,
        "openai_configured": bool(settings.openai_api_key),
        "pdf_ocr": get_ocr_runtime_status(settings),
    }


@app.post("/api/local-folders/browse", response_model=LocalFolderBrowseResponse)
def browse_local_folder() -> LocalFolderBrowseResponse:
    try:
        selected_path = _open_local_folder_picker()
        if selected_path is None:
            return LocalFolderBrowseResponse(
                path=None,
                cancelled=True,
                message="Folder selection cancelled.",
            )
        return LocalFolderBrowseResponse(
            path=selected_path,
            cancelled=False,
            message="Folder selected.",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while opening the local folder picker.",
        ) from exc


@app.get("/api/watch-folders", response_model=WatchedFolderListResponse)
def list_watched_folders() -> WatchedFolderListResponse:
    return WatchedFolderListResponse(
        watched_folders=[
            _watch_summary_from_payload(item)
            for item in watch_folder_service.list_watchers()
        ]
    )


@app.post("/api/watch-folders", response_model=WatchedFolderCreateResponse)
def create_watched_folder(request: WatchedFolderCreateRequest) -> WatchedFolderCreateResponse:
    try:
        watched_folder = watch_folder_service.create_watcher(
            root_path=request.root_path,
            include_subfolder=request.include_subfolder,
            alias=request.alias,
            display_name=request.display_name,
            library_folder=request.library_folder,
            category=request.category,
            tags=request.tags,
            recursive=request.recursive,
            enabled=request.enabled,
            interval_minutes=request.interval_minutes,
        )
        return WatchedFolderCreateResponse(
            watched_folder=_watch_summary_from_payload(watched_folder),
            message="Watched folder added.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while adding the watched folder.",
        ) from exc


@app.patch("/api/watch-folders/{watch_id}", response_model=WatchedFolderUpdateResponse)
def update_watched_folder(watch_id: str, request: WatchedFolderUpdateRequest) -> WatchedFolderUpdateResponse:
    try:
        watched_folder = watch_folder_service.update_watcher(
            watch_id,
            **request.model_dump(exclude_unset=True),
        )
        return WatchedFolderUpdateResponse(
            watched_folder=_watch_summary_from_payload(watched_folder),
            message="Watched folder settings updated.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while updating the watched folder.",
        ) from exc


@app.post("/api/watch-folders/sync", response_model=WatchedFolderSyncResponse)
def sync_all_watched_folders() -> WatchedFolderSyncResponse:
    try:
        results = watch_folder_service.sync_all(force=True)
        if any(result.semantic_index_rebuilt for result in results):
            _invalidate_document_store_cache()
        return WatchedFolderSyncResponse(
            results=[_watch_sync_result_from_payload(result) for result in results],
            message=f"Synced {len(results)} watched folder(s).",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while syncing watched folders.",
        ) from exc


@app.post("/api/watch-folders/{watch_id}/sync", response_model=WatchedFolderSyncResponse)
def sync_watched_folder(watch_id: str) -> WatchedFolderSyncResponse:
    try:
        result = watch_folder_service.sync_watcher(watch_id)
        if result.semantic_index_rebuilt:
            _invalidate_document_store_cache()
        return WatchedFolderSyncResponse(
            results=[_watch_sync_result_from_payload(result)],
            message=result.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while syncing the watched folder.",
        ) from exc


@app.delete("/api/watch-folders/{watch_id}", response_model=WatchedFolderDeleteResponse)
def delete_watched_folder(watch_id: str) -> WatchedFolderDeleteResponse:
    deleted = watch_folder_service.delete_watcher(watch_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Watched folder not found.")
    return WatchedFolderDeleteResponse(
        watch_id=watch_id,
        deleted=True,
        message=(
            "Folder path unsynchronized. Existing embedded documents and source files "
            "were not deleted."
        ),
    )


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        return agent.chat(
            request.conversation_id,
            request.message.strip(),
            request.source_mode,
            request.context_filter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while generating the response.",
        ) from exc


@app.get("/api/conversations", response_model=ConversationListResponse)
def list_conversations() -> ConversationListResponse:
    return ConversationListResponse(
        conversations=session_manager.list_saved_conversations(),
    )


@app.get("/api/conversations/{conversation_id}", response_model=SavedConversationDetail)
def get_conversation(conversation_id: str) -> SavedConversationDetail:
    conversation = session_manager.load_saved_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Saved conversation not found.")
    return conversation


@app.post("/api/conversations/save", response_model=ConversationSaveResponse)
def save_conversation(request: ConversationSaveRequest) -> ConversationSaveResponse:
    try:
        conversation = session_manager.save_conversation(
            request.conversation_id,
            title=request.title,
            source_mode=request.source_mode,
            context_filter=request.context_filter,
        )
        return ConversationSaveResponse(
            conversation=conversation,
            message="Conversation saved.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while saving the conversation.",
        ) from exc


@app.post("/api/conversations/settings", response_model=ConversationSettingsResponse)
def update_conversation_settings(
    request: ConversationSettingsRequest,
) -> ConversationSettingsResponse:
    try:
        conversation = session_manager.update_conversation_settings(
            request.conversation_id,
            request.source_mode,
            request.context_filter,
        )
        return ConversationSettingsResponse(
            conversation_id=request.conversation_id,
            source_mode=request.source_mode,
            context_filter=request.context_filter,
            saved=conversation is not None,
            conversation=conversation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while updating conversation settings.",
        ) from exc


@app.delete("/api/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
def delete_conversation(conversation_id: str) -> ConversationDeleteResponse:
    try:
        deleted = session_manager.delete_saved_conversation(conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Saved conversation not found.")
        return ConversationDeleteResponse(
            conversation_id=conversation_id,
            deleted=True,
            message="Saved conversation deleted.",
        )
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while deleting the conversation.",
        ) from exc


@app.get("/api/documents", response_model=DocumentLibraryResponse)
def list_documents() -> DocumentLibraryResponse:
    library = document_store.list_documents()
    folder_records = library.folders
    if settings.docstore_backend in {"json", "semantic"}:
        explicit_folder_ids = load_folder_registry(settings.docstore_folders_path)
        exact_folder_counts = {
            normalize_folder_path(folder.folder_id): folder.document_count
            for folder in library.folders
        }
        folder_records = build_folder_records(
            exact_folder_counts=exact_folder_counts,
            explicit_folder_ids=explicit_folder_ids,
        )

    return DocumentLibraryResponse(
        backend=library.backend,
        total_documents=library.total_documents,
        total_chunks=library.total_chunks,
        folders=[
            FolderSummary(
                folder_id=folder.folder_id,
                display_name=folder.display_name,
                document_count=folder.document_count,
            )
            for folder in folder_records
        ],
        documents=[
            DocumentSummary(
                document_id=document.document_id,
                title=document.title,
                category=document.category,
                folder=document.folder,
                tags=document.tags,
                summary=document.summary,
                source_url=document.source_url,
                updated_at=document.updated_at,
                chunk_count=document.chunk_count,
                embedded=document.embedded,
            )
            for document in library.documents
        ],
    )


@app.get("/api/documents/{document_id}", response_model=DocumentDetailResponse)
def get_document(document_id: str) -> DocumentDetailResponse:
    document = document_store.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    library = document_store.list_documents()
    metadata = next(
        (item for item in library.documents if item.document_id == document_id),
        None,
    )
    return DocumentDetailResponse(
        document_id=document.document_id,
        title=document.title,
        category=document.category,
        folder=document.folder,
        tags=document.tags,
        summary=document.summary,
        source_url=document.source_url,
        updated_at=document.updated_at,
        chunk_count=None if metadata is None else metadata.chunk_count,
        embedded=False if metadata is None else metadata.embedded,
        text=document.text,
    )


@app.post("/api/documents/upload", response_model=DocumentUploadResponse)
def upload_document(request: DocumentUploadRequest) -> DocumentUploadResponse:
    try:
        outcome = ingestion_service.ingest_upload(
            filename=request.filename,
            content_text=request.content_text,
            content_base64=request.content_base64,
            client_path=request.client_path,
            client_modified_ms=request.client_modified_ms,
            similarity_policy=request.similarity_policy,
            similarity_target_document_id=request.similarity_target_document_id,
            title=request.title,
            category=request.category,
            folder=request.folder,
            tags=request.tags,
        )
        _invalidate_document_store_cache()
        return DocumentUploadResponse(
            uploaded_documents=[
                UploadedDocumentSummary(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    folder=document.folder,
                )
                for document in outcome.uploaded_documents
            ],
            total_uploaded=len(outcome.uploaded_documents),
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
        
    except SimilarDocumentConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "similar_document_conflict",
                "message": str(exc),
                "conflicts": [
                    {
                        "upload_key": item.upload_key,
                        "upload_name": item.upload_name,
                        "incoming_title": item.incoming_title,
                        "existing_document_id": item.existing_document_id,
                        "existing_title": item.existing_title,
                        "existing_folder": item.existing_folder,
                        "existing_updated_at": item.existing_updated_at,
                        "match_count": item.match_count,
                        "candidates": [
                            {
                                "document_id": candidate.document_id,
                                "title": candidate.title,
                                "folder": candidate.folder,
                                "updated_at": candidate.updated_at,
                            }
                            for candidate in (item.candidates or [])
                        ],
                    }
                    for item in exc.conflicts
                ],
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while uploading the document.",
        ) from exc


@app.post("/api/documents/upload-batch", response_model=DocumentUploadResponse)
def upload_documents_batch(request: DocumentUploadBatchRequest) -> DocumentUploadResponse:
    try:
        outcome = ingestion_service.ingest_upload_batch(
            uploads=[item.model_dump() for item in request.documents],
        )
        _invalidate_document_store_cache()
        return DocumentUploadResponse(
            uploaded_documents=[
                UploadedDocumentSummary(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    folder=document.folder,
                )
                for document in outcome.uploaded_documents
            ],
            total_uploaded=len(outcome.uploaded_documents),
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except SimilarDocumentConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "similar_document_conflict",
                "message": str(exc),
                "conflicts": [
                    {
                        "upload_key": item.upload_key,
                        "upload_name": item.upload_name,
                        "incoming_title": item.incoming_title,
                        "existing_document_id": item.existing_document_id,
                        "existing_title": item.existing_title,
                        "existing_folder": item.existing_folder,
                        "existing_updated_at": item.existing_updated_at,
                        "match_count": item.match_count,
                        "candidates": [
                            {
                                "document_id": candidate.document_id,
                                "title": candidate.title,
                                "folder": candidate.folder,
                                "updated_at": candidate.updated_at,
                            }
                            for candidate in (item.candidates or [])
                        ],
                    }
                    for item in exc.conflicts
                ],
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while uploading documents.",
        ) from exc


@app.post("/api/documents/generate", response_model=DocumentGenerationResponse)
def generate_document(request: DocumentGenerationRequest) -> DocumentGenerationResponse:
    try:
        result = document_generator.generate_document(
            instructions=request.instructions,
            title=request.title,
            output_format=request.output_format,
            source_mode=request.source_mode,
            context_filter=request.context_filter,
        )
        return DocumentGenerationResponse(
            filename=result.filename,
            mime_type=result.mime_type,
            content_base64=base64.b64encode(result.content_bytes).decode("ascii"),
            message=result.message,
            citations=result.citations,
            source_mode=request.source_mode,
            context_filter=request.context_filter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while generating the document.",
        ) from exc


@app.post("/api/documents/delete", response_model=DocumentDeleteResponse)
def delete_documents(request: DocumentDeleteRequest) -> DocumentDeleteResponse:
    try:
        outcome = ingestion_service.delete_documents(
            document_ids=request.document_ids,
        )
        _invalidate_document_store_cache()
        return DocumentDeleteResponse(
            deleted_document_ids=outcome.deleted_document_ids,
            total_deleted=len(outcome.deleted_document_ids),
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while deleting documents.",
        ) from exc


@app.post("/api/documents/tags", response_model=DocumentTagUpdateResponse)
def update_document_tags(request: DocumentTagUpdateRequest) -> DocumentTagUpdateResponse:
    try:
        outcome = ingestion_service.update_document_tags(
            document_id=request.document_id,
            tags=request.tags,
        )
        _invalidate_document_store_cache()
        return DocumentTagUpdateResponse(
            document_id=outcome.updated_document.document_id,
            tags=outcome.updated_document.tags,
            updated_at=outcome.updated_document.updated_at,
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while updating document tags.",
        ) from exc


@app.post("/api/documents/metadata", response_model=DocumentMetadataUpdateResponse)
def update_document_metadata(request: DocumentMetadataUpdateRequest) -> DocumentMetadataUpdateResponse:
    try:
        outcome = ingestion_service.update_document_metadata(
            document_id=request.document_id,
            title=request.title,
            category=request.category,
            folder=request.folder,
            tags=request.tags,
        )
        _invalidate_document_store_cache()
        return DocumentMetadataUpdateResponse(
            document_id=outcome.updated_document.document_id,
            title=outcome.updated_document.title,
            category=outcome.updated_document.category,
            folder=outcome.updated_document.folder,
            tags=outcome.updated_document.tags,
            updated_at=outcome.updated_document.updated_at,
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while updating document metadata.",
        ) from exc


@app.post("/api/folders/create", response_model=FolderCreateResponse)
def create_folder(request: FolderCreateRequest) -> FolderCreateResponse:
    try:
        outcome = ingestion_service.create_folder(
            folder_name=request.folder_name,
            parent_folder_id=request.parent_folder_id,
        )
        _invalidate_document_store_cache()
        return FolderCreateResponse(
            folder_id=outcome.folder_id,
            parent_folder_id=outcome.parent_folder_id,
            created=outcome.created,
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while creating the folder.",
        ) from exc


@app.post("/api/folders/delete", response_model=FolderDeleteResponse)
def delete_folder(request: FolderDeleteRequest) -> FolderDeleteResponse:
    try:
        (
            outcome,
            unsynchronized_folders,
        ) = watch_folder_service.delete_library_folder_and_unsynchronize(
            request.folder_id,
        )
        _invalidate_document_store_cache()
        unsynchronized_count = len(unsynchronized_folders)
        message = outcome.message
        if unsynchronized_count:
            message = (
                f"{message} Unsynchronized {unsynchronized_count} source "
                f"folder{'s' if unsynchronized_count != 1 else ''}."
            )
        return FolderDeleteResponse(
            folder_id=outcome.folder_id,
            deleted_document_ids=outcome.deleted_document_ids,
            removed_folder_ids=outcome.removed_folder_ids,
            unsynchronized_watch_ids=[
                item["watch_id"] for item in unsynchronized_folders
            ],
            unsynchronized_source_paths=[
                item["source_path"] for item in unsynchronized_folders
            ],
            total_deleted_documents=len(outcome.deleted_document_ids),
            total_removed_folders=len(outcome.removed_folder_ids),
            total_unsynchronized_folders=unsynchronized_count,
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while deleting the folder.",
        ) from exc


@app.post("/api/folders/move", response_model=FolderMoveResponse)
def move_folder(request: FolderMoveRequest) -> FolderMoveResponse:
    try:
        outcome = ingestion_service.move_folder(
            folder_id=request.folder_id,
            new_parent_folder_id=request.new_parent_folder_id,
        )
        watch_folder_service.relocate_library_folder(
            outcome.folder_id,
            outcome.moved_folder_id,
        )
        _invalidate_document_store_cache()
        return FolderMoveResponse(
            folder_id=outcome.folder_id,
            moved_folder_id=outcome.moved_folder_id,
            updated_document_ids=outcome.updated_document_ids,
            total_updated=len(outcome.updated_document_ids),
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while moving the folder.",
        ) from exc


@app.post("/api/folders/rename", response_model=FolderRenameResponse)
def rename_folder(request: FolderRenameRequest) -> FolderRenameResponse:
    try:
        outcome = ingestion_service.rename_folder(
            folder_id=request.folder_id,
            new_name=request.new_name,
        )
        watch_folder_service.relocate_library_folder(
            outcome.folder_id,
            outcome.renamed_folder_id,
        )
        _invalidate_document_store_cache()
        return FolderRenameResponse(
            folder_id=outcome.folder_id,
            renamed_folder_id=outcome.renamed_folder_id,
            updated_document_ids=outcome.updated_document_ids,
            total_updated=len(outcome.updated_document_ids),
            semantic_index_rebuilt=outcome.semantic_index_rebuilt,
            message=outcome.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while renaming the folder.",
        ) from exc


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(
        content=_render_index_html(),
        headers={"Cache-Control": "no-store"},
    )
