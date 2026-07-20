from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


SourceMode = Literal["internal", "broader"]
UploadSimilarityPolicy = Literal["warn", "replace", "ignore"]
GeneratedDocumentFormat = Literal["txt", "docx", "xlsx"]


class ContextFilter(BaseModel):
    folder_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=8000)
    source_mode: SourceMode = "internal"
    context_filter: ContextFilter = Field(default_factory=ContextFilter)


class Citation(BaseModel):
    document_id: str
    title: str
    category: str | None = None
    excerpt: str | None = None
    source_url: str | None = None


class ToolTrace(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    summary: str


class GeneratedChatDocument(BaseModel):
    filename: str
    mime_type: str
    content_base64: str
    title: str | None = None


class ConversationMessage(BaseModel):
    role: Literal["assistant", "user", "system"]
    label: str
    body: str
    citations: list[Citation] = Field(default_factory=list)
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    generated_document: GeneratedChatDocument | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    assistant_message: str
    citations: list[Citation]
    tool_trace: list[ToolTrace]
    generated_document: GeneratedChatDocument | None = None
    source_mode: SourceMode
    context_filter: ContextFilter


class SavedConversationSummary(BaseModel):
    conversation_id: str
    title: str | None = None
    title_is_custom: bool = False
    summary: str = "Saved conversation"
    created_at: str
    updated_at: str
    message_count: int
    source_mode: SourceMode


class SavedConversationDetail(SavedConversationSummary):
    context_filter: ContextFilter = Field(default_factory=ContextFilter)
    messages: list[ConversationMessage] = Field(default_factory=list)


class ConversationListResponse(BaseModel):
    conversations: list[SavedConversationSummary]


class ConversationSaveRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=120)


class ConversationSaveResponse(BaseModel):
    conversation: SavedConversationDetail
    message: str


class ConversationDeleteResponse(BaseModel):
    conversation_id: str
    deleted: bool
    message: str


class FolderSummary(BaseModel):
    folder_id: str
    display_name: str
    document_count: int


class DocumentSummary(BaseModel):
    document_id: str
    title: str
    category: str
    folder: str
    tags: list[str]
    summary: str
    source_url: str | None = None
    updated_at: str | None = None
    chunk_count: int | None = None
    embedded: bool


class DocumentLibraryResponse(BaseModel):
    backend: str
    total_documents: int
    total_chunks: int | None = None
    folders: list[FolderSummary]
    documents: list[DocumentSummary]


class DocumentDetailResponse(DocumentSummary):
    text: str


class DocumentUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=260)
    content_text: str | None = Field(default=None, max_length=5_000_000)
    content_base64: str | None = Field(default=None, max_length=20_000_000)
    client_path: str | None = Field(default=None, max_length=1024)
    client_modified_ms: int | None = Field(default=None, ge=0)
    similarity_policy: UploadSimilarityPolicy = "warn"
    similarity_target_document_id: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    folder: str | None = Field(default=None, max_length=240)
    tags: list[str] = Field(default_factory=list)


class DocumentUploadBatchRequest(BaseModel):
    documents: list[DocumentUploadRequest] = Field(min_length=1, max_length=200)


class UploadedDocumentSummary(BaseModel):
    document_id: str
    title: str
    category: str
    folder: str


class DocumentUploadResponse(BaseModel):
    uploaded_documents: list[UploadedDocumentSummary]
    total_uploaded: int
    semantic_index_rebuilt: bool
    message: str


class WatchedFolderSummary(BaseModel):
    watch_id: str
    alias: str | None = None
    display_name: str
    root_path: str
    include_subfolder: str | None = None
    source_path: str
    library_folder: str
    category: str
    tags: list[str] = Field(default_factory=list)
    recursive: bool = True
    enabled: bool = True
    interval_minutes: int
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
    next_sync_at: str | None = None


class WatchedFolderListResponse(BaseModel):
    watched_folders: list[WatchedFolderSummary]


class LocalFolderBrowseResponse(BaseModel):
    path: str | None = None
    cancelled: bool = False
    message: str


class WatchedFolderCreateRequest(BaseModel):
    root_path: str = Field(min_length=1, max_length=1024)
    include_subfolder: str | None = Field(default=None, max_length=260)
    alias: str | None = Field(default=None, max_length=160)
    display_name: str | None = Field(default=None, max_length=160)
    library_folder: str | None = Field(default=None, max_length=240)
    category: str | None = Field(default="watched", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=100)
    recursive: bool = True
    enabled: bool = True
    interval_minutes: int = Field(default=30, ge=1, le=1440)


class WatchedFolderCreateResponse(BaseModel):
    watched_folder: WatchedFolderSummary
    message: str


class WatchedFolderUpdateRequest(BaseModel):
    alias: str | None = Field(default=None, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = Field(default=None, max_length=100)
    recursive: bool | None = None
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)


class WatchedFolderUpdateResponse(BaseModel):
    watched_folder: WatchedFolderSummary
    message: str


class WatchedFolderSyncResult(BaseModel):
    watch_id: str
    display_name: str
    source_path: str
    status: str
    message: str
    scanned_count: int = 0
    imported_count: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    semantic_index_rebuilt: bool = False
    synced_at: str


class WatchedFolderSyncResponse(BaseModel):
    results: list[WatchedFolderSyncResult]
    message: str


class WatchedFolderDeleteResponse(BaseModel):
    watch_id: str
    deleted: bool
    message: str


class DocumentGenerationRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=12000)
    title: str | None = Field(default=None, max_length=160)
    output_format: GeneratedDocumentFormat = "docx"
    source_mode: SourceMode = "internal"
    context_filter: ContextFilter = Field(default_factory=ContextFilter)


class DocumentGenerationResponse(BaseModel):
    filename: str
    mime_type: str
    content_base64: str
    message: str
    citations: list[Citation] = Field(default_factory=list)
    source_mode: SourceMode
    context_filter: ContextFilter


class DocumentDeleteRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=250)


class DocumentDeleteResponse(BaseModel):
    deleted_document_ids: list[str]
    total_deleted: int
    semantic_index_rebuilt: bool
    message: str


class DocumentTagUpdateRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=100)


class DocumentTagUpdateResponse(BaseModel):
    document_id: str
    tags: list[str]
    updated_at: str | None = None
    semantic_index_rebuilt: bool
    message: str


class DocumentMetadataUpdateRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=120)
    folder: str = Field(min_length=1, max_length=240)
    tags: list[str] = Field(default_factory=list, max_length=100)


class DocumentMetadataUpdateResponse(BaseModel):
    document_id: str
    title: str
    category: str
    folder: str
    tags: list[str]
    updated_at: str | None = None
    semantic_index_rebuilt: bool
    message: str


class FolderRenameRequest(BaseModel):
    folder_id: str = Field(min_length=1, max_length=240)
    new_name: str = Field(min_length=1, max_length=120)


class FolderCreateRequest(BaseModel):
    folder_name: str = Field(min_length=1, max_length=120)
    parent_folder_id: str | None = Field(default=None, max_length=240)


class FolderCreateResponse(BaseModel):
    folder_id: str
    parent_folder_id: str | None = None
    created: bool
    semantic_index_rebuilt: bool
    message: str


class FolderDeleteRequest(BaseModel):
    folder_id: str = Field(min_length=1, max_length=240)


class FolderDeleteResponse(BaseModel):
    folder_id: str
    deleted_document_ids: list[str]
    removed_folder_ids: list[str]
    total_deleted_documents: int
    total_removed_folders: int
    semantic_index_rebuilt: bool
    message: str


class FolderMoveRequest(BaseModel):
    folder_id: str = Field(min_length=1, max_length=240)
    new_parent_folder_id: str | None = Field(default=None, max_length=240)


class FolderMoveResponse(BaseModel):
    folder_id: str
    moved_folder_id: str
    updated_document_ids: list[str]
    total_updated: int
    semantic_index_rebuilt: bool
    message: str


class FolderRenameResponse(BaseModel):
    folder_id: str
    renamed_folder_id: str
    updated_document_ids: list[str]
    total_updated: int
    semantic_index_rebuilt: bool
    message: str


@dataclass
class SessionState:
    conversation_id: str
    history: list[Any] = field(default_factory=list)
    transcript: list[ConversationMessage] = field(default_factory=list)
    source_mode: SourceMode = "internal"
    context_filter: ContextFilter = field(default_factory=ContextFilter)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_touched: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
