"""FastAPI composition root and HTTP interface for Ask Jenny.

This module wires configuration, persistence, ingestion, watched folders, chat,
authentication, and static UI delivery into one local server.  Endpoint handlers
stay intentionally thin: they authorize and translate HTTP data, while domain
services own extraction, retrieval, generation, and persistence behavior.
"""

from __future__ import annotations

import base64
import json
import logging
import platform
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from openai import OpenAIError
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.connectivity import check_openai_network_access
from app.conversation_store import SavedConversationStore
from app.datastore import build_folder_records, build_document_store, load_folder_registry, normalize_folder_path
from app.document_generator import ContextDocumentGenerator
from app.ingestion import DocumentIngestionService, SimilarDocumentConflictError
from app.watch_folders import WatchedFolderService
from app.models import (
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthSessionResponse,
    AuthSignupRequest,
    ChatRequest,
    ChatResponse,
    ConversationDeleteResponse,
    ConversationListResponse,
    ConversationPairDeleteResponse,
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
    WatchedFolderOpenSourceResponse,
    WatchedFolderSummary,
    WatchedFolderSyncResponse,
    WatchedFolderSyncResult,
    WatchedFolderUpdateRequest,
    WatchedFolderUpdateResponse,
)
from app.openai_agent import BusinessKnowledgeAgent, ChatCancelledError, SessionManager
from app.ocr import get_ocr_runtime_status
from app.pdf_vision import get_pdf_vision_runtime_status
from app.reasoning_profiles import get_chat_reasoning_profiles
from app.ui_sessions import BrowserSessionRegistry
from app.user_store import DuplicateUsernameError, InvalidCredentialsError, UserStore


pdf_logger = logging.getLogger("app.pdf_ingestion")
network_logger = logging.getLogger("app.network")
settings = get_settings()
pdf_ocr_status = get_ocr_runtime_status(settings)
if pdf_ocr_status["enabled"] and not pdf_ocr_status["available"]:
    pdf_logger.error("PDF OCR is unavailable at startup: %s", pdf_ocr_status["detail"])
document_store = build_document_store(settings)
ingestion_service = DocumentIngestionService(settings)
watch_folder_service = WatchedFolderService(settings, ingestion_service)
user_store = UserStore(settings.application_database_path)
default_admin_user = user_store.ensure_default_admin(
    username=settings.default_admin_username,
    display_name=settings.default_admin_display_name,
    password=settings.default_admin_password,
)
conversation_store = SavedConversationStore(
    settings.saved_conversations_database_path,
    default_owner_user_id=default_admin_user.user_id,
    legacy_json_path=settings.saved_conversations_path,
)
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
ui_session_registry = BrowserSessionRegistry()

app = FastAPI(title=settings.app_title)
AUTH_COOKIE_NAME = "askjenny_session"

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_TEMPLATE_PATH = STATIC_DIR / "index.html"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

NETWORK_STATUS_CACHE_SECONDS = 30
_network_status_lock = threading.Lock()
_network_status_checked_at = 0.0
_network_status_cache: dict[str, object] | None = None


def _get_openai_network_status(*, force: bool = False) -> dict[str, object]:
    """Return a short-lived cached probe so routine health checks stay cheap."""
    global _network_status_cache, _network_status_checked_at

    now = time.monotonic()
    with _network_status_lock:
        if (
            not force
            and _network_status_cache is not None
            and now - _network_status_checked_at < NETWORK_STATUS_CACHE_SECONDS
        ):
            return dict(_network_status_cache)

        status = check_openai_network_access()
        _network_status_cache = status
        _network_status_checked_at = time.monotonic()
        return dict(status)


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
        .replace("{{APP_ICON_URL}}", _asset_url("jenny-logo.png"))
    )


def _invalidate_document_store_cache() -> None:
    """Make subsequent reads observe library changes made by write services."""
    document_store.invalidate_cache()


watch_folder_service.set_library_changed_callback(_invalidate_document_store_cache)


def _watch_summary_from_payload(payload: dict[str, object]) -> WatchedFolderSummary:
    return WatchedFolderSummary.model_validate(payload)


def _watch_sync_result_from_payload(result: object) -> WatchedFolderSyncResult:
    return WatchedFolderSyncResult.model_validate(result.__dict__)


def _open_local_folder_picker() -> str | None:
    """Open a native folder chooser only when the server is running locally."""
    if platform.system() == "Windows":
        return _open_windows_folder_picker()
    return _open_tk_folder_picker()


def _open_local_source_folder(source_path: Path) -> None:
    resolved_path = source_path.resolve()
    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError(f"Synchronized source folder does not exist: {resolved_path}")

    try:
        if platform.system() == "Windows":
            _open_windows_source_folder(resolved_path)
            return

        command = (
            ["open", str(resolved_path)]
            if platform.system() == "Darwin"
            else ["xdg-open", str(resolved_path)]
        )
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Could not open the synchronized source folder: {resolved_path}"
        ) from exc


def _open_windows_source_folder(source_path: Path) -> None:
    escaped_path = str(source_path).replace("'", "''")
    script = rf"""
$TargetPath = '{escaped_path}'

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class ExplorerForeground {{
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int command);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr SetFocus(IntPtr hWnd);
}}
"@

$resolvedTarget = [System.IO.Path]::GetFullPath($TargetPath).TrimEnd('\')
Start-Process -FilePath explorer.exe -ArgumentList ('"' + $resolvedTarget + '"')

$shell = New-Object -ComObject Shell.Application
$window = $null
for ($attempt = 0; $attempt -lt 30 -and $null -eq $window; $attempt++) {{
    Start-Sleep -Milliseconds 100
    foreach ($candidate in @($shell.Windows())) {{
        try {{
            $candidatePath = ([System.Uri]$candidate.LocationURL).LocalPath
            $resolvedCandidate = [System.IO.Path]::GetFullPath($candidatePath).TrimEnd('\')
            if ([string]::Equals(
                $resolvedCandidate,
                $resolvedTarget,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {{
                $window = $candidate
                break
            }}
        }} catch {{}}
    }}
}}

if ($null -ne $window) {{
    $handle = [IntPtr]::new([Int64]$window.HWND)
    $currentThread = [ExplorerForeground]::GetCurrentThreadId()
    $foregroundHandle = [ExplorerForeground]::GetForegroundWindow()
    $foregroundThread = [ExplorerForeground]::GetWindowThreadProcessId(
        $foregroundHandle,
        [IntPtr]::Zero
    )
    $attached = $false

    if ($foregroundThread -ne 0 -and $foregroundThread -ne $currentThread) {{
        $attached = [ExplorerForeground]::AttachThreadInput(
            $currentThread,
            $foregroundThread,
            $true
        )
    }}

    try {{
        [ExplorerForeground]::ShowWindowAsync($handle, 9) | Out-Null
        [ExplorerForeground]::BringWindowToTop($handle) | Out-Null
        [ExplorerForeground]::SetForegroundWindow($handle) | Out-Null
        [ExplorerForeground]::SetFocus($handle) | Out-Null
    }} finally {{
        if ($attached) {{
            [ExplorerForeground]::AttachThreadInput(
                $currentThread,
                $foregroundThread,
                $false
            ) | Out-Null
        }}
    }}
}}
"""

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Windows File Explorer is not available.") from exc


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
    network_status = _get_openai_network_status(force=True)
    if network_status["reachable"]:
        network_logger.info("OpenAI API network access is available.")
    else:
        network_logger.warning(
            "OpenAI API network access check failed: %s",
            network_status["detail"],
        )
    watch_folder_service.start()


@app.on_event("shutdown")
def stop_watched_folder_scheduler() -> None:
    watch_folder_service.stop()


@app.get("/api/health")
def health(refresh_network: bool = False) -> dict[str, object]:
    return {
        "status": "ok",
        "app_title": settings.app_title,
        "chat_reasoning_models": get_chat_reasoning_profiles(settings),
        "docstore_backend": settings.docstore_backend,
        "openai_configured": bool(settings.openai_api_key),
        "openai_network": _get_openai_network_status(force=refresh_network),
        "pdf_ocr": get_ocr_runtime_status(settings),
        "pdf_image_understanding": get_pdf_vision_runtime_status(settings),
    }


@app.get("/api/ui-sessions/active")
def active_ui_sessions() -> dict[str, object]:
    active_count = ui_session_registry.active_count()
    return {
        "active": active_count > 0,
        "count": active_count,
    }


@app.post("/api/ui-sessions/{session_id}")
def heartbeat_ui_session(session_id: str) -> dict[str, object]:
    try:
        active_count = ui_session_registry.heartbeat(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "active": True,
        "count": active_count,
    }


@app.delete("/api/ui-sessions/{session_id}")
def close_ui_session(session_id: str) -> dict[str, object]:
    try:
        active_count = ui_session_registry.remove(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "active": active_count > 0,
        "count": active_count,
    }


def _set_auth_cookie(response: Response, token: str) -> None:
    max_age = max(1, settings.auth_session_ttl_hours) * 60 * 60
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _require_authenticated_user(request: Request):
    """Resolve the signed-in user or terminate the request with HTTP 401."""
    user = user_store.get_user_for_session(request.cookies.get(AUTH_COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to access this resource.")
    if user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail="Change the temporary password before accessing this resource.",
        )
    return user


def _require_library_manager(request: Request):
    """Require a signed-in administrator or library manager."""
    user = _require_authenticated_user(request)
    if user.role not in {"admin", "library_manager"}:
        raise HTTPException(
            status_code=403,
            detail="Administrator or Library Manager access is required.",
        )
    return user


@app.get("/api/auth/session", response_model=AuthSessionResponse)
def get_auth_session(request: Request) -> AuthSessionResponse:
    user = user_store.get_user_for_session(request.cookies.get(AUTH_COOKIE_NAME))
    if user is None:
        return AuthSessionResponse(
            authenticated=False,
            user=None,
            message="Not signed in.",
        )
    return AuthSessionResponse(
        authenticated=True,
        user=user,
        message=f"Signed in as {user.display_name}.",
    )


@app.post("/api/auth/login", response_model=AuthSessionResponse)
def login(request: AuthLoginRequest, response: Response) -> AuthSessionResponse:
    try:
        user = user_store.authenticate(request.username, request.password)
    except (InvalidCredentialsError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Email or password is incorrect.",
        ) from exc
    token = user_store.create_session(user.user_id, settings.auth_session_ttl_hours)
    _set_auth_cookie(response, token)
    return AuthSessionResponse(
        authenticated=True,
        user=user,
        message=f"Welcome back, {user.display_name}.",
    )


@app.post("/api/auth/signup", response_model=AuthSessionResponse, status_code=201)
def signup(request: AuthSignupRequest, response: Response) -> AuthSessionResponse:
    try:
        user = user_store.create_user(
            username=request.username,
            display_name=request.display_name,
            password=request.password,
            role="member",
        )
    except DuplicateUsernameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = user_store.create_session(user.user_id, settings.auth_session_ttl_hours)
    _set_auth_cookie(response, token)
    return AuthSessionResponse(
        authenticated=True,
        user=user,
        message=f"Welcome, {user.display_name}. Your member account is ready.",
    )


@app.post("/api/auth/change-password", response_model=AuthSessionResponse)
def change_password(
    request: AuthChangePasswordRequest,
    http_request: Request,
) -> AuthSessionResponse:
    user = user_store.get_user_for_session(
        http_request.cookies.get(AUTH_COOKIE_NAME)
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to change your password.")
    try:
        updated_user = user_store.change_password(
            user_id=user.user_id,
            current_password=request.current_password,
            new_password=request.new_password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AuthSessionResponse(
        authenticated=True,
        user=updated_user,
        message="Password changed.",
    )


@app.post("/api/auth/logout", response_model=AuthSessionResponse)
def logout(request: Request, response: Response) -> AuthSessionResponse:
    user_store.delete_session(request.cookies.get(AUTH_COOKIE_NAME))
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
    return AuthSessionResponse(
        authenticated=False,
        user=None,
        message="Signed out.",
    )


@app.post(
    "/api/local-folders/browse",
    response_model=LocalFolderBrowseResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.get(
    "/api/watch-folders",
    response_model=WatchedFolderListResponse,
    dependencies=[Depends(_require_library_manager)],
)
def list_watched_folders() -> WatchedFolderListResponse:
    return WatchedFolderListResponse(
        watched_folders=[
            _watch_summary_from_payload(item)
            for item in watch_folder_service.list_watchers()
        ]
    )


@app.post(
    "/api/watch-folders/{watch_id}/open-source",
    response_model=WatchedFolderOpenSourceResponse,
    dependencies=[Depends(_require_library_manager)],
)
def open_watched_folder_source(watch_id: str) -> WatchedFolderOpenSourceResponse:
    try:
        source_path = watch_folder_service.resolve_watcher_source_path(watch_id)
        _open_local_source_folder(source_path)
        return WatchedFolderOpenSourceResponse(
            watch_id=watch_id,
            source_path=str(source_path),
            opened=True,
            message=f"Opened source location: {source_path}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while opening the synchronized source folder.",
        ) from exc


@app.post(
    "/api/watch-folders",
    response_model=WatchedFolderCreateResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.patch(
    "/api/watch-folders/{watch_id}",
    response_model=WatchedFolderUpdateResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/watch-folders/sync",
    response_model=WatchedFolderSyncResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/watch-folders/{watch_id}/sync",
    response_model=WatchedFolderSyncResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.delete(
    "/api/watch-folders/{watch_id}",
    response_model=WatchedFolderDeleteResponse,
    dependencies=[Depends(_require_library_manager)],
)
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
def chat(payload: ChatRequest, http_request: Request) -> ChatResponse:
    user = _require_authenticated_user(http_request)
    try:
        return agent.chat(
            payload.conversation_id,
            user.user_id,
            payload.message.strip(),
            payload.images,
            payload.source_mode,
            payload.context_filter,
            payload.reasoning_mode,
            request_id=payload.request_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Saved conversation not found.") from exc
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


@app.post("/api/chat/{request_id}/cancel")
def cancel_chat(request_id: str, http_request: Request) -> dict[str, object]:
    user = _require_authenticated_user(http_request)
    cancelled = agent.cancel_request(request_id, user.user_id)
    return {
        "request_id": request_id,
        "cancelled": cancelled,
        "message": (
            "Cancellation requested."
            if cancelled
            else "The response was already complete or is no longer active."
        ),
    }


@app.get("/api/conversations", response_model=ConversationListResponse)
def list_conversations(request: Request) -> ConversationListResponse:
    user = _require_authenticated_user(request)
    return ConversationListResponse(
        conversations=session_manager.list_saved_conversations(user.user_id),
    )


@app.get("/api/conversations/{conversation_id}", response_model=SavedConversationDetail)
def get_conversation(conversation_id: str, request: Request) -> SavedConversationDetail:
    user = _require_authenticated_user(request)
    conversation = session_manager.load_saved_conversation(
        conversation_id,
        user.user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Saved conversation not found.")
    return conversation


@app.post("/api/conversations/save", response_model=ConversationSaveResponse)
def save_conversation(
    payload: ConversationSaveRequest,
    http_request: Request,
) -> ConversationSaveResponse:
    user = _require_authenticated_user(http_request)
    try:
        conversation = session_manager.save_conversation(
            payload.conversation_id,
            user.user_id,
            title=payload.title,
            source_mode=payload.source_mode,
            reasoning_mode=payload.reasoning_mode,
            context_filter=payload.context_filter,
        )
        return ConversationSaveResponse(
            conversation=conversation,
            message="Conversation saved.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChatCancelledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Saved conversation not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while saving the conversation.",
        ) from exc


@app.post("/api/conversations/settings", response_model=ConversationSettingsResponse)
def update_conversation_settings(
    payload: ConversationSettingsRequest,
    http_request: Request,
) -> ConversationSettingsResponse:
    user = _require_authenticated_user(http_request)
    try:
        conversation = session_manager.update_conversation_settings(
            payload.conversation_id,
            user.user_id,
            payload.source_mode,
            payload.context_filter,
            payload.reasoning_mode,
        )
        return ConversationSettingsResponse(
            conversation_id=payload.conversation_id,
            source_mode=payload.source_mode,
            reasoning_mode=payload.reasoning_mode,
            context_filter=payload.context_filter,
            saved=conversation is not None,
            conversation=conversation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Saved conversation not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while updating conversation settings.",
        ) from exc


@app.delete(
    "/api/conversations/{conversation_id}/pairs/{assistant_message_index}",
    response_model=ConversationPairDeleteResponse,
)
def delete_conversation_pair(
    conversation_id: str,
    assistant_message_index: int,
    request: Request,
) -> ConversationPairDeleteResponse:
    user = _require_authenticated_user(request)
    try:
        conversation = session_manager.delete_saved_conversation_pair(
            conversation_id,
            assistant_message_index,
            user.user_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Saved conversation not found.")
        return ConversationPairDeleteResponse(
            conversation_id=conversation_id,
            deleted=True,
            message="Question and response deleted from saved conversation.",
            conversation=conversation,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected server error while deleting the question and response.",
        ) from exc


@app.delete("/api/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
def delete_conversation(conversation_id: str, request: Request) -> ConversationDeleteResponse:
    user = _require_authenticated_user(request)
    try:
        deleted = session_manager.delete_saved_conversation(
            conversation_id,
            user.user_id,
        )
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


@app.get(
    "/api/documents",
    response_model=DocumentLibraryResponse,
    dependencies=[Depends(_require_authenticated_user)],
)
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


@app.get(
    "/api/documents/{document_id}",
    response_model=DocumentDetailResponse,
    dependencies=[Depends(_require_authenticated_user)],
)
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


@app.post(
    "/api/documents/upload",
    response_model=DocumentUploadResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/documents/upload-batch",
    response_model=DocumentUploadResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/documents/generate",
    response_model=DocumentGenerationResponse,
    dependencies=[Depends(_require_authenticated_user)],
)
def generate_document(request: DocumentGenerationRequest) -> DocumentGenerationResponse:
    try:
        result = document_generator.generate_document(
            instructions=request.instructions,
            title=request.title,
            output_format=request.output_format,
            source_mode=request.source_mode,
            reasoning_mode=request.reasoning_mode,
            context_filter=request.context_filter,
        )
        return DocumentGenerationResponse(
            filename=result.filename,
            mime_type=result.mime_type,
            content_base64=base64.b64encode(result.content_bytes).decode("ascii"),
            message=result.message,
            citations=result.citations,
            source_mode=request.source_mode,
            reasoning_mode=request.reasoning_mode,
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


@app.post(
    "/api/documents/delete",
    response_model=DocumentDeleteResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/documents/delete/stream",
    dependencies=[Depends(_require_library_manager)],
)
def delete_documents_stream(request: DocumentDeleteRequest) -> StreamingResponse:
    events: queue.Queue[dict[str, object]] = queue.Queue()

    def report(phase: str, percent: int, detail: str) -> None:
        events.put(
            {
                "type": "progress",
                "phase": phase,
                "percent": max(0, min(100, int(percent))),
                "detail": detail,
            }
        )

    def run_delete() -> None:
        try:
            started_at = time.monotonic()
            outcome = ingestion_service.delete_documents(
                document_ids=request.document_ids,
                progress_callback=report,
            )
            _invalidate_document_store_cache()
            result = DocumentDeleteResponse(
                deleted_document_ids=outcome.deleted_document_ids,
                total_deleted=len(outcome.deleted_document_ids),
                semantic_index_rebuilt=outcome.semantic_index_rebuilt,
                message=outcome.message,
            )
            events.put(
                {
                    "type": "result",
                    "elapsed_seconds": round(time.monotonic() - started_at, 2),
                    "payload": result.model_dump(mode="json"),
                }
            )
        except (ValueError, RuntimeError) as exc:
            events.put({"type": "error", "detail": str(exc)})
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "Unexpected server error while deleting documents."
            )
            events.put(
                {
                    "type": "error",
                    "detail": "Unexpected server error while deleting documents.",
                }
            )

    threading.Thread(
        target=run_delete,
        name="askjenny-document-delete",
        daemon=True,
    ).start()

    def stream_events():
        while True:
            event = events.get()
            yield json.dumps(event, ensure_ascii=True) + "\n"
            if event.get("type") in {"result", "error"}:
                break

    return StreamingResponse(
        stream_events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/documents/tags",
    response_model=DocumentTagUpdateResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/documents/metadata",
    response_model=DocumentMetadataUpdateResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/folders/create",
    response_model=FolderCreateResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/folders/delete",
    response_model=FolderDeleteResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/folders/move",
    response_model=FolderMoveResponse,
    dependencies=[Depends(_require_library_manager)],
)
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


@app.post(
    "/api/folders/rename",
    response_model=FolderRenameResponse,
    dependencies=[Depends(_require_library_manager)],
)
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
